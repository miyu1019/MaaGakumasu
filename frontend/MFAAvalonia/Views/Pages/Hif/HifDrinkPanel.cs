using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Styling;
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

/// <summary>按职业编辑 HIF 饮料的获取顺序、购买范围和计划使用时机。</summary>
public sealed class HifDrinkPanel : HifStrategyPanel
{
    private readonly string _configPath = HifLayout.PersonalPath("hif_drink_profiles.json");
    private readonly string _catalogPath = HifLayout.CatalogPath("hif_drink_catalog.json");
    private readonly string _imageRoot = Path.Combine(HifLayout.ImageRoot, "hif", "hif_drink_icons");
    private readonly Dictionary<string, List<JObject>> _profiles = new();
    private readonly Dictionary<string, JObject> _defaultTimings = new();
    private readonly Dictionary<string, JObject> _firstBattlePolicies = new();
    private readonly Dictionary<int, JObject> _catalog = new();
    private readonly HashSet<string> _activeCardKeys = new();
    private readonly StackPanel _rows = new() { Spacing = 7 };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _save = new() { Classes = { "hif-drink-button" }, Content = "保存饮料配置", IsEnabled = false };
    private readonly string _taskProfession;
    private string _profession;
    private string _originalHash = "";
    private JObject _config = new();
    private bool _loaded;
    private readonly HifPanelHelp _help = new();

    public HifDrinkPanel(string? initialProfession = null)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        Title = "HIF 饮料配置";
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
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto") };
        root.Styles.Add(new Style(x => x.OfType<TextBlock>())
        {
            Setters = { new Setter(TextBlock.FontSizeProperty, 14d) }
        });
        root.Styles.Add(new Style(x => x.OfType<Button>().Class("hif-drink-button"))
        {
            Setters = { new Setter(Button.HeightProperty, 34d), new Setter(Button.MinHeightProperty, 34d),
                new Setter(Button.FontSizeProperty, 14d), new Setter(Button.PaddingProperty, new Thickness(10, 4)) }
        });
        root.Styles.Add(new Style(x => x.OfType<ComboBox>())
        {
            Setters = { new Setter(ComboBox.HeightProperty, 34d), new Setter(ComboBox.MinHeightProperty, 34d),
                new Setter(ComboBox.FontSizeProperty, 14d), new Setter(ComboBox.PaddingProperty, new Thickness(8, 4)) }
        });
        root.Styles.Add(new Style(x => x.OfType<NumericUpDown>())
        {
            Setters = { new Setter(NumericUpDown.HeightProperty, 34d), new Setter(NumericUpDown.MinHeightProperty, 34d),
                new Setter(NumericUpDown.FontSizeProperty, 14d) }
        });
        root.Styles.Add(new Style(x => x.OfType<CheckBox>())
        {
            Setters = { new Setter(CheckBox.HeightProperty, 34d), new Setter(CheckBox.MinHeightProperty, 34d),
                new Setter(CheckBox.FontSizeProperty, 14d) }
        });
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 12) };
        var help = new Button { Classes = { "hif-drink-button" }, Content = "使用说明", Width = 90 };
        help.Click += (_, _) => ShowHelp();
        toolbar.Children.Add(help);
        root.Children.Add(toolbar);
        var scroll = new ScrollViewer { Content = _rows };
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);
        _save.Click += (_, _) => Save();
        _save.MinWidth = 140;
        var footer = Footer(_status, _save);
        Grid.SetRow(footer, 2);
        root.Children.Add(footer);
        Content = root;
    }

    protected override Button SaveButton => _save;
    protected override void SaveCore() => Save();

    public override string Profession
    {
        get => _profession;
        set
        {
            if (!Professions.Contains(value) || value == _profession) return;
            _profession = value;
            if (_loaded) RenderRows();
        }
    }

    private void ShowHelp() => _help.Show(this, "HIF 饮料使用说明",
        "一战总上限\n所有种类的使用瓶数合计，跨回合、普通时机、组合技与关联卡牌共用计数。设为 2 时，喝两种各一瓶也达到上限。饮料栏容量 4 瓶，可设 0～4；0 表示一战不喝。默认不限。\n\n" +
        "默认使用时机\n用于未配置饮料，以及单项时机选择跟随默认的饮料。可选不自动使用、每场第1回合、剩余回合等于指定数值。\n\n" +
        "未配置饮料：一战使用\n仅作用于未导入名单的饮料，默认开启。关闭后为二战留存；未配置饮料二战默认使用。\n\n" +
        "未配置饮料：单种上限\n对未导入名单的各种饮料分别计数，同时受一战总上限限制。默认不限。\n\n" +
        "一战使用\n控制该种饮料一战是否自动使用。默认开启；关闭后保留原上限值，重新开启即可恢复。\n\n" +
        "一战单种上限\n1 表示一战最多使用同款一瓶；同时受一战总上限限制。默认不限，可设 0～4，0 表示一战不使用。\n\n" +
        "二战使用\n控制该种饮料二战是否自动使用，默认开启。按使用时机执行，不受一战瓶数上限限制。\n\n" +
        "使用时机\n跟随默认采用顶部默认时机；设单项时机时优先采用单项。剩余回合按精确相等触发，不在错过后补喝。\n\n" +
        "购买\n只有勾选的饮料参与商店购买，仍需开启相应任务选项。只关闭一战不影响购买；两战都关闭时暂停领取和购买，但保留购买勾选。\n\n" +
        "单独设置\n全部饮料一次列出。打开「单独设置」加入当前职业的领取名单，按单项规则购买和使用；关闭后移回「其他饮料」，按未配置饮料规则处理。保存前不写入配置。\n\n" +
        "上下移动\n列表从上到下为领取优先级，不决定同回合的饮料使用顺序。\n\n" +
        "只在组合技使用\n初星黒酢专用，不按普通时机使用，留给组合检索流程。\n\n" +
        "关联卡牌\n特製ハツボシエキス专用。原策略选中关联 A 卡时先喝一瓶再出牌，关联期间暂停普通时机，仍遵守一战开关和瓶数上限。\n\n" +
        "保存饮料配置\n按职业保存，下次培育读取。旧版不使用保留为两战都关闭。配置被其他窗口修改时提示重新打开。\n\n" +
        "上限生效范围\n限制本场自动使用，不改变持有数量或购买数量。场次无法确认时保留一战限制，确认二战后才放开。所持上限页由任务选项控制。");

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
            // 关联卡牌中的旧卡名换成目录正式名，保存时写回新名。
            HifCardRenames.Apply(_config);
        }
        else
            _config = new JObject { ["schema_version"] = 1, ["profiles"] = new JObject() };
        var profiles = _config["profiles"] as JObject ?? throw new InvalidOperationException("缺少职业饮料配置");
        var defaults = _config["default_use_timing_profiles"] as JObject;
        if (_config["first_battle_policy_profiles"] is { } policyToken && policyToken is not JObject)
            throw new InvalidOperationException("一战饮料配置结构无效");
        var policies = _config["first_battle_policy_profiles"] as JObject;
        foreach (var name in Professions)
        {
            var cards = (profiles[name] as JArray ?? new JArray()).OfType<JObject>()
                .Select(card => (JObject)card.DeepClone()).ToList();
            var ids = cards.Select(card => (int?)card["id"] ?? 0).ToArray();
            if (ids.Any(id => !_catalog.ContainsKey(id)) || ids.Distinct().Count() != ids.Length ||
                cards.Any(card => card["purchase_enabled"] is { Type: not JTokenType.Boolean }
                    || card["disabled"] is { Type: not JTokenType.Boolean }
                    || !ValidBattleUsage(card)
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
            var policy = policies?[name] as JObject ?? new JObject();
            if (policies?[name] != null && policies[name] is not JObject || !ValidFirstBattlePolicy(policy))
                throw new InvalidOperationException($"{name}的一战饮料配置无效");
            _firstBattlePolicies[name] = new JObject
            {
                ["enabled"] = (bool?)policy["enabled"] ?? true,
                ["max_uses"] = (int?)policy["max_uses"] ?? -1,
                ["unconfigured_max_uses"] = (int?)policy["unconfigured_max_uses"] ?? -1
            };
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
        var common = new StackPanel { Spacing = 4, Margin = new Thickness(8) };
        common.Children.Add(new TextBlock { Text = "本战通用设置", FontWeight = FontWeight.SemiBold });
        var policy = _firstBattlePolicies[profession];
        var totalRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        totalRow.Children.Add(new TextBlock { Text = "一战饮料总上限", VerticalAlignment = VerticalAlignment.Center });
        totalRow.Children.Add(UseLimit(policy, "max_uses"));
        var defaultRow = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 10,
        };
        defaultRow.Children.Add(new TextBlock
        {
            Text = "默认使用时机", VerticalAlignment = VerticalAlignment.Center
        });
        var defaultSelector = new ComboBox
        {
            ItemsSource = new[] { "不自动使用", "每场第1回合", "剩余回合=" },
            SelectedIndex = defaultMode == "never" ? 0 : defaultMode == "first_turn" ? 1 : 2,
            Width = 145
        };
        var defaultTurns = new NumericUpDown
        {
            Minimum = 1, Maximum = 99, Increment = 1, Width = 96,
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
        var commonRules = new WrapPanel { Orientation = Orientation.Horizontal };
        totalRow.Margin = new Thickness(0, 0, 24, 5);
        defaultRow.Margin = new Thickness(0, 0, 0, 5);
        commonRules.Children.Add(totalRow);
        commonRules.Children.Add(defaultRow);
        common.Children.Add(commonRules);
        ToolTip.SetTip(defaultRow, "用于未配置饮料及单项「跟随默认」；单项时机优先。");
        _rows.Children.Add(new Border { Name = "general", Child = common, BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(6) });
        var fallback = new StackPanel { Spacing = 4, Margin = new Thickness(8) };
        var limitsRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        limitsRow.Children.Add(new TextBlock { Text = "未配置饮料", FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        var (unconfiguredUse, unconfiguredEnabled) = LabeledSwitch("一战使用", (bool)policy["enabled"]!);
        var unconfiguredLimit = UseLimit(policy, "unconfigured_max_uses");
        unconfiguredLimit.IsEnabled = unconfiguredEnabled.IsChecked == true;
        unconfiguredEnabled.IsCheckedChanged += (_, _) =>
        {
            policy["enabled"] = unconfiguredEnabled.IsChecked == true;
            unconfiguredLimit.IsEnabled = unconfiguredEnabled.IsChecked == true;
            MarkChanged();
        };
        limitsRow.Children.Add(unconfiguredUse);
        limitsRow.Children.Add(new TextBlock { Text = "单种上限", VerticalAlignment = VerticalAlignment.Center });
        limitsRow.Children.Add(unconfiguredLimit);
        fallback.Children.Add(limitsRow);
        _rows.Children.Add(new Border { Name = "unconfigured", Child = fallback, BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(6) });
        var cards = _profiles[_profession];
        _rows.Children.Add(SectionHeader($"单独设置的饮料 · 领取优先级（{cards.Count}）",
            "从上到下依次领取；关闭「单独设置」移回下方，按未配置饮料规则处理。"));
        if (cards.Count == 0)
            _rows.Children.Add(new TextBlock { Text = "还没有单独设置的饮料。在下方打开某种饮料的「单独设置」即可加入。", Opacity = HifTheme.SecondaryOpacity, Margin = new Thickness(4, 0, 0, 4) });
        for (var index = 0; index < cards.Count; index++)
        {
            var card = cards[index];
            var id = (int)card["id"]!;
            var record = _catalog[id];
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("54,*,Auto"), RowDefinitions = new RowDefinitions("Auto,Auto"), RowSpacing = 8, Margin = new Thickness(10) };
            var icon = DrinkImage(id, 44);
            if (icon != null) row.Children.Add(icon);
            var title = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            title.Children.Add(new TextBlock { Text = $"#{index + 1}  {(string?)record["name"]}", FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
            title.Children.Add(new TextBlock { Text = $"{(string?)record["rarity"]} · {PlanName((string?)record["plan"])}", FontSize = 12, Opacity = HifTheme.SecondaryOpacity });
            Grid.SetColumn(title, 1);
            row.Children.Add(title);
            var (purchaseRow, purchase) = LabeledSwitch("购买", (bool?)card["purchase_enabled"] == true);
            purchase.IsCheckedChanged += (_, _) =>
            {
                card["purchase_enabled"] = purchase.IsChecked == true;
                MarkChanged();
            };
            var (firstRow, firstBattle) = LabeledSwitch("一战使用", (bool?)card["first_battle_enabled"] ?? (bool?)card["disabled"] != true);
            var (secondRow, secondBattle) = LabeledSwitch("二战使用", (bool?)card["second_battle_enabled"] ?? (bool?)card["disabled"] != true);
            purchase.IsEnabled = firstBattle.IsChecked == true || secondBattle.IsChecked == true;
            var switches = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4, VerticalAlignment = VerticalAlignment.Center };
            purchaseRow.Margin = new Thickness(0, 0, 10, 0);
            switches.Children.Add(purchaseRow);
            Grid.SetColumn(switches, 2);
            row.Children.Add(switches);
            var timing = card["use_timing"] as JObject;
            var mode = (string?)timing?["mode"];
            var selector = new ComboBox
            {
                ItemsSource = id == 22
                    ? new[] { "跟随默认", "每场第1回合", "剩余回合=", "只在组合技使用" }
                    : new[] { "跟随默认", "每场第1回合", "剩余回合=" },
                SelectedIndex = mode == "first_turn" ? 1 : mode == "remaining_turn" ? 2 : mode == "combo_only" ? 3 : 0,
                Width = 155, IsEnabled = purchase.IsEnabled && (card["before_card_targets"] as JArray)?.Count is not > 0,
                VerticalAlignment = VerticalAlignment.Center
            };
            var turns = new NumericUpDown
            {
                Minimum = 1, Maximum = 99, Increment = 1, Width = 96,
                Value = (decimal?)timing?["turn"] ?? 3,
                IsEnabled = selector.SelectedIndex == 2 && selector.IsEnabled,
                VerticalAlignment = VerticalAlignment.Center
            };
            void UpdateTurn()
            {
                turns.IsEnabled = selector.SelectedIndex == 2 && selector.IsEnabled;
                if (selector.SelectedIndex == 1)
                    card["use_timing"] = new JObject { ["mode"] = "first_turn" };
                else if (selector.SelectedIndex == 2)
                {
                    var value = turns.Value ?? 3;
                    card["use_timing"] = new JObject
                    {
                        ["mode"] = "remaining_turn", ["turn"] = value % 1 == 0 ? new JValue((int)value) : new JValue(value)
                    };
                }
                else if (id == 22 && selector.SelectedIndex == 3)
                    card["use_timing"] = new JObject { ["mode"] = "combo_only" };
                else
                    card.Remove("use_timing");
                MarkChanged();
            }
            selector.SelectionChanged += (_, _) => UpdateTurn();
            turns.ValueChanged += (_, _) => UpdateTurn();
            var battleRules = new WrapPanel { Orientation = Orientation.Horizontal };
            var reserve = new TextBlock { VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity };
            void UpdateReserve()
            {
                reserve.Text = secondBattle.IsChecked == true
                    ? firstBattle.IsChecked != true || (decimal?)card["first_battle_max_uses"] == 0 ? "为二战保留" : ""
                    : firstBattle.IsChecked != true ? "两战均关闭" : "";
                reserve.IsVisible = !string.IsNullOrEmpty(reserve.Text);
            }
            var bottleLimit = UseLimit(card, "first_battle_max_uses", UpdateReserve);
            bottleLimit.IsEnabled = firstBattle.IsChecked == true;
            UpdateReserve();
            var firstRules = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 16, 4) };
            firstRules.Children.Add(firstRow);
            firstRules.Children.Add(bottleLimit);
            battleRules.Children.Add(firstRules);
            var secondRules = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(0, 0, 16, 4) };
            secondRules.Children.Add(new Border { Width = 1, Height = 20, Background = HifTheme.LineBrush, VerticalAlignment = VerticalAlignment.Center });
            secondRules.Children.Add(secondRow);
            battleRules.Children.Add(secondRules);
            var timingRules = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 0, 0, 4) };
            timingRules.Children.Add(new TextBlock { Text = "时机", VerticalAlignment = VerticalAlignment.Center });
            timingRules.Children.Add(selector);
            timingRules.Children.Add(turns);
            var rightSettings = new StackPanel { Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
            rightSettings.Children.Add(timingRules);
            Grid.SetRow(rightSettings, 1); Grid.SetColumn(rightSettings, 2); row.Children.Add(rightSettings);
            title.Children.Add(reserve);
            Grid.SetRow(battleRules, 1); Grid.SetColumn(battleRules, 1); row.Children.Add(battleRules);
            void UpdateBattleUsage()
            {
                var excluded = firstBattle.IsChecked != true && secondBattle.IsChecked != true;
                card["first_battle_enabled"] = firstBattle.IsChecked == true;
                card["second_battle_enabled"] = secondBattle.IsChecked == true;
                card["disabled"] = excluded;
                purchase.IsEnabled = !excluded;
                bottleLimit.IsEnabled = firstBattle.IsChecked == true;
                UpdateReserve();
                selector.IsEnabled = !excluded && (card["before_card_targets"] as JArray)?.Count is not > 0;
                turns.IsEnabled = selector.IsEnabled && selector.SelectedIndex == 2;
                MarkChanged();
            }
            firstBattle.IsCheckedChanged += (_, _) => UpdateBattleUsage();
            secondBattle.IsCheckedChanged += (_, _) => UpdateBattleUsage();
            var sourceIndex = index;
            var up = new Button { Classes = { "hif-drink-button", "Basic" }, Content = "↑", Width = 34, Padding = new Thickness(4) };
            up.Click += (_, _) => Move(sourceIndex, sourceIndex - 1);
            up.IsEnabled = index > 0;
            switches.Children.Add(up);
            var down = new Button { Classes = { "hif-drink-button", "Basic" }, Content = "↓", Width = 34, Padding = new Thickness(4) };
            down.Click += (_, _) => Move(sourceIndex, sourceIndex + 1);
            down.IsEnabled = index < cards.Count - 1;
            switches.Children.Add(down);
            var (ownRow, own) = LabeledSwitch("单独设置", true);
            ownRow.Margin = new Thickness(10, 0, 0, 0);
            ToolTip.SetTip(ownRow, "关闭后移出领取名单，按未配置饮料规则处理；保存前可重新打开。");
            own.IsCheckedChanged += (_, _) =>
            {
                if (own.IsChecked == true) return;
                cards.RemoveAt(sourceIndex);
                MarkChanged();
                Dispatcher.UIThread.Post(RenderRows);
            };
            switches.Children.Add(ownRow);
            turns.IsVisible = selector.SelectedIndex == 2;
            selector.SelectionChanged += (_, _) => turns.IsVisible = selector.SelectedIndex == 2;
            if (id == 25)
            {
                var targets = card["before_card_targets"] as JArray;
                timingRules.IsVisible = targets is not { Count: > 0 };
                var associate = new Button { Classes = { "hif-drink-button" }, Content = targets is { Count: > 0 } ? "关联卡牌✓" : "关联卡牌", Width = 116, IsEnabled = purchase.IsEnabled };
                associate.HorizontalAlignment = HorizontalAlignment.Left;
                ToolTip.SetTip(associate, "关联 A 卡后，在目标出牌前使用，暂停普通时机；仍受一战开关、单种和总上限限制。");
                firstBattle.IsCheckedChanged += (_, _) => associate.IsEnabled = purchase.IsEnabled;
                secondBattle.IsCheckedChanged += (_, _) => associate.IsEnabled = purchase.IsEnabled;
                associate.Click += (_, _) =>
                {
                    var saved = targets == null ? null : new JObject { ["targets"] = targets.DeepClone() };
                    var dialog = new HifFollowupWindow((string?)record["name"] ?? "", true, saved, rule =>
                    {
                        if (rule == null) card.Remove("before_card_targets"); else card["before_card_targets"] = rule["targets"]!.DeepClone();
                        MarkChanged(); RenderRows();
                    });
                    _ = dialog.ShowDialog(OwnerWindow);
                };
                rightSettings.Children.Add(associate);
            }
            _rows.Children.Add(new Border { Child = row, BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10) });
        }
        RenderOtherDrinks(cards);
    }

    /// <summary>其余饮料一次全部列出；打开「单独设置」即加入领取名单末尾。</summary>
    private void RenderOtherDrinks(List<JObject> cards)
    {
        var configured = cards.Select(card => (int)card["id"]!).ToHashSet();
        var others = _catalog.Values.Where(drink => !configured.Contains((int)drink["id"]!))
            .OrderBy(drink => Array.IndexOf(new[] { "SSR", "SR", "R" }, (string?)drink["rarity"]))
            .ThenBy(drink => Array.IndexOf(new[] { "free", "sense", "logic", "anomaly" }, (string?)drink["plan"]))
            .ThenBy(drink => (int)drink["id"]!).ToList();
        _rows.Children.Add(SectionHeader($"其他饮料 · 按未配置饮料规则（{others.Count}）",
            "领取和使用按上方「未配置饮料」设置，不参与商店购买。打开「单独设置」可单独安排。"));
        var grid = new WrapPanel { Orientation = Orientation.Horizontal, Name = "others" };
        foreach (var drink in others)
        {
            var id = (int)drink["id"]!;
            var body = new DockPanel { LastChildFill = true };
            var (ownRow, own) = LabeledSwitch("单独设置", false);
            ownRow.Margin = new Thickness(6, 0, 0, 0);
            DockPanel.SetDock(ownRow, Dock.Right);
            body.Children.Add(ownRow);
            if (DrinkImage(id, 36) is { } icon)
            {
                icon.Margin = new Thickness(0, 0, 8, 0);
                DockPanel.SetDock(icon, Dock.Left);
                body.Children.Add(icon);
            }
            var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            text.Children.Add(new TextBlock { Text = (string?)drink["name"] ?? "", TextTrimming = TextTrimming.CharacterEllipsis });
            text.Children.Add(new TextBlock { Text = $"{(string?)drink["rarity"]} · {PlanName((string?)drink["plan"])}", FontSize = 12, Opacity = HifTheme.SecondaryOpacity });
            body.Children.Add(text);
            own.IsCheckedChanged += (_, _) =>
            {
                if (own.IsChecked != true || cards.Any(card => (int)card["id"]! == id)) return;
                cards.Add(new JObject { ["id"] = id });
                MarkChanged();
                LoggerHelper.UserAction("HIF 饮料单独设置", $"职业={_profession}, 饮料={(string?)drink["name"]}, ID={id}", operation: "HifDrinkEditor");
                Dispatcher.UIThread.Post(RenderRows);
            };
            grid.Children.Add(new Border
            {
                Child = body, Width = 296, Margin = new Thickness(0, 0, 8, 8), Padding = new Thickness(10, 8),
                BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10)
            });
        }
        _rows.Children.Add(grid);
    }

    private static StackPanel SectionHeader(string title, string hint)
    {
        var header = new StackPanel { Spacing = 2, Margin = new Thickness(3, 12, 0, 2) };
        header.Children.Add(new TextBlock { Text = title, FontWeight = FontWeight.SemiBold });
        header.Children.Add(new TextBlock { Text = hint, FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
        return header;
    }

    /// <summary>左侧文字、右侧开关。开关的 Tag 为文字，便于验证工具查找。</summary>
    private static (StackPanel Row, ToggleSwitch Switch) LabeledSwitch(string label, bool value)
    {
        var toggle = new ToggleSwitch { IsChecked = value, Tag = label, VerticalAlignment = VerticalAlignment.Center };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
        row.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
        row.Children.Add(toggle);
        return (row, toggle);
    }

    private ComboBox UseLimit(JObject rule, string field, Action? changed = null)
    {
        var selector = new ComboBox
        {
            Name = field, ItemsSource = new[] { "不限量", "最多0瓶", "最多1瓶", "最多2瓶", "最多3瓶", "最多4瓶" },
            SelectedIndex = ((int?)rule[field] ?? -1) + 1, Width = 110,
            VerticalAlignment = VerticalAlignment.Center
        };
        selector.SelectionChanged += (_, _) =>
        {
            if (selector.SelectedIndex < 0) return;
            rule[field] = selector.SelectedIndex - 1;
            changed?.Invoke();
            MarkChanged();
        };
        ToolTip.SetTip(selector, "一战使用上限；最多0瓶表示一战不喝。关闭一战开关时保留设置，二战不受此限额限制。");
        return selector;
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
            var policies = new JObject();
            foreach (var name in Professions)
            {
                var cards = _profiles[name];
                var ids = cards.Select(card => (int?)card["id"] ?? 0).ToArray();
                if (ids.Any(id => !_catalog.ContainsKey(id)) || ids.Distinct().Count() != ids.Length ||
                    cards.Any(card => !ValidTiming(card["use_timing"], (int?)card["id"] == 22)
                        || card["purchase_enabled"] is { Type: not JTokenType.Boolean }
                        || card["disabled"] is { Type: not JTokenType.Boolean }) ||
                    cards.Any(card => !ValidBattleUsage(card)) ||
                    !ValidFirstBattlePolicy(_firstBattlePolicies[name]) ||
                    cards.Any(card => !ValidBeforeTargets(card)) ||
                    !ValidDefaultTiming(_defaultTimings[name]))
                    throw new InvalidOperationException($"{name}的饮料或回合配置无效");
                foreach (var card in cards)
                {
                    var first = (bool?)card["first_battle_enabled"] ?? (bool?)card["disabled"] != true;
                    var second = (bool?)card["second_battle_enabled"] ?? (bool?)card["disabled"] != true;
                    card["first_battle_enabled"] = first;
                    card["second_battle_enabled"] = second;
                    card["disabled"] = !first && !second;
                }
                profiles[name] = new JArray(cards.Select(card => card.DeepClone()));
                defaults[name] = _defaultTimings[name].DeepClone();
                policies[name] = _firstBattlePolicies[name].DeepClone();
            }
            _config["profiles"] = profiles;
            _config["default_use_timing_profiles"] = defaults;
            _config["first_battle_policy_profiles"] = policies;
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

    private static bool ValidUseLimit(JToken? value) => value == null
        || value.Type == JTokenType.Integer && (long)value is >= -1 and <= 4;

    private static bool ValidBattleUsage(JObject entry) => ValidUseLimit(entry["first_battle_max_uses"])
        && entry["first_battle_enabled"] is not { Type: not JTokenType.Boolean }
        && entry["second_battle_enabled"] is not { Type: not JTokenType.Boolean };

    private static bool ValidFirstBattlePolicy(JObject policy) => ValidUseLimit(policy["max_uses"])
        && ValidUseLimit(policy["unconfigured_max_uses"])
        && policy["enabled"] is not { Type: not JTokenType.Boolean };

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
