using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.Linq;

namespace MFAAvalonia.Helper;

public static class HifFollowupConfig
{
    public static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    public static readonly string[] Policies = ["normal", "reserve_first", "combo_only"];

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
}
