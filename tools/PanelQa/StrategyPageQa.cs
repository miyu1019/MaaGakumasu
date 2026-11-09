using System.Reflection;
using System.Security.Cryptography;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using Avalonia.Controls.Primitives;
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
    // 有未保存修改的标签名后带「 •」，按前缀查找。
    static Button Tab(HifStrategyView view, string label) =>
        ((HifSegmented)Field(view, "_tabs")).Items.Single(b => ((string?)b.Content ?? "").Split(' ')[0] == label);

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
        Check(labels.SequenceEqual(new[] { "出牌优先级", "优先获取卡", "授业换出卡", "后续卡", "饮料", "技能卡定制", "卡库" }), "Unexpected tabs: " + string.Join(",", labels));
        Check(Panel(view) is HifPriorityPanel && (bool)Field(Panel(view), "_loaded"), "Priority tab did not load");
        Snapshot(host, Path.Combine(root, "strategy-page-priority.png"));
        // 宽窗口：详情随窗口变宽，卡表保持定宽；后续卡为精简编辑器。
        var detailPane = (StackPanel)Field(Panel(view), "_detail");
        detailPane.Children.OfType<HifSegmented>().Single().Items[1].RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Run();
        host.Width = 1500; Run();
        var listWidth = ((Grid)Field(Panel(view), "_body")).ColumnDefinitions[0].ActualWidth;
        Check(((ScrollViewer)Field(Panel(view), "_detailScroll")).Bounds.Width > listWidth, "Detail pane did not take the extra width");
        Snapshot(host, Path.Combine(root, "strategy-page-priority-wide.png"));
        host.Width = 1100; Run();
        detailPane.Children.OfType<HifSegmented>().Single().Items[0].RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Run();

        // 页面职业选择器驱动面板。
        var professions = (HifSegmented)Field(view, "_professions");
        Check(professions.Items.Select(b => (string?)b.Content).SequenceEqual(HifStrategyPanel.Professions), "Unexpected profession pills");
        Click(professions.Items.Single(b => (string?)b.Content == "全力"));
        Check(professions.SelectedIndex == Array.IndexOf(HifStrategyPanel.Professions, "全力"), "Profession pill not highlighted");
        Check(Panel(view).Profession == "全力", "Profession selector did not switch the panel");

        // 每个标签都能加载。
        foreach (var (label, type) in new[] { ("优先获取卡", typeof(HifPriorityPanel)), ("授业换出卡", typeof(HifPriorityPanel)), ("后续卡", typeof(HifPriorityPanel)),
                     ("饮料", typeof(HifDrinkPanel)), ("技能卡定制", typeof(HifCustomCardPanel)), ("出牌优先级", typeof(HifPriorityPanel)) })
        {
            Click(Tab(view, label));
            Check(Panel(view).GetType() == type && (bool)Field(Panel(view), "_loaded"), label + " tab did not load: " + Panel(view).GetType().Name + " " + ((TextBlock)Field(Panel(view), "_status")).Text);
            Check(Panel(view).Profession == "全力", label + " tab lost the selected profession");
            if (label == "饮料") Snapshot(host, Path.Combine(root, "strategy-page-drinks.png"));
        }

        // 三个卡牌标签共用一个面板；有未保存修改时切换标签不弹窗、不丢修改。
        var priority = Panel(view);
        Click(Tab(view, "优先获取卡"));
        Check(ReferenceEquals(Panel(view), priority), "Card tabs did not share one panel");
        Click(Tab(view, "出牌优先级"));
        priority.GetType().GetMethod("MarkChanged", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(priority, null);
        Check(priority.HasUnsavedChanges && HifStrategyView.HasUnsavedChanges, "Unsaved state was not reported");
        Check(((TextBlock)Field(view, "_unsaved")).IsVisible && ((Button)Field(view, "_save")).IsEnabled
            && ((Button)Field(view, "_discard")).IsVisible, "Unsaved marker, save or discard button not shown");
        Click(Tab(view, "饮料"));
        Check(!host.OwnedWindows.Any(), "Switching tabs opened a dialog");
        Check(Panel(view) is HifDrinkPanel && priority.HasUnsavedChanges, "Switching tabs lost card edits");
        Check(((string?)Tab(view, "出牌优先级").Content)!.EndsWith(" •"), "Changed tab not marked");

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

        // 卡库：点卡加入或移出所选名单，直接修改共用的编辑数据。
        Click(Tab(view, "卡库"));
        var library = (HifCardLibraryView)Field(view, "_library");
        var cards = (HifPriorityPanel)Field(view, "_cards");
        var custom = (HifCustomCardPanel)Field(view, "_custom");
        Check(ReferenceEquals(cards, priority), "Library edits a different card panel");
        Check(library.Target == HifLibraryTarget.Priority, "Library did not default to the priority list");
        var visible = (List<HifLibraryCard>)Field(library, "_visible");
        Check(visible.Count > 0 && visible.Count < cards.LibraryCards().Count, $"Plan filter did not narrow the library: {visible.Count}");
        Check(visible.All(card => card.Plan is "" or "free" or "anomaly"), "全力 library shows other plans");
        var pick = visible.First(card => !cards.InList(HifLibraryTarget.Priority, card.Name));
        library.Toggle(pick); Run();
        Check(cards.InList(HifLibraryTarget.Priority, pick.Name), "Library did not add to the priority list");
        Snapshot(host, Path.Combine(root, "strategy-page-library.png"));
        library.Toggle(pick); Run();
        Check(!cards.InList(HifLibraryTarget.Priority, pick.Name), "Library did not remove from the priority list");
        library.Open(HifLibraryTarget.Customization); Run();
        visible = (List<HifLibraryCard>)Field(library, "_visible");
        Check(visible.Count > 0 && visible.All(card => custom.Customizable(card.Id)), "Customization library shows non-customizable cards");
        var customPick = visible.First(card => !custom.InList(card.Id));
        library.Toggle(customPick); Run();
        Check(custom.InList(customPick.Id) && custom.HasUnsavedChanges, "Library did not add to customization");
        library.Toggle(customPick); Run();
        Check(!custom.InList(customPick.Id), "Library did not remove from customization");

        // 后续卡：选起始卡后点卡加入；首次加入新建组合，移空删除组合。
        library.Open(HifLibraryTarget.Followups); Run();
        Check(((List<HifLibraryCard>)Field(library, "_visible")).Count == 0 || cards.FollowupStart != null, "Follow-up library listed cards without a start card");
        var start = cards.LibraryCards().First(card => !card.PriorityOnly && card.Plan is "anomaly" or "free").Name;
        cards.FollowupStart = start;
        library.Open(HifLibraryTarget.Followups); Run();
        visible = (List<HifLibraryCard>)Field(library, "_visible");
        Check(visible.Count > 0 && visible.All(card => card.Name != start), "Follow-up library includes the start card");
        // 应用组合时会替换整个职业的组合对象，每次重新读取。
        Newtonsoft.Json.Linq.JObject Rules() => (Newtonsoft.Json.Linq.JObject)((System.Collections.IDictionary)Field(cards, "_followups"))["全力"]!;
        var hadRule = Rules()[start] != null;
        var followPick = visible.First(card => !cards.InList(HifLibraryTarget.Followups, card.Name));
        Check(library.Toggle(followPick), "Follow-up add was rejected");
        Run();
        Check(cards.InList(HifLibraryTarget.Followups, followPick.Name) && Rules()[start] != null, "Library did not add a follow-up");
        Snapshot(host, Path.Combine(root, "strategy-page-library-followups.png"));
        Check(library.Toggle(followPick), "Follow-up removal was rejected");
        Run();
        Check(!cards.InList(HifLibraryTarget.Followups, followPick.Name) && (hadRule || Rules()[start] == null), "Library did not remove the follow-up");

        // 加卡统一在卡库：各标签不再有「导入卡牌」按钮。
        foreach (var label in new[] { "出牌优先级", "优先获取卡", "授业换出卡", "后续卡", "饮料", "技能卡定制" })
        {
            Click(Tab(view, label));
            Check(!((ContentControl)Field(view, "_host")).GetLogicalDescendants().OfType<Button>().Any(b => b.Content as string is "导入卡牌" or "导入饮料" or "使用说明"),
                label + " still shows import or help buttons");
        }

        // 使用说明抽屉：随标签切换内容，不弹窗。
        var helpToggle = (ToggleButton)Field(view, "_helpToggle");
        helpToggle.IsChecked = true; Run();
        var drawer = (Border)Field(view, "_helpDrawer");
        var sections = (StackPanel)Field(view, "_helpSections");
        Check(drawer.IsVisible && sections.Children.Count >= 4, "Help drawer did not show sections");
        Click(Tab(view, "后续卡"));
        Check(((TextBlock)sections.Children[0]).Text!.StartsWith("后续卡"), "Help drawer did not follow the tab");
        Snapshot(host, Path.Combine(root, "strategy-page-followups-help.png"));
        Click(Tab(view, "卡库"));
        Check(((TextBlock)sections.Children[0]).Text!.StartsWith("卡库") && sections.Children.Count >= 4, "Library help missing");
        helpToggle.IsChecked = false; Run();
        Check(!drawer.IsVisible && !host.OwnedWindows.Any(), "Help drawer did not close or opened a window");
        Check(((Button)Field(view, "_shareMenu")).Flyout is MenuFlyout menu && menu.Items.Count == 2, "Profession share menu missing");

        // 放弃修改：重新读取，所有面板不再有未保存修改。
        Click((Button)Field(view, "_discard"));
        Check(!HifStrategyView.HasUnsavedChanges && !((Button)Field(view, "_save")).IsEnabled, "Discard kept unsaved changes");

        // 进入页面时重新读取。
        Click(Tab(view, "饮料"));
        view.OnPageEntered(); Run();
        Check(Panel(view) is HifDrinkPanel && (bool)Field(Panel(view), "_loaded"), "Re-entering did not reload the current tab");

        host.Close();
        foreach (var (path, hash) in before)
            Check(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))) == hash, "Strategy page changed configuration: " + path);
        Console.WriteLine("Strategy page passed: seven tabs incl. follow-ups, profession pills, shared card edits without prompts, all drinks, library add/remove, no import or help buttons in panels, help drawer follows tabs, share menu, discard, reload, configuration unchanged.");
    }
}
