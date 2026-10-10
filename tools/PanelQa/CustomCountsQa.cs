using System.Collections;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;

internal static class CustomCountsQa
{
    private static object Field(object window, string name) => window.GetType()
        .GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(window)!;
    private static void Call(object window, string name) => window.GetType()
        .GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(window, null);
    private static IDictionary Cards(object window, string profession) => (IDictionary)((IDictionary)Field(window, "_profiles"))[profession]!;
    private static void Assert(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }
    private static HifCustomCardWindow Open(string profession = "全力")
    {
        var window = new HifCustomCardWindow(profession);
        Assert((bool)Field(window, "_loaded"), ((TextBlock)Field(window, "_status")).Text ?? "Failed to load");
        return window;
    }

    public static void Run(string root)
    {
        var path = Path.Combine(root, "config/hif/hif_custom_card_selection.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, new JObject { ["cards"] = new JObject { ["547"] = new JArray(73, 43) } }.ToString());
        var before = File.ReadAllText(path);
        var legacy = Open();
        foreach (var profession in new[] { "好调", "集中", "好印象", "元気", "全力", "強気" })
            Assert(((Dictionary<int, decimal>)Cards(legacy, profession)[547]!)[73] == 3, "Legacy max migration failed");
        Assert(File.ReadAllText(path) == before, "Loading must not modify personal configuration");
        Call(legacy, "Save");
        var migrated = JObject.Parse(File.ReadAllText(path));
        Assert((int?)migrated["version"] == 2 && migrated["cards"] == null, "Version 2 migration failed");

        var window = Open();
        Cards(window, "集中")[547] = new Dictionary<int, decimal>();
        typeof(HifCustomCardWindow).GetField("_activeId", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(window, 547);
        Call(window, "RenderDetails");
        var inputs = ((StackPanel)Field(window, "_details")).Children.OfType<StackPanel>()
            .SelectMany(row => row.Children.OfType<NumericUpDown>()).ToArray();
        Assert(inputs.Length == 3 && inputs[0].Maximum == 3 && inputs[1].Maximum == 1, "Option controls/maxima differ");
        var untouched = File.ReadAllText(path);
        foreach (var text in new[] { "4", "1.5", "abc", "" })
        {
            inputs[0].Text = text;
            Call(window, "Save");
            Assert(File.ReadAllText(path) == untouched, "Invalid typed count was saved or silently clipped: " + text);
        }
        inputs[0].Value = 2;
        inputs[0].Text = "2";
        inputs[1].Value = 1;
        Call(window, "Save");
        var again = Open();
        Assert(((Dictionary<int, decimal>)Cards(again, "全力")[547]!)[73] == 2, "Count save/reopen failed");
        Assert(((Dictionary<int, decimal>)Cards(again, "集中")[547]!).Count == 0, "Profession isolation/empty import failed");
        var selector = ((Grid)window.Content!).Children.OfType<StackPanel>()
            .SelectMany(panel => panel.Children.OfType<ComboBox>()).Single();
        selector.SelectedItem = "集中";
        Assert((string)Field(window, "_profession") == "集中", "Profession switch failed");
        selector.SelectedItem = "全力";
        Assert(((Dictionary<int, decimal>)Cards(window, "全力")[547]!)[73] == 2, "Switch lost count edits");

        var saved = File.ReadAllText(path);
        foreach (var invalid in new decimal[] { -1, 4, 1.5m })
        {
            ((Dictionary<int, decimal>)Cards(again, "全力")[547]!)[73] = invalid;
            Call(again, "Save");
            Assert(File.ReadAllText(path) == saved, "Invalid count overwrote configuration");
        }
        ((Dictionary<int, decimal>)Cards(again, "全力")[547]!)[73] = 2;
        ((Dictionary<int, decimal>)Cards(again, "全力")[547]!)[999] = 1;
        Call(again, "Save");
        Assert(File.ReadAllText(path) == saved, "Invalid option overwrote configuration");
        File.WriteAllText(path, saved + "\n");
        Call(window, "Save");
        Assert(File.ReadAllText(path) == saved + "\n", "Concurrent edit was overwritten");
        Assert(((TextBlock)Field(window, "_status")).Text!.Contains("其他窗口"), "Conflict feedback missing");
        File.WriteAllText(path, saved);

        foreach (var value in new JToken[] { new JValue(-1), new JValue(4), new JValue(1.5), new JValue(true) })
        {
            var invalid = JObject.Parse(saved);
            invalid["profiles"]!["全力"]!["547"]!["73"] = value;
            File.WriteAllText(path, invalid.ToString());
            var rejected = new HifCustomCardWindow("全力");
            Assert(!(bool)Field(rejected, "_loaded"), "Malformed file was silently accepted");
        }
        File.WriteAllText(path, saved);
        var visual = Open();
        visual.WindowStartupLocation = WindowStartupLocation.Manual;
        visual.Position = new PixelPoint(-10000, -10000);
        visual.ShowInTaskbar = false;
        typeof(HifCustomCardWindow).GetField("_activeId", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(visual, 547);
        Call(visual, "RenderDetails");
        visual.Show();
        Avalonia.Threading.Dispatcher.UIThread.RunJobs();
        var content = (Control)visual.Content!;
        ((Grid)content).Background = Avalonia.Media.Brushes.White;
        content.Measure(new Size(900, 670));
        content.Arrange(new Rect(0, 0, 900, 670));
        using var bitmap = new RenderTargetBitmap(new PixelSize(900, 670));
        bitmap.Render(content);
        bitmap.Save(Path.Combine(root, "custom-counts.png"));
        visual.Close();
        Console.WriteLine("Customization counts: migration, controls, round trip, professions, invalid values and conflict checks passed.");
    }
}
