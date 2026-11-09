using System.Reflection;
using System.Security.Cryptography;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Pages.Hif;
using MFAAvalonia.Views.Windows;

internal static class PanelHelpQa
{
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }
    static void Snapshot(Control panel, string path)
    {
        var window = QaHost.Window(panel);
        var content = QaHost.Root(panel);
        // 后续卡窗口的内容是可嵌入的编辑器控件，背景设在其内部的 Grid 上。
        if ((content as Grid ?? (content as UserControl)?.Content as Grid) is { } grid) grid.Background = Brushes.White;
        content.Measure(new Size(window.Width, window.Height));
        content.Arrange(new Rect(0, 0, window.Width, window.Height));
        using var image = new RenderTargetBitmap(new PixelSize((int)window.Width, (int)window.Height));
        image.Render(content); image.Save(path);
    }
    public static void Run(string root)
    {
        var files = Directory.GetFiles(Path.Combine(root, "config"), "*", SearchOption.AllDirectories);
        var before = files.ToDictionary(path => path, path => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))));
        var panels = new (string Name, Control Window)[]
        {
            ("priority", new HifPriorityPanel("集中")),
            ("acquisition", new HifPriorityPanel("集中", true)),
            ("swap-out", new HifPriorityPanel("集中", false, true)),
            ("customization", new HifCustomCardPanel("集中")),
            ("drinks", new HifDrinkPanel("集中")),
            ("followups", new HifFollowupWindow("脚光+", false, null, _ => { })),
            ("drink-pairing", new HifFollowupWindow("特製ハツボシエキス", true, null, _ => { }))
        };
        foreach (var (name, window) in panels)
        {
            if (window is HifStrategyPanel panel)
            {
                // 策略面板的说明在「HIF 策略」页顶部的说明抽屉中显示；面板内不再有说明或导入按钮。
                Check((bool)panel.GetType().GetField("_loaded", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(panel)!, name + " failed to load");
                QaHost.Show(panel);
                Check(panel.HelpText.Split("\n\n").Count(part => part.Contains('\n')) >= 3, name + " missing help sections");
                Check(!panel.GetLogicalDescendants().OfType<Button>().Any(b => b.Content as string is "使用说明" or "导入卡牌" or "导入职业配置" or "导出职业配置"),
                    name + " still shows help, import or profession-share buttons");
                Snapshot(panel, Path.Combine(root, name + "-panel.png"));
                QaHost.Close(panel);
                continue;
            }
            var loaded = window.GetType().GetField("_loaded", BindingFlags.NonPublic | BindingFlags.Instance);
            Check(loaded == null || (bool)loaded.GetValue(window)!, name + " failed to load");
            var host = QaHost.Window(window);
            host.WindowStartupLocation = WindowStartupLocation.Manual;
            host.Position = new PixelPoint(-10000, -10000); host.ShowInTaskbar = false;
            host.Show(); Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            // 面板工具栏的说明按钮在最前；出牌优先级详情中的内嵌编辑器各有自己的说明按钮。
            var button = window.GetLogicalDescendants().OfType<Button>().First(b => b.Content as string == "使用说明");
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
            QaHost.Close(window);
        }
        foreach (var (path, hash) in before)
            Check(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))) == hash, "Help changed configuration: " + path);
        Console.WriteLine("Panel help passed: five panels expose drawer help without help/import buttons; follow-up dialogs keep nonmodal help with bold headings, reuse and close; configuration unchanged.");
    }
}
