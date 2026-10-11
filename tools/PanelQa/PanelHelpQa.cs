using System.Reflection;
using System.Security.Cryptography;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;

internal static class PanelHelpQa
{
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }
    static void Snapshot(Window window, string path)
    {
        var content = (Control)window.Content!;
        ((Grid)content).Background = Brushes.White;
        content.Measure(new Size(window.Width, window.Height));
        content.Arrange(new Rect(0, 0, window.Width, window.Height));
        using var image = new RenderTargetBitmap(new PixelSize((int)window.Width, (int)window.Height));
        image.Render(content); image.Save(path);
    }
    public static void Run(string root)
    {
        var files = Directory.GetFiles(Path.Combine(root, "config"), "*", SearchOption.AllDirectories);
        var before = files.ToDictionary(path => path, path => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))));
        var panels = new (string Name, Window Window)[]
        {
            ("priority", new HifPriorityWindow("集中")),
            ("acquisition", new HifPriorityWindow("集中", true)),
            ("swap-out", new HifPriorityWindow("集中", false, true)),
            ("customization", new HifCustomCardWindow("集中")),
            ("drinks", new HifDrinkWindow("集中")),
            ("followups", new HifFollowupWindow("脚光+", false, null, _ => { })),
            ("drink-pairing", new HifFollowupWindow("特製ハツボシエキス", true, null, _ => { }))
        };
        foreach (var (name, window) in panels)
        {
            var loaded = window.GetType().GetField("_loaded", BindingFlags.NonPublic | BindingFlags.Instance);
            Check(loaded == null || (bool)loaded.GetValue(window)!, name + " failed to load");
            window.WindowStartupLocation = WindowStartupLocation.Manual;
            window.Position = new PixelPoint(-10000, -10000); window.ShowInTaskbar = false;
            window.Show(); Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            var button = window.GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string == "使用说明");
            button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            var help = (HifPanelHelp)window.GetType().GetField("_help", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(window)!;
            var active = help.ActiveWindow!;
            Check(active.IsVisible && window.IsEnabled, name + " help blocked editing");
            Check(active.GetLogicalDescendants().OfType<TextBlock>().Count(t => t.FontWeight == FontWeight.Bold) >= 3,
                name + " missing distinct bold option headings");
            var selector = window.GetLogicalDescendants().OfType<ComboBox>().FirstOrDefault(c => c.SelectedItem as string == "集中");
            if (selector != null) { selector.SelectedItem = "全力"; selector.SelectedItem = "集中"; }
            button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(ReferenceEquals(active, help.ActiveWindow), name + " created duplicate help");
            Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            Snapshot(active, Path.Combine(root, name + "-help.png"));
            active.Close(); Check(help.ActiveWindow == null, name + " did not clear closed help");
            Snapshot(window, Path.Combine(root, name + "-panel.png"));
            window.Close();
        }
        foreach (var (path, hash) in before)
            Check(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))) == hash, "Help changed configuration: " + path);
        Console.WriteLine("Panel help passed: seven panels, nonmodal editing, separate bold headings, reuse, close, and unchanged configuration.");
    }
}
