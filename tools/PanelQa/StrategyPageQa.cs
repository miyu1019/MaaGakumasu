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
        Check(labels.SequenceEqual(new[] { "出牌优先级", "获取与定制", "授业换出卡", "出牌编排", "卡库", "饮料" }), "Unexpected tabs: " + string.Join(",", labels));
        Check(Panel(view) is HifPriorityPanel && (bool)Field(Panel(view), "_loaded"), "Priority tab did not load");
        Snapshot(host, Path.Combine(root, "strategy-page-priority.png"));
        // 宽窗口：详情随窗口变宽，卡表保持定宽。
        // 条件优先级与使用限制同时显示：宽窗口左右并排，窄窗口上下叠放，都不超出详情宽度。
        var detailPane = (StackPanel)Field(Panel(view), "_detail");
        var detailScroll = (ScrollViewer)Field(Panel(view), "_detailScroll");
        Border[] RuleCards() => detailPane.Children.OfType<Grid>().Single(g => g.Name == "rules").Children.OfType<Border>().ToArray();
        Check(RuleCards().Select(card => (string?)card.Tag).SequenceEqual(new[] { "条件优先级", "使用限制" }), "Rule cards missing");
        Check(!detailPane.Children.OfType<HifSegmented>().Any(), "Detail still switches rule editors with tabs");
        host.Width = 1500; Run();
        // 屏幕较小的环境（例如构建机）窗口到不了 1500 宽；宽窗口的排列只在确实变宽时检查。
        bool Wide() => host.Bounds.Width >= 1400;
        var listWidth = ((Grid)Field(Panel(view), "_body")).ColumnDefinitions[0].ActualWidth;
        Check(detailScroll.Bounds.Width > listWidth, "Detail pane did not take the extra width");
        var wideCards = RuleCards();
        Check(!Wide() || Math.Abs(wideCards[0].Bounds.Top - wideCards[1].Bounds.Top) < 1 && wideCards[1].Bounds.Left > wideCards[0].Bounds.Right,
            $"Rule cards are not side by side in a wide window: window {host.Bounds.Width}, detail {detailScroll.Bounds.Width}");
        Snapshot(host, Path.Combine(root, "strategy-page-priority-wide.png"));
        host.Width = 960; Run();
        var narrowCards = RuleCards();
        Check(narrowCards[1].Bounds.Top >= narrowCards[0].Bounds.Bottom, "Rule cards are not stacked in a narrow window");
        Check(narrowCards.All(card => card.Bounds.Right <= detailPane.Bounds.Width + 1
                && card.GetLogicalDescendants().OfType<Button>().All(b => b.Bounds.Width > 0)), "Rule cards overflow the narrow detail pane");
        Snapshot(host, Path.Combine(root, "strategy-page-priority-narrow.png"));
        host.Width = 1100; Run();

        // 页面职业选择器驱动面板。
        var professions = (HifSegmented)Field(view, "_professions");
        Check(professions.Items.Select(b => (string?)b.Content).SequenceEqual(HifStrategyPanel.Professions), "Unexpected profession pills");
        Click(professions.Items.Single(b => (string?)b.Content == "全力"));
        Check(professions.SelectedIndex == Array.IndexOf(HifStrategyPanel.Professions, "全力"), "Profession pill not highlighted");
        Check(Panel(view).Profession == "全力", "Profession selector did not switch the panel");

        // 每个标签都能加载。
        foreach (var (label, type) in new[] { ("授业换出卡", typeof(HifPriorityPanel)), ("出牌编排", typeof(HifPriorityPanel)),
                     ("饮料", typeof(HifDrinkPanel)), ("获取与定制", typeof(HifCustomCardPanel)), ("出牌优先级", typeof(HifPriorityPanel)) })
        {
            Click(Tab(view, label));
            Check(Panel(view).GetType() == type && (bool)Field(Panel(view), "_loaded"), label + " tab did not load: " + Panel(view).GetType().Name + " " + ((TextBlock)Field(Panel(view), "_status")).Text);
            Check(Panel(view).Profession == "全力", label + " tab lost the selected profession");
            if (label == "饮料") Snapshot(host, Path.Combine(root, "strategy-page-drinks.png"));
        }

        // 三个卡牌标签共用一个面板；有未保存修改时切换标签不弹窗、不丢修改。
        var priority = Panel(view);
        Click(Tab(view, "授业换出卡"));
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
        var others = rows.Children.OfType<HifTileGrid>().Single(p => p.Name == "others");
        Check(configured.Count + others.Children.Count == catalogCount, $"Drinks not all listed: {configured.Count}+{others.Children.Count} of {catalogCount}");
        var drinkCount = configured.Count;
        var firstOther = others.Children.OfType<Border>().First().GetLogicalDescendants().OfType<ToggleSwitch>().Single(t => (string?)t.Tag == "单独设置");
        firstOther.IsChecked = true; Run();
        Check(configured.Count == drinkCount + 1 && drinks.HasUnsavedChanges, "Turning on 单独设置 did not add the drink");
        others = rows.Children.OfType<HifTileGrid>().Single(p => p.Name == "others");
        Check(configured.Count + others.Children.Count == catalogCount, "Added drink still listed under other drinks");
        var drinkTiles = others.Children.OfType<Border>().ToList();
        var drinksPerRow = drinkTiles.Count(tile => Math.Abs(tile.Bounds.Top - drinkTiles[0].Bounds.Top) < 1);
        Check(drinksPerRow >= 2 && others.Bounds.Width - drinkTiles[drinksPerRow - 1].Bounds.Right < 16, "Other drinks leave a gap on the right");
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
        var visible = library.Visible;
        Check(visible.Count > 0 && visible.Count < cards.LibraryCards().Count, $"Plan filter did not narrow the library: {visible.Count}");
        Check(visible.All(card => card.Plan is "" or "free" or "anomaly"), "全力 library shows other plans");
        // 筛选多选：稀有度选 SSR 与 SR、计划选不限，叠加「只看非凡与不限」开关；点「全部」恢复。
        var picker = (HifCardPicker)Field(library, "_picker");
        void Pick(string filter, string label) =>
            ((HifSegmented)Field(picker, filter)).Items.Single(b => (string?)b.Content == label).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Pick("_rarity", "SSR"); Pick("_rarity", "SR"); Run();
        var rare = library.Visible;
        Check(rare.Count > 0 && rare.All(card => card.Rarity is "SSR" or "SR") && rare.Any(card => card.Rarity == "SSR") && rare.Any(card => card.Rarity == "SR"),
            "Rarity multi-select did not keep SSR and SR");
        Pick("_plan", "不限"); Run();
        Check(library.Visible.Count > 0 && library.Visible.All(card => card.Plan is "" or "free" && card.Rarity is "SSR" or "SR"), "Plan filter did not combine with rarity");
        Pick("_plan", "感性"); Run();
        Check(library.Visible.All(card => card.Plan is "" or "free"), "Plan-only switch no longer limits 全力 to 非凡与不限");
        var planOnly = (ToggleSwitch)Field(picker, "_planOnly");
        planOnly.IsChecked = false; Run();
        Check(library.Visible.Any(card => card.Plan == "sense") && library.Visible.All(card => card.Plan is "sense" or "free" or ""), "Plan multi-select without the switch failed");
        planOnly.IsChecked = true;
        Pick("_rarity", "全部"); Pick("_plan", "全部"); Run();
        Check(((HifSegmented)Field(picker, "_rarity")).SelectedIndex == 0 && library.Visible.Count == visible.Count, "「全部」did not reset the filters");
        var pick = visible.First(card => !cards.InList(HifLibraryTarget.Priority, card.Name));
        library.Toggle(pick); Run();
        Check(cards.InList(HifLibraryTarget.Priority, pick.Name), "Library did not add to the priority list");
        // 多个名单的角标分开显示，卡库有图例。
        library.Open(HifLibraryTarget.SwapOut); library.Toggle(pick); library.Open(HifLibraryTarget.Priority); Run();
        Check(library.GetLogicalDescendants().OfType<TextBlock>().Any(t => (string?)t.Tag == "legend" && t.Text == HifCardLibraryView.Legend), "Library badge legend missing");
        var marks = library.GetLogicalDescendants().OfType<Border>().First(b => b.Tag is HifLibraryCard shown && shown.Name == pick.Name)
            .GetLogicalDescendants().OfType<TextBlock>().Select(t => t.Text).ToArray();
        Check(marks.Contains("出") && marks.Contains("换") && !marks.Contains("出换"), "Library badges are not shown separately: " + string.Join("|", marks));
        Snapshot(host, Path.Combine(root, "strategy-page-library.png"));
        library.Open(HifLibraryTarget.SwapOut); library.Toggle(pick); library.Open(HifLibraryTarget.Priority); Run();
        library.Toggle(pick); Run();
        Check(!cards.InList(HifLibraryTarget.Priority, pick.Name), "Library did not remove from the priority list");
        // 获取与定制：卡库只有一个目标。可定制的卡加入后「优先获取」默认关闭；没有定制项目的卡直接加入优先获取。
        library.Open(HifLibraryTarget.Acquisition); Run();
        visible = library.Visible;
        var customPick = visible.First(card => custom.Customizable(card.Id) && !custom.InList(card.Id) && !cards.InList(HifLibraryTarget.Acquisition, card.Name));
        var plainPick = visible.First(card => !custom.Customizable(card.Id) && !cards.InList(HifLibraryTarget.Acquisition, card.Name));
        library.Toggle(customPick); Run();
        Check(custom.InList(customPick.Id) && custom.HasUnsavedChanges && !cards.InList(HifLibraryTarget.Acquisition, customPick.Name),
            "Customizable card was not added with acquisition off");
        library.Toggle(plainPick); Run();
        Check(cards.InList(HifLibraryTarget.Acquisition, plainPick.Name), "Card without customization was not added to acquisition");
        Click(Tab(view, "获取与定制"));
        var mergedRows = (StackPanel)Field(custom, "_rows");
        HifTileGrid MergedGrid() => mergedRows.Children.OfType<HifTileGrid>().Single(g => g.Name == "cards");
        Border MergedTile(string name) => MergedGrid().Children.OfType<Border>().Single(b => (string?)b.Tag == name);
        ToggleSwitch Acquire(string name) => MergedTile(name).GetLogicalDescendants().OfType<ToggleSwitch>().Single(t => (string?)t.Tag == "优先获取");
        Check(Acquire(customPick.Name) is { IsChecked: false, IsEnabled: true } && MergedTile(customPick.Name).GetLogicalDescendants().OfType<NumericUpDown>().Any(),
            "Customizable card tile should show an off acquisition switch and customization counts");
        Check(Acquire(plainPick.Name) is { IsChecked: true, IsEnabled: false } && !MergedTile(plainPick.Name).GetLogicalDescendants().OfType<NumericUpDown>().Any()
            && MergedTile(plainPick.Name).GetLogicalDescendants().OfType<TextBlock>().Any(t => t.Text == "这张卡没有定制项目"),
            "Card without customization should show a note and a locked-on acquisition switch");
        Acquire(customPick.Name).IsChecked = true; Run();
        Check(cards.InList(HifLibraryTarget.Acquisition, customPick.Name) && custom.InList(customPick.Id), "Turning on 优先获取 did not add to acquisition");
        Acquire(customPick.Name).IsChecked = false; Run();
        Check(!cards.InList(HifLibraryTarget.Acquisition, customPick.Name) && custom.InList(customPick.Id) && Acquire(customPick.Name).IsChecked == false,
            "Turning off 优先获取 should keep the card on the tab");
        Check(((string?)Tab(view, "获取与定制").Content)!.EndsWith(" •"), "Acquire-and-customize tab not marked as changed");
        // × 同时移出两份名单；名单为空时才显示「打开卡库添加」，点击后卡库选好「获取与定制」。
        Check(!mergedRows.Children.OfType<Button>().Any(b => b.Content as string == "打开卡库添加"), "打开卡库添加 shown while the list has cards");
        foreach (var name in new[] { customPick.Name, plainPick.Name })
            Click(MergedTile(name).GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string == "×"));
        Check(!custom.InList(customPick.Id) && !cards.InList(HifLibraryTarget.Acquisition, customPick.Name) && !cards.InList(HifLibraryTarget.Acquisition, plainPick.Name),
            "× did not remove the card from both lists");
        // 之前的检查可能已在配置里留下这个职业的卡：全部移出后才是空名单（最后「放弃修改」恢复）。
        while (mergedRows.Children.OfType<HifTileGrid>().FirstOrDefault()?.Children.OfType<Border>().FirstOrDefault() is { } left)
            Click(left.GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string == "×"));
        Check(mergedRows.Children.OfType<Button>().Any(b => b.Content as string == "打开卡库添加"), "Empty acquire-and-customize list does not offer 打开卡库添加");
        Click(mergedRows.Children.OfType<Button>().Single(b => b.Content as string == "打开卡库添加"));
        Check((HifStrategyTab)Field(view, "_tab") == HifStrategyTab.Library && library.Target == HifLibraryTarget.Acquisition,
            "打开卡库添加 did not open the library for acquire-and-customize");

        // 获取与定制、授业换出卡：名单排成等宽卡片，窗口变宽时每行张数增加。
        int PerRow(string name)
        {
            var tiles = ((StackPanel)Field(name == "cards" ? custom : cards, "_rows")).Children.OfType<HifTileGrid>().Single(p => p.Name == name).Children.OfType<Border>().ToList();
            // 卡片等宽并铺满整行：最右一张贴近网格右边，不留大片空白。
            var gridWidth = ((Control)tiles[0].Parent!).Bounds.Width;
            var perRow = tiles.Count(tile => Math.Abs(tile.Bounds.Top - tiles[0].Bounds.Top) < 1);
            Check(tiles.Count < perRow || gridWidth - tiles[perRow - 1].Bounds.Right < 16, $"{name} tiles leave a gap on the right: {gridWidth - tiles[perRow - 1].Bounds.Right}");
            return perRow;
        }
        foreach (var (target, label, grid) in new[] { (HifLibraryTarget.Acquisition, "获取与定制", "cards"), (HifLibraryTarget.SwapOut, "授业换出卡", "swap-out") })
        {
            library.Open(target); Run();
            // 获取与定制各取几张可定制和不可定制的卡。
            var picks = target == HifLibraryTarget.Acquisition
                ? library.Visible.Where(card => custom.Customizable(card.Id)).Take(4).Concat(library.Visible.Where(card => !custom.Customizable(card.Id)).Take(2)).ToList()
                : library.Visible.Where(card => !cards.InList(target, card.Name)).Take(6).ToList();
            foreach (var card in picks) library.Toggle(card);
            Click(Tab(view, label));
            if (grid == "swap-out")
                Check(((StackPanel)Field(cards, "_rows")).GetLogicalDescendants().OfType<CheckBox>().All(c => c.IsChecked == true), "New swap-out switches are not on");
            var narrow = PerRow(grid);
            var shot = grid == "cards" ? "acquire-customize" : grid;
            Snapshot(host, Path.Combine(root, $"strategy-page-{shot}.png"));
            host.Width = 1500; Run();
            var wide = PerRow(grid);
            var widened = Wide();
            Snapshot(host, Path.Combine(root, $"strategy-page-{shot}-wide.png"));
            host.Width = 1100; Run();
            Check(narrow >= 2 && (widened ? wide > narrow : wide >= narrow), $"{label} cards do not reflow with the window width: {narrow} per row, then {wide}");
        }
        // 换出卡在卡图左侧标顺位，→ 后移一位。
        var swapList = ((Dictionary<string, List<Newtonsoft.Json.Linq.JObject>>)Field(cards, "_swapOutProfiles"))["全力"];
        var firstSwap = (string?)swapList[0]["name"];
        var firstTile = ((StackPanel)Field(cards, "_rows")).Children.OfType<HifTileGrid>().Single(p => p.Name == "swap-out").Children.OfType<Border>().First();
        Check(firstTile.GetLogicalDescendants().OfType<TextBlock>().Any(t => t.Text == "1"), "Swap-out tile does not show its order");
        Click(firstTile.GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string == "→"));
        Check((string?)swapList[1]["name"] == firstSwap, "Moving a swap-out card right did not change the order");

        // 出牌编排：出牌优先级详情跳到这张卡的编排；在内嵌选择区点卡加入后续卡，选饮料放进前后位置。
        // 全力的公开默认卡表为空，先从卡库加入一张卡。
        library.Open(HifLibraryTarget.Priority); Run();
        library.Toggle(pick); Run();
        Click(Tab(view, "出牌优先级"));
        var cardDetail = (StackPanel)Field(cards, "_detail");
        Click(cardDetail.GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string is "编辑出牌编排" or "为这张卡新建出牌编排"));
        Check((HifStrategyTab)Field(view, "_tab") == HifStrategyTab.Sequences && cards.SelectedSequence != null, "Detail did not open the card's sequence");
        var start = cards.SelectedSequence!;
        Check(((StackPanel)Field(cards, "_rows")).Children.OfType<Button>().Any(b => b.Content as string == "＋ 新建组合"), "Sequence tab missing 新建组合");
        cards.OpenSequencePick("candidate"); Run();
        var sequencePicker = (HifCardPicker)Field(cards, "_cardPicker");
        var followPick = sequencePicker.Visible.First(card => card.Name != start);
        Check(cards.PickCard(followPick), "Sequence follow-up was not added"); Run();
        var sequences = (System.Collections.IDictionary)Field(cards, "_sequences");
        Check(HifPlaySequenceConfig.Targets((Newtonsoft.Json.Linq.JObject)((Newtonsoft.Json.Linq.JObject)sequences["全力"]!)[start]!).Contains(followPick.Name),
            "Sequence follow-up missing from the edit data");
        cards.OpenSequencePick("drink", "after", followPick.Name); Run();
        Check(cards.ToggleSequenceDrink(start, "after", followPick.Name, "初星湯"), "Sequence drink was not added"); Run();
        Snapshot(host, Path.Combine(root, "strategy-page-sequences.png"));
        Click(Tab(view, "饮料"));
        Check(((StackPanel)Field(drinks, "_rows")).GetLogicalDescendants().OfType<TextBlock>().Any(t => (string?)t.Tag == "sequence-uses"),
            "Drinks tab does not mark drinks used by sequences");

        // 加卡统一在卡库：各标签不再有「导入卡牌」按钮。
        foreach (var label in new[] { "出牌优先级", "授业换出卡", "出牌编排", "获取与定制", "饮料" })
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
        Click(Tab(view, "出牌编排"));
        Check(((TextBlock)sections.Children[0]).Text!.StartsWith("出牌编排") && sections.Children.Count >= 8, "Help drawer did not follow the tab");
        // 打开说明抽屉时，出牌编排的左右两栏仍在可见宽度内，不被截掉。
        var sequenceBody = (Grid)Field(cards, "_body");
        Check(sequenceBody.ColumnDefinitions.Sum(c => c.ActualWidth) <= sequenceBody.Bounds.Width + 1,
            $"Sequence columns overflow with the help drawer open: {sequenceBody.ColumnDefinitions.Sum(c => c.ActualWidth)} > {sequenceBody.Bounds.Width}");
        Snapshot(host, Path.Combine(root, "strategy-page-sequences-help.png"));
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
        Console.WriteLine("Strategy page passed: six tabs incl. play sequences and acquire-and-customize, profession pills, shared card edits without prompts, all drinks, library add/remove, acquisition switch and customization counts on one tile, card grids reflow with width, sequence follow-up and drink picking with drink-panel links, no import or help buttons in panels, help drawer follows tabs, share menu, discard, reload, configuration unchanged.");
    }
}
