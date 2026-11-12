using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Input;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

using MFAAvalonia.Views.Windows;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>
/// 「获取与定制」：每张卡一张卡片，带「优先获取」开关和技能卡定制项目的累计目标次数。
/// 定制选择保存在本面板的选择文件；优先获取名单属于卡牌面板（<see cref="Cards"/>），由它保存。
/// </summary>
public sealed class HifCustomCardPanel : HifStrategyPanel
{
    private readonly string _catalogPath = HifLayout.CatalogPath("hif_customization_catalog.json");
    private readonly string _selectionPath = HifLayout.PersonalPath("hif_custom_card_selection.json");
    private readonly string _imageRoot = Path.Combine(HifLayout.ImageRoot, "hif", "hif_card_icons");
    private readonly Dictionary<string, Dictionary<int, Dictionary<int, decimal>>> _profiles = new();
    private Dictionary<int, Dictionary<int, decimal>> _selected => _profiles[_profession];
    private readonly string _taskProfession;
    private string _profession;
    private JObject _selection = new();
    private readonly StackPanel _rows = new() { Spacing = 7 };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _save = new() { Content = "保存配置", IsEnabled = false };
    private JArray _catalog = new();
    private string _revision = "";
    private string _originalHash = "";
    private bool _loaded;

    public HifCustomCardPanel(string? initialProfession = null)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        Title = "HIF 获取与定制";
        BuildLayout();
        try
        {
            Load();
            _loaded = true;
            Render();
            LoggerHelper.UserAction("打开 HIF 技能卡定制面板",
                $"任务职业={_taskProfession}, 显示职业={_profession}, 目录卡数={_catalog.Count}, 已导入={_selected.Count}, 数据版本={_revision}", operation: "HifCustomCardEditor");
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException)
        {
            _status.Text = $"无法加载技能卡定制配置：{error.Message}";
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifCustomCardEditor");
            LoggerHelper.Error("加载 HIF 技能卡定制面板失败", error);
        }
    }

    private void BuildLayout()
    {
        var root = new Grid { RowDefinitions = new RowDefinitions("*,Auto") };
        root.Children.Add(new ScrollViewer { Content = _rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        _save.Click += (_, _) => Save();
        var footer = Footer(_status, _save);
        Grid.SetRow(footer, 1);
        root.Children.Add(footer);
        Content = root;
    }

    /// <summary>卡牌面板：提供优先获取名单与卡牌资料。未设置时只显示定制卡，不显示「优先获取」开关。</summary>
    public HifPriorityPanel? Cards { get; set; }

    /// <summary>重新显示卡片（例如卡库或卡牌面板改动了名单之后）。</summary>
    public void Refresh()
    {
        if (_loaded) Render();
    }

    public override string HelpText =>
            "职业\n每个职业独立保存优先获取名单和定制项目；HIF 执行时读取主界面所选职业。\n\n" +
            "添加卡牌\n在「卡库」标签选择「获取与定制」后点卡加入，再点一次移出。新加入的卡「优先获取」默认关闭；没有定制项目的卡只用于优先获取，加入后直接开启。保存前不写入配置。\n\n" +
            "优先获取\n开启后，授业换入和支给领卡时优先选择这张卡。候选仍按游戏中的出现顺序扫描，名单不改变扫描顺序。只想定制、不想获取的卡（例如偶像自带的卡）保持关闭。\n\n" +
            "累计目标次数\n技能卡定制各项目的目标，包含卡牌已有的定制次数。0 表示不执行该项；达到目标后停止该项定制。\n\n" +
            "项目上限\n各项目不能超过目录给出的上限。实际执行还受游戏总剩余次数限制。\n\n" +
            "没有定制项目的卡\n只能设优先获取，不需要时点卡片右上角的 × 移出。\n\n" +
            "移出\n点卡片右上角的 × 同时移出当前职业的优先获取名单和定制名单，不影响其他职业。\n\n" +
            "保存\n页面顶部「保存」写入全部修改，下次开始培育生效；配置被其他窗口修改时提示重新读取。";

    protected override Button SaveButton => _save;
    protected override void SaveCore() => Save();

    public override string Profession
    {
        get => _profession;
        set
        {
            if (!Professions.Contains(value) || value == _profession) return;
            _profession = value;
            if (!_loaded) return;
            Render();
            _status.Text = $"{(_save.IsEnabled ? "未保存；" : "")}{_profession}：定制名单 {_selected.Count} 张";
        }
    }

    private void Load()
    {
        var source = JObject.Parse(File.ReadAllText(_catalogPath, Encoding.UTF8));
        _catalog = source["cards"] as JArray ?? throw new InvalidOperationException("缺少 cards 目录");
        _revision = (string?)source["sourceRevision"] ?? throw new InvalidOperationException("缺少数据版本");
        _originalHash = SelectionHash();
        if (File.Exists(_selectionPath))
            _selection = JObject.Parse(File.ReadAllText(_selectionPath, Encoding.UTF8));
        var profiles = _selection["profiles"] as JObject;
        // 不分职业的旧版选择不再读取，保存时去掉。
        if (_selection["cards"] != null || _selection["profiles"] != null && profiles == null) NoteIncompatible("技能卡定制");
        foreach (var profession in Professions)
        {
            var selected = new Dictionary<int, Dictionary<int, decimal>>();
            _profiles[profession] = selected;
            var cards = profiles?[profession] as JObject ?? new JObject();
            foreach (var entry in cards.Properties())
            {
                if (!int.TryParse(entry.Name, out var id) || FindCard(id) is not { } card)
                    throw new InvalidOperationException($"{profession}：无效卡牌 ID {entry.Name}");
                var options = (card["options"] as JArray ?? new JArray()).OfType<JObject>()
                    .ToDictionary(option => (int)option["id"]!, option => (int?)option["max"]
                        ?? throw new InvalidOperationException($"{card["name"]}：缺少项目上限"));
                // v261015 目录曾给新卡用临时项目 ID -(卡ID×10+菜单序号)；按菜单顺序换成官方 ID，保存时写回官方 ID。
                var menu = (card["options"] as JArray ?? new JArray()).OfType<JObject>().Select(option => (int)option["id"]!).ToList();
                int Official(int optionId) => optionId < 0 && -optionId / 10 == id && -optionId % 10 is var index
                    && index >= 1 && index <= menu.Count ? menu[index - 1] : optionId;
                var counts = new Dictionary<int, decimal>();
                if (entry.Value is JArray)
                {
                    // 旧版本的勾选数组不再读取。
                    NoteIncompatible("技能卡定制");
                    continue;
                }
                if (entry.Value is JObject targets)
                {
                    foreach (var target in targets.Properties())
                    {
                        var optionId = int.TryParse(target.Name, out var rawId) ? Official(rawId) : 0;
                        if (!options.TryGetValue(optionId, out var maximum)
                            || target.Value.Type != JTokenType.Integer || !decimal.TryParse(target.Value.ToString(), out var count)
                            || count < 0 || count > maximum)
                            throw new InvalidOperationException($"{profession}/{card["name"]}：项目 {target.Name} 次数无效");
                        counts[optionId] = count;
                    }
                }
                else throw new InvalidOperationException($"{profession}/{card["name"]}：定制配置必须是对象");
                selected[id] = counts;
            }
        }
        _status.Text = $"{_profession}：可定制 {_catalog.Count} 张；定制名单 {_selected.Count} 张";
    }

    /// <summary>卡库用：这张卡是否有定制项目。</summary>
    public bool Customizable(int id) => _loaded && FindCard(id) != null;

    public bool InList(int id, string? profession = null) => _loaded && _profiles[profession ?? _profession].ContainsKey(id);

    /// <summary>加入或移出当前职业的定制名单。</summary>
    public void SetInList(int id, bool include)
    {
        if (!_loaded || FindCard(id) is not { } card || InList(id) == include) return;
        if (include) _selected[id] = new Dictionary<int, decimal>();
        else _selected.Remove(id);
        MarkChanged();
        Render();
        LoggerHelper.UserAction(include ? "卡库加入 HIF 技能卡定制" : "卡库移出 HIF 技能卡定制", $"职业={_profession}, 卡牌={card["name"]}, ID={id}", operation: "HifCustomCardEditor");
    }

    private string SelectionHash() => File.Exists(_selectionPath)
        ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_selectionPath))) : "";

    private JObject? FindCard(int id) => _catalog.OfType<JObject>().FirstOrDefault(card => (int?)card["id"] == id);



    /// <summary>
    /// 当前职业在优先获取名单或定制名单中的卡，各一张卡片；按卡牌目录顺序排列，开关和次数变化不改变位置。
    /// </summary>
    private void Render()
    {
        _rows.Children.Clear();
        var acquired = Cards?.AcquisitionNames().ToHashSet() ?? new HashSet<string>();
        var grid = new HifTileGrid { Name = "cards", MinTileWidth = HifTileGrid.CardTileWidth };
        var shownIds = new HashSet<int>();
        var shownNames = new HashSet<string>();
        foreach (var card in Cards?.LibraryCards().Where(card => !card.PriorityOnly) ?? Enumerable.Empty<HifLibraryCard>())
        {
            if (!acquired.Contains(card.Name) && !_selected.ContainsKey(card.Id)) continue;
            shownIds.Add(card.Id);
            shownNames.Add(card.Name);
            grid.Children.Add(BuildTile(card.Id, card.Name, HifCardPicker.CardDetail(card).Split('\n').Last(), acquired.Contains(card.Name)));
        }
        // 没有卡牌面板（单独打开）或目录对不上时，仍列出定制名单和名单中的旧卡名，保证都能看到和移出。
        foreach (var card in _catalog.OfType<JObject>().Where(card => _selected.ContainsKey((int)card["id"]!) && !shownIds.Contains((int)card["id"]!)))
            grid.Children.Add(BuildTile((int)card["id"]!, (string?)card["name"] ?? "", "", false));
        foreach (var name in acquired.Where(name => !shownNames.Contains(name)))
            grid.Children.Add(BuildTile(0, name, "", true));
        if (grid.Children.Count == 0)
        {
            _rows.Children.Add(new TextBlock { Text = "此职业还没有优先获取或定制的卡牌。", Margin = new Thickness(8, 16, 8, 8), TextWrapping = TextWrapping.Wrap });
            _rows.Children.Add(LibraryButton(HifLibraryTarget.Acquisition));
            return;
        }
        _rows.Children.Add(grid);
    }

    private Border BuildTile(int id, string name, string summary, bool acquired)
    {
        var card = FindCard(id);
        var body = new StackPanel { Spacing = 6 };

        var header = new DockPanel();
        var remove = new Button { Content = "×", Classes = { "Basic" }, Padding = new Thickness(8, 2), MinHeight = 0, VerticalAlignment = VerticalAlignment.Top };
        ToolTip.SetTip(remove, "移出当前职业的优先获取名单和定制名单");
        remove.Click += (_, _) =>
        {
            if (_selected.Remove(id)) MarkChanged();
            if (acquired) Cards?.SetInList(HifLibraryTarget.Acquisition, name, false);
            Render();
            LoggerHelper.UserAction("从 HIF 获取与定制移出卡牌", $"职业={_profession}, 卡牌={name}, ID={id}", operation: "HifCustomCardEditor");
        };
        DockPanel.SetDock(remove, Dock.Right);
        header.Children.Add(remove);
        var picture = new Grid { Width = 52, Height = 52, Margin = new Thickness(0, 0, 10, 0), VerticalAlignment = VerticalAlignment.Top };
        if (HifCardPicker.Icon(id) is { } bitmap)
            picture.Children.Add(new Image { Source = bitmap, Width = 52, Height = 52, Stretch = Stretch.Uniform });
        else
            picture.Children.Add(new TextBlock
            {
                Text = "无图", HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity
            });
        DockPanel.SetDock(picture, Dock.Left);
        header.Children.Add(picture);
        var title = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(new TextBlock { Text = name, TextWrapping = TextWrapping.Wrap, MaxLines = 2, TextTrimming = TextTrimming.CharacterEllipsis });
        if (summary != "") title.Children.Add(new TextBlock { Text = summary, FontSize = 12, Opacity = HifTheme.SecondaryOpacity });
        header.Children.Add(title);
        body.Children.Add(header);

        if (Cards != null)
        {
            // 没有定制项目的卡只用于优先获取：开关保持开启，不需要时点 × 移出。
            var toggle = new ToggleSwitch { IsChecked = acquired, Tag = "优先获取", IsEnabled = card != null, VerticalAlignment = VerticalAlignment.Center };
            ToolTip.SetTip(toggle, card != null
                ? "开启后，授业换入和支给领卡时优先选择这张卡"
                : "这张卡没有定制项目，只用于优先获取；不需要时点 × 移出");
            toggle.IsCheckedChanged += (_, _) =>
            {
                var include = toggle.IsChecked == true;
                // 关闭后仍留在本页：先记入定制名单（次数为 0 不会执行）。
                if (!include && !_selected.ContainsKey(id))
                {
                    _selected[id] = new Dictionary<int, decimal>();
                    MarkChanged();
                }
                Cards.SetInList(HifLibraryTarget.Acquisition, name, include);
                Dispatcher.UIThread.Post(Render);
            };
            var row = new DockPanel();
            DockPanel.SetDock(toggle, Dock.Right);
            row.Children.Add(toggle);
            row.Children.Add(new TextBlock { Text = "优先获取", VerticalAlignment = VerticalAlignment.Center });
            body.Children.Add(row);
        }

        if (card == null)
            body.Children.Add(new TextBlock { Text = "这张卡没有定制项目", FontSize = 12, Opacity = HifTheme.SecondaryOpacity, Margin = new Thickness(0, 4, 0, 0) });
        else
        {
            body.Children.Add(new Border { Height = 1, Background = HifTheme.LineBrush, Margin = new Thickness(0, 2) });
            var caption = new TextBlock { Text = "定制 · 累计目标次数", FontSize = 12, Opacity = HifTheme.SecondaryOpacity };
            ToolTip.SetTip(caption, "含卡牌已有的定制次数；0 为不执行");
            body.Children.Add(caption);
            foreach (var option in (card["options"] as JArray ?? new JArray()).OfType<JObject>())
            {
                var optionId = (int)option["id"]!;
                var maximum = (int)option["max"]!;
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 12 };
                var label = new StackPanel { Spacing = 1, VerticalAlignment = VerticalAlignment.Center };
                label.Children.Add(new TextBlock { Text = (string?)option["name"] ?? "", TextWrapping = TextWrapping.Wrap });
                label.Children.Add(new TextBlock { Text = $"上限 {maximum} 次", FontSize = 11, Opacity = HifTheme.SecondaryOpacity });
                row.Children.Add(label);
                var count = new NumericUpDown
                {
                    Minimum = 0, Maximum = maximum, Increment = 1, Width = 100,
                    ClipValueToMinMax = false, VerticalAlignment = VerticalAlignment.Center,
                    Value = _selected.GetValueOrDefault(id)?.GetValueOrDefault(optionId) ?? 0,
                    Tag = optionId
                };
                // 控件显示时会重新格式化文字；只有数值真正变化才算修改。未设置的项目按 0 次处理。
                void Update(decimal value)
                {
                    if ((_selected.GetValueOrDefault(id)?.GetValueOrDefault(optionId) ?? 0) == value) return;
                    if (!_selected.ContainsKey(id)) _selected[id] = new Dictionary<int, decimal>();
                    _selected[id][optionId] = value;
                    MarkChanged();
                }
                count.ValueChanged += (_, _) => Update(count.Value ?? -1);
                count.PropertyChanged += (_, change) =>
                {
                    if (change.Property != NumericUpDown.TextProperty) return;
                    Update(decimal.TryParse(count.Text, out var entered) ? entered : -1);
                };
                Grid.SetColumn(count, 1);
                row.Children.Add(count);
                body.Children.Add(row);
            }
        }
        return new Border
        {
            Child = body, Tag = name, Margin = new Thickness(0, 0, 8, 8), Padding = new Thickness(10, 8), VerticalAlignment = VerticalAlignment.Top,
            BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10)
        };
    }

    private void MarkChanged()
    {
        _save.IsEnabled = true;
        _status.Text = $"未保存：{_profession} 定制名单 {_selected.Count} 张";
    }

    private void Save()
    {
        if (!_loaded) return;
        try
        {
            if (SelectionHash() != _originalHash)
            {
                _status.Text = "配置已被其他窗口修改，未保存。点「放弃修改」重新读取后再修改。";
                using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifCustomCardEditor");
                LoggerHelper.Warning("HIF 技能卡定制保存被拒绝：选择文件在编辑期间变化");
                return;
            }
            var saved = (JObject)_selection.DeepClone();
            saved["version"] = 2;
            saved["sourceRevision"] = _revision;
            var profiles = saved["profiles"] as JObject ?? new JObject();
            foreach (var profession in Professions)
            {
                var cards = new JObject();
                foreach (var id in _profiles[profession].Keys.Order())
                {
                    var card = FindCard(id) ?? throw new InvalidOperationException($"无效卡牌 ID {id}");
                    var options = ((JArray)card["options"]!).OfType<JObject>()
                        .ToDictionary(option => (int)option["id"]!, option => (int)option["max"]!);
                    var targets = new JObject();
                    foreach (var (optionId, count) in _profiles[profession][id].OrderBy(item => item.Key))
                    {
                        if (!options.TryGetValue(optionId, out var maximum) || count % 1 != 0 || count < 0 || count > maximum)
                            throw new InvalidOperationException($"{profession}/{card["name"]}：项目 {optionId} 必须为 0～{options.GetValueOrDefault(optionId)} 的整数");
                        if (count > 0) targets[optionId.ToString()] = (int)count;
                    }
                    cards[id.ToString()] = targets;
                }
                profiles[profession] = cards;
            }
            saved["profiles"] = profiles;
            saved.Remove("cards");
            var temporary = _selectionPath + ".tmp";
            File.WriteAllText(temporary, saved.ToString(Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(temporary, _selectionPath, true);
            _originalHash = SelectionHash();
            _save.IsEnabled = false;
            var active = _selected.Values.Count(counts => counts.Values.Any(count => count > 0));
            var queueNote = active > 6 ? "；当前执行队列最多处理前 6 张" : "";
            _status.Text = $"已保存各职业配置；{_profession} 定制名单 {_selected.Count} 张，已配置 {active} 张{queueNote}";
            LoggerHelper.UserAction("保存 HIF 技能卡定制配置",
                $"职业={_profession}, 已导入={_selected.Count}, 已配置={active}, 执行队列上限=6, 文件={_selectionPath}",
                operation: "HifCustomCardEditor");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            _status.Text = $"保存失败：{error.Message}";
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifCustomCardEditor");
            LoggerHelper.Error("保存 HIF 技能卡定制配置失败", error);
        }
    }
}
