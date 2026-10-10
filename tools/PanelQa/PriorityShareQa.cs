using System.Globalization;
using System.Reflection;
using Avalonia.Controls;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;

static class PriorityShareQa
{
    static object? Invoke(object window, string method, params object[] args) =>
        window.GetType().GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(window, args);
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }

    public static void Run(string root)
    {
        var path = HifLayout.PersonalPath("cards_priority.json");
        var before = File.ReadAllText(path);
        var original = JObject.Parse(before);
        var window = new HifPriorityWindow("集中");
        Check((bool)window.GetType().GetField("_loaded", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!, "Panel failed to load");
        var catalog = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_catalog.json")));
        var exported = (JObject)Invoke(window, "ShareProfession", "集中")!;
        Check(exported["acquisition"] == null && exported["swap_out"] == null, "Acquisition or swap list exported");
        var data = HifPriorityShare.Validate(JObject.Parse(exported.ToString()), "集中", catalog);
        foreach (var profession in HifFollowupConfig.Professions)
            HifPriorityShare.Validate((JObject)Invoke(window, "ShareProfession", profession)!, profession, catalog);
        var unknown = (Dictionary<string, decimal>)window.GetType().GetField("_unknownPriorities", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
        unknown["全力"] = 8.5m; // 模拟另一个职业尚未保存的修改。
        original["unknown_priority_profiles"]!["全力"] = 8.5m;
        Check(!data.Properties().Any(p => p.Name.Contains("drink") || p.Name.Contains("instance")), "Unrelated settings exported");
        var oldCulture = CultureInfo.CurrentCulture;
        try
        {
            CultureInfo.CurrentCulture = CultureInfo.GetCultureInfo("de-DE");
            data["unknown_priority"] = 7.5m;
            Check((decimal)HifPriorityShare.Validate(data, "集中", catalog)["unknown_priority"]! == 7.5m, "Culture changed priority");
        }
        finally { CultureInfo.CurrentCulture = oldCulture; }
        var key = (string)data["priority"]![0]!["key"]!;
        var acquisition = (Dictionary<string, List<JObject>>)window.GetType().GetField("_targetProfiles", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
        var swap = (Dictionary<string, List<JObject>>)window.GetType().GetField("_swapOutProfiles", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
        acquisition["集中"] = [new JObject { ["name"] = key }];
        swap["集中"] = [new JObject { ["name"] = key }];
        // 旧分享文件中的名单也不能覆盖当前尚未保存的名单。
        data["acquisition"] = new JArray("ignored-acquisition");
        data["swap_out"] = new JArray("ignored-swap");
        data["priority"]![0]!["priority"] = 0.5m;
        data["priority"]![0]!["template"] = "untrusted/resource.png";
        data["no_extra_turn"] = new JArray(key);
        data["use_conditions"] = new JObject { [key] = new JArray(new JObject { ["remaining_turns_lte"] = 5 }) };
        data["conditional_priorities"] = new JObject { [key] = new JObject { ["priority"] = 1.5m, ["mode"] = "all", ["remaining_turns_gte"] = 2, ["remaining_turns_lte"] = 4 } };
        Invoke(window, "ApplyProfession", data, "集中");
        Check((string?)acquisition["集中"][0]["name"] == key && (string?)swap["集中"][0]["name"] == key,
            "Import overwrote current profession's unsaved acquisition/swap lists");
        Check(unknown["全力"] == 8.5m, "Import overwrote another profession's unsaved edit");
        Check(File.ReadAllText(path) == before, "Import saved before user pressed Save");
        Invoke(window, "SaveConfig");
        var saved = JObject.Parse(File.ReadAllText(path));
        Check(JToken.DeepEquals(saved["preferred_acquisition_profiles"]!["集中"], new JArray(new JObject { ["name"] = key }))
            && JToken.DeepEquals(saved["swap_out_priority_profiles"]!["集中"], new JArray(key)), "Acquisition/swap lists lost on save");
        Check((decimal)saved["unknown_priority_profiles"]!["集中"]! == 7.5m, "Unknown priority lost");
        Check((decimal)saved["unknown_priority_profiles"]!["全力"]! == 8.5m, "Other profession's edit lost on save");
        Check((string?)saved["priority_profiles"]!["集中"]![0]!["template"] == (string?)catalog[key]!["template"], "Imported external template");
        foreach (var field in original.Properties().Where(p => p.Value is JObject && p.Name.EndsWith("_profiles")))
            foreach (var profession in HifFollowupConfig.Professions.Where(p => p != "集中"))
                Check(JToken.DeepEquals(field.Value[profession], saved[field.Name]?[profession])
                    || field.Value[profession] == null, $"Other profession changed: {field.Name}/{profession}");
        var reopened = new HifPriorityWindow("集中");
        var roundtrip = HifPriorityShare.Validate((JObject)Invoke(reopened, "ShareProfession", "集中")!, "集中", catalog);
        var expected = HifPriorityShare.Validate(data, "集中", catalog);
        // Saving adds newly referenced cards to recognition, as ordinary panel edits do.
        expected["recognition"] = roundtrip["recognition"]!.DeepClone();
        Check(JToken.DeepEquals(expected, roundtrip), "Share/save/reopen lost strategy values");
        foreach (var mutate in new Action<JObject>[] {
            d => d["profession"] = "全力", d => d["version"] = 2,
            d => d["priority"]![0]!["key"] = "missing-card",
            d => ((JArray)d["priority"]!).Add(d["priority"]![0]!.DeepClone()),
            d => d["unknown_priority"] = "7.5", d => d.Remove("followups"),
            d => d["conditional_priorities"]![key]!["remaining_turns_gte"] = 6,
            d => d["use_conditions"]![key]![0]!["remaining_turns_lte"] = 1.5,
            d => d["followups"] = new JObject { [key] = new JObject { ["targets"] = new JArray(key) } }
        })
        {
            var invalid = (JObject)expected.DeepClone(); mutate(invalid);
            var unchanged = (JObject)Invoke(reopened, "ShareProfession", "集中")!;
            try { Invoke(reopened, "ApplyProfession", invalid, "集中"); throw new Exception("Invalid import accepted"); }
            catch (TargetInvocationException error) when (error.InnerException is InvalidOperationException) { }
            Check(JToken.DeepEquals(unchanged, Invoke(reopened, "ShareProfession", "集中") as JToken), "Failed import changed editor");
        }
        window.Close(); reopened.Close();
        Console.WriteLine("Priority sharing passed: roundtrip, save/reopen, local resources, culture, profession isolation and invalid import preservation.");
    }
}
