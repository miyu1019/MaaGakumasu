using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
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

namespace MFAAvalonia.Views.Windows;

/// <summary>按职业编辑 HIF 饮料的获取顺序、购买范围和计划使用时机。</summary>
public sealed class HifDrinkWindow : SukiWindow
{
    private static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    private readonly string _configPath = HifLayout.PersonalPath("hif_drink_profiles.json");
    private readonly string _catalogPath = HifLayout.CatalogPath("hif_drink_catalog.json");
    private readonly string _imageRoot = Path.Combine(HifLayout.ImageRoot, "hif", "hif_drink_icons");
    private readonly Dictionary<string, List<JObject>> _profiles = new();
    private readonly Dictionary<string, JObject> _defaultTimings = new();
    private readonly Dictionary<int, JObject> _catalog = new();
    private readonly HashSet<string> _activeCardKeys = new();
    private readonly StackPanel _rows = new() { Spacing = 7 };
    private readonly TextBlock _heading = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _save = new() { Content = "保存饮料配置", IsEnabled = false };
    private readonly string _taskProfession;
    private string _profession;
    private string _originalHash = "";
    private JObject _config = new();
    private bool _loaded;

    public HifDrinkWindow(string? initialProfession = null)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        Title = "HIF 饮料配置";
        Width = 850;
        Height = 650;
        MinWidth = 800;
        MinHeight = 450;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        BuildLayout();
        try
        {
            LoadData();
            _loaded = true;
            RenderRows();
            LoggerHelper.UserAction("打开 HIF 饮料面板",
                $"任务职业={_taskProfession}, 显示职业={_profession}", operation: "HifDrinkEditor");
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException)
        {
            _status.Text = $"饮料配置加载失败：{error.Message}";
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifDrinkEditor");
            LoggerHelper.Error("加载 HIF 饮料面板失败", error);
        }
    }

    private void BuildLayout()
    {
        var root = new Grid { Margin = new Thickness(18), RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto") };
        UpdateHeading();
        root.Children.Add(_heading);
        var selector = new ComboBox { ItemsSource = Professions, SelectedItem = _profession, Width = 130 };
        selector.SelectionChanged += (_, _) =>
        {
            _profession = selector.SelectedItem as string ?? "集中";
            UpdateHeading();
            if (_loaded) RenderRows();
        };
        var import = new Button { Content = "导入饮料" };
        import.Click += (_, _) => ShowImportDialog();
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Margin = new Thickness(0, 12) };
        toolbar.Children.Add(new TextBlock { Text = "职业", VerticalAlignment = VerticalAlignment.Center });
        toolbar.Children.Add(selector);
        toolbar.Children.Add(import);
        Grid.SetRow(toolbar, 1);
        root.Children.Add(toolbar);
        var scroll = new ScrollViewer { Content = _rows };
        Grid.SetRow(scroll, 2);
        root.Children.Add(scroll);
        var footer = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), Margin = new Thickness(0, 12, 0, 0) };
        footer.Children.Add(_status);
        _save.Click += (_, _) => Save();
        Grid.SetColumn(_save, 1);
        footer.Children.Add(_save);
        var close = new Button { Content = "关闭", Margin = new Thickness(10, 0, 0, 0) };
        close.Click += (_, _) => Close();
        Grid.SetColumn(close, 2);
        footer.Children.Add(close);
        Grid.SetRow(footer, 3);
        root.Children.Add(footer);
        Content = root;
    }

    private void UpdateHeading() => _heading.Text =
        $"正在编辑：{_profession}。领取按列表顺序比较三瓶；只有勾选「购买」的饮料才参与商店购买。" +
        "「不使用」会排除领取、购买和本战使用。未导入或单项时机为「未指定」的饮料按下方统一时机使用；初星黒酢可选「只在组合技使用」。特製ハツボシエキス可关联多张 A 卡，在目标出牌前使用。领取目标时若已满4瓶，会舍弃最低优先级的非目标饮料，确认后重新选择领取；单种饮料数量上限暂未启用。勾选式上限页由任务选项「所持上限页按优先级保留」控制：默认关闭时仅补足已有选择，开启时按本面板顺序保留。";

    private void LoadData()
    {
        var priority = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_catalog.json"), Encoding.UTF8));
        var cardsCatalog = JArray.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_target_card_catalog.json"), Encoding.UTF8));
        foreach (var entry in cardsCatalog.OfType<JObject>().Where(c => (string?)c["type"] == "active" && priority[(string)c["name"]!] != null))
            _activeCardKeys.Add((string)entry["name"]!);
        var catalog = JObject.Parse(File.ReadAllText(_catalogPath, Encoding.UTF8));
        foreach (var drink in catalog["drinks"] as JArray ?? throw new InvalidOperationException("缺少饮料目录"))
        {
            var record = drink as JObject ?? throw new InvalidOperationException("饮料目录条目无效");
            var id = (int?)record["id"] ?? 0;
            if (id <= 0 || !_catalog.TryAdd(id, record)) throw new InvalidOperationException($"重复或无效的饮料 ID：{id}");
        }
        if (File.Exists(_configPath))
        {
            var bytes = File.ReadAllBytes(_configPath);
            _originalHash = Convert.ToHexString(SHA256.HashData(bytes));
            _config = JObject.Parse(Encoding.UTF8.GetString(bytes));
        }
        else
            _config = new JObject { ["schema_version"] = 1, ["profiles"] = new JObject() };
        var profiles = _config["profiles"] as JObject ?? throw new InvalidOperationException("缺少职业饮料配置");
        var defaults = _config["default_use_timing_profiles"] as JObject;
        foreach (var name in Professions)
        {
            var cards = (profiles[name] as JArray ?? new JArray()).OfType<JObject>()
                .Select(card => (JObject)card.DeepClone()).ToList();
            var ids = cards.Select(card => (int?)card["id"] ?? 0).ToArray();
            if (ids.Any(id => !_catalog.ContainsKey(id)) || ids.Distinct().Count() != ids.Length ||
                cards.Any(card => card["purchase_enabled"] is { Type: not JTokenType.Boolean }
                    || card["disabled"] is { Type: not JTokenType.Boolean }
                    || !ValidBeforeTargets(card)
                    || !ValidTiming(card["use_timing"], (int?)card["id"] == 22)))
                throw new InvalidOperationException($"{name}含无效饮料 ID 或购买配置");
            _profiles[name] = cards;
            var fallback = defaults?[name] as JObject;
            if (fallback != null && !ValidDefaultTiming(fallback))
                throw new InvalidOperationException($"{name}的未指定饮料使用时机无效");
            _defaultTimings[name] = fallback == null
                ? new JObject { ["mode"] = "remaining_turn", ["turn"] = 4 }
                : (JObject)fallback.DeepClone();
        }
    }

    private Image? DrinkImage(int id, int size)
    {
        var path = Path.Combine(_imageRoot, $"{id}.webp");
        return File.Exists(path)
            ? new Image { Source = new Bitmap(path), Width = size, Height = size, Stretch = Stretch.Uniform }
            : null;
    }

    private static string PlanName(string? plan) => plan switch
    {
        "sense" => "感性", "logic" => "理性", "anomaly" => "非凡", _ => "不限"
    };

    private void RenderRows()
    {
        _rows.Children.Clear();
        var profession = _profession;
        var defaultRule = _defaultTimings[profession];
        var defaultMode = (string?)defaultRule["mode"];
        var defaultRow = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 10,
            Margin = new Thickness(8, 5, 8, 15)
        };
        defaultRow.Children.Add(new TextBlock
        {
            Text = "未指定饮料使用时机", VerticalAlignment = VerticalAlignment.Center
        });
        var defaultSelector = new ComboBox
        {
            ItemsSource = new[] { "不自动使用", "每场第1回合", "剩余回合=" },
            SelectedIndex = defaultMode == "never" ? 0 : defaultMode == "first_turn" ? 1 : 2,
            Width = 145
        };
        var defaultTurns = new NumericUpDown
        {
            Minimum = 1, Maximum = 99, Increment = 1, Width = 76,
            Value = (decimal?)defaultRule["turn"] ?? 4,
            IsEnabled = defaultSelector.SelectedIndex == 2
        };
        void UpdateDefault()
        {
            defaultTurns.IsEnabled = defaultSelector.SelectedIndex == 2;
            var turn = defaultTurns.Value ?? 4;
            _defaultTimings[profession] = defaultSelector.SelectedIndex switch
            {
                0 => new JObject { ["mode"] = "never" },
                1 => new JObject { ["mode"] = "first_turn" },
                _ => new JObject
                {
                    ["mode"] = "remaining_turn",
                    ["turn"] = turn % 1 == 0 ? new JValue((int)turn) : new JValue(turn)
                }
            };
            MarkChanged();
        }
        defaultSelector.SelectionChanged += (_, _) => UpdateDefault();
        defaultTurns.ValueChanged += (_, _) => UpdateDefault();
        defaultRow.Children.Add(defaultSelector);
        defaultRow.Children.Add(defaultTurns);
        _rows.Children.Add(defaultRow);
        var cards = _profiles[_profession];
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock { Text = "尚未导入饮料。点击「导入饮料」建立计划。", Margin = new Thickness(8, 18) });
            return;
        }
        for (var index = 0; index < cards.Count; index++)
        {
            var card = cards[index];
            var id = (int)card["id"]!;
            var record = _catalog[id];
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("65,*,100,160,80,36,36,52"), RowDefinitions = new RowDefinitions("Auto,Auto"), Margin = new Thickness(5) };
            var icon = DrinkImage(id, 55);
            if (icon != null) row.Children.Add(icon);
            var title = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            title.Children.Add(new TextBlock { Text = $"{index + 1}. {(string?)record["name"]}", FontSize = 17 });
            title.Children.Add(new TextBlock { Text = $"{(string?)record["rarity"]} · {PlanName((string?)record["plan"])}", Foreground = Brushes.Gray });
            Grid.SetColumn(title, 1);
            row.Children.Add(title);
            var purchase = new CheckBox
            {
                Content = "购买", IsChecked = (bool?)card["purchase_enabled"] == true,
                VerticalAlignment = VerticalAlignment.Center
            };
            purchase.IsCheckedChanged += (_, _) =>
            {
                card["purchase_enabled"] = purchase.IsChecked == true;
                MarkChanged();
            };
            var disabled = new CheckBox
            {
                Content = "不使用", IsChecked = (bool?)card["disabled"] == true,
                VerticalAlignment = VerticalAlignment.Center
            };
            purchase.IsEnabled = disabled.IsChecked != true;
            var switches = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            switches.Children.Add(purchase);
            switches.Children.Add(disabled);
            Grid.SetColumn(switches, 2);
            row.Children.Add(switches);
            var timing = card["use_timing"] as JObject;
            var mode = (string?)timing?["mode"];
            var selector = new ComboBox
            {
                ItemsSource = id == 22
                    ? new[] { "未指定", "每场第1回合", "剩余回合=", "只在组合技使用" }
                    : new[] { "未指定", "每场第1回合", "剩余回合=" },
                SelectedIndex = mode == "first_turn" ? 1 : mode == "remaining_turn" ? 2 : mode == "combo_only" ? 3 : 0,
                Width = 155, IsEnabled = disabled.IsChecked != true && (card["before_card_targets"] as JArray)?.Count is not > 0,
                VerticalAlignment = VerticalAlignment.Center
            };
            Grid.SetColumn(selector, 3);
            row.Children.Add(selector);
            var turns = new NumericUpDown
            {
                Minimum = 1, Maximum = 99, Increment = 1, Width = 76,
                Value = (decimal?)timing?["turn"] ?? 3,
                IsEnabled = selector.SelectedIndex == 2 && selector.IsEnabled,
                VerticalAlignment = VerticalAlignment.Center
            };
            void UpdateTurn()
            {
                turns.IsEnabled = selector.SelectedIndex == 2 && selector.IsEnabled;
                if (selector.SelectedIndex == 1)
                    card["use_timing"] = new JObject { ["mode"] = "first_turn" };
                else if (selector.SelectedIndex == 2 && turns.Value is { } value && value % 1 == 0)
                    card["use_timing"] = new JObject { ["mode"] = "remaining_turn", ["turn"] = (int)value };
                else if (id == 22 && selector.SelectedIndex == 3)
                    card["use_timing"] = new JObject { ["mode"] = "combo_only" };
                else
                    card.Remove("use_timing");
                MarkChanged();
            }
            selector.SelectionChanged += (_, _) => UpdateTurn();
            turns.ValueChanged += (_, _) => UpdateTurn();
            Grid.SetColumn(turns, 4);
            row.Children.Add(turns);
            disabled.IsCheckedChanged += (_, _) =>
            {
                var excluded = disabled.IsChecked == true;
                card["disabled"] = excluded;
                if (excluded) purchase.IsChecked = false;
                purchase.IsEnabled = !excluded;
                selector.IsEnabled = !excluded && (card["before_card_targets"] as JArray)?.Count is not > 0;
                turns.IsEnabled = selector.IsEnabled && selector.SelectedIndex == 2;
                MarkChanged();
            };
            var sourceIndex = index;
            var up = new Button { Content = "↑", Padding = new Thickness(5, 2) };
            up.Click += (_, _) => Move(sourceIndex, sourceIndex - 1);
            Grid.SetColumn(up, 5);
            row.Children.Add(up);
            var down = new Button { Content = "↓", Padding = new Thickness(5, 2) };
            down.Click += (_, _) => Move(sourceIndex, sourceIndex + 1);
            Grid.SetColumn(down, 6);
            row.Children.Add(down);
            var remove = new Button { Content = "删除", Padding = new Thickness(4, 2) };
            remove.Click += (_, _) =>
            {
                cards.RemoveAt(sourceIndex);
                MarkChanged();
                RenderRows();
            };
            Grid.SetColumn(remove, 7);
            row.Children.Add(remove);
            if (id == 25)
            {
                var pairing = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Margin = new Thickness(0, 5, 0, 0) };
                var targets = card["before_card_targets"] as JArray;
                var associate = new Button { Content = targets is { Count: > 0 } ? "关联卡牌✓" : "关联卡牌", IsEnabled = disabled.IsChecked != true };
                disabled.IsCheckedChanged += (_, _) => associate.IsEnabled = disabled.IsChecked != true;
                associate.Click += (_, _) =>
                {
                    var saved = targets == null ? null : new JObject { ["targets"] = targets.DeepClone() };
                    var dialog = new HifFollowupWindow((string?)record["name"] ?? "", true, saved, rule =>
                    {
                        if (rule == null) card.Remove("before_card_targets"); else card["before_card_targets"] = rule["targets"]!.DeepClone();
                        MarkChanged(); RenderRows();
                    });
                    _ = dialog.ShowDialog(this);
                };
                pairing.Children.Add(associate);
                Grid.SetRow(pairing, 1); Grid.SetColumn(pairing, 1); Grid.SetColumnSpan(pairing, 7); row.Children.Add(pairing);
            }
            _rows.Children.Add(new Border { Child = row, BorderBrush = Brushes.DimGray, BorderThickness = new Thickness(1) });
        }
    }

    private void Move(int from, int to)
    {
        var cards = _profiles[_profession];
        if (to < 0 || to >= cards.Count) return;
        var item = cards[from];
        cards.RemoveAt(from);
        cards.Insert(to, item);
        MarkChanged();
        RenderRows();
    }

    private void ShowImportDialog()
    {
        var profession = _profession;
        var dialog = new SukiWindow
        {
            Title = $"导入饮料 - {profession}", Width = 630, Height = 660,
            MinWidth = 500, MinHeight = 420, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var root = new Grid { Margin = new Thickness(15), RowDefinitions = new RowDefinitions("Auto,*,Auto") };
        var filters = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 10) };
        var search = new TextBox { Watermark = "搜索饮料名称", Width = 240 };
        var rarity = new ComboBox { ItemsSource = new[] { "全部稀有度", "R", "SR", "SSR" }, SelectedIndex = 0, Width = 115 };
        var plan = new ComboBox { ItemsSource = new[] { "全部计划", "不限", "感性", "理性", "非凡" }, SelectedIndex = 0, Width = 115 };
        filters.Children.Add(search);
        filters.Children.Add(rarity);
        filters.Children.Add(plan);
        root.Children.Add(filters);
        var items = new StackPanel { Spacing = 5 };
        var scroll = new ScrollViewer { Content = items };
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);
        void Render()
        {
            items.Children.Clear();
            var selected = _profiles[profession].Select(card => (int)card["id"]!).ToHashSet();
            foreach (var drink in _catalog.Values.OrderBy(drink => Array.IndexOf(new[] { "R", "SR", "SSR" }, (string?)drink["rarity"]))
                         .ThenBy(drink => Array.IndexOf(new[] { "free", "sense", "logic", "anomaly" }, (string?)drink["plan"]))
                         .ThenBy(drink => (int)drink["id"]!))
            {
                var id = (int)drink["id"]!;
                var name = (string?)drink["name"] ?? "";
                if (!name.Contains(search.Text ?? "", StringComparison.OrdinalIgnoreCase)) continue;
                if (rarity.SelectedIndex > 0 && (string?)drink["rarity"] != rarity.SelectedItem as string) continue;
                if (plan.SelectedIndex > 0 && PlanName((string?)drink["plan"]) != plan.SelectedItem as string) continue;
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("55,*,Auto"), Margin = new Thickness(3) };
                var icon = DrinkImage(id, 48);
                if (icon != null) row.Children.Add(icon);
                var label = new TextBlock
                {
                    Text = $"{name}  ·  {(string?)drink["rarity"]}  ·  {PlanName((string?)drink["plan"])}",
                    VerticalAlignment = VerticalAlignment.Center
                };
                Grid.SetColumn(label, 1);
                row.Children.Add(label);
                var add = new Button { Content = selected.Contains(id) ? "已导入" : "导入", IsEnabled = !selected.Contains(id) };
                add.Click += (_, _) =>
                {
                    _profiles[profession].Add(new JObject { ["id"] = id });
                    MarkChanged();
                    if (_profession == profession) RenderRows();
                    Render();
                };
                Grid.SetColumn(add, 2);
                row.Children.Add(add);
                items.Children.Add(new Border { Child = row, BorderBrush = Brushes.DimGray, BorderThickness = new Thickness(1) });
            }
        }
        search.TextChanged += (_, _) => Render();
        rarity.SelectionChanged += (_, _) => Render();
        plan.SelectionChanged += (_, _) => Render();
        Render();
        var close = new Button { Content = "返回面板", HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
        close.Click += (_, _) => dialog.Close();
        Grid.SetRow(close, 2);
        root.Children.Add(close);
        dialog.Content = root;
        _ = dialog.ShowDialog(this);
    }

    private void MarkChanged()
    {
        _save.IsEnabled = true;
        _status.Text = "有未保存的饮料配置。";
    }

    private void Save()
    {
        try
        {
            var currentHash = File.Exists(_configPath)
                ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_configPath))) : "";
            if (currentHash != _originalHash)
            {
                _status.Text = "配置已被其他窗口修改。请重新打开面板。";
                return;
            }
            var profiles = new JObject();
            var defaults = new JObject();
            foreach (var name in Professions)
            {
                var cards = _profiles[name];
                var ids = cards.Select(card => (int?)card["id"] ?? 0).ToArray();
                if (ids.Any(id => !_catalog.ContainsKey(id)) || ids.Distinct().Count() != ids.Length ||
                    cards.Any(card => !ValidTiming(card["use_timing"], (int?)card["id"] == 22)
                        || card["purchase_enabled"] is { Type: not JTokenType.Boolean }
                        || card["disabled"] is { Type: not JTokenType.Boolean }) ||
                    cards.Any(card => !ValidBeforeTargets(card)) ||
                    !ValidDefaultTiming(_defaultTimings[name]))
                    throw new InvalidOperationException($"{name}的饮料或回合配置无效");
                profiles[name] = new JArray(cards.Select(card => card.DeepClone()));
                defaults[name] = _defaultTimings[name].DeepClone();
            }
            _config["profiles"] = profiles;
            _config["default_use_timing_profiles"] = defaults;
            var temporary = _configPath + ".tmp";
            File.WriteAllText(temporary, _config.ToString(Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(temporary, _configPath, true);
            _originalHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_configPath)));
            _save.IsEnabled = false;
            _status.Text = "饮料配置已保存；下次培育的领取、购买和本战使用将读取。";
            LoggerHelper.UserAction("保存 HIF 饮料配置",
                $"配置文件={_configPath}, 各职业条目/购买数={string.Join(',', Professions.Select(name => $"{name}:{_profiles[name].Count}/{_profiles[name].Count(card => (bool?)card["purchase_enabled"] == true)}"))}",
                operation: "HifDrinkEditor");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            _status.Text = $"保存失败：{error.Message}";
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifDrinkEditor");
            LoggerHelper.Error("保存 HIF 饮料配置失败", error);
        }
    }

    private bool ValidBeforeTargets(JObject entry)
    {
        if (entry["before_card_targets"] == null) return true;
        return (int?)entry["id"] == 25 && entry["before_card_targets"] is JArray targets
            && targets.All(t => t.Type == JTokenType.String && _activeCardKeys.Contains((string)t!))
            && targets.Values<string>().Distinct().Count() == targets.Count;
    }

    private static bool ValidTiming(JToken? timing, bool allowComboOnly = false)
    {
        if (timing == null) return true;
        if (timing is not JObject rule) return false;
        return (string?)rule["mode"] switch
        {
            "first_turn" => rule.Properties().Count() == 1,
            "combo_only" => allowComboOnly && rule.Properties().Count() == 1,
            "remaining_turn" => rule.Properties().Count() == 2
                && rule["turn"]?.Type == JTokenType.Integer
                && (int)rule["turn"]! is >= 1 and <= 99,
            _ => false
        };
    }

    private static bool ValidDefaultTiming(JToken? timing)
    {
        if (timing is not JObject rule) return false;
        return (string?)rule["mode"] == "never" && rule.Properties().Count() == 1
            || ValidTiming(rule);
    }
}
