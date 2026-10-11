using System.Reflection;
using Avalonia.Controls;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;

static class SwapOutOptionsQa
{
    static object Field(object window, string name) => window.GetType()
        .GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
    static void Invoke(object window, string name) => window.GetType()
        .GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
    static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    static bool Preserves(JToken original, JToken? saved) => original is JObject obj
        ? saved is JObject current && obj.Properties().All(property => Preserves(property.Value, current[property.Name]))
        : JToken.DeepEquals(original, saved);

    public static void Run(string root)
    {
        var path = HifLayout.PersonalPath("cards_priority.json");
        var before = JObject.Parse(File.ReadAllText(path));
        before.Remove("swap_out_options_profiles");
        before["swap_out_priority_profiles"]!["集中"] = new JArray("スポットライト+", "魅惑の視線+");
        File.WriteAllText(path, before.ToString());
        var window = new HifPriorityWindow("集中", initialSwapOutMode: true);
        Check((bool)Field(window, "_loaded"), "Panel failed to load");
        var rows = (StackPanel)Field(window, "_rows");
        var cards = rows.Children.OfType<Border>().ToArray();
        Check(cards.Length == 2, "Expected two individual card rows");
        var controls = cards.Select(card => ((StackPanel)card.Child!).Children.OfType<StackPanel>()
            .Single().Children.OfType<CheckBox>().ToArray()).ToArray();
        Check(controls.All(row => row.Length == 2 && row.All(check => check.IsChecked == true)), "New switches must default on");
        controls[0][0].IsChecked = false;
        controls[0][1].IsChecked = false;
        Invoke(window, "SaveConfig");
        var saved = JObject.Parse(File.ReadAllText(path));
        var policy = saved["swap_out_options_profiles"]!["集中"]!["スポットライト+"]!;
        Check((bool)policy["prefer_uncustomized"]! == false && (bool)policy["allow_customized"]! == false,
            "Explicit false values were lost");
        foreach (var field in before.Properties())
            if (field.Name != "recognition_profiles")
                Check(Preserves(field.Value, saved[field.Name]), "Existing configuration changed: " + field.Name);
        var reopened = new HifPriorityWindow("集中", initialSwapOutMode: true);
        var rules = (Dictionary<string, JObject>)Field(reopened, "_swapOutOptions");
        Check((bool)rules["集中"]["スポットライト+"]!["allow_customized"]! == false, "Reopen reset saved switch");
        Check(rules["集中"]["魅惑の視線+"] == null && rules["全力"].Count == 0, "Switches leaked to another card or profession");
        Console.WriteLine("Swap-out panel passed: per-card controls, defaults on, explicit false save/reopen, old configuration preserved.");
    }
}
