using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Threading;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
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
using System.Threading.Tasks;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>编辑与 Agent 同一数据目录下的 HIF 战斗出牌优先级。</summary>
public sealed partial class HifPriorityPanel : HifStrategyPanel
{
    private readonly string _configPath = HifLayout.PersonalPath("cards_priority.json");
    private readonly string _imageRoot = HifLayout.ImageRoot;
    private readonly Dictionary<string, List<JObject>> _profiles = new();
    private readonly Dictionary<string, List<JObject>> _targetProfiles = new();
    private readonly Dictionary<string, List<JObject>> _swapOutProfiles = new();
    private readonly Dictionary<string, JObject> _swapOutOptions = new();
    private readonly Dictionary<string, JObject> _useConditions = new();
    private readonly Dictionary<string, HashSet<string>> _noExtraTurns = new();
    private readonly Dictionary<string, HashSet<string>> _holdCards = new();
    private readonly Dictionary<string, bool> _holdOrdered = new();
    private readonly Dictionary<string, decimal> _unknownPriorities = new();
    private readonly Dictionary<string, JObject> _typePreferences = new();
    private readonly HashSet<string> _changedProfessions = new();
    private readonly HashSet<string> _changedTargetProfessions = new();
    private readonly HashSet<string> _changedSwapOutProfessions = new();
    private readonly StackPanel _rows = new() { Spacing = 7 };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _copy = new() { Content = "从集中复制卡表" };
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
    // 出牌优先级：左侧卡表，右侧所选卡牌的详情（条件优先级、使用限制、出牌编排摘要）。
    private string? _selectedKey;
    private readonly StackPanel _detail = new() { Spacing = 12, Margin = new Thickness(0, 0, 8, 0) };
    private readonly ScrollViewer _detailScroll = new() { VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
    private readonly Grid _body = new() { ColumnDefinitions = new ColumnDefinitions("*,0,0") };
    // 职业通用设置（兜底优先级、未配置卡偏好、保留）横跨两栏，放在卡表和详情上方。
    private readonly WrapPanel _settings = new() { Orientation = Orientation.Horizontal };
    private readonly GridSplitter _splitter = new() { Width = 8, ResizeDirection = GridResizeDirection.Columns, Background = Brushes.Transparent, IsVisible = false };
    private string _detailLayout = "";
    private Grid? _ruleGrid;
    private bool? _ruleSideBySide;
    private bool _loaded;
    private bool _swapOutMode;

    public HifPriorityPanel(string? initialProfession = null, bool initialSwapOutMode = false)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        _swapOutMode = initialSwapOutMode;
        UpdateModeLabels();
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
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto") };
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
        // 加卡在「卡库」，职业配置导入导出与使用说明在页面顶部；这里只保留空卡表时的「从集中复制卡表」。
        var toolbar = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 6) };
        toolbar.Children.Add(_copy);
        toolbar.Bind(IsVisibleProperty, _copy.GetObservable(IsVisibleProperty));
        root.Children.Add(toolbar);

        var scroll = new ScrollViewer { Content = _rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        _body.Children.Add(scroll);
        Grid.SetColumn(_splitter, 1);
        _body.Children.Add(_splitter);
        _detailScroll.Content = _detail;
        _detailScroll.SizeChanged += (_, _) => ArrangeRules();
        Grid.SetColumn(_detailScroll, 2);
        _body.Children.Add(_detailScroll);
        var main = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), RowSpacing = 6 };
        main.Children.Add(_settings);
        Grid.SetRow(_body, 1);
        main.Children.Add(_body);
        Grid.SetRow(main, 1);
        root.Children.Add(main);

        _save.Click += (_, _) => SaveConfig();
        var footer = Footer(_status, _save);
        Grid.SetRow(footer, 2);
        root.Children.Add(footer);
        Content = root;
    }

    protected override Button SaveButton => _save;
    protected override void SaveCore() => SaveConfig();

    public override string Profession
    {
        get => _profession;
        set
        {
            if (!Professions.Contains(value) || value == _profession) return;
            _profession = value;
            _pick = null;
            if (_loaded) RenderCards();
        }
    }

    private JObject ShareProfession(string profession)
    {
        return new JObject
        {
            ["format"] = HifPriorityShare.Format, ["version"] = 1, ["profession"] = profession,
            ["priority"] = new JArray(_profiles[profession].OrderBy(c => (decimal?)c["priority"] ?? 0).Select(c => c.DeepClone())),
            ["unknown_priority"] = _unknownPriorities[profession],
            ["unconfigured_card_preference"] = _typePreferences[profession].DeepClone(),
            ["recognition"] = _config["recognition_profiles"]?[profession]?.DeepClone() ?? new JArray(),
            ["use_conditions"] = _useConditions[profession].DeepClone(),
            ["conditional_priorities"] = _conditionalPriorities[profession].DeepClone(),
            ["play_sequences"] = ShareSequences(profession),
            ["no_extra_turn"] = new JArray(_noExtraTurns[profession].OrderBy(k => k, StringComparer.Ordinal)),
            ["hold_priority"] = HoldPriority(profession)
        };
    }

    /// <summary>分享用的出牌编排：去掉没有内容的编排，校验由 HifPriorityShare 完成。</summary>
    private JObject ShareSequences(string profession)
    {
        var rules = (JObject)_sequences[profession].DeepClone();
        foreach (var empty in rules.Properties().Where(rule => HifPlaySequenceConfig.IsEmpty((JObject)rule.Value)).Select(rule => rule.Name).ToArray())
            rules.Remove(empty);
        return rules;
    }

    private static readonly FilePickerFileType ShareFileType = new("HIF 职业配置") { Patterns = ["*.json"] };

    /// <summary>导出当前职业的战斗配置（页面顶部「职业配置」菜单）。</summary>
    public Task ExportProfessionAsync() => ExportProfession();

    /// <summary>导入职业配置到当前职业的编辑数据（页面顶部「职业配置」菜单）。</summary>
    public Task ImportProfessionAsync() => ImportProfession();

    private async Task ExportProfession()
    {
        if (!_loaded) return;
        var profession = _profession;
        try
        {
            var data = HifPriorityShare.Validate(ShareProfession(profession), profession, _catalog);
            var file = await OwnerWindow.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
            {
                Title = $"导出 {profession} 的 HIF 职业配置", SuggestedFileName = $"HIF-优先级-{profession}.json",
                DefaultExtension = "json", FileTypeChoices = [ShareFileType], ShowOverwritePrompt = true
            });
            if (file == null) return;
            using (file)
            await using (var stream = await file.OpenWriteAsync())
            {
                var bytes = new UTF8Encoding(false).GetBytes(data.ToString(Formatting.Indented) + "\n");
                stream.SetLength(0);
                await stream.WriteAsync(bytes);
            }
            _status.Text = $"已导出 {profession} 的当前编辑配置，可分享给其他人";
            LoggerHelper.UserAction("导出 HIF 职业配置", $"职业={profession}", operation: "HifPriorityEditor");
        }
        catch (Exception error) when (IsShareError(error))
        {
            _status.Text = $"导出失败：{error.Message}";
        }
    }

    private async Task ImportProfession()
    {
        if (!_loaded) return;
        var profession = _profession;
        try
        {
            var files = await OwnerWindow.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = $"导入 {profession} 的 HIF 职业配置", AllowMultiple = false, FileTypeFilter = [ShareFileType]
            });
            if (files.Count == 0) return;
            string content;
            using (files[0])
            await using (var stream = await files[0].OpenReadAsync())
            using (var reader = new StreamReader(stream, Encoding.UTF8))
                content = await reader.ReadToEndAsync();
            var parsed = JObject.Parse(content, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
            var oldFollowups = parsed["followups"] != null;
            var data = HifPriorityShare.Validate(parsed, profession, _catalog);
            if (_profession != profession) throw new InvalidOperationException("职业已切换，请重新导入");
            var dialog = new SukiWindow
            {
                Title = $"导入职业配置 - {profession}", Width = 530, Height = 300,
                WindowStartupLocation = WindowStartupLocation.CenterOwner
            };
            var body = new StackPanel { Spacing = 16, Margin = new Thickness(18) };
            body.Children.Add(new TextBlock
            {
                Text = $"将替换 {profession} 的出牌优先级、出牌编排及相关卡牌规则。\n出牌卡：{((JArray)data["priority"]!).Count}；出牌编排：{((JObject)data["play_sequences"]!).Count} 条。" +
                       (oldFollowups ? "\n文件中的旧版后续卡组合不会导入，需要在出牌编排中重新设置。" : "") +
                       "\n优先获取和换出名单、其他职业、饮料和定制配置保持原样。应用后可检查或调整，点击页面顶部「保存」才写入配置。",
                TextWrapping = TextWrapping.Wrap
            });
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            var apply = new Button { Content = "替换本职业的编辑配置" };
            apply.Click += (_, _) =>
            {
                ApplyProfession(data, profession);
                dialog.Close();
            };
            buttons.Children.Add(apply);
            var cancel = new Button { Content = "取消" };
            cancel.Click += (_, _) => dialog.Close();
            buttons.Children.Add(cancel);
            body.Children.Add(buttons);
            dialog.Content = body;
            await dialog.ShowDialog(OwnerWindow);
        }
        catch (Exception error) when (IsShareError(error))
        {
            _status.Text = $"导入失败：{error.Message}";
        }
    }

    private void ApplyProfession(JObject data, string profession)
    {
        // 完整校验成功后才修改编辑状态，失败时保留全部已有配置。
        data = HifPriorityShare.Validate(data, profession, _catalog);
        if (_profession != profession) throw new InvalidOperationException("职业已切换，请重新导入");
        _profiles[profession] = ((JArray)data["priority"]!).OfType<JObject>().ToList();
        _unknownPriorities[profession] = (decimal)data["unknown_priority"]!;
        _typePreferences[profession] = (JObject)data["unconfigured_card_preference"]!;
        _useConditions[profession] = (JObject)data["use_conditions"]!;
        _conditionalPriorities[profession] = (JObject)data["conditional_priorities"]!;
        var sequences = (JObject)data["play_sequences"]!;
        foreach (var rule in sequences.Properties()) HifPlaySequenceConfig.Normalize((JObject)rule.Value);
        _sequences[profession] = sequences;
        _noExtraTurns[profession] = ((JArray)data["no_extra_turn"]!).Values<string>().Select(k => k!).ToHashSet();
        var hold = (JObject)data["hold_priority"]!;
        _holdOrdered[profession] = (bool)hold["ordered"]!;
        _holdCards[profession] = ((JArray)hold["cards"]!).Values<string>().Select(k => k!).ToHashSet();
        var recognition = _config["recognition_profiles"] as JObject ?? new JObject();
        recognition[profession] = data["recognition"]!.DeepClone();
        _config["recognition_profiles"] = recognition;
        MarkChanged();
        RenderCards();
        _status.Text = $"已导入 {profession}，请检查后保存；其他职业的未保存修改仍保留";
        LoggerHelper.UserAction("导入 HIF 职业配置到编辑面板", $"职业={profession}", operation: "HifPriorityEditor");
    }

    private static bool IsShareError(Exception error) => error is IOException or UnauthorizedAccessException
        or JsonException or InvalidOperationException or ArgumentException or FormatException or OverflowException or NotSupportedException;

    private void UpdateModeLabels()
    {
        Title = _sequenceMode ? "HIF 出牌编排" : _swapOutMode ? "HIF 授业优先换出卡配置" : "HIF 战斗出牌优先级配置";
    }

    /// <summary>切换显示的内容（出牌优先级、授业换出卡或出牌编排）。同属一个配置文件，切换不丢失未保存的修改，保存时一并写入。</summary>
    public void SetMode(bool swapOutMode, bool sequenceMode = false)
    {
        if (_swapOutMode == swapOutMode && _sequenceMode == sequenceMode) return;
        _swapOutMode = swapOutMode;
        _sequenceMode = sequenceMode;
        _pick = null;
        UpdateModeLabels();
        if (_loaded) RenderCards();
    }


    public override string HelpText => _sequenceMode ? SequenceHelp : (_swapOutMode
        ? "换出顺序\n按卡片顺序依次优先，卡图左侧的数字为顺位。首次授业先找名单，再找基本卡；第二次先找首次换入的记录卡，再找名单。空名单沿用基本卡规则。\n\n" +
          "优先换出未定制版本\n同名卡优先换出未定制版本，每张卡独立设置。\n\n允许换出定制版本\n关闭后，授业换出及兜底均跳过该卡的定制版。\n\n"
        : "基础优先级\n数字越小越优先。设置影响战斗出牌顺序。\n\n" +
              "兜底优先级\n未单独配置或暂未识别的卡牌使用这个数值。\n\n" +
              "未配置卡偏好\n可不偏好类型、优先 M 卡、优先 A 卡，或按剩余回合切换。只用于未配置卡，不改变已配置卡的基础顺序。\n\n" +
              "收尾门槛\n剩余回合小于等于门槛时使用收尾偏好，否则使用前期偏好。\n\n" +
              "优先使用强化卡\n没有类型偏好时，未配置卡同级优先强化；有类型偏好时先比较类型。已配置卡和组合衔接选定卡名后，同名手牌优先强化版。按实际读到的卡名末尾 + 判断。\n\n" +
              "出牌编排\n在「出牌编排」标签设置这张卡打出前后用饮，以及打出后按顺序衔接的后续卡；卡牌详情中显示摘要，点「编辑出牌编排」跳转。\n\n" +
              "使用限制\n控制卡能否出牌，不改变优先级。每组条件同时满足，满足任一组即可使用；全力与強気职业可设置当前状态。\n\n" +
              "保留\n仅全力与強気职业，在卡牌详情中勾选。打出卡牌或使用ほっと緑茶后进入保留页时，选择勾选的卡；卡组超过一屏会滚动查找。都未勾选或都未找到时选第一张；本領発揮+ 最多选两张，有勾选但都未找到时点取消。出牌编排中用饮后出现的选卡页按后续候选顺序选卡，不使用此勾选。\n\n" +
              "按优先级保留\n默认关闭：扫描卡组时先找到哪张勾选卡就保留哪张，找到即停。开启后扫描整个卡组，按卡表从上到下的顺序选择最靠前的勾选卡。\n\n" +
              "条件优先级\n条件成立时临时使用设置的数值，数字越小越优先；它改变出牌排序，不能阻止提前使用，需要禁止出牌时使用使用限制。可设剩余回合 ≥ / ≤（都勾选时只在区间内满足）、全力值 <（仅全力）、指针状态（全力与強気，可多选，无法识别时不满足），并选择满足任一或全部条件。\n\n" +
              "使用限制的条件组\n最多三组，每组内的状态和回合条件同时满足；满足任一组即可出牌，留空表示无限制。全力与強気职业可限制当前状态，无法确认时视为不满足。\n\n" +
              "有额外回合时不使用\n独立于条件组。左上角仍有待兑现的 +N 时保留此卡；+N 转入倒计时后恢复普通规则，标记未确认时也先保留。\n\n" +
              "职业配置\n页面顶部「职业配置」可导出当前职业的战斗配置便于分享，或导入他人的配置替换当前职业的战斗配置（保留优先获取与换出名单），核对后保存生效。\n\n" +
              "从集中复制卡表\n复制集中的战斗卡表到当前职业，替换当前出牌名单；移除不在新名单中的条件优先级。仅复制卡表，核对后再保存。\n\n") +
        "职业\n各职业独立编辑。执行时读取主界面所选职业。\n\n添加卡牌\n在「卡库」标签选好名单后点卡加入，再点一次移出。\n\n" +
        (_swapOutMode ? "调整顺序与移出\n点卡片上的 ← → 前移或后移，点 × 移出，修改后需保存。\n\n"
            : "上下移动与移出\n调整列表顺序或移除条目，修改后需保存。\n\n") +
        "保存\n页面顶部「保存」写入全部修改，下次开始培育生效；配置被其他窗口修改时提示重新读取。";

    /// <summary>旧版本留下、已不再读取的字段：全局卡表、旧组合与旧使用回合限制。</summary>
    private static readonly string[] OldFields = ["priority", "combo", "skip_limit", "use_turn_limit_profiles", "full_power_state_use_profiles"];

    private void LoadConfig()
    {
        var bytes = File.ReadAllBytes(_configPath);
        _originalHash = Convert.ToHexString(SHA256.HashData(bytes));
        _config = JObject.Parse(Encoding.UTF8.GetString(bytes));
        // 旧卡名先换成目录正式名，否则组合校验找不到目录键会使面板无法打开；保存时写回新名。
        HifCardRenames.Apply(_config);
        var profiles = _config["priority_profiles"] as JObject
            ?? throw new InvalidOperationException("缺少 priority_profiles");
        foreach (var name in Professions)
            _profiles[name] = (profiles[name] as JArray ?? throw new InvalidOperationException($"缺少职业 {name}"))
                .OfType<JObject>().Select(card => (JObject)card.DeepClone()).ToList();
        var targetProfiles = _config["preferred_acquisition_profiles"] as JObject
            ?? throw new InvalidOperationException("缺少 preferred_acquisition_profiles");
        foreach (var name in Professions)
        {
            var targets = targetProfiles[name] as JArray ?? throw new InvalidOperationException($"缺少目标卡职业 {name}");
            // 旧版本的字符串条目不再读取。
            if (targets.Any(card => card is not JObject)) NoteIncompatible("优先获取卡");
            _targetProfiles[name] = targets.OfType<JObject>().Select(card => (JObject)card.DeepClone()).ToList();
        }
        var swapOutProfiles = _config["swap_out_priority_profiles"] as JObject;
        foreach (var name in Professions)
            _swapOutProfiles[name] = (swapOutProfiles?[name] as JArray ?? new JArray())
                .Values<string>().Where(card => !string.IsNullOrWhiteSpace(card))
                .Select(card => new JObject { ["name"] = card }).ToList();
        foreach (var name in Professions)
            _swapOutOptions[name] = (JObject?)_config["swap_out_options_profiles"]?[name]?.DeepClone()
                ?? new JObject();
        var unknown = _config["unknown_priority_profiles"] as JObject;
        foreach (var name in Professions)
            _unknownPriorities[name] = (decimal?)unknown?[name] ?? (name == "全力" ? 8 : 11);
        foreach (var name in Professions)
            _typePreferences[name] = HifPriorityShare.TypePreference(_config["unconfigured_card_preference_profiles"]?[name]);
        var useLimits = _config["use_condition_profiles"] as JObject;
        foreach (var name in Professions)
            _useConditions[name] = (JObject?)useLimits?[name]?.DeepClone() ?? new JObject();
        foreach (var name in Professions)
            _conditionalPriorities[name] = (JObject?)_config["conditional_priority_profiles"]?[name]?.DeepClone() ?? new JObject();
        foreach (var name in Professions)
            _noExtraTurns[name] = (_config["no_extra_turn_profiles"]?[name] as JArray ?? new JArray())
                .Values<string>().Where(key => !string.IsNullOrWhiteSpace(key)).Select(key => key!).ToHashSet();
        foreach (var name in Professions)
        {
            var hold = _config["hold_priority_profiles"]?[name] as JObject;
            _holdOrdered[name] = hold?["ordered"]?.Type == JTokenType.Boolean && (bool)hold["ordered"]!;
            _holdCards[name] = (hold?["cards"] as JArray ?? new JArray()).Where(key => key.Type == JTokenType.String)
                .Select(key => (string)key!).Where(key => !string.IsNullOrWhiteSpace(key)).ToHashSet();
        }
        var manifest = HifLayout.CatalogPath("hif_priority_card_images.json");
        _images = JObject.Parse(File.ReadAllText(manifest, Encoding.UTF8));
        var catalog = HifLayout.CatalogPath("hif_priority_card_catalog.json");
        _catalog = JObject.Parse(File.ReadAllText(catalog, Encoding.UTF8));
        var targetCatalog = HifLayout.CatalogPath("hif_target_card_catalog.json");
        _targetCatalog = JArray.Parse(File.ReadAllText(targetCatalog, Encoding.UTF8));
        // 旧版本的全局卡表、组合与使用回合字段不再读取，保存时去掉。
        if (OldFields.Any(key => _config[key] != null)) NoteIncompatible("出牌优先级");
        LoadDrinkCatalog();
        LoadSequences();
        using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
        LoggerHelper.Info($"HIF 卡牌配置已加载：文件={_configPath}, 职业数={_profiles.Count}, 优先级条目数={_profiles.Values.Sum(cards => cards.Count)}, 优先获取条目数={_targetProfiles.Values.Sum(cards => cards.Count)}, 优先换出条目数={_swapOutProfiles.Values.Sum(cards => cards.Count)}, 可导入卡数={_targetCatalog.Count}");
    }

    /// <summary>
    /// 出牌优先级与出牌编排显示右侧详情：左侧名单定宽、可拖动分隔线调整，详情随窗口伸缩。
    /// 出牌编排的左侧较窄，打开使用说明抽屉时右侧的出牌顺序仍能完整显示。
    /// </summary>
    private void ShowDetail(bool show)
    {
        var (list, listMin, detailMin) = _sequenceMode ? (300d, 220d, 360d) : (400d, 300d, 460d);
        var layout = show ? $"{list}/{detailMin}" : "";
        if (layout != _detailLayout)
        {
            _detailLayout = layout;
            _body.ColumnDefinitions = show
                ? new ColumnDefinitions
                {
                    new ColumnDefinition(list, GridUnitType.Pixel) { MinWidth = listMin },
                    new ColumnDefinition(GridLength.Auto),
                    new ColumnDefinition(1, GridUnitType.Star) { MinWidth = detailMin }
                }
                : new ColumnDefinitions("*,0,0");
        }
        _splitter.IsVisible = show;
        _detailScroll.IsVisible = show;
        if (!show) _detail.Children.Clear();
    }

    private void RenderCards()
    {
        _rows.Children.Clear();
        _settings.Children.Clear();
        ShowDetail(!_swapOutMode);
        var cards = _profiles[_profession];
        _copy.IsVisible = !_swapOutMode && !_sequenceMode && cards.Count == 0 && _profession != "集中";
        if (_sequenceMode)
        {
            RenderSequences();
            return;
        }
        if (_swapOutMode)
        {
            RenderSwapOutCards();
            return;
        }
        RenderUnknownPriority();
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock
            {
                Text = "此职业目前没有独立卡表。在「卡库」中添加，或复制集中卡表后调整。",
                Margin = new Thickness(8, 16, 8, 8)
            });
            _rows.Children.Add(LibraryButton(HifLibraryTarget.Priority));
            RenderDetail(cards);
            return;
        }
        if (_selectedKey == null || cards.All(card => (string?)card["key"] != _selectedKey))
            _selectedKey = (string?)cards[0]["key"];
        for (var index = 0; index < cards.Count; index++)
        {
            var card = cards[index];
            var key = (string?)card["key"] ?? "";
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("28,60,*,100"), Margin = new Thickness(2) };
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

            if (CardImage(key) is { } image)
            {
                image.Width = 52; image.Height = 52;
                Grid.SetColumn(image, 1); row.Children.Add(image);
            }
            var label = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            label.Children.Add(new TextBlock { Text = key, TextWrapping = TextWrapping.Wrap });
            var badges = Badges(key);
            if (badges != "")
            {
                var badgeText = new TextBlock { Text = badges, FontSize = 11 };
                badgeText[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                label.Children.Add(badgeText);
            }
            Grid.SetColumn(label, 2);
            row.Children.Add(label);

            var rank = new NumericUpDown
            {
                Value = (decimal?)card["priority"] ?? 0,
                Minimum = 0,
                Maximum = 9999,
                Increment = 0.5m,
                VerticalAlignment = VerticalAlignment.Center,
                Width = 92
            };
            rank.ValueChanged += (_, _) =>
            {
                if (rank.Value is { } value)
                {
                    card["priority"] = value;
                    MarkChanged();
                }
            };
            ToolTip.SetTip(rank, "基础优先级，数字越小越优先");
            Grid.SetColumn(rank, 3);
            row.Children.Add(rank);

            var selected = key == _selectedKey;
            var border = new Border
            {
                Child = row,
                BorderThickness = new Thickness(selected ? 2 : 1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(6, 4),
                Cursor = new Cursor(StandardCursorType.Hand)
            };
            if (selected)
            {
                border[!Border.BorderBrushProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                border[!Border.BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor5");
            }
            else border[!Border.BorderBrushProperty] = new DynamicResourceExtension("SukiLightBorderBrush");
            border.PointerPressed += (_, e) =>
            {
                if (ReferenceEquals(e.Source, handle) || key == _selectedKey) return;
                _selectedKey = key;
                Dispatcher.UIThread.Post(RenderCards);
            };
            _rows.Children.Add(border);
        }
        RenderDetail(cards);
    }

    /// <summary>卡表行上的规则标记。</summary>
    private string Badges(string key)
    {
        var marks = new List<string>();
        if (_conditionalPriorities[_profession][key] is JObject) marks.Add("条件优先级");
        if (_useConditions[_profession][key] is JArray { Count: > 0 } || _noExtraTurns[_profession].Contains(key)) marks.Add("使用限制");
        if (_sequences[_profession][key] is JObject) marks.Add("出牌编排");
        if (_profession is "全力" or "強気" && _holdCards[_profession].Contains(key)) marks.Add("保留");
        return string.Join(" · ", marks);
    }

    /// <summary>右侧详情：所选卡牌的排序、移出、保留，条件优先级与使用限制的就地编辑，以及出牌编排摘要。</summary>
    private void RenderDetail(List<JObject> cards)
    {
        _detail.Children.Clear();
        var index = cards.FindIndex(card => (string?)card["key"] == _selectedKey);
        if (index < 0)
        {
            _detail.Children.Add(new TextBlock { Text = "在左侧选择一张卡，查看和编辑它的规则。", Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
            return;
        }
        var card = cards[index];
        var key = _selectedKey!;
        var profession = _profession;

        // 标题行：卡图、卡名与顺位在左，操作按钮在同一行；宽度不够时按钮换到下一行。
        var identity = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(0, 0, 16, 6) };
        if (CardImage(key) is { } image) identity.Children.Add(image);
        var title = new StackPanel { Spacing = 3, VerticalAlignment = VerticalAlignment.Center, MaxWidth = 300 };
        title.Children.Add(new TextBlock { Text = key, FontSize = 16, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
        title.Children.Add(new TextBlock { Text = $"第 {index + 1} 位 · 基础优先级 {(decimal?)card["priority"] ?? 0}", FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = TextWrapping.Wrap });
        identity.Children.Add(title);

        var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) };
        var up = new Button { Content = "↑ 上移", Classes = { "Basic" }, IsEnabled = index > 0 };
        up.Click += (_, _) => MoveCard(index, index - 1);
        var down = new Button { Content = "↓ 下移", Classes = { "Basic" }, IsEnabled = index < cards.Count - 1 };
        down.Click += (_, _) => MoveCard(index, index + 1);
        var remove = new Button { Content = "从卡表移出", Classes = { "Basic" } };
        ToolTip.SetTip(remove, "只移除当前职业的优先级，卡牌识别和模板文件保留");
        remove.Click += (_, _) =>
        {
            cards.RemoveAt(index);
            _conditionalPriorities[profession].Remove(key);
            _holdCards[profession].Remove(key);
            _selectedKey = null;
            MarkChanged();
            RenderCards();
            LoggerHelper.UserAction("移除 HIF 优先级卡牌", $"职业={profession}, 卡牌={key}", operation: "HifPriorityEditor");
        };
        foreach (var button in new[] { up, down, remove }) { button.Margin = new Thickness(0, 0, 6, 0); actions.Children.Add(button); }
        if (profession is "全力" or "強気")
        {
            var hold = new CheckBox { Content = "保留", IsChecked = _holdCards[profession].Contains(key), Margin = new Thickness(8, 0, 0, 0) };
            ToolTip.SetTip(hold, "进入保留页时选择勾选的卡；详见使用说明");
            hold.IsCheckedChanged += (_, _) =>
            {
                if (hold.IsChecked == true) _holdCards[profession].Add(key); else _holdCards[profession].Remove(key);
                MarkChanged();
                RenderCards();
            };
            actions.Children.Add(hold);
        }
        var header = new WrapPanel { Orientation = Orientation.Horizontal };
        header.Children.Add(identity);
        header.Children.Add(actions);
        _detail.Children.Add(header);

        // 条件优先级与使用限制同时显示：详情够宽时左右并排，不够宽时上下叠放。「取消」恢复为已应用的设置。
        void Restore() => RenderDetail(_profiles[_profession]);
        _ruleGrid = new Grid { ColumnSpacing = 10, RowSpacing = 10, Name = "rules" };
        _ruleGrid.Children.Add(RuleCard("条件优先级", ConditionSummary(key), BuildConditionEditor(key, (decimal?)card["priority"] ?? 0, Restore)));
        _ruleGrid.Children.Add(RuleCard("使用限制", UseConditionSummary(key), BuildUseConditionEditor(key, Restore)));
        _ruleSideBySide = null;
        ArrangeRules();
        _detail.Children.Add(_ruleGrid);

        // 出牌编排在独立标签中编辑，这里只显示摘要和跳转按钮。
        var sequence = new Button
        {
            Content = _sequences[profession][key] is JObject ? "编辑出牌编排" : "为这张卡新建出牌编排",
            Classes = { "Basic" }, Padding = new Thickness(8, 2), MinHeight = 0, VerticalAlignment = VerticalAlignment.Center
        };
        ToolTip.SetTip(sequence, "切到「出牌编排」标签，设置这张卡打出前后用饮和后续卡");
        sequence.Click += (_, _) => RequestSequence(key);
        var sequenceRow = new DockPanel();
        DockPanel.SetDock(sequence, Dock.Right);
        sequenceRow.Children.Add(sequence);
        var sequenceTitle = new TextBlock { Text = "出牌编排", FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 0) };
        DockPanel.SetDock(sequenceTitle, Dock.Left);
        sequenceRow.Children.Add(sequenceTitle);
        sequenceRow.Children.Add(RuleStatus(SequenceSummary(profession, key), HorizontalAlignment.Left));
        _detail.Children.Add(new Border
        {
            Child = sequenceRow, Padding = new Thickness(12, 8), CornerRadius = new CornerRadius(10),
            BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1)
        });
    }

    /// <summary>规则状态：未设置为次要色，已设置为主题色。</summary>
    private static TextBlock RuleStatus(string text, HorizontalAlignment alignment)
    {
        var status = new TextBlock
        {
            Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = alignment,
            TextAlignment = alignment == HorizontalAlignment.Right ? TextAlignment.Right : TextAlignment.Left
        };
        if (text == "未设置") status.Opacity = HifTheme.SecondaryOpacity;
        else status[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
        return status;
    }

    /// <summary>详情中的规则卡片：标题与当前状态在上，编辑内容在下。</summary>
    private static Border RuleCard(string name, string status, Control editor)
    {
        var head = new DockPanel();
        var title = new TextBlock { Text = name, FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 0) };
        DockPanel.SetDock(title, Dock.Left);
        head.Children.Add(title);
        head.Children.Add(RuleStatus(status, HorizontalAlignment.Right));
        var body = new StackPanel { Spacing = 10 };
        body.Children.Add(head);
        body.Children.Add(editor);
        return new Border
        {
            Child = body, Padding = new Thickness(12, 10), CornerRadius = new CornerRadius(10), Tag = name,
            BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), VerticalAlignment = VerticalAlignment.Top
        };
    }

    /// <summary>按详情宽度排列两张规则卡片；全力与強気的编辑行更宽，并排所需宽度也更大。</summary>
    private void ArrangeRules()
    {
        if (_ruleGrid is not { Children.Count: 2 } grid) return;
        var cardWidth = _profession is "全力" or "強気" ? 430d : 300d;
        var width = _detailScroll.Bounds.Width;
        var sideBySide = width <= 0 || width >= cardWidth * 2 + grid.ColumnSpacing + _detail.Margin.Right;
        if (sideBySide == _ruleSideBySide) return;
        _ruleSideBySide = sideBySide;
        grid.ColumnDefinitions = new ColumnDefinitions(sideBySide ? "*,*" : "*");
        grid.RowDefinitions = new RowDefinitions(sideBySide ? "Auto" : "Auto,Auto");
        Grid.SetColumn(grid.Children[1], sideBySide ? 1 : 0);
        Grid.SetRow(grid.Children[1], sideBySide ? 0 : 1);
    }

    private string ConditionSummary(string key)
    {
        if (_conditionalPriorities[_profession][key] is not JObject rule) return "未设置";
        var parts = new List<string> { $"优先级 {(decimal?)rule["priority"]}" };
        if (rule["remaining_turns_gte"] != null) parts.Add($"剩余回合 ≥ {(int?)rule["remaining_turns_gte"]}");
        if (rule["remaining_turns_lte"] != null) parts.Add($"剩余回合 ≤ {(int?)rule["remaining_turns_lte"]}");
        if (rule["full_power_lt"] != null) parts.Add($"全力值 < {(int?)rule["full_power_lt"]}");
        if (rule["pointer_states"] is JArray states && states.Count > 0) parts.Add(string.Join("/", states.Values<string>()));
        return string.Join("，", parts);
    }

    private string UseConditionSummary(string key)
    {
        var parts = new List<string>();
        if (_useConditions[_profession][key] is JArray { Count: > 0 } clauses) parts.Add($"{clauses.Count} 组条件");
        if (_noExtraTurns[_profession].Contains(key)) parts.Add("有额外回合时不使用");
        return parts.Count == 0 ? "未设置" : string.Join("，", parts);
    }

    private void RenderUnknownPriority()
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(8, 4, 20, 6) };
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
        _settings.Children.Add(row);
        var rule = _typePreferences[_profession];
        var modes = new[] { "none", "mental", "active", "remaining_turns" };
        var mode = new ComboBox
        {
            ItemsSource = new[] { "不偏好类型", "优先 M 卡", "优先 A 卡", "按剩余回合切换" },
            SelectedIndex = Array.IndexOf(modes, (string?)rule["mode"]), Width = 190
        };
        var preferenceRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(8, 0) };
        preferenceRow.Children.Add(new TextBlock { Text = "未配置卡偏好", Width = 105, VerticalAlignment = VerticalAlignment.Center });
        preferenceRow.Children.Add(mode);
        var upgraded = new CheckBox { Content = "优先使用强化卡", IsChecked = (bool?)rule["prefer_upgraded"] };
        upgraded.IsCheckedChanged += (_, _) => { rule["prefer_upgraded"] = upgraded.IsChecked == true; MarkChanged(); };
        preferenceRow.Children.Add(upgraded);
        _settings.Children.Add(preferenceRow);
        var phases = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(8, 0, 8, 0),
            IsVisible = (string?)rule["mode"] == "remaining_turns" };
        phases.Children.Add(new TextBlock { Text = "收尾：剩余回合 ≤", VerticalAlignment = VerticalAlignment.Center });
        var turns = new NumericUpDown { Value = (int)rule["remaining_turns_lte"]!, Minimum = 1, Maximum = 99,
            Increment = 1, FormatString = "0", Width = 75 };
        turns.ValueChanged += (_, _) => { if (turns.Value is { } value) { rule["remaining_turns_lte"] = (int)value; MarkChanged(); } };
        phases.Children.Add(turns);
        foreach (var field in new[] { "early", "late" })
        {
            phases.Children.Add(new TextBlock { Text = field == "early" ? "前期" : "收尾", VerticalAlignment = VerticalAlignment.Center });
            var choice = new ComboBox { ItemsSource = new[] { "不偏好", "M 卡", "A 卡" },
                SelectedIndex = Array.IndexOf(modes, (string?)rule[field]), Width = 95 };
            choice.SelectionChanged += (_, _) => { if (choice.SelectedIndex >= 0) { rule[field] = modes[choice.SelectedIndex]; MarkChanged(); } };
            phases.Children.Add(choice);
        }
        mode.SelectionChanged += (_, _) =>
        {
            if (mode.SelectedIndex < 0) return;
            rule["mode"] = modes[mode.SelectedIndex];
            phases.IsVisible = mode.SelectedIndex == 3;
            MarkChanged();
        };
        _settings.Children.Add(phases);
        if (_profession is "全力" or "強気")
        {
            var profession = _profession;
            var holdRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Margin = new Thickness(8, 4, 8, 8) };
            holdRow.Children.Add(new TextBlock { Text = "保留", Width = 105, VerticalAlignment = VerticalAlignment.Center });
            var ordered = new CheckBox { Content = "按优先级保留", IsChecked = _holdOrdered[profession] };
            ordered.IsCheckedChanged += (_, _) => { _holdOrdered[profession] = ordered.IsChecked == true; MarkChanged(); };
            holdRow.Children.Add(ordered);
            _settings.Children.Add(holdRow);
        }
    }

    /// <summary>按卡表顺序保存勾选的保留卡；已从卡表删除的卡不再保存。</summary>
    private JObject HoldPriority(string profession) => new()
    {
        ["ordered"] = _holdOrdered[profession],
        ["cards"] = new JArray(_profiles[profession].OrderBy(card => (decimal?)card["priority"] ?? 0)
            .Select(card => (string?)card["key"]).Where(key => key != null && _holdCards[profession].Contains(key)))
    };

    private static Control Thumbnail(string path) =>
        new Image { Source = new Bitmap(path), Width = 64, Height = 64, Stretch = Stretch.Fill };

    // 授业换出卡的名单排成卡片网格（与饮料页「其他饮料」相同）：按宽度分列并铺满整行，页面变宽或变窄时每行张数随之变化。

    private const double OrderWidth = 24;

    private void RenderSwapOutCards()
    {
        var cards = _swapOutProfiles[_profession];
        if (cards.Count == 0)
        {
            _rows.Children.Add(new TextBlock { Text = "此职业没有优先换出卡；空名单沿用基本卡规则。加入后可用 ← → 调整顺序。", Margin = new Thickness(8, 16, 8, 8) });
            _rows.Children.Add(LibraryButton(HifLibraryTarget.SwapOut));
            return;
        }
        var grid = new HifTileGrid { Name = "swap-out", MinTileWidth = HifTileGrid.CardTileWidth };
        for (var index = 0; index < cards.Count; index++)
        {
            var name = (string?)cards[index]["name"] ?? "";
            var sourceIndex = index;
            var actions = new StackPanel { Orientation = Orientation.Horizontal };
            foreach (var (text, delta, tip) in new[] { ("←", -1, "前移"), ("→", 1, "后移") })
            {
                var to = sourceIndex + delta;
                var move = TileButton(text, tip);
                move.IsEnabled = to >= 0 && to < cards.Count;
                move.Click += (_, _) => MoveSwapOutCard(sourceIndex, to);
                actions.Children.Add(move);
            }
            var remove = TileButton("×", "移出");
            remove.Click += (_, _) =>
            {
                cards.RemoveAt(sourceIndex);
                _swapOutOptions[_profession].Remove(name);
                MarkChanged();
                RenderCards();
                LoggerHelper.UserAction("移除 HIF 优先换出卡", $"职业={_profession}, 卡名={name}", operation: "HifPriorityEditor");
            };
            actions.Children.Add(remove);
            var profession = _profession;
            var options = _swapOutOptions[profession][name] as JObject
                ?? new JObject { ["prefer_uncustomized"] = true, ["allow_customized"] = true };
            // 两个开关与卡名左对齐（顺位 + 卡图 52 + 间距 10）。
            var switches = new StackPanel { Margin = new Thickness(OrderWidth + 62, 0, 0, 0) };
            foreach (var (key, optionLabel, defaultValue) in new[] {
                         ("prefer_uncustomized", "优先换出未定制版本", true),
                         ("allow_customized", "允许换出定制版本", true) })
            {
                var check = new CheckBox { Content = optionLabel, IsChecked = (bool?)options[key] ?? defaultValue };
                check.IsCheckedChanged += (_, _) => {
                    options[key] = check.IsChecked == true;
                    _swapOutOptions[profession][name] = options;
                    MarkChanged();
                };
                switches.Children.Add(check);
            }
            var body = new StackPanel { Spacing = 4 };
            body.Children.Add(TileHeader(name, index + 1, actions));
            body.Children.Add(switches);
            grid.Children.Add(ListTile(body));
        }
        _rows.Children.Add(grid);
    }

    private static Button TileButton(string text, string tip)
    {
        var button = new Button { Content = text, Classes = { "Basic" }, Padding = new Thickness(8, 2), MinHeight = 0 };
        ToolTip.SetTip(button, tip);
        return button;
    }

    private static Border ListTile(Control child) => new()
    {
        Child = child, Margin = new Thickness(0, 0, 8, 8), Padding = new Thickness(10, 8),
        BorderBrush = HifTheme.LineBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10)
    };

    /// <summary>名单卡片的上半部分：顺位（仅换出卡，卡图左上角已有费用数字，所以写在卡图左侧）、卡图、卡名与稀有度 · 计划 · 类型，右侧为操作按钮。</summary>
    private Control TileHeader(string name, int? order, Control actions)
    {
        var entry = _targetCatalog.OfType<JObject>().FirstOrDefault(card => (string?)card["name"] == name);
        var header = new DockPanel();
        actions.VerticalAlignment = VerticalAlignment.Top;
        DockPanel.SetDock(actions, Dock.Right);
        header.Children.Add(actions);
        if (order is { } position)
        {
            var number = new TextBlock
            {
                Text = $"{position}", Width = OrderWidth, FontSize = 15, FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center
            };
            number[!TextBlock.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
            ToolTip.SetTip(number, $"第 {position} 位");
            DockPanel.SetDock(number, Dock.Left);
            header.Children.Add(number);
        }
        var picture = new Grid { Width = 52, Height = 52, Margin = new Thickness(0, 0, 10, 0), VerticalAlignment = VerticalAlignment.Top };
        if (HifCardPicker.Icon((int?)entry?["id"] ?? (int?)_images[name] ?? 0) is { } bitmap)
            picture.Children.Add(new Image { Source = bitmap, Width = 52, Height = 52, Stretch = Stretch.Uniform });
        else
            picture.Children.Add(new TextBlock
            {
                Text = "无图", HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity
            });
        DockPanel.SetDock(picture, Dock.Left);
        header.Children.Add(picture);
        var text = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
        text.Children.Add(new TextBlock { Text = name, TextWrapping = TextWrapping.Wrap, MaxLines = 2, TextTrimming = TextTrimming.CharacterEllipsis });
        if (CardSummary(entry) is { Length: > 0 } summary)
            text.Children.Add(new TextBlock { Text = summary, FontSize = 12, Opacity = HifTheme.SecondaryOpacity });
        header.Children.Add(text);
        return header;
    }

    /// <summary>稀有度 · 计划 · 类型；不在可导入目录中的卡（例如「眠気」）不显示。</summary>
    private static string CardSummary(JObject? entry) => entry == null ? "" : string.Join(" · ", new[]
    {
        (string?)entry["rarity"] ?? "",
        (string?)entry["plan"] is { Length: > 0 } plan ? HifCardPicker.PlanName(plan) : "",
        (string?)entry["type"] switch { "active" => "A 卡", "mental" => "M 卡", "trouble" => "T 卡", _ => "" }
    }.Where(part => part is not ("" or "—")));

    private void MoveSwapOutCard(int from, int to)
    {
        var cards = _swapOutProfiles[_profession];
        if (from < 0 || to < 0 || from >= cards.Count || to >= cards.Count) return;
        (cards[from], cards[to]) = (cards[to], cards[from]);
        MarkChanged();
        RenderCards();
        LoggerHelper.UserAction("调整 HIF 优先换出顺序", $"职业={_profession}, 从第{from + 1}位到第{to + 1}位", operation: "HifPriorityEditor");
    }

    /// <summary>卡库用：可加入卡目录加上只在出牌优先级目录中的条目。</summary>
    public System.Collections.Generic.IReadOnlyList<HifLibraryCard> LibraryCards()
    {
        var cards = _targetCatalog.OfType<JObject>().Select(card => new HifLibraryCard(
            (int?)card["id"] ?? 0, (string?)card["name"] ?? "", (string?)card["rarity"] ?? "", (string?)card["plan"] ?? "",
            (string?)card["type"] ?? "", (string?)card["sourceType"] ?? "", false)).Where(card => card.Name != "").ToList();
        var names = cards.Select(card => card.Name).ToHashSet();
        cards.AddRange(_catalog.Properties().Where(entry => !names.Contains(entry.Name)).Select(entry => new HifLibraryCard(
            (int?)_images[entry.Name] ?? 0, entry.Name, "", "", (string?)entry.Value["type"] ?? "", "", true)));
        return cards;
    }

    private List<JObject> ListOf(HifLibraryTarget list, string profession) => list switch
    {
        HifLibraryTarget.Acquisition => _targetProfiles[profession],
        HifLibraryTarget.SwapOut => _swapOutProfiles[profession],
        _ => _profiles[profession]
    };

    /// <summary>某职业的优先获取卡名单（编辑中的数据）；在「获取与定制」标签中显示和修改。</summary>
    public IEnumerable<string> AcquisitionNames(string? profession = null) => !_loaded
        ? Enumerable.Empty<string>()
        : _targetProfiles[profession ?? _profession].Select(card => (string?)card["name"] ?? "").Where(name => name != "");

    public bool InList(HifLibraryTarget list, string name, string? profession = null)
    {
        if (!_loaded) return false;
        profession ??= _profession;
        return ListOf(list, profession).Any(card => (string?)card[list == HifLibraryTarget.Priority ? "key" : "name"] == name);
    }

    public bool CanAdd(HifLibraryTarget list, HifLibraryCard card) => _loaded && list switch
    {
        HifLibraryTarget.Acquisition => !card.PriorityOnly,
        HifLibraryTarget.Priority => _catalog[card.Name] is JObject,
        _ => true
    };

    /// <summary>加入或移出当前职业的名单；规则与行内删除按钮相同。返回 false 表示未能修改。</summary>
    public bool SetInList(HifLibraryTarget list, string name, bool include)
    {
        if (!_loaded) return false;
        if (InList(list, name) == include) return true;
        var profession = _profession;
        var cards = ListOf(list, profession);
        if (include)
        {
            if (list == HifLibraryTarget.Priority)
            {
                var priorityCard = (JObject)_catalog[name]!.DeepClone();
                priorityCard["key"] = name;
                priorityCard["priority"] = cards.Count == 0 ? 0 : cards.Max(item => (decimal?)item["priority"] ?? 0) + 1;
                cards.Add(priorityCard);
            }
            else cards.Add(new JObject { ["name"] = name });
        }
        else
        {
            cards.RemoveAll(card => (string?)card[list == HifLibraryTarget.Priority ? "key" : "name"] == name);
            if (list == HifLibraryTarget.Priority)
            {
                _conditionalPriorities[profession].Remove(name);
                _holdCards[profession].Remove(name);
            }
            if (list == HifLibraryTarget.SwapOut) _swapOutOptions[profession].Remove(name);
        }
        if (list == HifLibraryTarget.Acquisition) _changedTargetProfessions.Add(profession);
        MarkChanged();
        RenderCards();
        LoggerHelper.UserAction(include ? "卡库加入 HIF 名单" : "卡库移出 HIF 名单", $"职业={profession}, 名单={list}, 卡牌={name}", operation: "HifPriorityEditor");
        return true;
    }

    /// <summary>最近一次修改失败的原因（卡库显示）。</summary>
    public string Status => _status.Text ?? "";

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

    private void ShowUseConditionDialog(string key)
    {
        var dialog = new SukiWindow
        {
            Title = $"卡牌使用限制 - {key}", Width = 600, Height = 430,
            MinWidth = 550, MinHeight = 390, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var body = BuildUseConditionEditor(key, dialog.Close);
        body.Margin = new Thickness(18);
        dialog.Content = body;
        _ = dialog.ShowDialog(OwnerWindow);
    }

    /// <summary>使用限制编辑内容；对话框和卡牌详情共用。</summary>
    private StackPanel BuildUseConditionEditor(string key, Action close)
    {
        var profession = _profession;
        var saved = _useConditions[_profession][key] as JArray;
        var body = new StackPanel { Spacing = 12 };
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
            close();
        };
        buttons.Children.Add(apply);
        var cancel = new Button { Content = "取消" };
        cancel.Click += (_, _) => close();
        buttons.Children.Add(cancel);
        body.Children.Add(buttons);
        return body;
    }

    private void ShowConditionDialog(string key, decimal baseRank)
    {
        var fullPower = _profession == "全力";
        var pointer = _profession is "全力" or "強気";
        var dialog = new SukiWindow
        {
            Title = $"条件优先级 - {_profession} - {key}", Width = 510, Height = fullPower ? 520 : pointer ? 470 : 390,
            MinWidth = 440, MinHeight = 350, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var body = BuildConditionEditor(key, baseRank, dialog.Close);
        body.Margin = new Thickness(18);
        dialog.Content = body;
        _ = dialog.ShowDialog(OwnerWindow);
    }

    /// <summary>条件优先级编辑内容；对话框和卡牌详情共用。</summary>
    private StackPanel BuildConditionEditor(string key, decimal baseRank, Action close)
    {
        var saved = _conditionalPriorities[_profession][key] as JObject;
        var fullPower = _profession == "全力";
        var pointer = _profession is "全力" or "強気";
        var legacyExample = fullPower && saved == null && key == "アッチェレランド+";
        var body = new StackPanel { Spacing = 12 };
        body.Children.Add(new TextBlock { Text = $"基础优先级：{baseRank}" });
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

        var savedStates = (saved?["pointer_states"] as JArray)?.Values<string>().ToHashSet() ?? new HashSet<string?>();
        var stateChecks = new[] { "全力", "強気", "温存" }
            .Select(state => new CheckBox { Content = state, IsChecked = savedStates.Contains(state) }).ToList();
        var stateRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        stateRow.Children.Add(new TextBlock { Text = "指针状态", Width = 120, VerticalAlignment = VerticalAlignment.Center });
        foreach (var check in stateChecks) stateRow.Children.Add(check);
        ToolTip.SetTip(stateRow, "当前指针为任一勾选状态时满足；无法识别时不满足");
        if (pointer) body.Children.Add(stateRow);
        var selectedStates = () => pointer
            ? stateChecks.Where(check => check.IsChecked == true).Select(check => (string)check.Content!).ToList()
            : new List<string>();

        var mode = new ComboBox
        {
            ItemsSource = new[] { "满足任一条件", "同时满足" },
            SelectedIndex = (string?)saved?["mode"] == "all" ? 1 : 0,
            Width = 160
        };
        var modeRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        modeRow.Children.Add(new TextBlock { Text = "条件关系", Width = 120, VerticalAlignment = VerticalAlignment.Center });
        modeRow.Children.Add(mode);
        if (pointer) body.Children.Add(modeRow);
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        body.Children.Add(error);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var apply = new Button { Content = "应用到本次编辑" };
        apply.Click += (_, _) =>
        {
            var states = selectedStates();
            if (lowerEnabled.IsChecked != true && turnsEnabled.IsChecked != true && !(fullPower && powerEnabled.IsChecked == true) && states.Count == 0)
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
            if (states.Count > 0) rule["pointer_states"] = new JArray(states);
            _conditionalPriorities[_profession][key] = rule;
            MarkChanged();
            RenderCards();
            LoggerHelper.UserAction("设置 HIF 条件优先级", $"职业={_profession}, 卡牌={key}, 规则={rule.ToString(Formatting.None)}", operation: "HifPriorityEditor");
            close();
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
                close();
            };
            buttons.Children.Add(clear);
        }
        body.Children.Add(buttons);
        return body;
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
                _status.Text = "配置已被其他窗口修改，未保存。点「放弃修改」重新读取后再修改。";
                using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifPriorityEditor");
                LoggerHelper.Warning("HIF 优先级保存被拒绝：配置文件在编辑期间发生变化");
                return;
            }
            var profiles = (JObject)_config["priority_profiles"]!;
            var targetProfiles = (JObject)_config["preferred_acquisition_profiles"]!;
            var swapOutProfiles = _config["swap_out_priority_profiles"] as JObject ?? new JObject();
            var useConditions = new JObject();
            var recognition = _config["recognition_profiles"] as JObject ?? new JObject();
            var unknown = _config["unknown_priority_profiles"] as JObject ?? new JObject();
            // 切换页签后仍保留各职业的修改，因此一次保存写入全部职业。
            foreach (var name in Professions)
            {
                var sequences = SequencesForSave(name);
                var keys = _profiles[name].Select(card => (string?)card["key"]).ToArray();
                if (keys.Any(string.IsNullOrWhiteSpace) || keys.Distinct().Count() != keys.Length)
                    throw new InvalidOperationException($"{name}有空白或重复卡名");
                var known = (recognition[name] as JArray)?.Values<string>() ?? Enumerable.Empty<string>();
                var sequenceKeys = sequences.Properties().Select(p => p.Name)
                    .Concat(sequences.Properties().SelectMany(p => HifPlaySequenceConfig.Targets((JObject)p.Value)));
                recognition[name] = new JArray(known.Concat(keys!).Concat(sequenceKeys).Where(key => !string.IsNullOrWhiteSpace(key)).Distinct());
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
            _config[HifPlaySequenceConfig.ConfigKey] = JObject.FromObject(_sequences);
            // 旧版后续卡不迁移，保存时去掉。
            _config.Remove("followup_profiles");
            _config["unknown_priority_profiles"] = unknown;
            _config["unconfigured_card_preference_profiles"] = JObject.FromObject(
                _typePreferences.ToDictionary(p => p.Key, p => HifPriorityShare.TypePreference(p.Value)));
            _config["swap_out_priority_profiles"] = swapOutProfiles;
            var swapOutOptions = _config["swap_out_options_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) swapOutOptions[name] = _swapOutOptions[name].DeepClone();
            _config["swap_out_options_profiles"] = swapOutOptions;
            _config["use_condition_profiles"] = useConditions;
            var noExtraProfiles = _config["no_extra_turn_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) noExtraProfiles[name] = new JArray(_noExtraTurns[name].OrderBy(key => key, StringComparer.Ordinal));
            _config["no_extra_turn_profiles"] = noExtraProfiles;
            var holdProfiles = _config["hold_priority_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) holdProfiles[name] = HoldPriority(name);
            _config["hold_priority_profiles"] = holdProfiles;
            foreach (var key in OldFields) _config.Remove(key);
            var conditional = _config["conditional_priority_profiles"] as JObject ?? new JObject();
            foreach (var name in Professions) conditional[name] = _conditionalPriorities[name].DeepClone();
            _config["conditional_priority_profiles"] = conditional;
            var temporary = _configPath + ".tmp";
            File.WriteAllText(temporary, _config.ToString(Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(temporary, _configPath, true);
            _originalHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(_configPath)));
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
