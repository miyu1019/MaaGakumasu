using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
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

/// <summary>编辑与 Agent 同一数据目录下的 HIF 战斗出牌优先级。</summary>
public sealed class HifPriorityWindow : SukiWindow
{
    private static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    private readonly string _configPath = HifLayout.PersonalPath("cards_priority.json");
    private readonly string _imageRoot = HifLayout.ImageRoot;
    private readonly Dictionary<string, List<JObject>> _profiles = new();
    private readonly Dictionary<string, List<JObject>> _targetProfiles = new();
    private readonly Dictionary<string, List<JObject>> _swapOutProfiles = new();
    private readonly Dictionary<string, JObject> _useConditions = new();
    private readonly Dictionary<string, HashSet<string>> _noExtraTurns = new();
    private readonly Dictionary<string, JObject> _followups = new();
    private readonly Dictionary<string, int[]> _migrationChoices = new();
    private Dictionary<string, int[]> _legacyWaits = new();
    private readonly Dictionary<string, decimal> _unknownPriorities = new();
    private readonly HashSet<string> _changedProfessions = new();
    private readonly HashSet<string> _changedTargetProfessions = new();
    private readonly HashSet<string> _changedSwapOutProfessions = new();
    private readonly TextBlock _heading = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12) };
    private readonly StackPanel _rows = new() { Spacing = 7 };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _copy = new() { Content = "从集中复制卡表" };
    private readonly Button _import = new() { Content = "导入已有卡牌" };
    private readonly Button _targetModeButton = new() { Content = "优先获取卡" };
    private readonly Button _targetImport = new() { Content = "导入优先获取卡" };
    private readonly Button _swapOutModeButton = new() { Content = "优先换出卡" };
    private readonly Button _swapOutImport = new() { Content = "导入换出卡" };
    private readonly Button _save = new() { Content = "保存优先级", IsEnabled = false };
    private JObject _config = new();
    private JObject _images = new();
    private JObject _catalog = new();
    private JArray _targetCatalog = new();
    private readonly Dictionary<string, JObject> _conditionalPriorities = new();
    private string _originalHash = "";
    private readonly string _taskProfession;
    private string _profession = "集中";
    private int? _dragIndex;
    private bool _loaded;
    private bool _targetMode;
    private bool _swapOutMode;

    public HifPriorityWindow(string? initialProfession = null, bool initialTargetMode = false, bool initialSwapOutMode = false)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        _targetMode = initialTargetMode;
        _swapOutMode = initialSwapOutMode;
        UpdateModeLabels();
        Width = 940;
        Height = 670;
        MinWidth = 720;
        MinHeight = 450;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        BuildLayout();
        try
        {
            LoadConfig();
            _loaded = true;
            RenderCards();
            LoggerHelper.UserAction($"打开 {Title}", $"任务职业={_taskProfession}, 显示职业={_profession}, 配置文件={_configPath}", operation: "HifPriorityEditor");
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException)
        {
            _status.Text = $"无法加载 HIF 优先级：{error.Message}";
            _save.IsEnabled = false;
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
            LoggerHelper.Error("加载 HIF 战斗出牌优先级配置失败", error);
        }
    }

    private void BuildLayout()
    {
        var root = new Grid { Margin = new Thickness(18), RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto") };
        UpdateHeading();
        root.Children.Add(_heading);

        var selector = new ComboBox { ItemsSource = Professions, SelectedItem = _profession, Width = 145 };
        selector.SelectionChanged += (_, _) =>
        {
            _profession = selector.SelectedItem as string ?? "集中";
            UpdateHeading();
            if (_loaded) RenderCards();
        };
        _copy.Click += (_, _) =>
        {
            _profiles[_profession] = _profiles["集中"].Select(card => (JObject)card.DeepClone()).ToList();
            {
                var keys = _profiles[_profession].Select(card => (string?)card["key"]).ToHashSet();
                foreach (var key in _conditionalPriorities[_profession].Properties().Select(rule => rule.Name).Where(key => !keys.Contains(key)).ToArray())
                    _conditionalPriorities[_profession].Remove(key);
            }
            LoggerHelper.UserAction("复制 HIF 集中卡表", $"目标职业={_profession}, 卡片数={_profiles[_profession].Count}", operation: "HifPriorityEditor");
            MarkChanged();
            RenderCards();
        };
        var toolbar = new StackPanel { Spacing = 6, Margin = new Thickness(0, 0, 0, 10) };
        var professionRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        professionRow.Children.Add(new TextBlock { Text = "职业", VerticalAlignment = VerticalAlignment.Center });
        professionRow.Children.Add(selector);
        professionRow.Children.Add(_copy);
        toolbar.Children.Add(professionRow);
        var navigationRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        _import.Click += (_, _) => ShowCardImportDialog(false);
        navigationRow.Children.Add(_import);
        _targetModeButton.Click += (_, _) =>
        {
            _targetMode = !_targetMode;
            _swapOutMode = false;
            UpdateModeLabels();
            UpdateHeading();
            RenderCards();
        };
        navigationRow.Children.Add(_targetModeButton);
        _targetImport.Click += (_, _) => ShowCardImportDialog(true);
        navigationRow.Children.Add(_targetImport);
        _swapOutModeButton.Click += (_, _) =>
        {
            _swapOutMode = !_swapOutMode;
            _targetMode = false;
            UpdateModeLabels();
            UpdateHeading();
            RenderCards();
        };
        navigationRow.Children.Add(_swapOutModeButton);
        _swapOutImport.Click += (_, _) => ShowCardImportDialog(false, true);
        navigationRow.Children.Add(_swapOutImport);
        toolbar.Children.Add(navigationRow);
        Grid.SetRow(toolbar, 1);
        root.Children.Add(toolbar);

        var scroll = new ScrollViewer { Content = _rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(scroll, 2);
        root.Children.Add(scroll);

        var footer = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), Margin = new Thickness(0, 12, 0, 0) };
        footer.Children.Add(_status);
        _save.Click += (_, _) => SaveConfig();
        Grid.SetColumn(_save, 1);
        footer.Children.Add(_save);
        var close = new Button { Content = "关闭", Margin = new Thickness(8, 0, 0, 0) };
        close.Click += (_, _) => Close();
        Grid.SetColumn(close, 2);
        footer.Children.Add(close);
        Grid.SetRow(footer, 3);
        root.Children.Add(footer);
        Content = root;
    }

    private void UpdateModeLabels()
    {
        Title = _swapOutMode ? "HIF 授业优先换出卡配置" : _targetMode ? "HIF 优先获取卡配置" : "HIF 战斗出牌优先级配置";
        _targetModeButton.Content = _targetMode ? "返回出牌优先级" : "优先获取卡";
        _swapOutModeButton.Content = _swapOutMode ? "返回出牌优先级" : "优先换出卡";
    }

    private void UpdateHeading() => _heading.Text = _swapOutMode
        ? $"主界面职业：{_taskProfession}；正在编辑：{_profession} 的优先换出卡。名单从上到下依次优先；首次先找名单，第二次先找记录卡。保存后下次开始培育生效。"
        : _targetMode
            ? $"主界面职业：{_taskProfession}；正在编辑：{_profession} 的优先获取卡。用于第二次授业换入和支给领卡；候选仍按游戏中的出现顺序扫描。保存后下次开始培育生效。"
            : $"主界面职业：{_taskProfession}；正在编辑：{_profession} 的战斗出牌顺序。数字越小越优先；「后续卡」设置组合、等待回合与留卡方式；「使用限制」控制卡能否出牌，全力与強気职业可设置当前状态；「条件优先级」可按剩余回合区间改变各职业的出牌优先级。保存后下次开始培育生效。";

    private void LoadConfig()
    {
        var bytes = File.ReadAllBytes(_configPath);
        _originalHash = Convert.ToHexString(SHA256.HashData(bytes));
        _config = JObject.Parse(Encoding.UTF8.GetString(bytes));
        var profiles = _config["priority_profiles"] as JObject
            ?? throw new InvalidOperationException("缺少 priority_profiles");
        foreach (var name in Professions)
            _profiles[name] = (profiles[name] as JArray ?? throw new InvalidOperationException($"缺少职业 {name}"))
                .OfType<JObject>().Select(card => (JObject)card.DeepClone()).ToList();
        var targetProfiles = _config["preferred_acquisition_profiles"] as JObject
            ?? throw new InvalidOperationException("缺少 preferred_acquisition_profiles");
        foreach (var name in Professions)
            _targetProfiles[name] = (targetProfiles[name] as JArray ?? throw new InvalidOperationException($"缺少目标卡职业 {name}"))
                .Select(card => card is JObject obj ? (JObject)obj.DeepClone() : new JObject { ["name"] = (string?)card ?? "" }).ToList();
        var swapOutProfiles = _config["swap_out_priority_profiles"] as JObject;
        foreach (var name in Professions)
            _swapOutProfiles[name] = (swapOutProfiles?[name] as JArray ?? new JArray())
                .Values<string>().Where(card => !string.IsNullOrWhiteSpace(card))
                .Select(card => new JObject { ["name"] = card }).ToList();
        var unknown = _config["unknown_priority_profiles"] as JObject;
        foreach (var name in Professions)
            _unknownPriorities[name] = (decimal?)unknown?[name] ?? (name == "全力" ? 8 : 11);
        var useLimits = _config["use_condition_profiles"] as JObject;
        foreach (var name in Professions)
            _useConditions[name] = (JObject?)useLimits?[name]?.DeepClone() ?? new JObject();
        foreach (var name in Professions)
            _conditionalPriorities[name] = (JObject?)_config["conditional_priority_profiles"]?[name]?.DeepClone() ?? new JObject();
        foreach (var name in Professions)
            _noExtraTurns[name] = (_config["no_extra_turn_profiles"]?[name] as JArray ?? new JArray())
                .Values<string>().Where(key => !string.IsNullOrWhiteSpace(key)).Select(key => key!).ToHashSet();
        var manifest = HifLayout.CatalogPath("hif_priority_card_images.json");
        _images = JObject.Parse(File.ReadAllText(manifest, Encoding.UTF8));
        var catalog = HifLayout.CatalogPath("hif_priority_card_catalog.json");
        _catalog = JObject.Parse(File.ReadAllText(catalog, Encoding.UTF8));
        var targetCatalog = HifLayout.CatalogPath("hif_target_card_catalog.json");
        _targetCatalog = JArray.Parse(File.ReadAllText(targetCatalog, Encoding.UTF8));
        _legacyWaits = HifFollowupConfig.LegacyWaits();
        if (_config["followup_profiles"] != null && _config["followup_profiles"] is not JObject)
            throw new InvalidOperationException("followup_profiles 无效");
        foreach (var name in Professions)
        {
            var values = _legacyWaits.GetValueOrDefault(name, []);
            if (_config["followup_profiles"]?[name] != null && _config["followup_profiles"]?[name] is not JObject)
                throw new InvalidOperationException($"{name}组合配置无效");
            _followups[name] = _config["followup_profiles"]?[name] is JObject rules
                ? (JObject)rules.DeepClone() : HifFollowupConfig.LegacyRules(_config, name, values.Length == 1 ? values[0] : null);
            HifFollowupConfig.Validate(_followups[name], _catalog);
            if (_config["followup_profiles"]?[name] == null && values.Length > 1 && _followups[name].Count > 0)
                _migrationChoices[name] = values;
        }
        using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
        LoggerHelper.Info($"HIF 卡牌配置已加载：文件={_configPath}, 职业数={_profiles.Count}, 优先级条目数={_profiles.Values.Sum(cards => cards.Count)}, 优先获取条目数={_targetProfiles.Values.Sum(cards => cards.Count)}, 优先换出条目数={_swapOutProfiles.Values.Sum(cards => cards.Count)}, 可导入卡数={_targetCatalog.Count}");
    }

    private void RenderCards()
    {
        _rows.Children.Clear();
        var cards = _profiles[_profession];
        _targetModeButton.IsEnabled = _loaded;
        _swapOutModeButton.IsEnabled = _loaded;
        _targetImport.IsVisible = _targetMode;
        _swapOutImport.IsVisible = _swapOutMode;
        _copy.IsVisible = !_targetMode && !_swapOutMode && cards.Count == 0 && _profession != "集中";
        _import.IsVisible = !_targetMode && !_swapOutMode;
        if (_swapOutMode)
        {
            RenderSwapOutCards();
            return;
        }
        if (_targetMode)
        {
            RenderTargetCards();
            return;
        }
        RenderUnknownPriority();
        if (_migrationChoices.TryGetValue(_profession, out var waits))
        {
            var migration = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Margin = new Thickness(8) };
            migration.Children.Add(new TextBlock { Text = "旧任务等待值冲突，请选择迁移值", VerticalAlignment = VerticalAlignment.Center });
            var choice = new ComboBox { ItemsSource = waits, PlaceholderText = "选择等待回合", Width = 130 };
            var profession = _profession;
            choice.SelectionChanged += (_, _) =>
            {
                if (choice.SelectedItem is not int value) return;
                foreach (var rule in _followups[profession].Properties()) rule.Value["wait_turns"] = value;
                _migrationChoices.Remove(profession); MarkChanged(); RenderCards();
            };
            migration.Children.Add(choice); _rows.Children.Add(migration);
        }
        _import.IsEnabled = _loaded && _catalog.Properties().Any(entry => cards.All(card => (string?)card["key"] != entry.Name));
        foreach (var orphan in _followups[_profession].Properties().Where(p => cards.All(c => (string?)c["key"] != p.Name)))
        {
            var key = orphan.Name;
            var edit = new Button { Content = $"{key} → 后续卡（未配置普通优先级）", HorizontalAlignment = HorizontalAlignment.Left };
            edit.Click += (_, _) => ShowFollowupDialog(key);
            _rows.Children.Add(edit);
        }
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock
            {
                Text = "此职业目前没有独立卡表。可导入已有卡牌，或复制集中卡表后调整。",
                Margin = new Thickness(8, 16)
            });
            return;
        }
        for (var index = 0; index < cards.Count; index++)
        {
            var card = cards[index];
            var key = (string?)card["key"] ?? "";
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("35,74,*,100,34,34,50,100,80,72"), Margin = new Thickness(2) };
            var handle = new TextBlock
            {
                Text = "⋮⋮",
                FontSize = 21,
                Cursor = new Cursor(StandardCursorType.Hand),
                VerticalAlignment = VerticalAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Center
            };
            ToolTip.SetTip(handle, "按住拖到另一张卡上排序");
            var sourceIndex = index;
            handle.PointerPressed += (_, e) =>
            {
                if (!e.GetCurrentPoint(handle).Properties.IsLeftButtonPressed) return;
                _dragIndex = sourceIndex;
                e.Pointer.Capture(handle);
            };
            handle.PointerReleased += (_, e) =>
            {
                e.Pointer.Capture(null);
                if (_dragIndex is not { } from) return;
                _dragIndex = null;
                var y = e.GetPosition(_rows).Y;
                var target = _rows.Children.OfType<Border>().ToList().FindIndex(item => y >= item.Bounds.Top && y < item.Bounds.Bottom);
                if (target >= 0) MoveCard(from, target);
            };
            row.Children.Add(handle);

            var image = CardImage(key);
            if (image != null) { Grid.SetColumn(image, 1); row.Children.Add(image); }
            var label = new TextBlock { Text = key, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap };
            Grid.SetColumn(label, 2);
            row.Children.Add(label);

            var rank = new NumericUpDown
            {
                Value = (decimal?)card["priority"] ?? 0,
                Minimum = 0,
                Maximum = 9999,
                Increment = 0.5m,
                VerticalAlignment = VerticalAlignment.Center,
                Width = 88
            };
            rank.ValueChanged += (_, _) =>
            {
                if (rank.Value is { } value)
                {
                    card["priority"] = value;
                    MarkChanged();
                }
            };
            Grid.SetColumn(rank, 3);
            row.Children.Add(rank);
            var up = new Button { Content = "↑", Padding = new Thickness(6, 3) };
            up.Click += (_, _) => MoveCard(sourceIndex, sourceIndex - 1);
            Grid.SetColumn(up, 4);
            row.Children.Add(up);
            var down = new Button { Content = "↓", Padding = new Thickness(6, 3) };
            down.Click += (_, _) => MoveCard(sourceIndex, sourceIndex + 1);
            Grid.SetColumn(down, 5);
            row.Children.Add(down);
            var remove = new Button { Content = "删除", Padding = new Thickness(4, 3) };
            ToolTip.SetTip(remove, "只移除当前职业的优先级，卡牌识别和模板文件保留");
            remove.Click += (_, _) =>
            {
                cards.RemoveAt(sourceIndex);
                _conditionalPriorities[_profession].Remove(key);
                MarkChanged();
                RenderCards();
                LoggerHelper.UserAction("移除 HIF 优先级卡牌", $"职业={_profession}, 卡牌={key}", operation: "HifPriorityEditor");
            };
            Grid.SetColumn(remove, 6);
            row.Children.Add(remove);
            {
                var condition = new Button
                {
                    Content = _conditionalPriorities[_profession][key] is JObject ? "条件优先级✓" : "条件优先级",
                    Padding = new Thickness(4, 3)
                };
                ToolTip.SetTip(condition, "设置剩余回合区间触发的优先级；全力职业还可使用全力值条件");
                condition.Click += (_, _) => ShowConditionDialog(key, (decimal?)card["priority"] ?? 0);
                Grid.SetColumn(condition, 7);
                row.Children.Add(condition);
            }
            var turnLimit = _useConditions[_profession][key] as JArray;
            var use = new Button
            {
                Content = turnLimit is { Count: > 0 } || _noExtraTurns[_profession].Contains(key) ? "限制✓" : "使用限制",
                Padding = new Thickness(4, 3)
            };
            ToolTip.SetTip(use, "设置额外回合留卡及最多三组条件：组内同时满足，组间满足任一组。不改变出牌优先级");
            use.Click += (_, _) => ShowUseConditionDialog(key);
            Grid.SetColumn(use, 8);
            row.Children.Add(use);
            var followup = new Button { Content = _followups[_profession][key] is JObject ? "后续卡✓" : "后续卡", Padding = new Thickness(5, 3) };
            followup.Click += (_, _) => ShowFollowupDialog(key);
            Grid.SetColumn(followup, 9); row.Children.Add(followup);
            _rows.Children.Add(new Border
            {
                Child = row,
                BorderThickness = new Thickness(1),
                BorderBrush = Brushes.DimGray,
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(5)
            });
        }
    }

    private void RenderUnknownPriority()
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(8, 4, 8, 12) };
        row.Children.Add(new TextBlock { Text = "兜底优先级", Width = 105, VerticalAlignment = VerticalAlignment.Center });
        var rank = new NumericUpDown
        {
            Value = _unknownPriorities[_profession], Minimum = 0, Maximum = 9999,
            Increment = 0.5m, Width = 88
        };
        rank.ValueChanged += (_, _) =>
        {
            if (rank.Value is not { } value) return;
            _unknownPriorities[_profession] = value;
            MarkChanged();
        };
        row.Children.Add(rank);
        row.Children.Add(new TextBlock
        {
            Text = "用于未单独配置或暂未识别的卡牌", VerticalAlignment = VerticalAlignment.Center
        });
        _rows.Children.Add(row);
    }

    private Control? TargetImage(int id)
    {
        var path = Path.Combine(_imageRoot, "hif", "hif_card_icons", $"{id}.webp");
        if (!File.Exists(path)) return null;
        try { return Thumbnail(path); }
        catch (Exception error) when (error is IOException or NotSupportedException)
        {
            _status.Text = $"部分卡图无法显示：{error.Message}";
            return null;
        }
    }

    private static Control Thumbnail(string path) =>
        new Image { Source = new Bitmap(path), Width = 64, Height = 64, Stretch = Stretch.Fill };

    private void RenderTargetCards()
    {
        var cards = _targetProfiles[_profession];
        _targetImport.IsEnabled = _loaded && _targetCatalog.OfType<JObject>()
            .Any(card => cards.All(selected => (string?)selected["name"] != (string?)card["name"]));
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock { Text = "此职业尚未设置优先获取卡。点「导入优先获取卡」按卡面选择。", Margin = new Thickness(8, 16) });
            return;
        }
        for (var index = 0; index < cards.Count; index++)
        {
            var name = (string?)cards[index]["name"] ?? "";
            var entry = _targetCatalog.OfType<JObject>().FirstOrDefault(card => (string?)card["name"] == name);
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("74,*,70"), Margin = new Thickness(2) };
            var image = entry == null ? null : TargetImage((int?)entry["id"] ?? 0);
            if (image != null) row.Children.Add(image);
            else row.Children.Add(new TextBlock { Text = "无图", Width = 64, VerticalAlignment = VerticalAlignment.Center });
            var label = new TextBlock { Text = name, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(label, 1);
            row.Children.Add(label);
            var remove = new Button { Content = "删除" };
            var sourceIndex = index;
            remove.Click += (_, _) =>
            {
                cards.RemoveAt(sourceIndex);
                MarkChanged();
                RenderCards();
                LoggerHelper.UserAction("移除 HIF 优先获取卡", $"职业={_profession}, 卡名={name}", operation: "HifPriorityEditor");
            };
            Grid.SetColumn(remove, 2);
            row.Children.Add(remove);
            _rows.Children.Add(new Border
            {
                Child = row, BorderThickness = new Thickness(1), BorderBrush = Brushes.DimGray,
                CornerRadius = new CornerRadius(8), Padding = new Thickness(5)
            });
        }
    }

    private void RenderSwapOutCards()
    {
        var cards = _swapOutProfiles[_profession];
        _swapOutImport.IsEnabled = _loaded;
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock { Text = "此职业没有优先换出卡。导入后可用箭头调整顺序；空名单沿用基本卡规则。", Margin = new Thickness(8, 16) });
            return;
        }
        for (var index = 0; index < cards.Count; index++)
        {
            var name = (string?)cards[index]["name"] ?? "";
            var id = (int?)_images[name] ?? 0;
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("38,74,*,40,40,70"), Margin = new Thickness(2) };
            row.Children.Add(new TextBlock { Text = $"{index + 1}.", VerticalAlignment = VerticalAlignment.Center });
            var image = TargetImage(id);
            if (image != null) { Grid.SetColumn(image, 1); row.Children.Add(image); }
            var label = new TextBlock { Text = name, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(label, 2);
            row.Children.Add(label);
            var sourceIndex = index;
            var up = new Button { Content = "↑", IsEnabled = index > 0 };
            up.Click += (_, _) => MoveSwapOutCard(sourceIndex, sourceIndex - 1);
            Grid.SetColumn(up, 3);
            row.Children.Add(up);
            var down = new Button { Content = "↓", IsEnabled = index + 1 < cards.Count };
            down.Click += (_, _) => MoveSwapOutCard(sourceIndex, sourceIndex + 1);
            Grid.SetColumn(down, 4);
            row.Children.Add(down);
            var remove = new Button { Content = "删除" };
            remove.Click += (_, _) =>
            {
                cards.RemoveAt(sourceIndex);
                MarkChanged();
                RenderCards();
                LoggerHelper.UserAction("移除 HIF 优先换出卡", $"职业={_profession}, 卡名={name}", operation: "HifPriorityEditor");
            };
            Grid.SetColumn(remove, 5);
            row.Children.Add(remove);
            _rows.Children.Add(new Border
            {
                Child = row, BorderThickness = new Thickness(1), BorderBrush = Brushes.DimGray,
                CornerRadius = new CornerRadius(8), Padding = new Thickness(5)
            });
        }
    }

    private void MoveSwapOutCard(int from, int to)
    {
        var cards = _swapOutProfiles[_profession];
        if (from < 0 || to < 0 || from >= cards.Count || to >= cards.Count) return;
        (cards[from], cards[to]) = (cards[to], cards[from]);
        MarkChanged();
        RenderCards();
        LoggerHelper.UserAction("调整 HIF 优先换出顺序", $"职业={_profession}, 从第{from + 1}位到第{to + 1}位", operation: "HifPriorityEditor");
    }

    private void ShowCardImportDialog(bool targetMode, bool swapOutMode = false)
    {
        var profession = _profession;
        var selected = swapOutMode ? _swapOutProfiles[profession]
            : targetMode ? _targetProfiles[profession] : _profiles[profession];
        var candidates = _targetCatalog.OfType<JObject>().Select(card => (JObject)card.DeepClone()).ToList();
        if (!targetMode)
        {
            var names = candidates.Select(card => (string?)card["name"]).ToHashSet();
            foreach (var entry in _catalog.Properties().Where(entry => !names.Contains(entry.Name)))
                candidates.Add(new JObject
                {
                    ["name"] = entry.Name, ["id"] = (int?)_images[entry.Name] ?? 0,
                    ["rarity"] = "", ["plan"] = "", ["type"] = (string?)entry.Value["type"] ?? "", ["sourceType"] = ""
                });
        }
        var dialog = new SukiWindow
        {
            Title = $"导入{(swapOutMode ? "优先换出卡" : targetMode ? "优先获取卡" : "优先级卡牌")} - {profession}", Width = 760, Height = 690,
            MinWidth = 650, MinHeight = 460, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto"), Margin = new Thickness(12) };
        var search = new TextBox { Watermark = "输入卡名筛选；留空可翻页浏览全部卡牌" };
        var filters = new StackPanel { Spacing = 8, Margin = new Thickness(0, 0, 0, 8) };
        filters.Children.Add(search);
        var rarity = new ComboBox { ItemsSource = new[] { "稀有度：全部", "LR", "SSR", "SR", "R", "N" }, SelectedIndex = 0, Width = 125 };
        var plan = new ComboBox { ItemsSource = new[] { "计划：全部", "不限", "感性", "理性", "非凡" }, SelectedIndex = 0, Width = 125 };
        var category = new ComboBox { ItemsSource = new[] { "分类：全部", "A 卡", "M 卡", "T 卡" }, SelectedIndex = 0, Width = 125 };
        var origin = new ComboBox { ItemsSource = new[] { "来源：全部", "无", "偶像卡", "支援卡" }, SelectedIndex = 0, Width = 125 };
        var filterRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var filter in new[] { rarity, plan, category, origin }) filterRow.Children.Add(filter);
        filters.Children.Add(filterRow);
        root.Children.Add(filters);
        var rows = new StackPanel { Spacing = 5 };
        var scroll = new ScrollViewer { Content = rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);
        var footer = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Margin = new Thickness(0, 8, 0, 0) };
        var previous = new Button { Content = "上一页" };
        var next = new Button { Content = "下一页" };
        var count = new TextBlock { VerticalAlignment = VerticalAlignment.Center };
        footer.Children.Add(previous);
        footer.Children.Add(next);
        footer.Children.Add(count);
        footer.Children.Add(new TextBlock { Text = "导入后可继续选择；返回主面板保存", VerticalAlignment = VerticalAlignment.Center });
        Grid.SetRow(footer, 2);
        root.Children.Add(footer);
        const int pageSize = 40;
        var page = 0;
        bool IsSelected(string name) => selected.Any(card => (string?)card[targetMode || swapOutMode ? "name" : "key"] == name);
        bool MatchesOrigin(JObject card) => origin.SelectedIndex switch
        {
            1 => (string?)card["sourceType"] is "default" or "produce",
            2 => (string?)card["sourceType"] == "pIdol",
            3 => (string?)card["sourceType"] == "support",
            _ => true
        };
        void RenderPage(bool preserveScroll = false)
        {
            var previousOffset = scroll.Offset;
            var query = (search.Text ?? "").Trim();
            var filtered = candidates.Where(card => (string?)card["name"] is { } name
                    && name.Contains(query, StringComparison.OrdinalIgnoreCase)
                    && (rarity.SelectedIndex == 0 || (string?)card["rarity"] == new[] { "", "L", "SSR", "SR", "R", "N" }[rarity.SelectedIndex]
                        || rarity.SelectedIndex == 1 && (string?)card["rarity"] == "LR")
                    && (plan.SelectedIndex == 0 || (string?)card["plan"] == new[] { "", "free", "sense", "logic", "anomaly" }[plan.SelectedIndex])
                    && (category.SelectedIndex == 0 || (string?)card["type"] == new[] { "", "active", "mental", "trouble" }[category.SelectedIndex])
                    && MatchesOrigin(card))
                .OrderBy(card => (string?)card["type"] == "trouble" ? 0 : 1)
                .ThenBy(card => (string?)card["rarity"] switch { "N" => 0, "R" => 1, "SR" => 2, "SSR" => 3, "L" or "LR" => 4, _ => 5 })
                .ThenBy(card => (string?)card["sourceType"] switch { "default" or "produce" => 0, "pIdol" => 1, "support" => 2, _ => 3 })
                .ThenBy(card => (string?)card["type"] switch { "active" => 0, "mental" => 1, "trouble" => 2, _ => 3 })
                .ThenBy(card => (string?)card["plan"] switch { "free" => 0, "sense" => 1, "logic" => 2, "anomaly" => 3, _ => 4 })
                .ThenBy(card => (int?)card["id"] ?? int.MaxValue)
                .ToList();
            var totalPages = Math.Max(1, (filtered.Count + pageSize - 1) / pageSize);
            page = Math.Clamp(page, 0, totalPages - 1);
            rows.Children.Clear();
            if (filtered.Count == 0) rows.Children.Add(new TextBlock { Text = "没有符合条件的卡牌", Margin = new Thickness(8, 16) });
            var remaining = filtered.Count(card => !IsSelected((string)card["name"]!));
            foreach (var card in filtered.Skip(page * pageSize).Take(pageSize))
            {
                var name = (string)card["name"]!;
                var id = (int?)card["id"] ?? 0;
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("74,*,74"), Margin = new Thickness(2) };
                var image = TargetImage(id);
                if (image != null) row.Children.Add(image);
                else row.Children.Add(new TextBlock { Text = "无图", Width = 64, VerticalAlignment = VerticalAlignment.Center });
                var label = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
                label.Children.Add(new TextBlock { Text = name, TextWrapping = TextWrapping.Wrap });
                var template = (string?)_catalog[name]?["template"] ?? "";
                var hasTemplate = !string.IsNullOrEmpty(template) && File.Exists(Path.Combine(_imageRoot, template));
                var ocrName = (string?)_catalog[name]?["name"] ?? "";
                var overlap = !hasTemplate && !string.IsNullOrEmpty(ocrName) && _profiles[profession]
                    .Select(item => (string?)_catalog[(string?)item["key"] ?? ""]?["name"])
                    .Any(other => !string.IsNullOrEmpty(other)
                        && (ocrName.StartsWith(other, StringComparison.Ordinal) || other.StartsWith(ocrName, StringComparison.Ordinal)));
                if (!targetMode && !swapOutMode)
                    label.Children.Add(new TextBlock
                    {
                        Text = hasTemplate ? "有模板" : overlap ? "无模板 · OCR 前缀冲突" : "无模板 · 仅卡名 OCR",
                        FontSize = 12, Foreground = hasTemplate ? Brushes.Gray : Brushes.DarkOrange
                    });
                Grid.SetColumn(label, 1);
                row.Children.Add(label);
                var alreadyAdded = IsSelected(name);
                var add = new Button { Content = alreadyAdded ? "已加入" : "导入", IsEnabled = !alreadyAdded };
                add.Click += (_, _) =>
                {
                    if (IsSelected(name)) return;
                    if (targetMode || swapOutMode)
                        selected.Add(new JObject { ["name"] = name });
                    else
                    {
                        var priorityCard = (JObject)_catalog[name]!.DeepClone();
                        priorityCard["key"] = name;
                        priorityCard["priority"] = selected.Count == 0 ? 0 : selected.Max(item => (decimal?)item["priority"] ?? 0) + 1;
                        selected.Add(priorityCard);
                    }
                    MarkChanged();
                    RenderCards();
                    RenderPage(true);
                    LoggerHelper.UserAction(swapOutMode ? "导入 HIF 优先换出卡" : targetMode ? "导入 HIF 优先获取卡" : "导入 HIF 优先级卡牌",
                        $"职业={profession}, 卡牌={name}, 图像ID={id}, 识别模板={(hasTemplate ? "有" : "无")}", operation: "HifPriorityEditor");
                };
                Grid.SetColumn(add, 2);
                row.Children.Add(add);
                rows.Children.Add(row);
            }
            previous.IsEnabled = page > 0;
            next.IsEnabled = page + 1 < totalPages;
            count.Text = $"第 {page + 1}/{totalPages} 页；可导入 {remaining} 张";
            scroll.Offset = preserveScroll ? previousOffset : new Vector(0, 0);
        }
        search.TextChanged += (_, _) => { page = 0; RenderPage(); };
        foreach (var filter in new[] { rarity, plan, category, origin })
            filter.SelectionChanged += (_, _) => { page = 0; RenderPage(); };
        previous.Click += (_, _) => { page--; RenderPage(); };
        next.Click += (_, _) => { page++; RenderPage(); };
        dialog.Content = root;
        RenderPage();
        _ = dialog.ShowDialog(this);
    }

    private Control? CardImage(string key)
    {
        var id = (int?)_images[key];
        if (id == null) return null;
        var path = Path.Combine(_imageRoot, "hif", "hif_card_icons", $"{id}.webp");
        if (!File.Exists(path)) return null;
        try { return Thumbnail(path); }
        catch (Exception error) when (error is IOException or NotSupportedException)
        {
            _status.Text = $"部分卡图无法显示：{error.Message}";
            return null;
        }
    }

    private void ShowFollowupDialog(string key)
    {
        var profession = _profession;
        var window = new HifFollowupWindow(key, false, _followups[profession][key] as JObject, rule =>
        {
            var updated = (JObject)_followups[profession].DeepClone();
            if (rule == null) updated.Remove(key); else updated[key] = rule;
            HifFollowupConfig.Validate(updated, _catalog);
            _followups[profession] = updated;
            MarkChanged(); RenderCards();
        });
        _ = window.ShowDialog(this);
    }

    private void ShowUseConditionDialog(string key)
    {
        var profession = _profession;
        var saved = _useConditions[_profession][key] as JArray;
        var dialog = new SukiWindow
        {
            Title = $"卡牌使用限制 - {key}", Width = 600, Height = 430,
            MinWidth = 550, MinHeight = 390, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var body = new StackPanel { Spacing = 12, Margin = new Thickness(18) };
        body.Children.Add(new TextBlock
        {
            Text = $"职业：{_profession}。最多三组；每组的状态与回合条件同时满足，满足任一组即可使用。留空表示无限制；不改变优先级。无法确认的条件视为不满足。",
            TextWrapping = TextWrapping.Wrap
        });
        var noExtra = new CheckBox { Content = "有额外回合时不使用", IsChecked = _noExtraTurns[profession].Contains(key) };
        ToolTip.SetTip(noExtra, "独立于下方三组条件；左上角仍有待兑现的 +N 时保留此卡，+N 转入倒计时后恢复普通规则。标记未确认时也先保留。");
        body.Children.Add(noExtra);
        var turnChecks = new List<CheckBox>();
        var turnValues = new List<NumericUpDown>();
        var states = new List<ComboBox?>();
        for (var index = 0; index < 3; index++)
        {
            var clause = saved?.ElementAtOrDefault(index) as JObject;
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            row.Children.Add(new TextBlock { Text = $"条件 {index + 1}", Width = 55, VerticalAlignment = VerticalAlignment.Center });
            ComboBox? state = null;
            if (_profession is "全力" or "強気")
            {
                state = new ComboBox
                {
                    ItemsSource = new[] { "任意状态", "全力", "強気", "温存" },
                    SelectedItem = (string?)clause?["state"] ?? "任意状态",
                    Width = 105
                };
                row.Children.Add(state);
            }
            states.Add(state);
            var enabled = new CheckBox { Content = "剩余回合 ≤", IsChecked = clause?["remaining_turns_lte"] != null };
            var turns = new NumericUpDown
            {
                Value = (decimal?)clause?["remaining_turns_lte"] ?? 3,
                Minimum = 1, Maximum = 99, Increment = 1, Width = 85
            };
            row.Children.Add(enabled);
            row.Children.Add(turns);
            turnChecks.Add(enabled);
            turnValues.Add(turns);
            body.Children.Add(row);
        }
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        body.Children.Add(error);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var apply = new Button { Content = "应用到本次编辑" };
        apply.Click += (_, _) =>
        {
            var clauses = new JArray();
            for (var index = 0; index < 3; index++)
            {
                var clause = new JObject();
                var state = states[index]?.SelectedItem as string;
                if (state is "全力" or "強気" or "温存") clause["state"] = state;
                if (turnChecks[index].IsChecked == true)
                {
                    var value = turnValues[index].Value;
                    if (value == null || value.Value % 1 != 0)
                    {
                        error.Text = $"条件 {index + 1} 的剩余回合阈值必须是整数";
                        return;
                    }
                    clause["remaining_turns_lte"] = (int)value.Value;
                }
                if (clause.HasValues) clauses.Add(clause);
            }
            if (clauses.Count > 0)
                _useConditions[_profession][key] = clauses;
            else
                _useConditions[_profession].Remove(key);
            if (noExtra.IsChecked == true) _noExtraTurns[profession].Add(key); else _noExtraTurns[profession].Remove(key);
            MarkChanged();
            RenderCards();
            LoggerHelper.UserAction("设置 HIF 卡牌使用限制",
                $"职业={_profession}, 卡牌={key}, 条件={clauses.ToString(Formatting.None)}",
                operation: "HifPriorityEditor");
            dialog.Close();
        };
        buttons.Children.Add(apply);
        var cancel = new Button { Content = "取消" };
        cancel.Click += (_, _) => dialog.Close();
        buttons.Children.Add(cancel);
        body.Children.Add(buttons);
        dialog.Content = body;
        _ = dialog.ShowDialog(this);
    }

    private void ShowConditionDialog(string key, decimal baseRank)
    {
        var saved = _conditionalPriorities[_profession][key] as JObject;
        var fullPower = _profession == "全力";
        var legacyExample = fullPower && saved == null && key == "アッチェレランド+";
        var dialog = new SukiWindow
        {
            Title = $"条件优先级 - {_profession} - {key}", Width = 510, Height = fullPower ? 470 : 390,
            MinWidth = 440, MinHeight = 350, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var body = new StackPanel { Spacing = 12, Margin = new Thickness(18) };
        body.Children.Add(new TextBlock { Text = $"基础优先级：{baseRank}；仅在条件成立时使用下方数值。", TextWrapping = TextWrapping.Wrap });
        var rank = new NumericUpDown
        {
            Value = (decimal?)saved?["priority"] ?? (legacyExample ? 7 : baseRank),
            Minimum = 0, Maximum = 9999, Increment = 0.5m, Width = 90
        };
        var rankRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        rankRow.Children.Add(new TextBlock { Text = "生效优先级", Width = 120, VerticalAlignment = VerticalAlignment.Center });
        rankRow.Children.Add(rank);
        body.Children.Add(rankRow);

        var lowerEnabled = new CheckBox { Content = "剩余回合 ≥", IsChecked = saved?["remaining_turns_gte"] != null };
        var lower = new NumericUpDown { Value = (decimal?)saved?["remaining_turns_gte"] ?? 1, Minimum = 0, Maximum = 99, Increment = 1, Width = 90 };
        var lowerRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        lowerRow.Children.Add(lowerEnabled); lowerRow.Children.Add(lower); body.Children.Add(lowerRow);
        body.Children.Add(new TextBlock { Text = "回合上下限同时勾选时，只有落在区间内才生效。", TextWrapping = TextWrapping.Wrap, Foreground = Brushes.Gray });
        var turnsEnabled = new CheckBox { Content = "剩余回合 ≤", IsChecked = saved?["remaining_turns_lte"] != null || legacyExample };
        var turns = new NumericUpDown
        {
            Value = (decimal?)saved?["remaining_turns_lte"] ?? 4,
            Minimum = 0, Maximum = 99, Increment = 1, Width = 90
        };
        var turnsRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        turnsRow.Children.Add(turnsEnabled);
        turnsRow.Children.Add(turns);
        body.Children.Add(turnsRow);

        var powerEnabled = new CheckBox { Content = "全力值 <", IsChecked = saved?["full_power_lt"] != null || legacyExample };
        var power = new NumericUpDown
        {
            Value = (decimal?)saved?["full_power_lt"] ?? 10,
            Minimum = 0, Maximum = 999, Increment = 1, Width = 90
        };
        var powerRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        powerRow.Children.Add(powerEnabled);
        powerRow.Children.Add(power);
        if (fullPower) body.Children.Add(powerRow);

        var mode = new ComboBox
        {
            ItemsSource = new[] { "满足任一条件", "同时满足" },
            SelectedIndex = (string?)saved?["mode"] == "all" ? 1 : 0,
            Width = 160
        };
        var modeRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        modeRow.Children.Add(new TextBlock { Text = "条件关系", Width = 120, VerticalAlignment = VerticalAlignment.Center });
        modeRow.Children.Add(mode);
        if (fullPower) body.Children.Add(modeRow);
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        body.Children.Add(error);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var apply = new Button { Content = "应用到本次编辑" };
        apply.Click += (_, _) =>
        {
            if (lowerEnabled.IsChecked != true && turnsEnabled.IsChecked != true && !(fullPower && powerEnabled.IsChecked == true))
            {
                error.Text = "请至少勾选一个条件";
                return;
            }
            if (rank.Value is not { } value || (turnsEnabled.IsChecked == true && turns.Value == null)
                || (lowerEnabled.IsChecked == true && lower.Value == null)
                || (fullPower && powerEnabled.IsChecked == true && power.Value == null))
            {
                error.Text = "请填写完整的优先级和阈值";
                return;
            }
            if ((turnsEnabled.IsChecked == true && turns.Value!.Value % 1 != 0)
                || (lowerEnabled.IsChecked == true && lower.Value!.Value % 1 != 0)
                || (fullPower && powerEnabled.IsChecked == true && power.Value!.Value % 1 != 0))
            {
                error.Text = "回合和全力值阈值必须是整数";
                return;
            }
            if (lowerEnabled.IsChecked == true && turnsEnabled.IsChecked == true && lower.Value > turns.Value)
            {
                error.Text = "回合下限不能大于上限"; return;
            }
            var rule = new JObject { ["priority"] = value, ["mode"] = mode.SelectedIndex == 1 ? "all" : "any" };
            if (lowerEnabled.IsChecked == true) rule["remaining_turns_gte"] = (int)lower.Value!.Value;
            if (turnsEnabled.IsChecked == true) rule["remaining_turns_lte"] = (int)turns.Value!.Value;
            if (fullPower && powerEnabled.IsChecked == true) rule["full_power_lt"] = (int)power.Value!.Value;
            _conditionalPriorities[_profession][key] = rule;
            MarkChanged();
            RenderCards();
            LoggerHelper.UserAction("设置 HIF 条件优先级", $"职业={_profession}, 卡牌={key}, 规则={rule.ToString(Formatting.None)}", operation: "HifPriorityEditor");
            dialog.Close();
        };
        buttons.Children.Add(apply);
        if (saved != null)
        {
            var clear = new Button { Content = "清除条件规则" };
            clear.Click += (_, _) =>
            {
                _conditionalPriorities[_profession].Remove(key);
                MarkChanged();
                RenderCards();
                LoggerHelper.UserAction("清除 HIF 条件优先级", $"职业={_profession}, 卡牌={key}", operation: "HifPriorityEditor");
                dialog.Close();
            };
            buttons.Children.Add(clear);
        }
        body.Children.Add(buttons);
        dialog.Content = body;
        _ = dialog.ShowDialog(this);
    }

    private void MoveCard(int from, int to)
    {
        var cards = _profiles[_profession];
        if (from < 0 || to < 0 || from >= cards.Count || to >= cards.Count || from == to) return;
        var card = cards[from];
        cards.RemoveAt(from);
        cards.Insert(to, card);
        for (var index = 0; index < cards.Count; index++) cards[index]["priority"] = index;
        MarkChanged();
        RenderCards();
    }

    private void MarkChanged()
    {
        _changedProfessions.Add(_profession);
        if (_targetMode) _changedTargetProfessions.Add(_profession);
        if (_swapOutMode) _changedSwapOutProfessions.Add(_profession);
        _save.IsEnabled = true;
        _status.Text = $"未保存：{string.Join("、", _changedProfessions.OrderBy(name => Array.IndexOf(Professions, name)))}";
    }

    private void SaveConfig()
    {
        try
        {
            var currentHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_configPath)));
            if (currentHash != _originalHash)
            {
                _status.Text = "配置已被其他窗口修改。请关闭并重新打开面板。";
                using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
                LoggerHelper.Warning("HIF 优先级保存被拒绝：配置文件在编辑期间发生变化");
                return;
            }
            if (_migrationChoices.Count != 0)
                throw new InvalidOperationException("请先为以下职业选择旧等待迁移值：" + string.Join("、", _migrationChoices.Keys));
            var currentWaits = HifFollowupConfig.LegacyWaits();
            if (_legacyWaits.Count != currentWaits.Count || _legacyWaits.Any(p => !currentWaits.TryGetValue(p.Key, out var values) || !p.Value.SequenceEqual(values)))
                throw new InvalidOperationException("旧任务等待值已改变，请重新打开面板再迁移");
            var profiles = (JObject)_config["priority_profiles"]!;
            var targetProfiles = (JObject)_config["preferred_acquisition_profiles"]!;
            var swapOutProfiles = _config["swap_out_priority_profiles"] as JObject ?? new JObject();
            var useConditions = new JObject();
            var recognition = _config["recognition_profiles"] as JObject ?? new JObject();
            var unknown = _config["unknown_priority_profiles"] as JObject ?? new JObject();
            // 切换页签后仍保留各职业的修改，因此一次保存写入全部职业。
            foreach (var name in Professions)
            {
                HifFollowupConfig.Validate(_followups[name], _catalog);
                var keys = _profiles[name].Select(card => (string?)card["key"]).ToArray();
                if (keys.Any(string.IsNullOrWhiteSpace) || keys.Distinct().Count() != keys.Length)
                    throw new InvalidOperationException($"{name}有空白或重复卡名");
                var known = (recognition[name] as JArray)?.Values<string>() ?? Enumerable.Empty<string>();
                var followupKeys = _followups[name].Properties().Select(p => p.Name)
                    .Concat(_followups[name].Properties().SelectMany(p => ((JArray)p.Value["targets"]!).Values<string>()!));
                recognition[name] = new JArray(known.Concat(keys!).Concat(followupKeys).Where(key => !string.IsNullOrWhiteSpace(key)).Distinct());
                profiles[name] = new JArray(_profiles[name].OrderBy(card => (decimal?)card["priority"] ?? 0).Select(card => card.DeepClone()));
                unknown[name] = _unknownPriorities[name];
                var targetNames = _targetProfiles[name].Select(card => (string?)card["name"]).ToArray();
                if (targetNames.Any(string.IsNullOrWhiteSpace) || targetNames.Distinct().Count() != targetNames.Length)
                    throw new InvalidOperationException($"{name}有空白或重复目标卡名");
                targetProfiles[name] = new JArray(_targetProfiles[name].Select(card => card.DeepClone()));
                var swapOutNames = _swapOutProfiles[name].Select(card => (string?)card["name"]).ToArray();
                if (swapOutNames.Any(string.IsNullOrWhiteSpace) || swapOutNames.Distinct().Count() != swapOutNames.Length)
                    throw new InvalidOperationException($"{name}有空白或重复优先换出卡名");
                swapOutProfiles[name] = new JArray(swapOutNames);
                useConditions[name] = _useConditions[name].DeepClone();
            }
            _config["recognition_profiles"] = recognition;
            _config["followup_profiles"] = JObject.FromObject(_followups);
            _config.Remove("combo");
            _config.Remove("skip_limit");
            _config["unknown_priority_profiles"] = unknown;
            _config["swap_out_priority_profiles"] = swapOutProfiles;
            _config["use_condition_profiles"] = useConditions;
            var noExtraProfiles = _config["no_extra_turn_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) noExtraProfiles[name] = new JArray(_noExtraTurns[name].OrderBy(key => key, StringComparer.Ordinal));
            _config["no_extra_turn_profiles"] = noExtraProfiles;
            _config.Remove("use_turn_limit_profiles");
            _config.Remove("full_power_state_use_profiles");
            var conditional = _config["conditional_priority_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) conditional[name] = _conditionalPriorities[name].DeepClone();
            _config["conditional_priority_profiles"] = conditional;
            var backup = Path.Combine(Path.GetDirectoryName(_configPath)!, "cards_priority.before_visual_editor.json");
            if (!File.Exists(backup)) File.Copy(_configPath, backup);
            var temporary = _configPath + ".tmp";
            HifFollowupConfig.Backup(_configPath);
            var instances = Path.Combine(AppPaths.DataRoot, "config", "instances");
            foreach (var instance in Directory.Exists(instances) ? Directory.GetFiles(instances, "*.json") : []) HifFollowupConfig.Backup(instance);
            File.WriteAllText(temporary, _config.ToString(Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(temporary, _configPath, true);
            _originalHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_configPath)));
            HifFollowupConfig.RemoveLegacyWaits();
            _legacyWaits = HifFollowupConfig.LegacyWaits();
            foreach (var name in Professions)
                _profiles[name] = _profiles[name].OrderBy(card => (decimal?)card["priority"] ?? 0).ToList();
            RenderCards();
            _save.IsEnabled = false;
            var changed = string.Join("、", _changedProfessions.OrderBy(name => Array.IndexOf(Professions, name)));
            var targetChanged = string.Join("、", _changedTargetProfessions.OrderBy(name => Array.IndexOf(Professions, name)));
            var swapOutChanged = string.Join("、", _changedSwapOutProfessions.OrderBy(name => Array.IndexOf(Professions, name)));
            _status.Text = $"已保存：{changed}；卡片已按优先级排序，下次开始培育时生效";
            LoggerHelper.UserAction("保存 HIF 卡牌配置", $"任务职业={_taskProfession}, 修改职业={changed}, 优先获取修改职业={targetChanged}, 优先换出修改职业={swapOutChanged}, 配置文件={_configPath}, 优先级条目数={_profiles.Values.Sum(cards => cards.Count)}, 使用限制条目数={_useConditions.Values.Sum(rules => rules.Properties().Count())}, 优先获取条目数={_targetProfiles.Values.Sum(cards => cards.Count)}, 优先换出条目数={_swapOutProfiles.Values.Sum(cards => cards.Count)}", operation: "HifPriorityEditor");
            _changedProfessions.Clear();
            _changedTargetProfessions.Clear();
            _changedSwapOutProfessions.Clear();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            _status.Text = $"保存失败：{error.Message}";
            using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
            LoggerHelper.Error("保存 HIF 战斗出牌优先级失败", error);
        }
    }
}
