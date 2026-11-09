using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.Helper;

/// <summary>
/// 出牌编排配置：以起始卡为键，后续候选按顺序衔接第一张可用的卡；起始卡与每张候选可设前置、后置饮料，组合另有等待用饮。
/// 规则与 Agent 端 extensions/hif/followups.py 的 validate_sequences 一致。
/// </summary>
public static class HifPlaySequenceConfig
{
    public const string ConfigKey = "play_sequence_profiles";
    public static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    public static readonly string[] Policies = ["normal", "reserve_first", "combo_only"];
    public static readonly string[] PolicyLabels = ["可单独出", "首次组合前保留", "只用于衔接"];

    private static IReadOnlySet<string>? _drinkNames;

    /// <summary>饮料目录中的全部饮料名。</summary>
    public static IReadOnlySet<string> DrinkNames => _drinkNames ??= LoadDrinkNames();

    private static HashSet<string> LoadDrinkNames()
    {
        var catalog = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_drink_catalog.json"), Encoding.UTF8));
        return (catalog["drinks"] as JArray ?? throw new InvalidOperationException("缺少饮料目录")).OfType<JObject>()
            .Select(drink => (string?)drink["name"]).OfType<string>().ToHashSet();
    }

    /// <summary>新建编排的默认设置：没有后续候选与饮料，等待 3 回合、后续卡可单独出。</summary>
    public static JObject NewRule() => Normalize(new JObject
    {
        ["targets"] = new JArray(), ["wait_turns"] = 3, ["wait_after_success"] = false, ["target_policy"] = Policies[0]
    });

    public static List<string> Targets(JObject rule) =>
        (rule["targets"] as JArray)?.Values<string>().OfType<string>().ToList() ?? new List<string>();

    /// <summary>补齐饮料各位置，按候选清理候选饮料；没有候选时去掉等待用饮。就地修改并返回同一对象。</summary>
    public static JObject Normalize(JObject rule)
    {
        var targets = Targets(rule);
        var drinks = rule["drinks"] as JObject ?? new JObject();
        foreach (var slot in new[] { "before", "after", "waiting" })
            if (drinks[slot] is not JArray) drinks[slot] = new JArray();
        if (targets.Count == 0) drinks["waiting"] = new JArray();
        var old = drinks["targets"] as JObject ?? new JObject();
        var perTarget = new JObject();
        foreach (var target in targets)
        {
            var entry = old[target] as JObject ?? new JObject();
            foreach (var slot in new[] { "before", "after" })
                if (entry[slot] is not JArray) entry[slot] = new JArray();
            perTarget[target] = entry;
        }
        drinks["targets"] = perTarget;
        rule["drinks"] = drinks;
        return rule;
    }

    /// <summary>饮料位置；target 为空时是起始卡的 before/after 或组合的 waiting。</summary>
    public static JArray Slot(JObject rule, string slot, string? target = null) =>
        (JArray)(target == null ? rule["drinks"]![slot]! : rule["drinks"]!["targets"]![target]![slot]!);

    /// <summary>没有后续候选也没有起始卡饮料的编排没有作用，保存时去掉。</summary>
    public static bool IsEmpty(JObject rule) =>
        Targets(rule).Count == 0 && Slot(rule, "before").Count == 0 && Slot(rule, "after").Count == 0;

    /// <summary>编排用到的饮料（含重复引用）。</summary>
    public static IEnumerable<string> DrinkUses(JObject rule)
    {
        foreach (var slot in new[] { "before", "after", "waiting" })
            foreach (var name in Slot(rule, slot).Values<string>().OfType<string>()) yield return name;
        foreach (var target in Targets(rule))
            foreach (var slot in new[] { "before", "after" })
                foreach (var name in Slot(rule, slot, target).Values<string>().OfType<string>()) yield return name;
    }

    /// <summary>加入 candidate 后是否形成循环：candidate 已能（直接或间接）衔接回 start。</summary>
    public static bool WouldCycle(JObject rules, string start, string candidate)
    {
        if (candidate == start) return true;
        var seen = new HashSet<string>();
        var stack = new Stack<string>();
        stack.Push(candidate);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            if (current == start) return true;
            if (!seen.Add(current) || rules[current] is not JObject rule) continue;
            foreach (var next in Targets(rule)) stack.Push(next);
        }
        return false;
    }

    public static void Validate(JObject rules, JObject catalog, IReadOnlySet<string>? drinks = null)
    {
        drinks ??= DrinkNames;
        void DrinkList(JToken? token, string label)
        {
            if (token == null) return;
            if (token is not JArray list || list.Any(item => item.Type != JTokenType.String || !drinks.Contains((string)item!))
                || list.Values<string>().Distinct().Count() != list.Count)
                throw new InvalidOperationException($"无效饮料：{label}");
        }
        foreach (var property in rules.Properties())
        {
            var source = property.Name;
            if (catalog[source] == null || property.Value is not JObject rule || rule["targets"] is not JArray targets
                || targets.Any(t => t.Type != JTokenType.String || catalog[(string)t!] == null || (string)t! == source)
                || targets.Values<string>().Distinct().Count() != targets.Count
                || rule["wait_turns"]?.Type != JTokenType.Integer || (int)rule["wait_turns"]! is < 0 or > 6
                || rule["wait_after_success"]?.Type != JTokenType.Boolean
                || !Policies.Contains((string?)rule["target_policy"]))
                throw new InvalidOperationException($"无效出牌编排：{source}");
            var names = targets.Values<string>().OfType<string>().ToHashSet();
            var drinkRules = rule["drinks"] ?? new JObject();
            if (drinkRules is not JObject drinkObject
                || drinkObject.Properties().Any(p => p.Name is not ("before" or "after" or "waiting" or "targets")))
                throw new InvalidOperationException($"无效编排饮料：{source}");
            foreach (var slot in new[] { "before", "after", "waiting" }) DrinkList(drinkObject[slot], $"{source}/{slot}");
            if (drinkObject["targets"] is { } perToken)
            {
                if (perToken is not JObject perTarget || perTarget.Properties().Any(p => !names.Contains(p.Name)))
                    throw new InvalidOperationException($"无效后续卡饮料：{source}");
                foreach (var entry in perTarget.Properties())
                {
                    if (entry.Value is not JObject slots || slots.Properties().Any(p => p.Name is not ("before" or "after")))
                        throw new InvalidOperationException($"无效后续卡饮料：{source}/{entry.Name}");
                    DrinkList(slots["before"], $"{source}/{entry.Name}/before");
                    DrinkList(slots["after"], $"{source}/{entry.Name}/after");
                }
            }
            bool HasDrinks(string slot) => drinkObject[slot] is JArray { Count: > 0 };
            if (targets.Count == 0 && HasDrinks("waiting"))
                throw new InvalidOperationException($"没有后续卡的编排不能设置等待用饮：{source}");
            if (targets.Count == 0 && !HasDrinks("before") && !HasDrinks("after"))
                throw new InvalidOperationException($"空的出牌编排：{source}");
        }
        var visiting = new HashSet<string>();
        var visited = new HashSet<string>();
        void Visit(string source)
        {
            if (visited.Contains(source)) return;
            if (!visiting.Add(source)) throw new InvalidOperationException("组合不能形成循环");
            foreach (var target in (rules[source]?["targets"] as JArray ?? new JArray()).Values<string>()) Visit(target!);
            visiting.Remove(source);
            visited.Add(source);
        }
        foreach (var source in rules.Properties()) Visit(source.Name);
    }
}
