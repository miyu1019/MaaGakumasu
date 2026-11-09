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

internal static class PanelHelpQa
{
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }
    static void Snapshot(Control panel, string path)
    {
        var window = QaHost.Window(panel);
        var content = QaHost.Root(panel);
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
        var sequences = new HifPriorityPanel("集中");
        sequences.SetMode(false, true);
        var panels = new (string Name, HifStrategyPanel Panel)[]
        {
            ("priority", new HifPriorityPanel("集中")),
            ("swap-out", new HifPriorityPanel("集中", true)),
            ("sequences", sequences),
            ("customization", new HifCustomCardPanel("集中")),
            ("drinks", new HifDrinkPanel("集中"))
        };
        foreach (var (name, panel) in panels)
        {
            // 策略面板的说明在「HIF 策略」页顶部的说明抽屉中显示；面板内不再有说明或导入按钮。
            Check((bool)panel.GetType().GetField("_loaded", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(panel)!, name + " failed to load");
            QaHost.Show(panel);
            Check(panel.HelpText.Split("\n\n").Count(part => part.Contains('\n')) >= 3, name + " missing help sections");
            Check(!panel.GetLogicalDescendants().OfType<Button>().Any(b => b.Content as string is "使用说明" or "导入卡牌" or "导入职业配置" or "导出职业配置"),
                name + " still shows help, import or profession-share buttons");
            Snapshot(panel, Path.Combine(root, name + "-panel.png"));
            QaHost.Close(panel);
        }
        foreach (var (path, hash) in before)
            Check(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))) == hash, "Help changed configuration: " + path);
        Console.WriteLine("Panel help passed: five panels incl. play sequences and acquire-and-customize expose drawer help without help/import buttons; configuration unchanged.");
    }
}
