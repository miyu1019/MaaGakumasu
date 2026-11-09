using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Data;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using MFAAvalonia.Helper;
using SukiUI.Controls;
using System;
using System.Linq;
using System.Threading.Tasks;

namespace MFAAvalonia.Views.Pages.Hif;

public enum HifStrategyTab
{
    Priority,
    Acquisition,
    SwapOut,
    Followups,
    Drinks,
    Customization,
    Library
}

/// <summary>「HIF 策略」页：按职业编辑出牌优先级、优先获取卡、授业换出卡、饮料和技能卡定制。</summary>
public sealed class HifStrategyView : UserControl
{
    private static readonly (HifStrategyTab Tab, string Label, string Description)[] Tabs =
    [
        (HifStrategyTab.Priority, "出牌优先级", "战斗中按优先级出牌，并可设置条件优先级和使用限制。"),
        (HifStrategyTab.Acquisition, "优先获取卡", "授业换入和支给领卡时优先选择的卡牌。"),
        (HifStrategyTab.SwapOut, "授业换出卡", "授业换卡时优先换出的卡牌。"),
        (HifStrategyTab.Followups, "后续卡", "起始卡打出后按顺序衔接的后续卡组合。"),
        (HifStrategyTab.Drinks, "饮料", "饮料的领取顺序、购买范围和使用时机。"),
        (HifStrategyTab.Customization, "技能卡定制", "强化卡的定制项目与目标次数。"),
        (HifStrategyTab.Library, "卡库", "按职业筛选全部卡牌，点卡加入或移出所选名单。")
    ];

    /// <summary>主窗口导航到本页；由主界面在加载时注册。</summary>
    public static Action? ShowPage { get; set; }

    public static HifStrategyView? Current { get; private set; }

    /// <summary>页面中任一面板有未保存修改。更新安装前据此等待。</summary>
    public static bool HasUnsavedChanges => Current?.Panels.Any(panel => panel.HasUnsavedChanges) == true;

    private readonly HifSegmented _professions = new(HifStrategyPanel.Professions);
    private readonly HifSegmented _tabs = new(Tabs.Select(item => item.Label));
    private readonly TextBlock _description = new() { FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _unsaved = new() { Text = "● 未保存", VerticalAlignment = VerticalAlignment.Center, IsVisible = false };
    private readonly Button _save = new() { Content = "保存", Classes = { "Flat" }, MinWidth = 96, IsEnabled = false };
    private readonly Button _discard = new() { Content = "放弃修改", Classes = { "Basic" }, IsVisible = false };
    private readonly Button _shareMenu = new() { Content = "职业配置 ▾", Classes = { "Basic" } };
    private readonly ToggleButton _helpToggle = new() { Content = "使用说明", Classes = { "Basic" } };
    private readonly Border _helpDrawer = new() { Width = 340, IsVisible = false, Padding = new Thickness(14), CornerRadius = new CornerRadius(12) };
    private readonly StackPanel _helpSections = new() { Spacing = 10 };
    private readonly ContentControl _host = new();
    private readonly Border _incompatibleNotice = new() { Padding = new Thickness(12, 8), CornerRadius = new CornerRadius(8), IsVisible = false };
    private readonly TextBlock _incompatibleText = new() { TextWrapping = TextWrapping.Wrap };
    private HifStrategyTab _tab = HifStrategyTab.Priority;
    private string _profession = "集中";
    private HifStrategyPanel? _panel;
    // 出牌优先级、优先获取卡、授业换出卡共用一个面板实例（同一配置文件）；各面板保留到保存或重新进入页面。
    private HifPriorityPanel? _cards;
    private HifDrinkPanel? _drinks;
    private HifCustomCardPanel? _custom;
    private HifCardLibraryView? _library;

    private System.Collections.Generic.IEnumerable<HifStrategyPanel> Panels =>
        new HifStrategyPanel?[] { _cards, _drinks, _custom }.OfType<HifStrategyPanel>();

    public HifStrategyView()
    {
        Current = this;
        _unsaved[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,Auto,*"), Margin = new Thickness(20, 16, 20, 16), RowSpacing = 12 };

        // 顶部：页面名称 · 职业 · 保存
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,Auto,*,Auto,Auto,Auto,Auto,Auto"), ColumnSpacing = 12 };
        header.Children.Add(new TextBlock { Text = "HIF 策略", FontSize = 22, FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        Grid.SetColumn(_professions, 1);
        header.Children.Add(_professions);
        // 职业配置的导入导出作用于当前职业的战斗卡表与规则，与具体标签无关，放在页面顶部。
        var export = new MenuItem { Header = "导出当前职业配置…" };
        export.Click += async (_, _) => { ShowTab(HifStrategyTab.Priority); await EnsureCards().ExportProfessionAsync(); };
        var import = new MenuItem { Header = "导入职业配置…" };
        import.Click += async (_, _) => { ShowTab(HifStrategyTab.Priority); await EnsureCards().ImportProfessionAsync(); };
        _shareMenu.Flyout = new MenuFlyout { Items = { export, import } };
        ToolTip.SetTip(_shareMenu, "导出当前职业的战斗配置分享给他人，或导入他人的配置；导入后核对再保存。");
        _shareMenu.Bind(IsEnabledProperty, new Binding("Idle") { Source = Instances.RootViewModel });
        Grid.SetColumn(_shareMenu, 3);
        header.Children.Add(_shareMenu);
        ToolTip.SetTip(_helpToggle, "在右侧显示当前标签的使用说明");
        _helpToggle.IsCheckedChanged += (_, _) => { _helpDrawer.IsVisible = _helpToggle.IsChecked == true; UpdateHelp(); };
        Grid.SetColumn(_helpToggle, 4);
        header.Children.Add(_helpToggle);
        Grid.SetColumn(_unsaved, 5);
        header.Children.Add(_unsaved);
        ToolTip.SetTip(_save, "保存所有面板的修改；下次开始培育时生效。");
        _save.Click += (_, _) => SaveAll();
        ToolTip.SetTip(_discard, "丢弃所有面板未保存的修改，重新读取已保存的配置。");
        _discard.Click += (_, _) => DiscardAll();
        Grid.SetColumn(_discard, 6);
        header.Children.Add(_discard);
        Grid.SetColumn(_save, 7);
        header.Children.Add(_save);
        root.Children.Add(header);

        var running = new Border
        {
            Padding = new Thickness(12, 8), CornerRadius = new CornerRadius(8),
            Child = new TextBlock { Text = "任务运行中，策略只读。停止任务后可编辑和保存；运行中的任务使用开始时已保存的策略。", TextWrapping = TextWrapping.Wrap }
        };
        running[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor15");
        running.Bind(IsVisibleProperty, new Binding("!Idle") { Source = Instances.RootViewModel });
        // 旧版本配置不迁移：读取时忽略并在此提示是哪些模块。
        _incompatibleNotice.Child = _incompatibleText;
        _incompatibleNotice[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor15");
        var notices = new StackPanel { Spacing = 8 };
        notices.Children.Add(running);
        notices.Children.Add(_incompatibleNotice);
        Grid.SetRow(notices, 1);
        root.Children.Add(notices);

        // 面板切换与说明
        var tabs = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 16 };
        tabs.Children.Add(_tabs);
        Grid.SetColumn(_description, 1);
        tabs.Children.Add(_description);
        Grid.SetRow(tabs, 2);
        root.Children.Add(tabs);

        var card = new GlassCard { Padding = new Thickness(16), Content = _host };
        _host.Bind(IsEnabledProperty, new Binding("Idle") { Source = Instances.RootViewModel });
        _save.Bind(IsVisibleProperty, new Binding("Idle") { Source = Instances.RootViewModel });
        // 使用说明抽屉：不弹窗，随标签切换内容，可与编辑同时查看。
        _helpDrawer[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiCardBackground");
        _helpDrawer.BorderBrush = HifTheme.LineBrush;
        _helpDrawer.BorderThickness = new Thickness(1);
        _helpDrawer.Child = new ScrollViewer { Content = _helpSections, VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto };
        var body = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 12 };
        body.Children.Add(card);
        Grid.SetColumn(_helpDrawer, 1);
        body.Children.Add(_helpDrawer);
        Grid.SetRow(body, 3);
        root.Children.Add(body);
        Content = root;

        _profession = SafeProfession(HifLayout.ActiveTaskProfession());
        _professions.Select(Array.IndexOf(HifStrategyPanel.Professions, _profession));
        _professions.SelectionRequested += index => SetProfession(HifStrategyPanel.Professions[index], true);
        _tabs.SelectionRequested += index => _ = OpenAsync(null, Tabs[index].Tab);
        ShowTab(_tab);
    }

    /// <summary>从任务设置跳转：切到指定职业与面板，并显示本页。</summary>
    public static void Navigate(string? profession, HifStrategyTab tab)
    {
        ShowPage?.Invoke();
        if (Current is { } view) _ = view.OpenAsync(profession, tab);
    }

    /// <summary>进入本页时：没有未保存修改就重新读取配置，并切到任务当前选择的职业。</summary>
    public void OnPageEntered()
    {
        if (HasUnsavedChanges) return;
        _cards = null; _drinks = null; _custom = null; _library = null;
        SetProfession(SafeProfession(HifLayout.ActiveTaskProfession()), false);
        ShowTab(_tab);
    }

    private Task OpenAsync(string? profession, HifStrategyTab tab)
    {
        if (profession != null) SetProfession(SafeProfession(profession), false);
        if (tab != _tab || _host.Content == null) ShowTab(tab);
        return Task.CompletedTask;
    }

    /// <summary>打开卡库并选好要加入的名单。</summary>
    public void OpenLibrary(HifLibraryTarget target)
    {
        ShowTab(HifStrategyTab.Library);
        _library!.Open(target);
    }

    private void SetProfession(string profession, bool byUser)
    {
        _profession = profession;
        _professions.Select(Array.IndexOf(HifStrategyPanel.Professions, profession));
        // 所有已打开的面板跟随页面职业，切换标签时看到的是同一职业。
        foreach (var panel in Panels.Where(panel => panel.Profession != profession)) panel.Profession = profession;
        _library?.Refresh();
        if (byUser) LoggerHelper.UserAction("HIF 策略切换职业", $"职业={profession}, 面板={_panel?.Title}", operation: "HifStrategyPage");
    }

    private void ShowTab(HifStrategyTab tab)
    {
        _tab = tab;
        // 三个面板随页面一起读取，顶部提示能覆盖所有模块的旧配置。
        EnsureCards();
        _drinks ??= Watch(new HifDrinkPanel(_profession));
        _custom ??= Watch(new HifCustomCardPanel(_profession));
        Control content;
        switch (tab)
        {
            case HifStrategyTab.Priority or HifStrategyTab.Acquisition or HifStrategyTab.SwapOut or HifStrategyTab.Followups:
                EnsureCards().SetMode(tab == HifStrategyTab.Acquisition, tab == HifStrategyTab.SwapOut, tab == HifStrategyTab.Followups);
                content = _panel = _cards!;
                break;
            case HifStrategyTab.Drinks:
                content = _panel = _drinks;
                break;
            case HifStrategyTab.Library:
                // 卡库直接修改卡牌面板和定制面板中的编辑数据，保存仍由顶部按钮统一完成。
                _library ??= new HifCardLibraryView(EnsureCards(), _custom, () => _profession);
                _library.Refresh();
                _panel = null;
                content = _library;
                break;
            default:
                content = _panel = _custom;
                break;
        }
        UpdateSaveState();
        _host.Content = content;
        var index = Array.FindIndex(Tabs, item => item.Tab == tab);
        _tabs.Select(index);
        _description.Text = Tabs[index].Description;
        UpdateHelp();
    }

    private HifPriorityPanel EnsureCards() => _cards ??= Watch(new HifPriorityPanel(_profession));

    /// <summary>说明抽屉显示当前标签的说明：每段第一行加粗为标题。</summary>
    private void UpdateHelp()
    {
        if (!_helpDrawer.IsVisible) return;
        _helpSections.Children.Clear();
        var label = Tabs[Array.FindIndex(Tabs, item => item.Tab == _tab)].Label;
        _helpSections.Children.Add(new TextBlock { Text = label + " · 使用说明", FontSize = 16, FontWeight = FontWeight.SemiBold });
        var text = _tab == HifStrategyTab.Library ? HifCardLibraryView.HelpText : _panel?.HelpText ?? "";
        foreach (var paragraph in text.Split("\n\n", StringSplitOptions.RemoveEmptyEntries))
        {
            var lines = paragraph.Split('\n', 2);
            var section = new StackPanel { Spacing = 3 };
            if (lines.Length == 2) section.Children.Add(new TextBlock { Text = lines[0], FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
            section.Children.Add(new TextBlock { Text = lines[^1], FontSize = 13, LineHeight = 20, TextWrapping = TextWrapping.Wrap, Opacity = 0.85 });
            _helpSections.Children.Add(section);
        }
    }

    private T Watch<T>(T panel) where T : HifStrategyPanel
    {
        panel.UnsavedChanged += (_, _) => UpdateSaveState();
        panel.LibraryRequested += OpenLibrary;
        return panel;
    }

    /// <summary>保存所有有修改的面板；某个面板保存失败时停在该面板，原因显示在其状态栏。</summary>
    private void SaveAll()
    {
        foreach (var panel in Panels.Where(panel => panel.HasUnsavedChanges).ToArray())
        {
            if (panel.TrySave()) continue;
            if (!ReferenceEquals(panel, _panel))
                ShowTab(panel == _drinks ? HifStrategyTab.Drinks : panel == _custom ? HifStrategyTab.Customization : _tab is HifStrategyTab.Acquisition or HifStrategyTab.SwapOut or HifStrategyTab.Followups ? _tab : HifStrategyTab.Priority);
            break;
        }
        UpdateSaveState();
    }

    /// <summary>丢弃全部未保存修改：重新读取配置并回到当前标签。</summary>
    public void DiscardAll()
    {
        LoggerHelper.UserAction("HIF 策略放弃未保存修改", $"职业={_profession}", operation: "HifStrategyPage");
        _cards = null; _drinks = null; _custom = null; _library = null;
        ShowTab(_tab);
    }

    private void UpdateSaveState()
    {
        var dirty = HasUnsavedChanges;
        _unsaved.IsVisible = dirty;
        _discard.IsVisible = dirty;
        _save.IsEnabled = dirty;
        var modules = Panels.SelectMany(panel => panel.Incompatible).Distinct().ToArray();
        _incompatibleNotice.IsVisible = modules.Length > 0;
        _incompatibleText.Text = $"以下模块的配置含旧版本格式的内容，已忽略：{string.Join("、", modules)}。在本页重新设置并保存后不再提示。";
        // 标签名后加圆点，提示哪些面板有未保存修改；三个卡牌标签共用一份数据，同时标记。
        for (var i = 0; i < Tabs.Length; i++)
        {
            var panel = Tabs[i].Tab switch
            {
                HifStrategyTab.Drinks => (HifStrategyPanel?)_drinks,
                HifStrategyTab.Customization => _custom,
                HifStrategyTab.Library => null,
                _ => _cards
            };
            _tabs.Items[i].Content = Tabs[i].Label + (panel?.HasUnsavedChanges == true ? " •" : "");
        }
    }

    private static string SafeProfession(string? profession) =>
        HifStrategyPanel.Professions.Contains(profession) ? profession! : "集中";
}
