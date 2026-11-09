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

/// <summary>
/// 卡牌选择网格：按卡名、稀有度、类型、来源与当前职业计划筛选，只生成可见行。
/// 「卡库」和「出牌编排」共用；点卡后的处理、卡片状态由使用方提供。
/// </summary>
public sealed class HifCardPicker : UserControl
{
    /// <summary>卡片显示状态：选中描边、不可点（变灰）、右上角标记和悬停说明。</summary>
    public sealed record TileState(bool Selected, bool Disabled, string Badge, string Tooltip);

    private static readonly string[] Rarities = ["全部", "L", "SSR", "SR", "R", "N"];
    private static readonly string[] Types = ["全部", "A 卡", "M 卡", "T 卡"];
    private static readonly string[] Sources = ["全部", "偶像卡", "支援卡", "其他"];
    private static readonly Dictionary<int, Bitmap?> Icons = new();
    private const double TileWidth = 104;

    private readonly IReadOnlyList<HifLibraryCard> _all;
    private readonly Func<string> _profession;
    private readonly HifSegmented _rarity = new(Rarities);
    private readonly HifSegmented _type = new(Types);
    private readonly HifSegmented _source = new(Sources);
    private readonly ToggleSwitch _planOnly = new() { IsChecked = true, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _planLabel = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBox _search = new() { Watermark = "搜索卡名", Width = 200 };
    private readonly TextBlock _count = new() { VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap };
    private readonly ItemsControl _rows = new();
    private readonly ScrollViewer _scroll = new() { HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
    private readonly HashSet<Border> _tiles = new();
    private List<HifLibraryCard> _visible = new();
    private int _allowedCount;
    private int _columns = 6;

    /// <summary>参与筛选的卡（例如只列可定制的卡）。</summary>
    public Func<HifLibraryCard, bool> Allowed { get; set; } = _ => true;

    /// <summary>排在最前的卡（例如同计划另一职业已在用）。</summary>
    public Func<HifLibraryCard, bool> Preferred { get; set; } = _ => false;

    public Func<HifLibraryCard, TileState> Describe { get; set; } = card => new TileState(false, false, "", card.Name);

    /// <summary>点卡；返回 false 表示未能修改，计数行改为显示 <see cref="Failure"/>。</summary>
    public Func<HifLibraryCard, bool>? Clicked { get; set; }

    public Func<string> Failure { get; set; } = () => "";

    /// <summary>计数行文字；参数为显示张数、可选张数。</summary>
    public Func<int, int, string> CountText { get; set; } = (visible, allowed) => $"显示 {visible} / {allowed} 张";

    public IReadOnlyList<HifLibraryCard> Visible => _visible;

    public HifCardPicker(IReadOnlyList<HifLibraryCard> all, Func<string> profession)
    {
        _all = all;
        _profession = profession;
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*"), RowSpacing = 10 };
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
        root.Children.Add(filters);
        Grid.SetRow(_count, 1);
        root.Children.Add(_count);
        // 只生成可见的行，几百张卡图也不会一次全部加载；回收容器时会以 null 调用模板。
        _rows.ItemsPanel = new FuncTemplate<Panel?>(() => new VirtualizingStackPanel());
        _rows.ItemTemplate = new FuncDataTemplate<List<HifLibraryCard>>((row, _) => row == null ? new Panel() : BuildRow(row));
        _scroll.Content = _rows;
        Grid.SetRow(_scroll, 2);
        root.Children.Add(_scroll);
        Content = root;

        _rarity.Select(0);
        _type.Select(0);
        _source.Select(0);
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
    }

    private static StackPanel Labeled(string label, Control control)
    {
        var panel = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        panel.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
        panel.Children.Add(control);
        return panel;
    }

    public static string PlanOf(string profession) => profession switch
    {
        "好调" or "集中" => "sense",
        "好印象" or "元気" => "logic",
        _ => "anomaly"
    };

    public static string PlanName(string plan) => plan switch
    {
        "sense" => "感性", "logic" => "理性", "anomaly" => "非凡", "free" => "不限", _ => "—"
    };

    /// <summary>卡牌的稀有度、计划、类型与来源说明，用于悬停提示。</summary>
    public static string CardDetail(HifLibraryCard card) =>
        $"{card.Name}\n{(card.Rarity == "" ? "旧条目" : card.Rarity)} · {PlanName(card.Plan)} · " +
        (card.Type switch { "active" => "A 卡", "mental" => "M 卡", "trouble" => "T 卡", _ => "—" }) + " · " +
        (card.Source switch { "pIdol" => "偶像卡", "support" => "支援卡", "default" => "初始卡", "produce" => "培育卡", _ => "—" });

    /// <summary>按筛选条件重新列出卡牌：优先的卡排在最前，其次按稀有度从高到低、A 卡在前。</summary>
    public void Refresh()
    {
        var plan = PlanOf(_profession());
        _planLabel.Text = $"只看{PlanName(plan)}与不限";
        var query = (_search.Text ?? "").Trim().TrimEnd('+');
        var rarity = Rarities[Math.Max(0, _rarity.SelectedIndex)];
        var type = _type.SelectedIndex switch { 1 => "active", 2 => "mental", 3 => "trouble", _ => "" };
        var source = _source.SelectedIndex;
        var allowed = _all.Where(Allowed).ToList();
        _allowedCount = allowed.Count;
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
            .OrderBy(card => Preferred(card) ? 0 : 1)
            .ThenBy(card => card.Rarity switch { "L" or "LR" => 0, "SSR" => 1, "SR" => 2, "R" => 3, "N" => 4, _ => 5 })
            .ThenBy(card => card.Type switch { "active" => 0, "mental" => 1, _ => 2 })
            .ThenBy(card => card.Id)
            .ToList();
        UpdateCount();
        Layout();
    }

    /// <summary>名单变化后刷新已显示的卡片状态，不重排、不回到顶部。</summary>
    public void RefreshTiles()
    {
        foreach (var tile in _tiles.ToArray()) Fill(tile, (HifLibraryCard)tile.Tag!);
        UpdateCount();
    }

    public void UpdateCount() => _count.Text = CountText(_visible.Count, _allowedCount);

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
        _tiles.Add(tile);
        tile.DetachedFromVisualTree += (_, _) => _tiles.Remove(tile);
        tile.AttachedToVisualTree += (_, _) => _tiles.Add(tile);
        Fill(tile, card);
        tile.PointerPressed += (_, e) =>
        {
            if (!e.GetCurrentPoint(tile).Properties.IsLeftButtonPressed || Clicked == null) return;
            var changed = Clicked(card);
            RefreshTiles();
            if (!changed) _count.Text = Failure();
        };
        return tile;
    }

    private void Fill(Border tile, HifLibraryCard card)
    {
        var state = Describe(card);
        if (state.Selected) tile[!Border.BorderBrushProperty] = new DynamicResourceExtension("SukiPrimaryColor");
        else tile.BorderBrush = Brushes.Transparent;
        tile[!Border.BackgroundProperty] = new DynamicResourceExtension(state.Selected ? "SukiPrimaryColor5" : "SukiCardBackground");
        tile.Opacity = state.Disabled ? 0.4 : 1;
        tile.Cursor = new Cursor(state.Disabled ? StandardCursorType.No : StandardCursorType.Hand);

        var body = new StackPanel { Spacing = 4 };
        var image = new Grid { Width = 72, Height = 72, HorizontalAlignment = HorizontalAlignment.Center };
        if (Icon(card.Id) is { } bitmap)
            image.Children.Add(new Image { Source = bitmap, Width = 72, Height = 72, Stretch = Stretch.Uniform });
        else
            image.Children.Add(new TextBlock { Text = "无图", HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity });
        if (state.Badge != "")
        {
            // 多个标记用空格分开，各自一个小块，避免连成一个词。
            var badges = new StackPanel
            {
                Orientation = Orientation.Horizontal, Spacing = 2, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top
            };
            foreach (var mark in state.Badge.Split(' ', StringSplitOptions.RemoveEmptyEntries))
            {
                var badge = new Border
                {
                    CornerRadius = new CornerRadius(6), Padding = new Thickness(4, 1),
                    Child = new TextBlock { Text = mark, FontSize = 11, Foreground = Brushes.White, FontWeight = FontWeight.SemiBold }
                };
                badge[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                badges.Children.Add(badge);
            }
            image.Children.Add(badges);
        }
        body.Children.Add(image);
        body.Children.Add(new TextBlock
        {
            Text = card.Name, FontSize = 11, TextTrimming = TextTrimming.CharacterEllipsis, HorizontalAlignment = HorizontalAlignment.Center,
            FontWeight = state.Selected ? FontWeight.SemiBold : FontWeight.Normal
        });
        tile.Child = body;
        ToolTip.SetTip(tile, state.Tooltip);
    }

    /// <summary>卡图按 ID 缓存，卡库与出牌编排共用。</summary>
    public static Bitmap? Icon(int id)
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
