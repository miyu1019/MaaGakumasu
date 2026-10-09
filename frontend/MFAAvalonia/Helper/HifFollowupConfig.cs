using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.Helper;

public static class HifFollowupConfig
{
    public static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    public static readonly string[] Policies = ["normal", "reserve_first", "combo_only"];
    public const string OldWait = "HIF.国民脚光等待回合";

    public static Dictionary<string, int[]> LegacyWaits()
    {
        var values = new Dictionary<string, HashSet<int>>();
        var directory = Path.Combine(AppPaths.DataRoot, "config", "instances");
        foreach (var path in Directory.Exists(directory) ? Directory.GetFiles(directory, "*.json") : [])
        {
            var data = JObject.Parse(File.ReadAllText(path, Encoding.UTF8));
            foreach (var task in (data["TaskItems"] as JArray ?? new JArray()).OfType<JObject>())
            {
                if ((string?)task["entry"] != "ProduceHIF") continue;
                var options = (task["option"] as JArray ?? new JArray()).OfType<JObject>().ToArray();
                var profession = options.FirstOrDefault(o => (string?)o["name"] == "HIF.角色职业");
                var index = (int?)profession?["index"] ?? 1;
                var name = index is >= 0 and < 6 ? Professions[index] : "集中";
                foreach (var option in options.Where(o => (string?)o["name"] == OldWait))
                    if (int.TryParse((string?)option["data"]?["combo_wait_turns"], out var turn) && turn is >= 0 and <= 6)
                    {
                        if (!values.ContainsKey(name)) values[name] = new HashSet<int>();
                        values[name].Add(turn);
                    }
            }
        }
        return values.ToDictionary(p => p.Key, p => p.Value.Order().ToArray());
    }

    public static JObject LegacyRules(JObject config, string profession, int? wait = null)
    {
        var first = (string?)config["combo"]?["first"];
        var second = (string?)config["combo"]?["second"];
        var keys = (config["priority_profiles"]?[profession] as JArray ?? new JArray())
            .OfType<JObject>().Select(c => (string?)c["key"]).ToHashSet();
        if (first == null || second == null || first == second || !keys.Contains(first) || !keys.Contains(second)) return new JObject();
        var turn = wait ?? (config["skip_limit"]?.Type == JTokenType.Integer ? (int)config["skip_limit"]! : 3);
        return new JObject { [first] = new JObject {
            ["targets"] = new JArray(second), ["wait_turns"] = turn is >= 0 and <= 6 ? turn : 3,
            ["use_black_vinegar"] = true, ["wait_after_success"] = false, ["target_policy"] = "reserve_first"
        }};
    }

    public static void Validate(JObject rules, JObject catalog)
    {
        foreach (var property in rules.Properties())
        {
            if (catalog[property.Name] == null || property.Value is not JObject rule || rule["targets"] is not JArray targets
                || targets.Count == 0 || targets.Any(t => t.Type != JTokenType.String || catalog[(string)t!] == null || (string)t! == property.Name)
                || targets.Values<string>().Distinct().Count() != targets.Count
                || rule["wait_turns"]?.Type != JTokenType.Integer || (int)rule["wait_turns"]! is < 0 or > 6
                || rule["use_black_vinegar"]?.Type != JTokenType.Boolean || rule["wait_after_success"]?.Type != JTokenType.Boolean
                || !Policies.Contains((string?)rule["target_policy"]))
                throw new InvalidOperationException($"无效组合配置：{property.Name}");
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

    public static void Backup(string path)
    {
        var destination = Path.Combine(AppPaths.DataRoot, "backup", "followups-before-migration", Path.GetRelativePath(AppPaths.DataRoot, path));
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        if (!File.Exists(destination)) File.Copy(path, destination);
    }

    public static void RemoveLegacyWaits()
    {
        var directory = Path.Combine(AppPaths.DataRoot, "config", "instances");
        var pending = new List<(string Path, JObject Data)>();
        foreach (var path in Directory.Exists(directory) ? Directory.GetFiles(directory, "*.json") : [])
        {
            var data = JObject.Parse(File.ReadAllText(path, Encoding.UTF8));
            var changed = false;
            foreach (var task in (data["TaskItems"] as JArray ?? new JArray()).OfType<JObject>())
                if ((string?)task["entry"] == "ProduceHIF" && task["option"] is JArray options)
                    foreach (var option in options.OfType<JObject>().Where(o => (string?)o["name"] == OldWait).ToArray())
                    { option.Remove(); changed = true; }
            if (changed) pending.Add((path, data));
        }
        foreach (var item in pending) Backup(item.Path);
        foreach (var item in pending)
        {
            File.WriteAllText(item.Path + ".tmp", item.Data.ToString(Newtonsoft.Json.Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(item.Path + ".tmp", item.Path, true);
        }
    }
}
