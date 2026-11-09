using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>
/// 「出牌编排」标签：以起始卡为单位，设置起始卡与后续候选的前置、后置饮料，组合的等待用饮与等待设置。
/// 与出牌优先级同属 cards_priority.json，由页面顶部「保存」统一写入。
/// </summary>
public sealed partial class HifPriorityPanel
{
    private enum PickKind { NewStart, ChangeStart, Candidate, Drink }

    /// <summary>右侧打开的选择区：选起始卡、更换起始卡、加后续卡，或为某个位置选饮料。</summary>
    private sealed record SequencePick(PickKind Kind, string? Slot = null, string? Target = null);

    private static readonly Dictionary<int, Bitmap?> DrinkIcons = new();
    private static readonly string[] PolicyHints =
    [
        "后续卡也按普通优先级单独出牌。",
        "首次组合完成前，后续卡留给衔接，不单独出牌。",
        "后续卡只用于衔接，完成组合或等待超时后也不单独出牌。"
    ];

    private readonly Dictionary<string, JObject> _sequences = new();
    private readonly List<JObject> _drinkCatalog = new();
    private readonly ContentControl _sequenceChain = new();
    private readonly ContentControl _sequenceSettings = new();
    private readonly ContentControl _drinkPicker = new();
    private bool _sequenceMode;
    private string? _selectedSequence;
    private SequencePick? _pick;
    private HifCardPicker? _cardPicker;

    /// <summary>饮料在该职业按当前设置不会被喝时的说明（来自饮料面板的编辑数据）；参数为职业、饮料名。</summary>
    public Func<string, string, string?>? DrinkHint { get; set; }

    /// <summary>出牌优先级详情中点「编辑出牌编排」；页面切到「出牌编排」标签。</summary>
    public event Action<string>? SequenceRequested;

    /// <summary>当前选中的编排（起始卡）。</summary>
    public string? SelectedSequence => _selectedSequence;

    private const string SequenceHelp =
        "出牌编排\n以起始卡为单位。起始卡被选中出牌时：先喝它的前置饮料，重新找到这张卡打出，仍在同一回合时再喝后置饮料。没有后续卡的编排只给这张卡前后用饮。\n\n" +
        "新建组合\n点「新建组合」后点卡选择起始卡，接着点卡按顺序加入后续卡；已有编排的卡会直接打开它的编排。点起始卡或「更换起始卡」可换成别的卡。\n\n" +
        "后续候选\n起始卡打出后，按顺序衔接第一张在手牌且符合使用限制的候选，打完这一张组合就完成，不会依次打出全部候选。衔接时用该候选自己的前置、后置饮料；普通出牌不使用这些位置。\n\n" +
        "饮料\n每个位置可放多瓶，按从左到右的顺序各喝一瓶。饮料栏里没有、已达到一战单种或总上限，或该场关闭的饮料直接跳过，照常出牌，不补喝。全部 28 种饮料都能选；按饮料面板设置不会被喝的会在旁边提示。整栏确认没有某种饮料后，本场编排不再为它扫描饮料栏。\n\n" +
        "后置饮料\n只在打出后仍是同一回合、还在战斗页时才喝；回合已结束（通常每回合只能出一张卡）就跳过。\n\n" +
        "等待回合与等待用饮\n起始卡打出后，候选都不在手牌时最多空过几回合等待，0 表示不空过；同手有候选时仍会衔接。准备空过等待前先喝等待用饮，每次衔接一轮；用饮后出现选卡页时按后续候选顺序选卡。\n\n" +
        "再次等待\n完成一次组合后，再遇到起始卡时是否继续等待。\n\n" +
        "后续卡出牌\n可单独出：后续卡也按普通优先级单独出牌。首次组合前保留：首次组合完成前留给衔接。只用于衔接：始终不单独出牌。\n\n" +
        "饮料的普通时机\n编排用到的饮料仍按饮料面板的时机使用；只想在编排里喝时，把它的时机设为「只在出牌编排中使用」。\n\n" +
        "旧版后续卡\n旧版后续卡组合、初星黒酢组合开关和特製ハツボシエキス关联卡牌不再读取，需要在这里重新设置。";

    private void LoadDrinkCatalog()
    {
        _drinkCatalog.Clear();
        var catalog = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_drink_catalog.json"), Encoding.UTF8));
        _drinkCatalog.AddRange((catalog["drinks"] as JArray ?? throw new InvalidOperationException("缺少饮料目录")).OfType<JObject>());
    }

    /// <summary>读取各职业的出牌编排；旧版后续卡不迁移，无法识别的编排忽略并提示。</summary>
    private void LoadSequences()
    {
        if (_config["followup_profiles"] != null) NoteIncompatible("后续卡");
        foreach (var name in Professions)
        {
            var rules = _config[HifPlaySequenceConfig.ConfigKey]?[name] is JObject saved ? (JObject)saved.DeepClone() : new JObject();
            try
            {
                HifPlaySequenceConfig.Validate(rules, _catalog);
                foreach (var rule in rules.Properties()) HifPlaySequenceConfig.Normalize((JObject)rule.Value);
            }
            catch (InvalidOperationException)
            {
                NoteIncompatible("出牌编排");
                rules = new JObject();
            }
            _sequences[name] = rules;
        }
    }

    /// <summary>保存前：去掉没有内容的编排并完整校验。</summary>
    private JObject SequencesForSave(string profession)
    {
        var rules = _sequences[profession];
        foreach (var empty in rules.Properties().Where(rule => HifPlaySequenceConfig.IsEmpty((JObject)rule.Value)).Select(rule => rule.Name).ToArray())
            rules.Remove(empty);
        foreach (var rule in rules.Properties()) HifPlaySequenceConfig.Normalize((JObject)rule.Value);
        HifPlaySequenceConfig.Validate(rules, _catalog);
        return rules;
    }

    /// <summary>饮料面板用：该职业各饮料被出牌编排引用的次数。</summary>
    public IReadOnlyDictionary<string, int> SequenceDrinkUses(string profession) => !_loaded
        ? new Dictionary<string, int>()
        : _sequences[profession].Properties().SelectMany(rule => HifPlaySequenceConfig.DrinkUses((JObject)rule.Value))
            .GroupBy(name => name).ToDictionary(group => group.Key, group => group.Count());

    /// <summary>出牌编排的摘要：后续卡、等待设置与用饮数。</summary>
    private string SequenceSummary(string profession, string key)
    {
        if (_sequences[profession][key] is not JObject rule) return "未设置";
        var targets = HifPlaySequenceConfig.Targets(rule);
        var parts = new List<string>();
        if (targets.Count > 0)
        {
            parts.Add("→ " + string.Join("、", targets.Take(2)) + (targets.Count > 2 ? $" 等 {targets.Count} 张" : ""));
            parts.Add($"等 {(int?)rule["wait_turns"] ?? 0} 回合");
            parts.Add(HifPlaySequenceConfig.PolicyLabels[Math.Max(0, Array.IndexOf(HifPlaySequenceConfig.Policies, (string?)rule["target_policy"]))]);
        }
        var drinks = HifPlaySequenceConfig.DrinkUses(rule).Count();
        if (drinks > 0) parts.Add($"用饮 {drinks} 处");
        return parts.Count == 0 ? "未设置，保存时去掉" : string.Join(" · ", parts);
    }

    /// <summary>出牌优先级详情：编辑或新建这张卡的出牌编排，并切到「出牌编排」标签。</summary>
    private void RequestSequence(string key)
    {
        var rules = _sequences[_profession];
        if (rules[key] == null)
        {
            rules[key] = HifPlaySequenceConfig.NewRule();
            MarkChanged();
        }
        _selectedSequence = key;
        _pick = null;
        SequenceRequested?.Invoke(key);
    }

    private IEnumerable<string> SequenceKeys(string profession)
    {
        var cards = _profiles[profession];
        return _sequences[profession].Properties().Select(rule => rule.Name)
            .OrderBy(name => cards.FindIndex(card => (string?)card["key"] == name) is var index && index >= 0 ? index : int.MaxValue)
            .ThenBy(name => name, StringComparer.Ordinal);
    }

    private void RenderSequences()
    {
        RenderSequenceList();
        RenderSequenceDetail();
    }

    private void OpenPick(SequencePick? pick)
    {
        _pick = pick;
        RenderCards();
    }

    /// <summary>左侧：「新建组合」与已有编排。</summary>
    private void RenderSequenceList()
    {
        _rows.Children.Clear();
        var profession = _profession;
        var create = new Button
        {
            Content = "＋ 新建组合", Classes = { "Flat" }, HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Center, Margin = new Thickness(2, 0, 2, 6)
        };
        ToolTip.SetTip(create, "点卡选择起始卡；之后添加后续卡，或只给这张卡设置前置、后置饮料");
        create.Click += (_, _) => OpenPick(new SequencePick(PickKind.NewStart));
        _rows.Children.Add(create);
        var keys = SequenceKeys(profession).ToList();
        if (_selectedSequence != null && !keys.Contains(_selectedSequence)) _selectedSequence = null;
        if (_selectedSequence == null && _pick?.Kind != PickKind.NewStart) _selectedSequence = keys.FirstOrDefault();
        if (keys.Count == 0)
            _rows.Children.Add(new TextBlock
            {
                Text = "此职业还没有出牌编排。点「新建组合」选择起始卡。",
                Opacity = HifTheme.SecondaryOpacity, Margin = new Thickness(6, 8), TextWrapping = TextWrapping.Wrap
            });
        foreach (var key in keys)
        {
            var rule = (JObject)_sequences[profession][key]!;
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 8 };
            if (CardImage(key) is { } image) { image.Width = 44; image.Height = 44; row.Children.Add(image); }
            var text = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            text.Children.Add(new TextBlock { Text = key, TextWrapping = TextWrapping.Wrap });
            var targets = HifPlaySequenceConfig.Targets(rule);
            if (targets.Count > 0)
            {
                var chain = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 3 };
                chain.Children.Add(new TextBlock { Text = "→", FontSize = 11, Opacity = HifTheme.SecondaryOpacity, VerticalAlignment = VerticalAlignment.Center });
                foreach (var target in targets.Take(4))
                    if (HifCardPicker.Icon((int?)_images[target] ?? 0) is { } bitmap)
                        chain.Children.Add(new Image { Source = bitmap, Width = 22, Height = 22, Stretch = Stretch.Uniform });
                text.Children.Add(chain);
            }
            var summary = new TextBlock { Text = SequenceSummary(profession, key), FontSize = 11, TextWrapping = TextWrapping.Wrap };
            summary[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
            text.Children.Add(summary);
            Grid.SetColumn(text, 1);
            row.Children.Add(text);
            var selected = key == _selectedSequence && _pick?.Kind != PickKind.NewStart;
            var border = new Border
            {
                Child = row, Padding = new Thickness(6, 4), CornerRadius = new CornerRadius(10), Tag = key,
                BorderThickness = new Thickness(selected ? 2 : 1), Cursor = new Cursor(StandardCursorType.Hand)
            };
            border[!Border.BorderBrushProperty] = new DynamicResourceExtension(selected ? "SukiPrimaryColor" : "SukiLightBorderBrush");
            if (selected) border[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor5");
            border.PointerPressed += (_, _) =>
            {
                if (selected) return;
                _selectedSequence = key;
                _pick = null;
                Dispatcher.UIThread.Post(RenderCards);
            };
            _rows.Children.Add(border);
        }
    }

    /// <summary>右侧：起始卡、出牌顺序（卡与饮料）、选择区、组合设置与删除。</summary>
    private void RenderSequenceDetail()
    {
        _detail.Children.Clear();
        var profession = _profession;
        if (_pick?.Kind == PickKind.NewStart)
        {
            _detail.Children.Add(SectionTitle("新建组合", "点卡选择起始卡；已有编排的卡（标「编」）会直接打开它的编排。"));
            _detail.Children.Add(PickerBox("选择起始卡", CardPicker()));
            return;
        }
        if (_selectedSequence is not { } key || _sequences[profession][key] is not JObject rule)
        {
            _detail.Children.Add(new TextBlock { Text = "在左侧选择一条出牌编排，或点「新建组合」。", Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
            return;
        }
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"), ColumnSpacing = 12 };
        if (CardImage(key) is { } image) header.Children.Add(image);
        var title = new StackPanel { Spacing = 3, VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(new TextBlock { Text = key, FontSize = 16, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
        var index = _profiles[profession].FindIndex(card => (string?)card["key"] == key);
        var targets = HifPlaySequenceConfig.Targets(rule);
        title.Children.Add(new TextBlock
        {
            Text = (targets.Count > 0 ? $"组合 · {targets.Count} 张后续候选" : "单卡用饮 · 没有后续卡") + " · " +
                   (index >= 0 ? $"出牌卡表第 {index + 1} 位" : "不在出牌卡表中，按兜底优先级出牌"),
            FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap
        });
        Grid.SetColumn(title, 1);
        header.Children.Add(title);
        var change = new Button { Content = "更换起始卡", Classes = { "Basic" }, VerticalAlignment = VerticalAlignment.Center };
        change.Click += (_, _) => OpenPick(new SequencePick(PickKind.ChangeStart));
        Grid.SetColumn(change, 2);
        header.Children.Add(change);
        _detail.Children.Add(header);

        _sequenceChain.Content = BuildChain(key, rule);
        _detail.Children.Add(_sequenceChain);
        if (_pick != null)
            _detail.Children.Add(PickerBox(PickTitle(key), _pick.Kind == PickKind.Drink ? DrinkPicker(rule) : CardPicker()));
        _sequenceSettings.Content = BuildSequenceSettings(rule);
        _detail.Children.Add(_sequenceSettings);

        var remove = new Button { Content = "删除编排", Classes = { "Basic" }, HorizontalAlignment = HorizontalAlignment.Left };
        ToolTip.SetTip(remove, "删除这张起始卡的后续卡、饮料和等待设置");
        remove.Click += (_, _) =>
        {
            _sequences[profession].Remove(key);
            _selectedSequence = null;
            _pick = null;
            MarkChanged();
            RenderCards();
            LoggerHelper.UserAction("删除 HIF 出牌编排", $"职业={profession}, 起始卡={key}", operation: "HifPriorityEditor");
        };
        _detail.Children.Add(remove);
    }

    /// <summary>编辑后刷新左侧列表与右侧出牌顺序；选择区保持原状（不重置筛选和滚动）。</summary>
    private void RefreshSequence(bool settings = true)
    {
        RenderSequenceList();
        if (_selectedSequence is not { } key || _sequences[_profession][key] is not JObject rule) return;
        _sequenceChain.Content = BuildChain(key, rule);
        if (settings) _sequenceSettings.Content = BuildSequenceSettings(rule);
        if (_pick?.Kind == PickKind.Drink) _drinkPicker.Content = DrinkTiles(rule);
    }

    private static StackPanel SectionTitle(string title, string hint)
    {
        var panel = new StackPanel { Spacing = 2 };
        panel.Children.Add(new TextBlock { Text = title, FontWeight = FontWeight.SemiBold });
        panel.Children.Add(new TextBlock { Text = hint, FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
        return panel;
    }

    private static Border Section(Control child) => new()
    {
        Child = child, Padding = new Thickness(12, 10), CornerRadius = new CornerRadius(8),
        BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1)
    };

    private string SlotName(string slot) => slot switch { "before" => "前置", "after" => "后置", _ => "等待" };

    private string PickTitle(string start) => _pick switch
    {
        { Kind: PickKind.ChangeStart } => $"更换「{start}」的起始卡 · 已有编排的卡和会形成循环的卡不能选",
        { Kind: PickKind.Candidate } => $"为「{start}」选择后续卡 · 点卡按顺序加入，再点一次移出",
        { Kind: PickKind.Drink, Slot: "waiting" } => $"「{start}」的等待用饮 · 后续卡都不在手牌、准备空过等待前喝；按点选顺序，再点一次移出",
        { Kind: PickKind.Drink } p => $"「{p.Target ?? start}」的{SlotName(p.Slot!)}饮料 · 按点选顺序喝，再点一次移出",
        _ => ""
    };

    /// <summary>选择区外框：标题与「完成」。</summary>
    private Border PickerBox(string title, Control content)
    {
        var body = new StackPanel { Spacing = 8 };
        var head = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 8 };
        head.Children.Add(new TextBlock { Text = title, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center });
        var done = new Button { Content = "完成", Classes = { "Flat" } };
        done.Click += (_, _) => OpenPick(null);
        Grid.SetColumn(done, 1);
        head.Children.Add(done);
        body.Children.Add(head);
        if (content.Parent is Decorator decorator) decorator.Child = null;
        else if (content.Parent is Panel panel) panel.Children.Remove(content);
        body.Children.Add(content);
        var box = Section(body);
        box[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor5");
        return box;
    }

    /// <summary>出牌顺序：前置 → 起始卡 → 后置，下方为后续候选（各自的前置 → 候选 → 后置）。</summary>
    private Control BuildChain(string start, JObject rule)
    {
        var body = new StackPanel { Spacing = 10 };
        body.Children.Add(SectionTitle("出牌顺序",
            "饮料按从左到右的顺序各喝一瓶；饮料栏里没有、已达上限或该场关闭的饮料跳过，照常出牌。后置饮料只在打出后仍是同一回合时喝。"));
        body.Children.Add(ChainRow(DrinkSlot(rule, "before", null), CardNode(start, true), DrinkSlot(rule, "after", null)));
        var targets = HifPlaySequenceConfig.Targets(rule);
        if (targets.Count > 0)
        {
            body.Children.Add(new TextBlock
            {
                Text = "后续候选 · 起始卡打出后按顺序衔接第一张可用的，打完这一张组合完成",
                FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap
            });
            for (var index = 0; index < targets.Count; index++)
            {
                var target = targets[index];
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("24,*,Auto"), ColumnSpacing = 6 };
                row.Children.Add(new TextBlock { Text = $"{index + 1}", Opacity = HifTheme.SecondaryOpacity, VerticalAlignment = VerticalAlignment.Center });
                var chain = ChainRow(DrinkSlot(rule, "before", target), CardNode(target, false), DrinkSlot(rule, "after", target));
                Grid.SetColumn(chain, 1);
                row.Children.Add(chain);
                var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
                var from = index;
                foreach (var (text, delta, tip) in new[] { ("↑", -1, "上移"), ("↓", 1, "下移") })
                {
                    var to = from + delta;
                    var move = new Button { Content = text, Classes = { "Basic" }, Padding = new Thickness(8, 2), IsEnabled = to >= 0 && to < targets.Count };
                    ToolTip.SetTip(move, tip);
                    move.Click += (_, _) =>
                    {
                        var array = (JArray)rule["targets"]!;
                        (array[from], array[to]) = (array[to].DeepClone(), array[from].DeepClone());
                        SequenceChanged(rule, false);
                    };
                    actions.Children.Add(move);
                }
                var remove = new Button { Content = "×", Classes = { "Basic" }, Padding = new Thickness(8, 2) };
                ToolTip.SetTip(remove, "移出这张后续卡及它的饮料");
                remove.Click += (_, _) =>
                {
                    ((JArray)rule["targets"]!).FirstOrDefault(item => (string?)item == target)?.Remove();
                    SequenceChanged(rule, true);
                };
                actions.Children.Add(remove);
                Grid.SetColumn(actions, 2);
                row.Children.Add(actions);
                body.Children.Add(row);
            }
        }
        var add = new Button { Content = "＋ 添加后续卡", Classes = { "Basic" }, HorizontalAlignment = HorizontalAlignment.Left };
        ToolTip.SetTip(add, "在下方选择区点卡，按顺序加入后续候选");
        add.Click += (_, _) => OpenPick(new SequencePick(PickKind.Candidate));
        body.Children.Add(add);
        return Section(body);
    }

    private void SequenceChanged(JObject rule, bool settings)
    {
        HifPlaySequenceConfig.Normalize(rule);
        MarkChanged();
        Dispatcher.UIThread.Post(() =>
        {
            RefreshSequence(settings);
            _cardPicker?.RefreshTiles();
        });
    }

    private static WrapPanel ChainRow(Control before, Control card, Control after)
    {
        var row = new WrapPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        foreach (var item in new[] { before, Arrow(), card, Arrow(), after })
        {
            item.Margin = new Thickness(0, 0, 4, 4);
            item.VerticalAlignment = VerticalAlignment.Center;
            row.Children.Add(item);
        }
        return row;
    }

    private static TextBlock Arrow() => new() { Text = "→", Opacity = HifTheme.SecondaryOpacity, FontSize = 16 };

    private Control CardNode(string name, bool start)
    {
        var body = new StackPanel { Spacing = 2, HorizontalAlignment = HorizontalAlignment.Center };
        if (HifCardPicker.Icon((int?)_images[name] ?? 0) is { } bitmap)
            body.Children.Add(new Image { Source = bitmap, Width = 52, Height = 52, Stretch = Stretch.Uniform, HorizontalAlignment = HorizontalAlignment.Center });
        body.Children.Add(new TextBlock { Text = name, FontSize = 11, TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 84, HorizontalAlignment = HorizontalAlignment.Center });
        var node = new Border
        {
            Child = body, Width = 92, Padding = new Thickness(4), CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(start ? 2 : 1)
        };
        node[!Border.BorderBrushProperty] = new DynamicResourceExtension(start ? "SukiPrimaryColor" : "SukiLightBorderBrush");
        ToolTip.SetTip(node, start ? $"起始卡「{name}」· 点击更换" : $"后续卡「{name}」");
        if (start)
        {
            node.Cursor = new Cursor(StandardCursorType.Hand);
            node.PointerPressed += (_, _) => Dispatcher.UIThread.Post(() => OpenPick(new SequencePick(PickKind.ChangeStart)));
        }
        return node;
    }

    private int DrinkId(string name) => (int?)_drinkCatalog.FirstOrDefault(drink => (string?)drink["name"] == name)?["id"] ?? 0;

    private static Bitmap? DrinkIcon(int id)
    {
        if (DrinkIcons.TryGetValue(id, out var cached)) return cached;
        Bitmap? bitmap = null;
        var path = Path.Combine(HifLayout.ImageRoot, "hif", "hif_drink_icons", $"{id}.webp");
        try { if (File.Exists(path)) bitmap = new Bitmap(path); }
        catch (Exception error) when (error is IOException or NotSupportedException) { }
        DrinkIcons[id] = bitmap;
        return bitmap;
    }

    /// <summary>一个饮料位置：已选饮料（点一下移出）和「＋」打开饮料选择区。</summary>
    private Control DrinkSlot(JObject rule, string slot, string? target)
    {
        var panel = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 3 };
        panel.Children.Add(new TextBlock { Text = SlotName(slot), FontSize = 11, Opacity = HifTheme.SecondaryOpacity, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 2, 0) });
        var array = HifPlaySequenceConfig.Slot(rule, slot, target);
        foreach (var name in array.Values<string>().OfType<string>().ToList())
        {
            var chip = new Button { Classes = { "Basic" }, Padding = new Thickness(3), MinHeight = 0, Tag = name };
            var content = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4 };
            if (DrinkIcon(DrinkId(name)) is { } bitmap)
                content.Children.Add(new Image { Source = bitmap, Width = 28, Height = 28, Stretch = Stretch.Uniform });
            content.Children.Add(new TextBlock
            {
                Text = name, FontSize = 11, MaxWidth = 96, TextTrimming = TextTrimming.CharacterEllipsis, VerticalAlignment = VerticalAlignment.Center
            });
            var hint = DrinkHint?.Invoke(_profession, name);
            if (hint != null)
                content.Children.Add(new TextBlock { Text = "!", FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Top, Foreground = Brushes.OrangeRed });
            chip.Content = content;
            ToolTip.SetTip(chip, name + (hint != null ? $"\n{hint}" : "") + "\n点击移出");
            chip.Click += (_, _) =>
            {
                array.FirstOrDefault(item => (string?)item == name)?.Remove();
                SequenceChanged(rule, slot == "waiting");
            };
            panel.Children.Add(chip);
        }
        var open = _pick is { Kind: PickKind.Drink } pick && pick.Slot == slot && pick.Target == target;
        var add = new Button { Content = "＋", Classes = { open ? "Flat" : "Basic" }, Padding = new Thickness(8, 2), MinHeight = 0, Tag = $"{slot}:{target}" };
        ToolTip.SetTip(add, $"添加{SlotName(slot)}饮料");
        add.Click += (_, _) => OpenPick(open ? null : new SequencePick(PickKind.Drink, slot, target));
        panel.Children.Add(add);
        return panel;
    }

    /// <summary>饮料选择区：全部 28 种饮料，点选按顺序加入当前位置，再点一次移出。</summary>
    private Control DrinkPicker(JObject rule)
    {
        _drinkPicker.Content = DrinkTiles(rule);
        if (_drinkPicker.Parent is Decorator decorator) decorator.Child = null;
        else if (_drinkPicker.Parent is Panel panel) panel.Children.Remove(_drinkPicker);
        return _drinkPicker;
    }

    private Control DrinkTiles(JObject rule)
    {
        var tiles = new WrapPanel { Orientation = Orientation.Horizontal };
        if (_pick is not { Kind: PickKind.Drink } pick || pick.Slot is not { } slot) return tiles;
        var array = HifPlaySequenceConfig.Slot(rule, slot, pick.Target);
        var chosen = array.Values<string>().OfType<string>().ToList();
        foreach (var drink in _drinkCatalog)
        {
            var name = (string?)drink["name"] ?? "";
            var id = (int?)drink["id"] ?? 0;
            var order = chosen.IndexOf(name);
            var hint = DrinkHint?.Invoke(_profession, name);
            var body = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 6 };
            var icon = new Grid { Width = 36, Height = 36 };
            if (DrinkIcon(id) is { } bitmap) icon.Children.Add(new Image { Source = bitmap, Width = 36, Height = 36, Stretch = Stretch.Uniform });
            if (order >= 0)
            {
                var badge = new Border
                {
                    CornerRadius = new CornerRadius(6), Padding = new Thickness(4, 0), HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top,
                    Child = new TextBlock { Text = $"{order + 1}", FontSize = 10, Foreground = Brushes.White, FontWeight = FontWeight.SemiBold }
                };
                badge[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                icon.Children.Add(badge);
            }
            body.Children.Add(icon);
            var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            text.Children.Add(new TextBlock { Text = name, FontSize = 11, TextWrapping = TextWrapping.Wrap });
            text.Children.Add(new TextBlock
            {
                Text = hint ?? $"{(string?)drink["rarity"]} · {HifCardPicker.PlanName((string?)drink["plan"] ?? "")}",
                FontSize = 10, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap
            });
            Grid.SetColumn(text, 1);
            body.Children.Add(text);
            var tile = new Border
            {
                Child = body, Width = 150, Margin = new Thickness(0, 0, 6, 6), Padding = new Thickness(6), CornerRadius = new CornerRadius(10),
                BorderThickness = new Thickness(2), Cursor = new Cursor(StandardCursorType.Hand), Tag = name
            };
            if (order >= 0)
            {
                tile[!Border.BorderBrushProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                tile[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiCardBackground");
            }
            else
            {
                tile.BorderBrush = Brushes.Transparent;
                tile[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiCardBackground");
            }
            ToolTip.SetTip(tile, name + (hint != null ? $"\n{hint}，仍可加入" : "") + (order >= 0 ? $"\n第 {order + 1} 瓶 · 点击移出" : "\n点击加入"));
            tile.PointerPressed += (_, e) =>
            {
                if (!e.GetCurrentPoint(tile).Properties.IsLeftButtonPressed) return;
                ToggleDrink(rule, slot, pick.Target, name);
            };
            tiles.Children.Add(tile);
        }
        return tiles;
    }

    /// <summary>饮料选择区点选：不在该位置就加到末尾，已在就移出。</summary>
    public bool ToggleSequenceDrink(string start, string slot, string? target, string name)
    {
        if (!_loaded || _sequences[_profession][start] is not JObject rule || !HifPlaySequenceConfig.DrinkNames.Contains(name)) return false;
        if (target != null && !HifPlaySequenceConfig.Targets(rule).Contains(target)) return false;
        if (slot == "waiting" && HifPlaySequenceConfig.Targets(rule).Count == 0) return false;
        ToggleDrink(rule, slot, target, name);
        return true;
    }

    private void ToggleDrink(JObject rule, string slot, string? target, string name)
    {
        var array = HifPlaySequenceConfig.Slot(rule, slot, target);
        var existing = array.FirstOrDefault(item => (string?)item == name);
        if (existing != null) existing.Remove(); else array.Add(name);
        SequenceChanged(rule, slot == "waiting");
        LoggerHelper.UserAction(existing != null ? "出牌编排移出饮料" : "出牌编排加入饮料",
            $"职业={_profession}, 起始卡={_selectedSequence}, 后续卡={target ?? "-"}, 位置={slot}, 饮料={name}", operation: "HifPriorityEditor");
    }

    /// <summary>组合设置：等待回合、再次等待、后续卡出牌方式与等待用饮；没有后续卡时只显示说明。</summary>
    private Control BuildSequenceSettings(JObject rule)
    {
        if (HifPlaySequenceConfig.Targets(rule).Count == 0)
            return new TextBlock
            {
                Text = "加入后续卡后可设置等待回合、再次等待、后续卡出牌方式和等待用饮。",
                FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap
            };
        var body = new StackPanel { Spacing = 10 };
        body.Children.Add(SectionTitle("组合设置", "起始卡打出后，候选都不在手牌时的等待方式。"));
        var waitRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        waitRow.Children.Add(new TextBlock { Text = "等待回合", Width = 84, VerticalAlignment = VerticalAlignment.Center });
        var wait = new NumericUpDown
        {
            Minimum = 0, Maximum = 6, Increment = 1, Width = 100, FormatString = "0", Name = "wait_turns",
            Value = (int?)rule["wait_turns"] ?? 3, VerticalAlignment = VerticalAlignment.Center
        };
        wait.ValueChanged += (_, _) =>
        {
            if (wait.Value is not { } value || value % 1 != 0 || value < 0 || value > 6 || (int)value == (int?)rule["wait_turns"]) return;
            rule["wait_turns"] = (int)value;
            MarkChanged();
            RenderSequenceList();
        };
        waitRow.Children.Add(wait);
        waitRow.Children.Add(new TextBlock { Text = "0 表示不空过等待；同手有候选时仍会衔接", FontSize = 12, Opacity = HifTheme.SecondaryOpacity, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap });
        body.Children.Add(waitRow);
        var repeat = new CheckBox { Content = "再次等待：完成一次组合后，再遇到起始卡继续等待", IsChecked = (bool?)rule["wait_after_success"] == true, Name = "wait_after_success" };
        repeat.IsCheckedChanged += (_, _) =>
        {
            rule["wait_after_success"] = repeat.IsChecked == true;
            MarkChanged();
        };
        body.Children.Add(repeat);
        var policyRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        policyRow.Children.Add(new TextBlock { Text = "后续卡出牌", Width = 84, VerticalAlignment = VerticalAlignment.Center });
        var policy = new HifSegmented(HifPlaySequenceConfig.PolicyLabels) { Name = "target_policy" };
        var policyHint = new TextBlock { FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(94, 0, 0, 0) };
        void ShowPolicy(int index)
        {
            policy.Select(index);
            policyHint.Text = PolicyHints[index];
        }
        ShowPolicy(Math.Max(0, Array.IndexOf(HifPlaySequenceConfig.Policies, (string?)rule["target_policy"])));
        policy.SelectionRequested += index =>
        {
            rule["target_policy"] = HifPlaySequenceConfig.Policies[index];
            ShowPolicy(index);
            MarkChanged();
            RenderSequenceList();
        };
        policyRow.Children.Add(policy);
        body.Children.Add(policyRow);
        body.Children.Add(policyHint);
        var waitingRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        waitingRow.Children.Add(new TextBlock { Text = "等待用饮", Width = 84, VerticalAlignment = VerticalAlignment.Center });
        waitingRow.Children.Add(DrinkSlot(rule, "waiting", null));
        body.Children.Add(waitingRow);
        body.Children.Add(new TextBlock
        {
            Text = "后续卡都不在手牌、准备空过等待前喝，每次衔接一轮；用饮后出现选卡页时按后续候选顺序选卡。",
            FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(94, 0, 0, 0)
        });
        return Section(body);
    }

    /// <summary>卡牌选择区（起始卡或后续卡）；同一实例在打开期间保留筛选和滚动位置。</summary>
    private HifCardPicker CardPicker()
    {
        if (_cardPicker == null)
        {
            _cardPicker = new HifCardPicker(LibraryCards(), () => _profession)
            {
                Height = 460,
                Allowed = card => _catalog[card.Name] is JObject,
                Preferred = card => _profiles[_profession].Any(item => (string?)item["key"] == card.Name),
                Describe = DescribePick,
                Clicked = PickCard,
                Failure = () => _status.Text ?? "",
                CountText = (visible, allowed) => (_pick?.Kind is PickKind.NewStart or PickKind.ChangeStart
                    ? "点卡选择起始卡"
                    : $"点卡按顺序加入后续卡，再点一次移出 · 已有 {HifPlaySequenceConfig.Targets(CurrentSequence() ?? new JObject()).Count} 张") +
                    $" · 显示 {visible} / {allowed} 张"
            };
        }
        _cardPicker.Refresh();
        return _cardPicker;
    }

    private JObject? CurrentSequence() => _selectedSequence is { } key ? _sequences[_profession][key] as JObject : null;

    /// <summary>更换起始卡后，原有后续卡不能衔接回新的起始卡。</summary>
    private bool CanRenameStart(string name)
    {
        if (_selectedSequence is not { } start || _sequences[_profession][start] is not JObject rule) return false;
        var targets = HifPlaySequenceConfig.Targets(rule);
        if (targets.Contains(name)) return false;
        var rules = (JObject)_sequences[_profession].DeepClone();
        rules.Remove(start);
        rules[name] = rule.DeepClone();
        return targets.All(target => !HifPlaySequenceConfig.WouldCycle(rules, name, target));
    }

    private HifCardPicker.TileState DescribePick(HifLibraryCard card)
    {
        var rules = _sequences[_profession];
        var detail = HifCardPicker.CardDetail(card);
        if (_pick?.Kind is PickKind.NewStart or PickKind.ChangeStart)
        {
            var current = _pick.Kind == PickKind.ChangeStart && card.Name == _selectedSequence;
            var existing = rules[card.Name] != null;
            var blocked = _pick.Kind == PickKind.ChangeStart && !current && (existing || !CanRenameStart(card.Name));
            var hint = current ? "当前起始卡"
                : existing ? (_pick.Kind == PickKind.NewStart ? "已有编排，点击打开" : "已有编排的卡不能作起始卡")
                : blocked ? "会形成循环，不能作起始卡"
                : "点击选为起始卡";
            return new HifCardPicker.TileState(current, blocked, existing ? "编" : "", detail + "\n" + hint);
        }
        if (CurrentSequence() is not { } rule || _selectedSequence is not { } start)
            return new HifCardPicker.TileState(false, true, "", detail);
        var targets = HifPlaySequenceConfig.Targets(rule);
        var index = targets.IndexOf(card.Name);
        if (index >= 0) return new HifCardPicker.TileState(true, false, $"{index + 1}", detail + $"\n第 {index + 1} 张后续卡 · 点击移出");
        if (card.Name == start) return new HifCardPicker.TileState(false, true, "起", detail + "\n起始卡本身");
        if (HifPlaySequenceConfig.WouldCycle(rules, start, card.Name))
            return new HifCardPicker.TileState(false, true, "", detail + "\n会形成循环，不能加入");
        return new HifCardPicker.TileState(false, false, "", detail + "\n点击加入后续卡末尾");
    }

    /// <summary>选择区点卡：选起始卡、更换起始卡，或加入/移出后续卡。返回 false 表示未能修改。</summary>
    public bool PickCard(HifLibraryCard card)
    {
        if (!_loaded) return false;
        var profession = _profession;
        var rules = _sequences[profession];
        switch (_pick?.Kind)
        {
            case PickKind.NewStart:
                if (_catalog[card.Name] is not JObject) return false;
                if (rules[card.Name] == null)
                {
                    rules[card.Name] = HifPlaySequenceConfig.NewRule();
                    MarkChanged();
                    LoggerHelper.UserAction("新建 HIF 出牌编排", $"职业={profession}, 起始卡={card.Name}", operation: "HifPriorityEditor");
                }
                _selectedSequence = card.Name;
                // 接着点卡加入后续卡；只想给这张卡用饮时点「完成」。
                _pick = new SequencePick(PickKind.Candidate);
                Dispatcher.UIThread.Post(RenderCards);
                return true;
            case PickKind.ChangeStart:
                if (_selectedSequence is not { } old || card.Name == old) return true;
                if (rules[card.Name] != null || !CanRenameStart(card.Name))
                {
                    _status.Text = $"「{card.Name}」不能作起始卡：已有编排或会形成循环";
                    return false;
                }
                var moved = rules[old]!;
                rules.Remove(old);
                rules[card.Name] = moved;
                _selectedSequence = card.Name;
                _pick = null;
                MarkChanged();
                Dispatcher.UIThread.Post(RenderCards);
                LoggerHelper.UserAction("更换 HIF 出牌编排起始卡", $"职业={profession}, 原起始卡={old}, 新起始卡={card.Name}", operation: "HifPriorityEditor");
                return true;
            case PickKind.Candidate:
                if (_selectedSequence is not { } start || rules[start] is not JObject rule) return false;
                var targets = (JArray)rule["targets"]!;
                var existing = targets.FirstOrDefault(item => (string?)item == card.Name);
                if (existing != null) existing.Remove();
                else
                {
                    if (_catalog[card.Name] is not JObject || card.Name == start || HifPlaySequenceConfig.WouldCycle(rules, start, card.Name))
                    {
                        _status.Text = $"无法加入「{card.Name}」：起始卡本身或会形成循环";
                        return false;
                    }
                    targets.Add(card.Name);
                }
                HifPlaySequenceConfig.Normalize(rule);
                MarkChanged();
                RefreshSequence();
                LoggerHelper.UserAction(existing != null ? "出牌编排移出后续卡" : "出牌编排加入后续卡", $"职业={profession}, 起始卡={start}, 后续卡={card.Name}", operation: "HifPriorityEditor");
                return true;
        }
        return false;
    }

    /// <summary>打开选择区（供页面和验证工具使用）。</summary>
    public void OpenSequencePick(string kind, string? slot = null, string? target = null)
    {
        var pick = kind switch
        {
            "new" => new SequencePick(PickKind.NewStart),
            "start" => new SequencePick(PickKind.ChangeStart),
            "candidate" => new SequencePick(PickKind.Candidate),
            "drink" => new SequencePick(PickKind.Drink, slot ?? "before", target),
            _ => null
        };
        OpenPick(pick);
    }

    /// <summary>选中左侧的一条编排。</summary>
    public void SelectSequence(string? key)
    {
        _selectedSequence = key;
        _pick = null;
        if (_loaded) RenderCards();
    }
}
