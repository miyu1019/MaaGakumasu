using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using MFAAvalonia.Helper;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>「卡库」：按职业筛选全部卡牌，点卡加入或移出出牌优先级、优先获取卡、授业换出卡或技能卡定制。</summary>
public sealed class HifCardLibraryView : UserControl
{
    private static readonly (HifLibraryTarget Target, string Label, string Badge)[] Targets =
    [
        (HifLibraryTarget.Priority, "出牌优先级", "出"),
        (HifLibraryTarget.Acquisition, "优先获取卡", "获"),
        (HifLibraryTarget.SwapOut, "授业换出卡", "换"),
        (HifLibraryTarget.Followups, "后续卡", "后"),
        (HifLibraryTarget.Customization, "技能卡定制", "定")
    ];
    private static readonly string[] Rarities = ["全部", "L", "SSR", "SR", "R", "N"];
    private static readonly string[] Types = ["全部", "A 卡", "M 卡", "T 卡"];
    private static readonly string[] Sources = ["全部", "偶像卡", "支援卡", "其他"];
    private static readonly Dictionary<int, Bitmap?> Icons = new();
    private const double TileWidth = 104;

    private readonly HifPriorityPanel _cards;
    private readonly HifCustomCardPanel _custom;
    private readonly Func<string> _profession;
    private readonly IReadOnlyList<HifLibraryCard> _all;
    private readonly HifSegmented _target = new(Targets.Select(item => item.Label));
    private readonly HifSegmented _rarity = new(Rarities);
    private readonly HifSegmented _type = new(Types);
    private readonly HifSegmented _source = new(Sources);
    private readonly ToggleSwitch _planOnly = new() { IsChecked = true, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _planLabel = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBox _search = new() { Watermark = "搜索卡名", Width = 200 };
    private readonly StackPanel _startRow = new() { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 6), IsVisible = false };
    private readonly ComboBox _start = new() { Width = 220 };
    private readonly TextBlock _startHint = new() { Text = "先在「后续卡」标签搜索并选择起始卡", VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity };
    private bool _syncingStart;
    private readonly TextBlock _count = new() { VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity };
    private readonly ItemsControl _rows = new();
    private readonly ScrollViewer _scroll = new() { HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
    private List<HifLibraryCard> _visible = new();
    private int _columns = 6;

    public HifLibraryTarget Target { get; private set; } = HifLibraryTarget.Priority;

    public static string HelpText =>
        "点卡加入\n先选要加入的名单：出牌优先级、优先获取卡、授业换出卡、后续卡或技能卡定制。点卡加入，再点一次移出；选「技能卡定制」时只显示有定制项目的卡。\n\n" +
        "后续卡\n选「后续卡」后再选起始卡，点卡按顺序加入它的后续卡；首次加入时按默认设置（等待 3 回合、正常出牌）新建组合，移空时删除组合。顺序与等待设置在「后续卡」标签调整；会形成循环的卡不能加入。新起始卡先在「后续卡」标签搜索选择。\n\n" +
        "默认筛选\n只显示当前职业的计划与不限计划：好调、集中为感性，好印象、元気为理性，全力、強気为非凡。关闭开关可查看全部卡牌。\n\n" +
        "排序\n同计划另一职业已在用的卡排在最前，其次按稀有度从高到低、A 卡在前。\n\n" +
        "标记\n卡图右上角标出这张卡已在当前职业的哪些名单：出（出牌优先级）、获（优先获取卡）、换（授业换出卡）、定（技能卡定制）。悬停可看稀有度、来源和识别模板情况。\n\n" +
        "保存\n卡库直接修改各标签的编辑数据，点页面顶部「保存」写入配置。";

    public HifCardLibraryView(HifPriorityPanel cards, HifCustomCardPanel custom, Func<string> profession)
    {
        _cards = cards;
        _custom = custom;
        _profession = profession;
        _all = cards.LibraryCards();

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,Auto,*"), RowSpacing = 10 };
        var targetRow = new WrapPanel { Orientation = Orientation.Horizontal };
        var targetLabel = new TextBlock { Text = "点卡加入", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 6) };
        targetRow.Children.Add(targetLabel);
        _target.Margin = new Thickness(0, 0, 14, 6);
        targetRow.Children.Add(_target);
        // 后续卡按起始卡加入：选择要编辑的起始卡。
        _startRow.Children.Add(new TextBlock { Text = "起始卡", VerticalAlignment = VerticalAlignment.Center });
        _startRow.Children.Add(_start);
        _startRow.Children.Add(_startHint);
        _start.SelectionChanged += (_, _) =>
        {
            if (_syncingStart || _start.SelectedItem is not string start) return;
            _cards.FollowupStart = start;
            Refresh();
        };
        targetRow.Children.Add(_startRow);
        root.Children.Add(targetRow);

        var filters = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var control in new Control[] { _search, Labeled("稀有度", _rarity), Labeled("类型", _type), Labeled("来源", _source) })
        {
            control.Margin = new Thickness(0, 0, 14, 6);
            filters.Children.Add(control);
        }
        var plan = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, Margin = new Thickness(0, 0, 14, 6) };
        plan.Children.Add(_planLabel);
        plan.Children.Add(_planOnly);
        filters.Children.Add(plan);
        Grid.SetRow(filters, 1);
        root.Children.Add(filters);

        Grid.SetRow(_count, 2);
        root.Children.Add(_count);

        // 只生成可见的行，几百张卡图也不会一次全部加载。
        _rows.ItemsPanel = new FuncTemplate<Panel?>(() => new VirtualizingStackPanel());
        // 回收容器时会以 null 调用模板。
        _rows.ItemTemplate = new FuncDataTemplate<List<HifLibraryCard>>((row, _) => row == null ? new Panel() : BuildRow(row));
        _scroll.Content = _rows;
        Grid.SetRow(_scroll, 3);
        root.Children.Add(_scroll);
        Content = root;

        _target.Select(0);
        _rarity.Select(0);
        _type.Select(0);
        _source.Select(0);
        _target.SelectionRequested += index => Open(Targets[index].Target);
        foreach (var segmented in new[] { _rarity, _type, _source })
            segmented.SelectionRequested += index => { segmented.Select(index); Refresh(); };
        _planOnly.IsCheckedChanged += (_, _) => Refresh();
        _search.TextChanged += (_, _) => Refresh();
        _scroll.SizeChanged += (_, _) =>
        {
            var columns = Math.Max(1, (int)((_scroll.Bounds.Width - 12) / (TileWidth + 8)));
            if (columns == _columns) return;
            _columns = columns;
            Layout();
        };
        Refresh();
    }

    private static StackPanel Labeled(string label, Control control)
    {
        var panel = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        panel.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
        panel.Children.Add(control);
        return panel;
    }

    /// <summary>切换要加入的名单，并刷新可选卡牌。</summary>
    public void Open(HifLibraryTarget target)
    {
        Target = target;
        _target.Select(Array.FindIndex(Targets, item => item.Target == target));
        Refresh();
    }

    private static string PlanOf(string profession) => profession switch
    {
        "好调" or "集中" => "sense",
        "好印象" or "元気" => "logic",
        _ => "anomaly"
    };

    private static string PlanName(string plan) => plan switch
    {
        "sense" => "感性", "logic" => "理性", "anomaly" => "非凡", "free" => "不限", _ => "—"
    };

    /// <summary>与当前职业同计划的另一个职业。</summary>
    private static string Sibling(string profession) => profession switch
    {
        "好调" => "集中", "集中" => "好调", "好印象" => "元気", "元気" => "好印象", "全力" => "強気", _ => "全力"
    };

    private bool Allowed(HifLibraryCard card) => Target == HifLibraryTarget.Customization
        ? _custom.Customizable(card.Id)
        : _cards.CanAdd(Target, card);

    private bool Contains(HifLibraryCard card, HifLibraryTarget target, string? profession = null) => target == HifLibraryTarget.Customization
        ? _custom.InList(card.Id, profession)
        : _cards.InList(target, card.Name, profession);

    /// <summary>按筛选条件重新列出卡牌：同计划另一职业在用的卡排在最前，其次按稀有度从高到低。</summary>
    public void Refresh()
    {
        var profession = _profession();
        var plan = PlanOf(profession);
        var sibling = Sibling(profession);
        _startRow.IsVisible = Target == HifLibraryTarget.Followups;
        if (_startRow.IsVisible)
        {
            _syncingStart = true;
            var starts = _cards.FollowupStarts();
            _start.ItemsSource = starts;
            _start.SelectedItem = _cards.FollowupStart;
            _start.IsVisible = starts.Count > 0;
            _startHint.IsVisible = starts.Count == 0;
            _syncingStart = false;
        }
        _planLabel.Text = $"只看{PlanName(plan)}与不限";
        var query = (_search.Text ?? "").Trim().TrimEnd('+');
        var rarity = Rarities[Math.Max(0, _rarity.SelectedIndex)];
        var type = _type.SelectedIndex switch { 1 => "active", 2 => "mental", 3 => "trouble", _ => "" };
        var source = _source.SelectedIndex;
        var allowed = _all.Where(Allowed).ToList();
        _visible = allowed
            .Where(card => query == "" || card.Name.Contains(query, StringComparison.OrdinalIgnoreCase))
            .Where(card => rarity == "全部" || card.Rarity == rarity || rarity == "L" && card.Rarity == "LR")
            .Where(card => type == "" || card.Type == type)
            .Where(card => source switch
            {
                1 => card.Source == "pIdol",
                2 => card.Source == "support",
                3 => card.Source is "default" or "produce" or "",
                _ => true
            })
            .Where(card => _planOnly.IsChecked != true || card.Plan is "" or "free" || card.Plan == plan)
            .OrderBy(card => Targets.Any(item => Contains(card, item.Target, sibling)) ? 0 : 1)
            .ThenBy(card => card.Rarity switch { "L" or "LR" => 0, "SSR" => 1, "SR" => 2, "R" => 3, "N" => 4, _ => 5 })
            .ThenBy(card => card.Type switch { "active" => 0, "mental" => 1, _ => 2 })
            .ThenBy(card => card.Id)
            .ToList();
        UpdateCount();
        Layout();
    }

    private void Layout()
    {
        var rows = new List<List<HifLibraryCard>>();
        for (var index = 0; index < _visible.Count; index += _columns)
            rows.Add(_visible.Skip(index).Take(_columns).ToList());
        _rows.ItemsSource = rows;
    }

    private Control BuildRow(List<HifLibraryCard> row)
    {
        var panel = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 8) };
        foreach (var card in row) panel.Children.Add(BuildTile(card));
        return panel;
    }

    private Control BuildTile(HifLibraryCard card)
    {
        var tile = new Border
        {
            Width = TileWidth, Padding = new Thickness(6), CornerRadius = new CornerRadius(10), BorderThickness = new Thickness(2),
            Cursor = new Cursor(StandardCursorType.Hand), Tag = card
        };
        Fill(tile, card);
        tile.PointerPressed += (_, e) =>
        {
            if (!e.GetCurrentPoint(tile).Properties.IsLeftButtonPressed) return;
            var changed = Toggle(card);
            Fill(tile, card);
            UpdateCount();
            if (!changed) _count.Text = _cards.Status;
        };
        return tile;
    }

    /// <summary>点卡：不在当前名单就加入，已在就移出。</summary>
    public bool Toggle(HifLibraryCard card)
    {
        var include = !Contains(card, Target);
        if (Target == HifLibraryTarget.Customization)
        {
            _custom.SetInList(card.Id, include);
            return true;
        }
        return _cards.SetInList(Target, card.Name, include);
    }

    private void UpdateCount()
    {
        var profession = _profession();
        var allowed = _all.Where(Allowed).ToList();
        var selected = allowed.Count(card => Contains(card, Target));
        var list = Target == HifLibraryTarget.Followups
            ? (_cards.FollowupStart is { } start ? $"「{start}」的后续卡" : "后续卡（未选起始卡）")
            : $"「{Targets.First(item => item.Target == Target).Label}」";
        _count.Text = $"显示 {_visible.Count} / {allowed.Count} 张 · {profession}{list}已有 {selected} 张 · 再点一次可移出";
    }

    private void Fill(Border tile, HifLibraryCard card)
    {
        var inTarget = Contains(card, Target);
        if (inTarget) tile[!Border.BorderBrushProperty] = new DynamicResourceExtension("SukiPrimaryColor");
        else tile.BorderBrush = Brushes.Transparent;
        tile[!Border.BackgroundProperty] = new DynamicResourceExtension(inTarget ? "SukiPrimaryColor5" : "SukiCardBackground");

        var body = new StackPanel { Spacing = 4 };
        var image = new Grid { Width = 72, Height = 72, HorizontalAlignment = HorizontalAlignment.Center };
        if (Icon(card.Id) is { } bitmap)
            image.Children.Add(new Image { Source = bitmap, Width = 72, Height = 72, Stretch = Stretch.Uniform });
        else
            image.Children.Add(new TextBlock { Text = "无图", HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity });
        // 右上角标出这张卡已在当前职业的哪些名单。
        var badges = string.Concat(Targets.Where(item => Contains(card, item.Target)).Select(item => item.Badge));
        if (badges != "")
        {
            var badge = new Border
            {
                CornerRadius = new CornerRadius(6), Padding = new Thickness(4, 1),
                HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top,
                Child = new TextBlock { Text = badges, FontSize = 11, Foreground = Brushes.White, FontWeight = FontWeight.SemiBold }
            };
            badge[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
            image.Children.Add(badge);
        }
        body.Children.Add(image);
        body.Children.Add(new TextBlock
        {
            Text = card.Name, FontSize = 11, TextTrimming = TextTrimming.CharacterEllipsis, HorizontalAlignment = HorizontalAlignment.Center,
            FontWeight = inTarget ? FontWeight.SemiBold : FontWeight.Normal
        });
        tile.Child = body;

        var detail = $"{card.Name}\n{(card.Rarity == "" ? "旧条目" : card.Rarity)} · {PlanName(card.Plan)} · " +
            (card.Type switch { "active" => "A 卡", "mental" => "M 卡", "trouble" => "T 卡", _ => "—" }) + " · " +
            (card.Source switch { "pIdol" => "偶像卡", "support" => "支援卡", "default" => "初始卡", "produce" => "培育卡", _ => "—" });
        if (Target == HifLibraryTarget.Priority) detail += "\n" + _cards.PriorityHint(card.Name);
        if (badges != "") detail += "\n已在：" + string.Join("、", Targets.Where(item => Contains(card, item.Target)).Select(item => item.Label));
        detail += inTarget ? "\n点击移出" : "\n点击加入";
        ToolTip.SetTip(tile, detail);
    }

    private static Bitmap? Icon(int id)
    {
        if (Icons.TryGetValue(id, out var cached)) return cached;
        Bitmap? bitmap = null;
        var path = Path.Combine(HifLayout.ImageRoot, "hif", "hif_card_icons", $"{id}.webp");
        try { if (File.Exists(path)) bitmap = new Bitmap(path); }
        catch (Exception error) when (error is IOException or NotSupportedException) { }
        Icons[id] = bitmap;
        return bitmap;
    }
}
