using Newtonsoft.Json.Linq;
using System;
using System.Globalization;
using System.Linq;

namespace MFAAvalonia.Helper;

/// <summary>单职业分享文件；只接收策略值，识别资源始终使用本地目录。</summary>
public static class HifPriorityShare
{
    public const string Format = "MaaGakumasu-HIF-priority";

    public static JObject TypePreference(JToken? token)
    {
        var rule = token == null ? new JObject() : token as JObject
            ?? throw new InvalidOperationException("未配置卡偏好必须是对象");
        string Choice(string field, string fallback, string[] choices)
        {
            if (rule[field] == null) return fallback;
            if (rule[field]!.Type != JTokenType.String || !choices.Contains((string?)rule[field]))
                throw new InvalidOperationException($"未配置卡偏好 {field} 无效");
            return (string)rule[field]!;
        }
        if (rule.Properties().Any(p => p.Name is not ("mode" or "early" or "late" or "remaining_turns_lte" or "prefer_upgraded")))
            throw new InvalidOperationException("未配置卡偏好含不支持的设置");
        if (rule["remaining_turns_lte"] is { } turns && (turns.Type != JTokenType.Integer || (long)turns < 1 || (long)turns > 99))
            throw new InvalidOperationException("收尾门槛必须是 1–99 的整数");
        if (rule["prefer_upgraded"] is { } upgraded && upgraded.Type != JTokenType.Boolean)
            throw new InvalidOperationException("优先强化卡必须是开关值");
        return new JObject
        {
            ["mode"] = Choice("mode", "none", ["none", "mental", "active", "remaining_turns"]),
            ["early"] = Choice("early", "mental", ["none", "mental", "active"]),
            ["late"] = Choice("late", "active", ["none", "mental", "active"]),
            ["remaining_turns_lte"] = (int?)rule["remaining_turns_lte"] ?? 3,
            ["prefer_upgraded"] = (bool?)rule["prefer_upgraded"] ?? false
        };
    }

    public static JObject Validate(JObject data, string profession, JObject catalog)
    {
        // 旧版分享文件中的旧卡名按目录正式名导入。
        HifCardRenames.Apply(data);
        void Require(bool valid, string message)
        {
            if (!valid) throw new InvalidOperationException(message);
        }
        decimal Number(JToken? token, int maximum = 9999)
        {
            Require(token?.Type is JTokenType.Integer or JTokenType.Float, "优先级必须是数字");
            Require(decimal.TryParse(token!.ToString(Newtonsoft.Json.Formatting.None), NumberStyles.Float, CultureInfo.InvariantCulture,
                out var value) && value >= 0 && value <= maximum, "优先级超出范围");
            return value;
        }
        void Threshold(JObject rule, string field, int minimum, int maximum)
        {
            if (rule[field] == null) return;
            Require(rule[field]!.Type == JTokenType.Integer && (long)rule[field]! >= minimum && (long)rule[field]! <= maximum,
                $"{field} 必须是 {minimum}–{maximum} 的整数");
        }
        Require(data["format"]?.Type == JTokenType.String && (string?)data["format"] == Format
            && data["version"]?.Type == JTokenType.Integer && (long)data["version"]! == 1, "不是受支持的 HIF 职业分享文件");
        Require(HifPlaySequenceConfig.Professions.Contains(profession) && data["profession"]?.Type == JTokenType.String,
            "职业无效");
        Require((string?)data["profession"] == profession, $"文件职业为 {data["profession"]}，请先切换到该职业再导入");
        foreach (var field in new[] { "priority", "recognition", "no_extra_turn" })
            Require(data[field] is JArray, $"缺少或无效的 {field}");
        foreach (var field in new[] { "use_conditions", "conditional_priorities" })
            Require(data[field] is JObject, $"缺少或无效的 {field}");
        Require(data["play_sequences"] is null or JObject, "出牌编排格式无效");

        var result = (JObject)data.DeepClone();
        result.Remove("acquisition");
        result.Remove("swap_out");
        // 旧版后续卡组合不导入，出牌编排需重新设置。
        result.Remove("followups");
        var keys = new System.Collections.Generic.HashSet<string>();
        var priority = new JArray();
        foreach (var entry in (JArray)data["priority"]!)
        {
            Require(entry is JObject && entry["key"]?.Type == JTokenType.String, "优先级卡牌格式无效");
            var key = (string)entry["key"]!;
            Require(catalog[key] is JObject && keys.Add(key), $"未知或重复卡牌：{key}；请检查双方卡牌目录版本");
            var local = (JObject)catalog[key]!.DeepClone();
            local["key"] = key;
            local["priority"] = Number(entry["priority"]);
            priority.Add(local);
        }
        result["priority"] = priority;
        result["unknown_priority"] = Number(data["unknown_priority"]);
        result["unconfigured_card_preference"] = TypePreference(data["unconfigured_card_preference"]);
        foreach (var field in new[] { "recognition", "no_extra_turn" })
        {
            var values = (JArray)data[field]!;
            Require(values.All(v => v.Type == JTokenType.String && catalog[(string)v!] != null)
                && values.Values<string>().Distinct().Count() == values.Count, $"{field} 含未知或重复卡牌");
        }
        foreach (var property in ((JObject)data["use_conditions"]!).Properties())
        {
            Require(catalog[property.Name] != null && property.Value is JArray { Count: > 0 and <= 3 }, $"使用限制无效：{property.Name}");
            foreach (var item in (JArray)property.Value)
            {
                Require(item is JObject, "使用限制格式无效");
                var clause = (JObject)item;
                Require(clause.Properties().All(p => p.Name is "state" or "remaining_turns_lte") && clause.HasValues, "使用限制含不支持的条件");
                if (clause["state"] != null)
                    Require(profession is "全力" or "強気" && clause["state"]!.Type == JTokenType.String
                        && new[] { "全力", "強気", "温存" }.Contains((string?)clause["state"]), "使用限制状态无效");
                Threshold(clause, "remaining_turns_lte", 1, 99);
            }
        }
        foreach (var property in ((JObject)data["conditional_priorities"]!).Properties())
        {
            Require(keys.Contains(property.Name) && property.Value is JObject, $"条件优先级无效：{property.Name}");
            var rule = (JObject)property.Value;
            Number(rule["priority"]);
            Require(rule["mode"]?.Type == JTokenType.String && new[] { "any", "all" }.Contains((string?)rule["mode"]), "条件优先级模式无效");
            Require(rule.Properties().All(p => p.Name is "priority" or "mode" or "remaining_turns_gte" or "remaining_turns_lte" or "full_power_lt" or "pointer_states")
                && (rule["remaining_turns_gte"] != null || rule["remaining_turns_lte"] != null || rule["full_power_lt"] != null || rule["pointer_states"] != null), "条件优先级缺少条件或含不支持的条件");
            if (rule["pointer_states"] != null)
                Require(profession is "全力" or "強気" && rule["pointer_states"] is JArray { Count: > 0 and <= 3 } states
                    && states.All(s => s.Type == JTokenType.String && new[] { "全力", "強気", "温存" }.Contains((string?)s))
                    && states.Values<string>().Distinct().Count() == states.Count, "条件优先级指针状态无效");
            Threshold(rule, "remaining_turns_gte", 0, 99);
            Threshold(rule, "remaining_turns_lte", 0, 99);
            Threshold(rule, "full_power_lt", 0, 999);
            Require(rule["full_power_lt"] == null || profession == "全力", "全力值条件只适用于全力职业");
            Require(rule["remaining_turns_gte"] == null || rule["remaining_turns_lte"] == null
                || (int)rule["remaining_turns_gte"]! <= (int)rule["remaining_turns_lte"]!, "回合下限不能大于上限");
        }
        var sequences = data["play_sequences"] as JObject ?? new JObject();
        HifPlaySequenceConfig.Validate(sequences, catalog);
        result["play_sequences"] = sequences.DeepClone();
        // 保留为后加字段：旧分享文件没有时按空名单、先找到先保留导入。
        var hold = data["hold_priority"] == null ? new JObject { ["ordered"] = false, ["cards"] = new JArray() }
            : data["hold_priority"] as JObject ?? throw new InvalidOperationException("缺少或无效的 hold_priority");
        Require(hold.Properties().All(p => p.Name is "ordered" or "cards")
            && hold["ordered"]?.Type == JTokenType.Boolean && hold["cards"] is JArray, "保留格式无效");
        var holdCards = (JArray)hold["cards"]!;
        Require(holdCards.All(v => v.Type == JTokenType.String && keys.Contains((string)v!))
            && holdCards.Values<string>().Distinct().Count() == holdCards.Count, "保留含未知、重复或不在出牌卡表中的卡牌");
        Require(holdCards.Count == 0 || profession is "全力" or "強気", "保留只适用于全力与強気职业");
        result["hold_priority"] = hold.DeepClone();
        return result;
    }
}
