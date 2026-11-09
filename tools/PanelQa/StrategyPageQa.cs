using System.Reflection;
using System.Security.Cryptography;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Pages.Hif;

/// <summary>「HIF 策略」页：五个标签、职业切换、未保存修改的离开确认，以及不改动配置。</summary>
internal static class StrategyPageQa
{
    static void Check(bool valid, string message) { if (!valid) throw new Exception(message); }
    static object Field(object instance, string name) =>
        instance.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(instance)!;
    static HifStrategyPanel Panel(HifStrategyView view) => (HifStrategyPanel)Field(view, "_panel");
    static void Run() { for (var i = 0; i < 10; i++) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(10); } }
    static Button Tab(HifStrategyView view, string label) =>
        ((HifSegmented)Field(view, "_tabs")).Items.Single(b => (string?)b.Content == label);

    static void Click(Button button) { button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Run(); }

    static void Snapshot(Window window, string path)
    {
        Run();
        using var image = new RenderTargetBitmap(new PixelSize((int)window.Width, (int)window.Height));
        image.Render(window); image.Save(path);
    }

    public static void Run(string root)
    {
        // 只比较策略文件；主界面视图模型初始化会写入通用设置 config.json。
        var files = Directory.GetFiles(Path.Combine(root, "config", "hif"), "*", SearchOption.AllDirectories);
        var before = files.ToDictionary(path => path, path => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))));
        var view = new HifStrategyView();
        var host = QaHost.Window(view);
        host.Width = 1100; host.Height = 760; host.Background = Brushes.White;
        host.Show(); Run();

        Check(HifStrategyView.Current == view, "Page was not registered as current");
        var labels = ((HifSegmented)Field(view, "_tabs")).Items.Select(b => (string?)b.Content).ToArray();
        Check(labels.SequenceEqual(new[] { "出牌优先级", "优先获取卡", "授业换出卡", "饮料", "技能卡定制" }), "Unexpected tabs: " + string.Join(",", labels));
        Check(Panel(view) is HifPriorityPanel && (bool)Field(Panel(view), "_loaded"), "Priority tab did not load");
        Snapshot(host, Path.Combine(root, "strategy-page-priority.png"));

        // 页面职业选择器驱动面板。
        var professions = (HifSegmented)Field(view, "_professions");
        Check(professions.Items.Select(b => (string?)b.Content).SequenceEqual(HifStrategyPanel.Professions), "Unexpected profession pills");
        Click(professions.Items.Single(b => (string?)b.Content == "全力"));
        Check(professions.SelectedIndex == Array.IndexOf(HifStrategyPanel.Professions, "全力"), "Profession pill not highlighted");
        Check(Panel(view).Profession == "全力", "Profession selector did not switch the panel");

        // 每个标签都能加载。
        foreach (var (label, type) in new[] { ("优先获取卡", typeof(HifPriorityPanel)), ("授业换出卡", typeof(HifPriorityPanel)),
                     ("饮料", typeof(HifDrinkPanel)), ("技能卡定制", typeof(HifCustomCardPanel)), ("出牌优先级", typeof(HifPriorityPanel)) })
        {
            Click(Tab(view, label));
            Check(Panel(view).GetType() == type && (bool)Field(Panel(view), "_loaded"), label + " tab did not load: " + Panel(view).GetType().Name + " " + ((TextBlock)Field(Panel(view), "_status")).Text);
            Check(Panel(view).Profession == "全力", label + " tab lost the selected profession");
            if (label == "饮料") Snapshot(host, Path.Combine(root, "strategy-page-drinks.png"));
        }

        // 有未保存修改时切换标签：取消留在原面板，不保存则切换且不写配置。
        var priority = Panel(view);
        priority.GetType().GetMethod("MarkChanged", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(priority, null);
        Check(priority.HasUnsavedChanges && HifStrategyView.HasUnsavedChanges, "Unsaved state was not reported");
        Check(((TextBlock)Field(view, "_unsaved")).IsVisible && ((Button)Field(view, "_save")).IsEnabled, "Unsaved marker or save button not shown");
        foreach (var (choice, leaves) in new[] { ("取消", false), ("不保存", true) })
        {
            Tab(view, "饮料").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Run();
            var dialog = host.OwnedWindows.Single();
            Check(((Control)dialog.Content!).GetLogicalDescendants().OfType<TextBlock>().Any(t => t.Text?.Contains("未保存") == true), "Leave prompt text missing");
            if (choice == "取消") Snapshot(dialog, Path.Combine(root, "strategy-page-leave-prompt.png"));
            ((Control)dialog.Content!).GetLogicalDescendants().OfType<Button>().Single(b => (string?)b.Content == choice)
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Run();
            Check((Panel(view) is HifDrinkPanel) == leaves, choice + " gave the wrong tab");
        }
        Check(!HifStrategyView.HasUnsavedChanges, "Discarded changes still reported");

        // 饮料一次列出全部；「单独设置」开关加入或移出领取名单。
        var drinks = (HifDrinkPanel)Panel(view);
        var catalogCount = ((System.Collections.IDictionary)Field(drinks, "_catalog")).Count;
        var profiles = (System.Collections.IDictionary)Field(drinks, "_profiles");
        var configured = (List<Newtonsoft.Json.Linq.JObject>)profiles["全力"]!;
        var rows = (StackPanel)Field(drinks, "_rows");
        var others = rows.Children.OfType<WrapPanel>().Single(p => p.Name == "others");
        Check(configured.Count + others.Children.Count == catalogCount, $"Drinks not all listed: {configured.Count}+{others.Children.Count} of {catalogCount}");
        var drinkCount = configured.Count;
        var firstOther = others.Children.OfType<Border>().First().GetLogicalDescendants().OfType<ToggleSwitch>().Single(t => (string?)t.Tag == "单独设置");
        firstOther.IsChecked = true; Run();
        Check(configured.Count == drinkCount + 1 && drinks.HasUnsavedChanges, "Turning on 单独设置 did not add the drink");
        others = rows.Children.OfType<WrapPanel>().Single(p => p.Name == "others");
        Check(configured.Count + others.Children.Count == catalogCount, "Added drink still listed under other drinks");
        Snapshot(host, Path.Combine(root, "strategy-page-drinks-all.png"));
        var lastCard = rows.Children.OfType<Border>().Where(b => b.Child is Grid).Last();
        lastCard.GetLogicalDescendants().OfType<ToggleSwitch>().Single(t => (string?)t.Tag == "单独设置").IsChecked = false; Run();
        Check(configured.Count == drinkCount, "Turning off 单独设置 did not remove the drink");
        Tab(view, "技能卡定制").RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Run();
        ((Control)host.OwnedWindows.Single().Content!).GetLogicalDescendants().OfType<Button>().Single(b => (string?)b.Content == "不保存")
            .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Run();
        Check(Panel(view) is HifCustomCardPanel, "Discarding drink edits did not switch tabs");
        Click(Tab(view, "饮料"));

        // 进入页面时重新读取。
        view.OnPageEntered(); Run();
        Check(Panel(view) is HifDrinkPanel && (bool)Field(Panel(view), "_loaded"), "Re-entering did not reload the current tab");

        host.Close();
        foreach (var (path, hash) in before)
            Check(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))) == hash, "Strategy page changed configuration: " + path);
        Console.WriteLine("Strategy page passed: five tabs load, profession selector, leave prompt cancel/discard, reload on entry, configuration unchanged.");
    }
}
