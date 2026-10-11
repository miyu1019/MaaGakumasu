using System.Collections;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.LogicalTree;
using Avalonia.Media.Imaging;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;

internal static class DrinkLimitsQa
{
    static object Field(object window, string name) => window.GetType()
        .GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(window)!;
    static void Call(object window, string name) => window.GetType()
        .GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(window, null);
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }
    static Window? Help(object window) => ((HifPanelHelp)Field(window, "_help")).ActiveWindow;
    static HifDrinkWindow Open()
    {
        var window = new HifDrinkWindow("集中");
        Check((bool)Field(window, "_loaded"), ((TextBlock)Field(window, "_status")).Text ?? "Load failed");
        return window;
    }

    public static void Run(string root)
    {
        var path = HifLayout.PersonalPath("hif_drink_profiles.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, new JObject { ["profiles"] = new JObject
        {
            ["集中"] = new JArray(new JObject { ["id"] = 28 }, new JObject { ["id"] = 14, ["disabled"] = true }),
            ["全力"] = new JArray(new JObject { ["id"] = 27 })
        } }.ToString());
        var before = File.ReadAllText(path);
        var window = Open();
        var rows = (StackPanel)Field(window, "_rows");
        var policy = rows.Children.OfType<Border>().Single(p => p.Name == "unconfigured");
        var general = rows.Children.OfType<Border>().Single(p => p.Name == "general");
        var unconfiguredUse = policy.GetLogicalDescendants().OfType<CheckBox>().Single(c => (string?)c.Content == "一战使用");
        Check(unconfiguredUse.IsChecked == true, "Default first battle disabled");
        var fallbackLimit = policy.GetLogicalDescendants().OfType<ComboBox>().Single(p => p.Name == "unconfigured_max_uses");
        var totalLimit = general.GetLogicalDescendants().OfType<ComboBox>().Single(p => p.Name == "max_uses");
        foreach (var limit in new[] { fallbackLimit, totalLimit })
        {
            Check(limit.SelectedIndex == 0, "Default limits not unlimited");
            Check(limit.ItemsSource!.Cast<string>().SequenceEqual(new[] { "不限量", "最多0瓶", "最多1瓶", "最多2瓶", "最多3瓶", "最多4瓶" }), "Ambiguous or invalid limit choices");
        }
        unconfiguredUse.IsChecked = false;
        Check(!fallbackLimit.IsEnabled, "Unused fallback limit remains editable");
        unconfiguredUse.IsChecked = true;
        var grid = (Grid)rows.Children.OfType<Border>().First(p => p.Child is Grid).Child!;
        var switches = grid.GetLogicalDescendants().OfType<CheckBox>().Where(c => (string?)c.Content != "不限").ToArray();
        Check(switches.Where(c => ((string)c.Content!).Contains("使用")).All(c => c.IsChecked == true), "Both battles must default on");
        switches.Single(c => (string?)c.Content == "一战使用").IsChecked = false;
        var bottle = grid.GetLogicalDescendants().OfType<ComboBox>().Single(p => p.Name == "first_battle_max_uses");
        Check(!bottle.IsEnabled, "Unused bottle limit remains editable");
        switches.Single(c => (string?)c.Content == "一战使用").IsChecked = true;
        bottle.SelectedIndex = 2;
        var purchase = switches.Single(c => (string?)c.Content == "购买");
        purchase.IsChecked = true;
        switches.Single(c => (string?)c.Content == "一战使用").IsChecked = false;
        switches.Single(c => (string?)c.Content == "二战使用").IsChecked = false;
        Check(!purchase.IsEnabled && purchase.IsChecked == true, "Turning battles off erased purchase preference");
        switches.Single(c => (string?)c.Content == "二战使用").IsChecked = true;
        Check(purchase.IsEnabled && purchase.IsChecked == true, "Purchase preference was not restored");
        fallbackLimit.SelectedIndex = 2;
        totalLimit.SelectedIndex = 3;
        totalLimit.SelectedIndex = 1;
        Check((int)((JObject)((IDictionary)Field(window, "_firstBattlePolicies"))["集中"]!)["max_uses"]! == 0, "Zero total did not update");
        totalLimit.SelectedIndex = 3;
        totalLimit.SelectedIndex = 0;
        totalLimit.SelectedIndex = 3;
        Check((int)((JObject)((IDictionary)Field(window, "_firstBattlePolicies"))["集中"]!)["max_uses"]! == 2, "Limit selection changed the wrong value");
        Check(File.ReadAllText(path) == before, "Editing wrote before Save");
        window.WindowStartupLocation = WindowStartupLocation.Manual;
        window.Position = new PixelPoint(-10000, -10000);
        window.ShowInTaskbar = false;
        window.Show();
        Call(window, "ShowHelp");
        var help = Help(window)!;
        Check(help.IsVisible && window.IsEnabled, "Help blocked the editor");
        switches.Single(c => (string?)c.Content == "一战使用").IsChecked = true;
        Check((bool)((List<JObject>)((IDictionary)Field(window, "_profiles"))["集中"]!)[0]["first_battle_enabled"]!, "Editor did not update while help was open");
        switches.Single(c => (string?)c.Content == "一战使用").IsChecked = false;
        Call(window, "ShowHelp");
        Check(ReferenceEquals(help, Help(window)), "Duplicate help window opened");
        help.Close();
        Check(Help(window) == null, "Help reference was not cleared");
        window.Hide();
        var timing = grid.GetLogicalDescendants().OfType<ComboBox>().Single(c => c.Name != "first_battle_max_uses");
        timing.SelectedIndex = 2;
        var turns = grid.GetLogicalDescendants().OfType<NumericUpDown>().Single(n => n.Maximum == 99);
        turns.Value = 1.5m;
        Call(window, "Save");
        Check(File.ReadAllText(path) == before, "Fractional timing silently fell back to default");
        turns.Value = 3;
        timing.SelectedIndex = 0;
        Call(window, "Save");
        var saved = JObject.Parse(File.ReadAllText(path));
        var item = saved["profiles"]!["集中"]![0]!;
        Check(!(bool)item["first_battle_enabled"]! && (bool)item["second_battle_enabled"]!
            && (int)item["first_battle_max_uses"]! == 1 && !(bool)item["disabled"]!, "Switch or limit round trip failed");
        var legacy = saved["profiles"]!["集中"]![1]!;
        Check(!(bool)legacy["first_battle_enabled"]! && !(bool)legacy["second_battle_enabled"]!, "Legacy disabled lost");
        Check((int)saved["first_battle_policy_profiles"]!["集中"]!["max_uses"]! == 2
            && (int)saved["first_battle_policy_profiles"]!["集中"]!["unconfigured_max_uses"]! == 1, "Policy not saved");
        Check((bool)saved["profiles"]!["全力"]![0]!["first_battle_enabled"]!, "Other profession changed");
        var reopened = Open();
        var policies = (IDictionary)Field(reopened, "_firstBattlePolicies");
        Check(JToken.DeepEquals((JObject)policies["集中"]!, saved["first_battle_policy_profiles"]!["集中"]), "Reopen lost limits");
        ((JObject)policies["集中"]!)["max_uses"] = 1.5;
        Call(reopened, "Save");
        Check(JToken.DeepEquals(saved, JObject.Parse(File.ReadAllText(path))), "Fractional cap overwrote settings");
        saved["profiles"]!["集中"] = new JArray(saved["profiles"]!["集中"]![0]!.DeepClone(),
            new JObject { ["id"] = 25, ["before_card_targets"] = new JArray("脚光+") }, new JObject { ["id"] = 22 });
        saved["profiles"]!["集中"]![0]!["id"] = 12;
        saved["profiles"]!["集中"]![0]!["first_battle_max_uses"] = 1;
        saved["profiles"]!["集中"]![0]!["first_battle_enabled"] = true;
        saved["first_battle_policy_profiles"]!["集中"]!["max_uses"] = -1;
        saved["first_battle_policy_profiles"]!["集中"]!["unconfigured_max_uses"] = -1;
        File.WriteAllText(path, saved.ToString());
        var visual = Open();
        visual.WindowStartupLocation = WindowStartupLocation.Manual;
        visual.Position = new PixelPoint(-10000, -10000);
        visual.ShowInTaskbar = false;
        visual.Show();
        Avalonia.Threading.Dispatcher.UIThread.RunJobs();
        var content = (Control)visual.Content!;
        ((Grid)content).Background = Avalonia.Media.Brushes.White;
        foreach (var width in new[] { 920, 800 })
        {
            visual.Width = width;
            Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            content.Measure(new Size(width, 680));
            content.Arrange(new Rect(0, 0, width, 680));
            using var bitmap = new RenderTargetBitmap(new PixelSize(width, 680));
            bitmap.Render(content);
            bitmap.Save(Path.Combine(root, width == 920 ? "drink-limits.png" : "drink-limits-min-width.png"));
        }
        Call(visual, "ShowHelp");
        var helpVisual = Help(visual)!;
        Avalonia.Threading.Dispatcher.UIThread.RunJobs();
        var helpContent = (Control)helpVisual.Content!;
        ((Grid)helpContent).Background = Avalonia.Media.Brushes.White;
        helpContent.Measure(new Size(640, 590));
        helpContent.Arrange(new Rect(0, 0, 640, 590));
        using (var helpImage = new RenderTargetBitmap(new PixelSize(640, 590)))
        {
            helpImage.Render(helpContent);
            helpImage.Save(Path.Combine(root, "drink-help.png"));
        }
        helpVisual.Close();
        visual.Close();
        Console.WriteLine("Drink limits: defaults, controls, legacy migration, save/reopen, professions, invalid values and modeless help passed.");
    }
}
