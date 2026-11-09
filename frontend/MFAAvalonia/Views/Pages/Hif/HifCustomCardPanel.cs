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

/// <summary>从固定卡牌目录选择 HIF 技能卡定制项目；面板选择文件是执行配置的唯一来源。</summary>
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
    private readonly StackPanel _cards = new() { Spacing = 7, Margin = new Thickness(0, 0, 8, 0) };
    private readonly StackPanel _details = new() { Spacing = 12, Margin = new Thickness(0, 0, 8, 0) };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _save = new() { Content = "保存配置", IsEnabled = false };
    private JArray _catalog = new();
    private string _revision = "";
    private string _originalHash = "";
    private int? _activeId;
    private bool _loaded;

    public HifCustomCardPanel(string? initialProfession = null)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        Title = "HIF 技能卡定制配置";
        BuildLayout();
        try
        {
            Load();
            _loaded = true;
            RenderCards();
            RenderDetails();
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
        // 与出牌优先级一致：左侧名单定宽、可拖动分隔线调整，右侧详情随窗口伸缩。加卡在「卡库」。
        var root = new Grid { RowDefinitions = new RowDefinitions("*,Auto") };
        var body = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions
            {
                new ColumnDefinition(340, GridUnitType.Pixel) { MinWidth = 260 },
                new ColumnDefinition(GridLength.Auto),
                new ColumnDefinition(1, GridUnitType.Star) { MinWidth = 380 }
            }
        };
        root.Children.Add(body);
        body.Children.Add(new ScrollViewer { Content = _cards, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        var splitter = new GridSplitter { Width = 8, ResizeDirection = GridResizeDirection.Columns, Background = Brushes.Transparent };
        Grid.SetColumn(splitter, 1);
        body.Children.Add(splitter);
        var detailScroll = new ScrollViewer { Content = _details, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(8, 0, 0, 0) };
        Grid.SetColumn(detailScroll, 2);
        body.Children.Add(detailScroll);
        _save.Click += (_, _) => Save();
        var footer = Footer(_status, _save);
        Grid.SetRow(footer, 1);
        root.Children.Add(footer);
        Content = root;
    }

    public override string HelpText =>
            "职业\n每个职业独立保存导入卡牌和定制项目；HIF 执行时读取主界面所选职业。\n\n" +
            "添加卡牌\n在「卡库」标签选择「技能卡定制」后点卡加入，再点一次移出；卡库只显示有定制项目的卡。保存前不写入配置。\n\n" +
            "定制名单\n左侧列出当前职业已加入的卡，选择后右侧设置各项目的累计目标次数。\n\n" +
            "累计目标次数\n包含卡牌已有的定制次数。0 表示不执行该项；达到目标后停止该项定制。\n\n" +
            "项目上限\n各项目不能超过目录给出的上限。实际执行还受游戏总剩余次数限制。\n\n" +
            "移出名单\n从当前职业的定制名单移除，不影响其他职业。\n\n" +
            "保存配置\n保存后下次培育读取；旧全局配置在首次保存时复制到各职业，保留原选择。";

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
            _activeId = _selected.Keys.Cast<int?>().FirstOrDefault();
            RenderCards();
            RenderDetails();
            _status.Text = $"{(_save.IsEnabled ? "未保存；" : "")}{_profession}：已导入 {_selected.Count} 张";
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
        if (_selection.ContainsKey("profiles") && profiles == null)
            throw new InvalidOperationException("职业定制配置必须是对象");
        foreach (var profession in Professions)
        {
            var selected = new Dictionary<int, Dictionary<int, decimal>>();
            _profiles[profession] = selected;
            // 旧全局配置首次保存时复制到各职业，保留原有选择；职业配置存在后不再回退。
            var cards = (profiles == null ? _selection["cards"] : profiles[profession]) as JObject ?? new JObject();
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
                if (entry.Value is JArray legacy)
                {
                    foreach (var token in legacy)
                    {
                        var optionId = token.Type == JTokenType.Integer && int.TryParse(token.ToString(), out var rawId) ? Official(rawId) : 0;
                        if (!options.TryGetValue(optionId, out var maximum))
                            throw new InvalidOperationException($"{profession}/{card["name"]}：无效项目 {token}");
                        counts[optionId] = maximum;
                    }
                }
                else if (entry.Value is JObject targets)
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
                else throw new InvalidOperationException($"{profession}/{card["name"]}：定制配置必须是对象或旧数组");
                selected[id] = counts;
            }
        }
        _activeId = _catalog.OfType<JObject>().Select(card => (int)card["id"]!).FirstOrDefault(_selected.ContainsKey);
        if (_activeId == 0) _activeId = null;
        _status.Text = $"{_profession}：可导入 {_catalog.Count} 张；已导入 {_selected.Count} 张";
    }

    /// <summary>卡库用：这张卡是否有定制项目。</summary>
    public bool Customizable(int id) => _loaded && FindCard(id) != null;

    public bool InList(int id, string? profession = null) => _loaded && _profiles[profession ?? _profession].ContainsKey(id);

    /// <summary>加入或移出当前职业的定制名单；规则与导入窗口、移除按钮相同。</summary>
    public void SetInList(int id, bool include)
    {
        if (!_loaded || FindCard(id) is not { } card || InList(id) == include) return;
        if (include)
        {
            _selected[id] = new Dictionary<int, decimal>();
            _activeId = id;
        }
        else
        {
            _selected.Remove(id);
            _activeId = _catalog.OfType<JObject>().Select(item => (int)item["id"]!).FirstOrDefault(_selected.ContainsKey);
            if (_activeId == 0) _activeId = null;
        }
        MarkChanged();
        RenderCards();
        RenderDetails();
        LoggerHelper.UserAction(include ? "卡库加入 HIF 技能卡定制" : "卡库移出 HIF 技能卡定制", $"职业={_profession}, 卡牌={card["name"]}, ID={id}", operation: "HifCustomCardEditor");
    }

    private string SelectionHash() => File.Exists(_selectionPath)
        ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_selectionPath))) : "";

    private JObject? FindCard(int id) => _catalog.OfType<JObject>().FirstOrDefault(card => (int?)card["id"] == id);

    private Image? CardImage(int id, int size)
    {
        var path = Path.Combine(_imageRoot, $"{id}.webp");
        return File.Exists(path)
            ? new Image { Source = new Bitmap(path), Width = size, Height = size, Stretch = Stretch.Fill }
            : null;
    }

    private string CountSummary(int id) =>
        _selected[id].Values.Any(count => count > 0)
            ? $"已配置 {_selected[id].Values.Count(count => count > 0)} 项 · 累计目标 {_selected[id].Values.Sum()} 次"
            : "未设置项目";

    private void RenderCards()
    {
        _cards.Children.Clear();
        foreach (var card in _catalog.OfType<JObject>().Where(card => _selected.ContainsKey((int)card["id"]!)))
        {
            var id = (int)card["id"]!;
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 8 };
            if (CardImage(id, 52) is { } image) row.Children.Add(image);
            var label = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            label.Children.Add(new TextBlock { Text = (string?)card["name"] ?? "", TextWrapping = TextWrapping.Wrap });
            var summary = new TextBlock { Text = CountSummary(id), FontSize = 11, TextWrapping = TextWrapping.Wrap };
            if (_selected[id].Values.Any(count => count > 0))
                summary[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
            else summary.Opacity = HifTheme.SecondaryOpacity;
            label.Children.Add(summary);
            Grid.SetColumn(label, 1);
            row.Children.Add(label);
            var selected = id == _activeId;
            var border = new Border
            {
                Child = row, Padding = new Thickness(6, 4), CornerRadius = new CornerRadius(10),
                BorderThickness = new Thickness(selected ? 2 : 1), Cursor = new Cursor(StandardCursorType.Hand)
            };
            border[!Border.BorderBrushProperty] = new DynamicResourceExtension(selected ? "SukiPrimaryColor" : "SukiLightBorderBrush");
            if (selected) border[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor5");
            border.PointerPressed += (_, _) =>
            {
                if (id == _activeId) return;
                _activeId = id;
                Dispatcher.UIThread.Post(() => { RenderCards(); RenderDetails(); });
            };
            _cards.Children.Add(border);
        }
        if (_cards.Children.Count == 0)
            _cards.Children.Add(new TextBlock
            {
                Text = "此职业还没有定制卡牌。在「卡库」选择「技能卡定制」后点卡加入。",
                Opacity = HifTheme.SecondaryOpacity, Margin = new Thickness(6, 8), TextWrapping = TextWrapping.Wrap
            });
    }

    private void RenderDetails()
    {
        _details.Children.Clear();
        if (_activeId is not { } id || !_selected.ContainsKey(id))
        {
            _details.Children.Add(new TextBlock { Text = "在左侧选择一张卡，设置它的定制项目。", Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
            return;
        }
        var card = FindCard(id);
        if (card == null) return;
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 12 };
        if (CardImage(id, 64) is { } image) header.Children.Add(image);
        var title = new StackPanel { Spacing = 3, VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(new TextBlock { Text = (string?)card["name"] ?? "", FontSize = 16, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
        var subtitle = new TextBlock { Text = CountSummary(id), FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap };
        title.Children.Add(subtitle);
        Grid.SetColumn(title, 1);
        header.Children.Add(title);
        _details.Children.Add(header);

        var remove = new Button { Content = "从定制名单移出", Classes = { "Basic" }, HorizontalAlignment = HorizontalAlignment.Left };
        ToolTip.SetTip(remove, "只移出当前职业，不影响其他职业");
        remove.Click += (_, _) =>
        {
            _selected.Remove(id);
            _activeId = _catalog.OfType<JObject>().Select(item => (int)item["id"]!).FirstOrDefault(_selected.ContainsKey);
            if (_activeId == 0) _activeId = null;
            MarkChanged();
            RenderCards();
            RenderDetails();
            LoggerHelper.UserAction("从 HIF 技能卡定制面板移除卡牌", $"职业={_profession}, 卡牌={card["name"]}, ID={id}", operation: "HifCustomCardEditor");
        };
        _details.Children.Add(remove);

        // 定制项目：名称与上限在左，累计目标次数在右，用细边框分组（与出牌优先级的规则摘要一致）。
        var options = new StackPanel { Spacing = 10 };
        options.Children.Add(new TextBlock { Text = "累计目标次数（含已有定制次数，0 为不执行）", FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
        foreach (var option in (card["options"] as JArray ?? new JArray()).OfType<JObject>())
        {
            var optionId = (int)option["id"]!;
            var maximum = (int)option["max"]!;
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 12 };
            var name = new StackPanel { Spacing = 1, VerticalAlignment = VerticalAlignment.Center };
            name.Children.Add(new TextBlock { Text = (string?)option["name"] ?? "", TextWrapping = TextWrapping.Wrap });
            name.Children.Add(new TextBlock { Text = $"上限 {maximum} 次", FontSize = 11, Opacity = HifTheme.SecondaryOpacity });
            row.Children.Add(name);
            var count = new NumericUpDown
            {
                Minimum = 0, Maximum = maximum, Increment = 1, Width = 100,
                ClipValueToMinMax = false, VerticalAlignment = VerticalAlignment.Center,
                Value = _selected[id].GetValueOrDefault(optionId),
                Tag = optionId
            };
            // 控件显示时会重新格式化文字；只有数值真正变化才算修改。
            void Update(decimal value)
            {
                // 未设置的项目按 0 次处理。
                if (_selected[id].GetValueOrDefault(optionId) == value) return;
                _selected[id][optionId] = value;
                subtitle.Text = CountSummary(id);
                MarkChanged();
                RenderCards();
            }
            count.ValueChanged += (_, _) => Update(count.Value ?? -1);
            count.PropertyChanged += (_, change) =>
            {
                if (change.Property != NumericUpDown.TextProperty) return;
                Update(decimal.TryParse(count.Text, out var entered) ? entered : -1);
            };
            Grid.SetColumn(count, 1);
            row.Children.Add(count);
            options.Children.Add(row);
        }
        _details.Children.Add(new Border
        {
            Child = options, Padding = new Thickness(12, 10), CornerRadius = new CornerRadius(8),
            BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1)
        });
    }

    private void MarkChanged()
    {
        _save.IsEnabled = true;
        _status.Text = $"未保存：{_profession} 已导入 {_selected.Count} 张";
    }

    private void Save()
    {
        if (!_loaded) return;
        try
        {
            if (SelectionHash() != _originalHash)
            {
                _status.Text = "选择文件已由其他窗口修改，请重新打开面板。";
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
            _status.Text = $"已保存各职业配置；{_profession} 已导入 {_selected.Count} 张，已配置 {active} 张{queueNote}";
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
