using Avalonia;
using Avalonia.Controls;
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
    Drinks,
    Customization
}

/// <summary>「HIF 策略」页：按职业编辑出牌优先级、优先获取卡、授业换出卡、饮料和技能卡定制。</summary>
public sealed class HifStrategyView : UserControl
{
    private static readonly (HifStrategyTab Tab, string Label, string Description)[] Tabs =
    [
        (HifStrategyTab.Priority, "出牌优先级", "战斗中按优先级出牌，并可设置条件优先级、使用限制和后续卡。"),
        (HifStrategyTab.Acquisition, "优先获取卡", "授业换入和支给领卡时优先选择的卡牌。"),
        (HifStrategyTab.SwapOut, "授业换出卡", "授业换卡时优先换出的卡牌。"),
        (HifStrategyTab.Drinks, "饮料", "饮料的领取顺序、购买范围和使用时机。"),
        (HifStrategyTab.Customization, "技能卡定制", "强化卡的定制项目与目标次数。")
    ];

    /// <summary>主窗口导航到本页；由主界面在加载时注册。</summary>
    public static Action? ShowPage { get; set; }

    public static HifStrategyView? Current { get; private set; }

    /// <summary>当前面板有未保存修改。更新安装前据此等待。</summary>
    public static bool HasUnsavedChanges => Current?._panel?.HasUnsavedChanges == true;

    private readonly HifSegmented _professions = new(HifStrategyPanel.Professions);
    private readonly HifSegmented _tabs = new(Tabs.Select(item => item.Label));
    private readonly TextBlock _description = new() { FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _unsaved = new() { Text = "● 未保存", VerticalAlignment = VerticalAlignment.Center, IsVisible = false };
    private readonly Button _save = new() { Content = "保存", Classes = { "Flat" }, MinWidth = 96, IsEnabled = false };
    private readonly ContentControl _host = new();
    private HifStrategyTab _tab = HifStrategyTab.Priority;
    private string _profession = "集中";
    private HifStrategyPanel? _panel;

    public HifStrategyView()
    {
        Current = this;
        _unsaved[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,Auto,*"), Margin = new Thickness(20, 16, 20, 16), RowSpacing = 12 };

        // 顶部：页面名称 · 职业 · 保存
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,Auto,*,Auto,Auto"), ColumnSpacing = 16 };
        header.Children.Add(new TextBlock { Text = "HIF 策略", FontSize = 22, FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        Grid.SetColumn(_professions, 1);
        header.Children.Add(_professions);
        Grid.SetColumn(_unsaved, 3);
        header.Children.Add(_unsaved);
        ToolTip.SetTip(_save, "保存当前面板的修改；下次开始培育时生效。");
        _save.Click += (_, _) => _panel?.TrySave();
        Grid.SetColumn(_save, 4);
        header.Children.Add(_save);
        root.Children.Add(header);

        var running = new Border
        {
            Padding = new Thickness(12, 8), CornerRadius = new CornerRadius(8),
            Child = new TextBlock { Text = "任务运行中，策略只读。停止任务后可编辑和保存；运行中的任务使用开始时已保存的策略。", TextWrapping = TextWrapping.Wrap }
        };
        running[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor15");
        running.Bind(IsVisibleProperty, new Binding("!Idle") { Source = Instances.RootViewModel });
        Grid.SetRow(running, 1);
        root.Children.Add(running);

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
        Grid.SetRow(card, 3);
        root.Children.Add(card);
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
        if (_panel?.HasUnsavedChanges == true) return;
        SetProfession(SafeProfession(HifLayout.ActiveTaskProfession()), false);
        ShowTab(_tab);
    }

    private async Task OpenAsync(string? profession, HifStrategyTab tab)
    {
        if (tab != _tab && !await ConfirmLeaveAsync()) return;
        if (profession != null) SetProfession(SafeProfession(profession), false);
        if (tab != _tab || _panel == null) ShowTab(tab);
    }

    private void SetProfession(string profession, bool byUser)
    {
        _profession = profession;
        _professions.Select(Array.IndexOf(HifStrategyPanel.Professions, profession));
        if (_panel == null || _panel.Profession == profession) return;
        _panel.Profession = profession;
        if (byUser) LoggerHelper.UserAction("HIF 策略切换职业", $"职业={profession}, 面板={_panel.Title}", operation: "HifStrategyPage");
    }

    private void ShowTab(HifStrategyTab tab)
    {
        _tab = tab;
        // 每次切换都重新读取配置；三个卡牌面板共用同一配置文件，离开前已保存或放弃修改。
        _panel = tab switch
        {
            HifStrategyTab.Acquisition => new HifPriorityPanel(_profession, true),
            HifStrategyTab.SwapOut => new HifPriorityPanel(_profession, false, true),
            HifStrategyTab.Drinks => new HifDrinkPanel(_profession),
            HifStrategyTab.Customization => new HifCustomCardPanel(_profession),
            _ => new HifPriorityPanel(_profession)
        };
        _panel.UnsavedChanged += (_, _) => UpdateSaveState();
        UpdateSaveState();
        _host.Content = _panel;
        var index = Array.FindIndex(Tabs, item => item.Tab == tab);
        _tabs.Select(index);
        _description.Text = Tabs[index].Description;
    }

    private void UpdateSaveState()
    {
        var dirty = _panel?.HasUnsavedChanges == true;
        _unsaved.IsVisible = dirty;
        _save.IsEnabled = dirty;
    }

    /// <summary>离开有未保存修改的面板前询问；返回 false 表示留在当前面板。</summary>
    private async Task<bool> ConfirmLeaveAsync()
    {
        if (_panel is not { HasUnsavedChanges: true } panel) return true;
        var owner = TopLevel.GetTopLevel(this) as Window ?? Instances.RootView;
        switch (await AskAsync(owner, panel.Title))
        {
            case LeaveChoice.Save:
                return panel.TrySave(); // 保存失败时原因已显示在面板状态栏。
            case LeaveChoice.Discard:
                LoggerHelper.UserAction("HIF 策略放弃未保存修改", $"面板={panel.Title}", operation: "HifStrategyPage");
                return true;
            default:
                return false;
        }
    }

    private enum LeaveChoice { Cancel, Save, Discard }

    private static async Task<LeaveChoice> AskAsync(Window owner, string title)
    {
        var result = LeaveChoice.Cancel;
        var dialog = new SukiWindow
        {
            Title = "有未保存的修改", Width = 440, Height = 220, CanResize = false,
            WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var body = new StackPanel { Spacing = 18, Margin = new Thickness(20) };
        body.Children.Add(new TextBlock { Text = $"「{title}」有未保存的修改。切换前要保存吗？", TextWrapping = TextWrapping.Wrap });
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, HorizontalAlignment = HorizontalAlignment.Right };
        foreach (var (text, choice, primary) in new[] { ("保存", LeaveChoice.Save, true), ("不保存", LeaveChoice.Discard, false), ("取消", LeaveChoice.Cancel, false) })
        {
            var button = new Button { Content = text, MinWidth = 80 };
            if (primary) button.Classes.Add("Flat");
            button.Click += (_, _) => { result = choice; dialog.Close(); };
            buttons.Children.Add(button);
        }
        body.Children.Add(buttons);
        dialog.Content = body;
        await dialog.ShowDialog(owner);
        return result;
    }

    private static string SafeProfession(string? profession) =>
        HifStrategyPanel.Professions.Contains(profession) ? profession! : "集中";
}
