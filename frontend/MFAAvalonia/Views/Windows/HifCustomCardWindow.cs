using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
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

/// <summary>从固定卡牌目录选择 HIF 技能卡定制项目；面板选择文件是执行配置的唯一来源。</summary>
public sealed class HifCustomCardWindow : SukiWindow
{
    private static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];
    private readonly string _catalogPath = HifLayout.CatalogPath("hif_customization_catalog.json");
    private readonly string _selectionPath = HifLayout.PersonalPath("hif_custom_card_selection.json");
    private readonly string _imageRoot = Path.Combine(HifLayout.ImageRoot, "hif", "hif_card_icons");
    private readonly Dictionary<string, Dictionary<int, HashSet<int>>> _profiles = new();
    private Dictionary<int, HashSet<int>> _selected => _profiles[_profession];
    private readonly string _taskProfession;
    private string _profession;
    private JObject _selection = new();
    private readonly TextBlock _heading = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12) };
    private readonly StackPanel _cards = new() { Spacing = 5 };
    private readonly StackPanel _details = new() { Spacing = 10 };
    private readonly TextBox _search = new() { Watermark = "搜索已导入的卡" };
    private readonly TextBlock _status = new() { VerticalAlignment = VerticalAlignment.Center };
    private readonly Button _save = new() { Content = "保存配置", IsEnabled = false };
    private JArray _catalog = new();
    private string _revision = "";
    private string _originalHash = "";
    private int? _activeId;
    private bool _loaded;

    public HifCustomCardWindow(string? initialProfession = null)
    {
        _taskProfession = Professions.Contains(initialProfession) ? initialProfession! : "集中";
        _profession = _taskProfession;
        Title = "HIF 技能卡定制配置";
        Width = 900;
        Height = 670;
        MinWidth = 700;
        MinHeight = 480;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
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
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto"), Margin = new Thickness(16) };
        UpdateHeading();
        root.Children.Add(_heading);
        var selector = new ComboBox { ItemsSource = Professions, SelectedItem = _profession, Width = 130 };
        selector.SelectionChanged += (_, _) =>
        {
            _profession = selector.SelectedItem as string ?? "集中";
            UpdateHeading();
            if (!_loaded) return;
            _activeId = _selected.Keys.Cast<int?>().FirstOrDefault();
            _search.Text = "";
            RenderCards();
            RenderDetails();
            _status.Text = $"{(_save.IsEnabled ? "未保存；" : "")}{_profession}：已导入 {_selected.Count} 张";
        };
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Margin = new Thickness(0, 0, 0, 12) };
        toolbar.Children.Add(new TextBlock { Text = "职业", VerticalAlignment = VerticalAlignment.Center });
        toolbar.Children.Add(selector);
        Grid.SetRow(toolbar, 1);
        root.Children.Add(toolbar);
        var body = new Grid { ColumnDefinitions = new ColumnDefinitions("300,*"), ColumnSpacing = 16 };
        Grid.SetRow(body, 2);
        root.Children.Add(body);
        var left = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*") };
        _search.TextChanged += (_, _) => { if (_loaded) RenderCards(); };
        left.Children.Add(_search);
        var import = new Button { Content = "导入卡牌", Margin = new Thickness(0, 8, 0, 8) };
        import.Click += (_, _) => ShowImportDialog();
        Grid.SetRow(import, 1);
        left.Children.Add(import);
        var cardScroll = new ScrollViewer { Content = _cards, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(cardScroll, 2);
        left.Children.Add(cardScroll);
        body.Children.Add(left);
        var detailScroll = new ScrollViewer { Content = _details, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetColumn(detailScroll, 1);
        body.Children.Add(detailScroll);
        var footer = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), Margin = new Thickness(0, 12, 0, 0) };
        footer.Children.Add(_status);
        _save.Click += (_, _) => Save();
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

    private void UpdateHeading() => _heading.Text =
        $"主界面职业：{_taskProfession}；正在编辑：{_profession}。各职业独立保存导入卡牌和定制项目，HIF 执行时读取主界面所选职业。";

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
            var selected = new Dictionary<int, HashSet<int>>();
            _profiles[profession] = selected;
            // 旧全局配置首次保存时复制到各职业，保留原有选择；职业配置存在后不再回退。
            var cards = (profiles == null ? _selection["cards"] : profiles[profession]) as JObject ?? new JObject();
            foreach (var entry in cards.Properties())
            {
                if (!int.TryParse(entry.Name, out var id) || FindCard(id) == null) continue;
                selected[id] = (entry.Value as JArray ?? new JArray()).Values<int>().ToHashSet();
            }
        }
        _activeId = _catalog.OfType<JObject>().Select(card => (int)card["id"]!).FirstOrDefault(_selected.ContainsKey);
        if (_activeId == 0) _activeId = null;
        _status.Text = $"{_profession}：可导入 {_catalog.Count} 张；已导入 {_selected.Count} 张";
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

    private void RenderCards()
    {
        _cards.Children.Clear();
        var query = (_search.Text ?? "").Trim();
        foreach (var card in _catalog.OfType<JObject>().Where(card =>
                     _selected.ContainsKey((int)card["id"]!)
                     && ((string?)card["name"] ?? "").Contains(query, StringComparison.OrdinalIgnoreCase)))
        {
            var id = (int)card["id"]!;
            var row = new Grid
            {
                ColumnDefinitions = new ColumnDefinitions("58,*"),
                HorizontalAlignment = HorizontalAlignment.Stretch
            };
            if (CardImage(id, 48) is { } image)
            {
                image.HorizontalAlignment = HorizontalAlignment.Left;
                row.Children.Add(image);
            }
            var label = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            label.Children.Add(new TextBlock { Text = (string?)card["name"] ?? "" });
            label.Children.Add(new TextBlock
            {
                Text = $"已选 {_selected[id].Count} 项",
                FontSize = 12, Foreground = Brushes.Gray
            });
            Grid.SetColumn(label, 1);
            row.Children.Add(label);
            var button = new Button
            {
                Content = row,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                MinHeight = 68,
                Padding = new Thickness(10, 7)
            };
            button.Click += (_, _) => { _activeId = id; RenderDetails(); };
            _cards.Children.Add(button);
        }
        if (_cards.Children.Count == 0)
            _cards.Children.Add(new TextBlock { Text = "暂无已导入卡牌。点「导入卡牌」选择。", Margin = new Thickness(8) });
    }

    private void RenderDetails()
    {
        _details.Children.Clear();
        if (_activeId is not { } id || !_selected.ContainsKey(id))
        {
            _details.Children.Add(new TextBlock { Text = "选择左侧卡牌查看定制项目。", Margin = new Thickness(8) });
            return;
        }
        var card = FindCard(id);
        if (card == null) return;
        var header = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        if (CardImage(id, 76) is { } image) header.Children.Add(image);
        var title = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(new TextBlock { Text = (string?)card["name"] ?? "", FontSize = 20 });
        title.Children.Add(new TextBlock
        {
            Text = "保存后按所选定制项目执行",
            Foreground = Brushes.Gray, TextWrapping = TextWrapping.Wrap
        });
        header.Children.Add(title);
        _details.Children.Add(header);
        foreach (var option in (card["options"] as JArray ?? new JArray()).OfType<JObject>())
        {
            var optionId = (int)option["id"]!;
            var check = new CheckBox
            {
                Content = (string?)option["name"] ?? "",
                IsChecked = _selected[id].Contains(optionId), Margin = new Thickness(4, 5)
            };
            check.IsCheckedChanged += (_, _) =>
            {
                if (check.IsChecked == true) _selected[id].Add(optionId);
                else _selected[id].Remove(optionId);
                MarkChanged();
                RenderCards();
            };
            _details.Children.Add(check);
        }
        var remove = new Button { Content = "从面板移除这张卡", Margin = new Thickness(0, 18, 0, 0) };
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
    }

    private void ShowImportDialog()
    {
        var dialog = new SukiWindow
        {
            Title = $"导入可定制的强化卡 - {_profession}", Width = 780, Height = 690,
            MinWidth = 650, MinHeight = 480, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto"), Margin = new Thickness(12) };
        var search = new TextBox { Watermark = "输入卡名筛选；留空可翻页浏览全部卡牌" };
        var filters = new StackPanel { Spacing = 8, Margin = new Thickness(0, 0, 0, 8) };
        filters.Children.Add(search);
        var rarity = new ComboBox { ItemsSource = new[] { "稀有度：全部", "SSR", "SR", "R" }, SelectedIndex = 0, Width = 170 };
        var plan = new ComboBox { ItemsSource = new[] { "计划：全部", "不限", "感性", "理性", "非凡" }, SelectedIndex = 0, Width = 170 };
        var category = new ComboBox { ItemsSource = new[] { "分类：全部", "A 卡", "M 卡" }, SelectedIndex = 0, Width = 170 };
        var origin = new ComboBox { ItemsSource = new[] { "来源：全部", "无", "偶像卡", "支援卡" }, SelectedIndex = 0, Width = 170 };
        var status = new ComboBox { ItemsSource = new[] { "导入状态：全部", "未导入", "已导入" }, SelectedIndex = 0, Width = 170 };
        var optionCount = new ComboBox { ItemsSource = new[] { "定制项数：全部", "2项", "3项" }, SelectedIndex = 0, Width = 170 };
        var firstRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var filter in new[] { rarity, plan, category }) firstRow.Children.Add(filter);
        filters.Children.Add(firstRow);
        var secondRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var filter in new[] { origin, status, optionCount }) secondRow.Children.Add(filter);
        filters.Children.Add(secondRow);
        root.Children.Add(filters);
        var rows = new StackPanel { Spacing = 5 };
        var scroll = new ScrollViewer { Content = rows, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(scroll, 1);
        root.Children.Add(scroll);
        var footer = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var previous = new Button { Content = "上一页" };
        var next = new Button { Content = "下一页" };
        var count = new TextBlock { VerticalAlignment = VerticalAlignment.Center };
        footer.Children.Add(previous);
        footer.Children.Add(next);
        footer.Children.Add(count);
        Grid.SetRow(footer, 2);
        root.Children.Add(footer);
        const int pageSize = 30;
        var page = 0;
        bool MatchesOrigin(JObject card) => origin.SelectedIndex switch
        {
            1 => (string?)card["sourceType"] is "default" or "produce",
            2 => (string?)card["sourceType"] == "pIdol",
            3 => (string?)card["sourceType"] == "support",
            _ => true
        };
        void RenderPage(bool preserveScroll = false)
        {
            var offset = scroll.Offset;
            var query = (search.Text ?? "").Trim();
            var filtered = _catalog.OfType<JObject>()
                .Where(card => ((string?)card["name"] ?? "").Contains(query, StringComparison.OrdinalIgnoreCase)
                    && (rarity.SelectedIndex <= 0 || (string?)card["rarity"] == new[] { "", "SSR", "SR", "R" }[rarity.SelectedIndex])
                    && (plan.SelectedIndex <= 0 || (string?)card["plan"] == new[] { "", "free", "sense", "logic", "anomaly" }[plan.SelectedIndex])
                    && (category.SelectedIndex <= 0 || (string?)card["type"] == new[] { "", "active", "mental" }[category.SelectedIndex])
                    && MatchesOrigin(card)
                    && (status.SelectedIndex <= 0 || _selected.ContainsKey((int)card["id"]!) == (status.SelectedIndex == 2))
                    && (optionCount.SelectedIndex <= 0 || (card["options"] as JArray)?.Count == optionCount.SelectedIndex + 1))
                .OrderBy(card => (string?)card["rarity"] switch { "R" => 0, "SR" => 1, "SSR" => 2, _ => 3 })
                .ThenBy(card => (string?)card["sourceType"] switch { "default" or "produce" => 0, "pIdol" => 1, "support" => 2, _ => 3 })
                .ThenBy(card => (string?)card["type"] switch { "active" => 0, "mental" => 1, _ => 2 })
                .ThenBy(card => (string?)card["plan"] switch { "free" => 0, "sense" => 1, "logic" => 2, "anomaly" => 3, _ => 4 })
                .ThenBy(card => (int)card["id"]!)
                .ToList();
            var totalPages = Math.Max(1, (filtered.Count + pageSize - 1) / pageSize);
            page = Math.Clamp(page, 0, totalPages - 1);
            rows.Children.Clear();
            if (filtered.Count == 0)
                rows.Children.Add(new TextBlock { Text = "没有符合条件的卡牌", Margin = new Thickness(8, 16) });
            foreach (var card in filtered.Skip(page * pageSize).Take(pageSize))
            {
                var id = (int)card["id"]!;
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("58,*,80"), Margin = new Thickness(2) };
                if (CardImage(id, 52) is { } image) row.Children.Add(image);
                var label = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
                label.Children.Add(new TextBlock { Text = (string?)card["name"] ?? "", TextWrapping = TextWrapping.Wrap });
                label.Children.Add(new TextBlock
                {
                    Text = $"{(card["options"] as JArray)?.Count ?? 0} 个定制项目",
                    FontSize = 12, Foreground = Brushes.Gray
                });
                Grid.SetColumn(label, 1);
                row.Children.Add(label);
                var imported = _selected.ContainsKey(id);
                var add = new Button { Content = imported ? "已导入" : "导入", IsEnabled = !imported };
                add.Click += (_, _) =>
                {
                    if (_selected.ContainsKey(id)) return;
                    _selected[id] = new HashSet<int>();
                    _activeId = id;
                    MarkChanged();
                    RenderCards();
                    RenderDetails();
                    RenderPage(true);
                    LoggerHelper.UserAction("导入 HIF 技能卡定制卡牌",
                        $"职业={_profession}, 卡牌={card["name"]}, ID={id}",
                        operation: "HifCustomCardEditor");
                };
                Grid.SetColumn(add, 2);
                row.Children.Add(add);
                rows.Children.Add(row);
            }
            previous.IsEnabled = page > 0;
            next.IsEnabled = page + 1 < totalPages;
            count.Text = $"第 {page + 1}/{totalPages} 页；符合 {filtered.Count} 张，可导入 {filtered.Count(card => !_selected.ContainsKey((int)card["id"]!))} 张";
            scroll.Offset = preserveScroll ? offset : new Vector(0, 0);
        }
        search.TextChanged += (_, _) => { page = 0; RenderPage(); };
        foreach (var filter in new[] { rarity, plan, category, origin, status, optionCount })
            filter.SelectionChanged += (_, _) => { page = 0; RenderPage(); };
        previous.Click += (_, _) => { page--; RenderPage(); };
        next.Click += (_, _) => { page++; RenderPage(); };
        dialog.Content = root;
        RenderPage();
        _ = dialog.ShowDialog(this);
    }

    private void MarkChanged()
    {
        _save.IsEnabled = true;
        _status.Text = $"未保存：{_profession} 已导入 {_selected.Count} 张";
    }

    private void Save()
    {
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
            saved["sourceRevision"] = _revision;
            var profiles = saved["profiles"] as JObject ?? new JObject();
            foreach (var profession in Professions)
            {
                var cards = new JObject();
                foreach (var id in _profiles[profession].Keys.Order())
                    cards[id.ToString()] = new JArray(_profiles[profession][id].Order());
                profiles[profession] = cards;
            }
            saved["profiles"] = profiles;
            saved.Remove("cards");
            var temporary = _selectionPath + ".tmp";
            File.WriteAllText(temporary, saved.ToString(Formatting.Indented) + "\n", new UTF8Encoding(false));
            File.Move(temporary, _selectionPath, true);
            _originalHash = SelectionHash();
            _save.IsEnabled = false;
            var active = _selected.Values.Count(ids => ids.Count > 0);
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
