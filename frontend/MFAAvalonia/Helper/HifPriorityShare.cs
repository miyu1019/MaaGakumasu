using Newtonsoft.Json.Linq;
using System;
using System.Globalization;
using System.Linq;

namespace MFAAvalonia.Helper;

/// <summary>单职业分享文件；只接收策略值，识别资源始终使用本地目录。</summary>
public static class HifPriorityShare
{
    public const string Format = "MaaGakumasu-HIF-priority";

    public static JObject Validate(JObject data, string profession, JObject catalog)
    {
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
        Require(HifFollowupConfig.Professions.Contains(profession) && data["profession"]?.Type == JTokenType.String,
            "职业无效");
        Require((string?)data["profession"] == profession, $"文件职业为 {data["profession"]}，请先切换到该职业再导入");
        foreach (var field in new[] { "priority", "recognition", "no_extra_turn" })
            Require(data[field] is JArray, $"缺少或无效的 {field}");
        foreach (var field in new[] { "use_conditions", "conditional_priorities", "followups" })
            Require(data[field] is JObject, $"缺少或无效的 {field}");

        var result = (JObject)data.DeepClone();
        result.Remove("acquisition");
        result.Remove("swap_out");
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
            Require(rule.Properties().All(p => p.Name is "priority" or "mode" or "remaining_turns_gte" or "remaining_turns_lte" or "full_power_lt")
                && (rule["remaining_turns_gte"] != null || rule["remaining_turns_lte"] != null || rule["full_power_lt"] != null), "条件优先级缺少条件或含不支持的条件");
            Threshold(rule, "remaining_turns_gte", 0, 99);
            Threshold(rule, "remaining_turns_lte", 0, 99);
            Threshold(rule, "full_power_lt", 0, 999);
            Require(rule["full_power_lt"] == null || profession == "全力", "全力值条件只适用于全力职业");
            Require(rule["remaining_turns_gte"] == null || rule["remaining_turns_lte"] == null
                || (int)rule["remaining_turns_gte"]! <= (int)rule["remaining_turns_lte"]!, "回合下限不能大于上限");
        }
        HifFollowupConfig.Validate((JObject)data["followups"]!, catalog);
        return result;
    }
}
