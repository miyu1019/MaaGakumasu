using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.Views.Windows;


/// <summary>后续卡与饮料关联卡的编辑内容；可放在对话框中，也可嵌入「HIF 策略」页的卡牌详情。</summary>
public sealed class HifFollowupEditor : UserControl
{
    private readonly List<string> _targets;
    private readonly HifPanelHelp _help;
    /// <param name="compact">嵌入卡牌详情时使用：只保留有序名单、一个搜索框和必要设置，不显示筛选与翻页。</param>
    public HifFollowupEditor(string source, bool drink, JObject? saved, Action<JObject?> apply, Action close, HifPanelHelp? help = null, bool compact = false)
    {
        _help = help ?? new HifPanelHelp();
        _targets = (saved?["targets"] as JArray ?? new JArray()).Values<string>().Select(s => s!).ToList();
        var priority = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_catalog.json"), Encoding.UTF8));
        var catalog = JArray.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_target_card_catalog.json"), Encoding.UTF8))
            .OfType<JObject>().Where(c => priority[(string)c["name"]!] != null && (drink ? (string?)c["type"] == "active" : (string?)c["name"] != source)).ToArray();
        var images = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_images.json"), Encoding.UTF8));
        if (compact)
        {
            Content = BuildCompact(source, drink, saved, apply, close, catalog, images);
            return;
        }
        var root = new Grid { Margin = new Thickness(18), RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto") };
        root.Children.Add(_help.CreateButton(this, drink ? "饮料关联卡牌 · 使用说明" : "后续卡 · 使用说明", drink
            ? "已关联候选卡\n原策略选中任一关联 A 卡时，先使用一瓶饮料，再重新定位并打出目标卡。不改变卡牌优先级，所有用饮仍受一战开关及瓶数上限限制。\n\n" +
              "添加与移除\n仅可选择 A 卡。清空关联后恢复普通使用时机。\n\n应用到本次编辑\n返回饮料面板，点击主面板保存后生效。"
            : "后续卡优先顺序\n起始卡确认打出后，按左侧顺序衔接第一张可用卡，所有卡仍遵守使用限制。\n\n" +
              "等待回合\n设置等待后续卡的回合数，0 表示不空过等待；同手可用时仍可衔接。\n\n" +
              "使用初星黒酢\n等待组合时尝试使用一瓶初星黒酢，遵守饮料面板的一战开关、单种和总上限。\n\n" +
              "再次等待\n开启后，完成一次组合又遇到起始卡时继续等待。关闭后不再次空过，但同手仍可衔接。\n\n" +
              "正常出牌\n后续卡可以按普通优先级单独使用。\n\n首次组合前保留\n首次组合完成前为衔接保留后续卡。\n\n" +
              "只用于衔接\n完成组合或等待超时后，后续卡仍不能单独出牌。\n\n" +
              "添加、移除与上下移动\n编辑候选卡及其衔接优先顺序。\n\n应用到本次编辑\n返回优先级面板，点击主面板保存后生效。"));
        var settings = new StackPanel { Spacing = 7, Margin = new Thickness(0, 10) };
        var turns = new NumericUpDown { Minimum = 0, Maximum = 6, Increment = 1, Value = (decimal?)saved?["wait_turns"] ?? 3, Width = 75 };
        var vinegar = new CheckBox { Content = "使用初星黒酢", IsChecked = (bool?)saved?["use_black_vinegar"] == true };
        var repeat = new CheckBox { Content = "完成一次组合后，再次遇到起始卡继续等待", IsChecked = (bool?)saved?["wait_after_success"] == true };
        var policy = new ComboBox { ItemsSource = new[] { "正常出牌", "首次组合前保留", "只用于衔接" }, SelectedIndex = Math.Max(0, Array.IndexOf(HifFollowupConfig.Policies, (string?)saved?["target_policy"])), Width = 180 };
        if (!drink)
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
            row.Children.Add(new TextBlock { Text = "等待回合", VerticalAlignment = VerticalAlignment.Center }); row.Children.Add(turns); row.Children.Add(vinegar);
            row.Children.Add(new TextBlock { Text = "后续卡使用方式", VerticalAlignment = VerticalAlignment.Center }); row.Children.Add(policy);
            settings.Children.Add(row); settings.Children.Add(repeat);
        }
        Grid.SetRow(settings, 1); root.Children.Add(settings);
        var columns = new Grid { ColumnDefinitions = new ColumnDefinitions("*,*") };
        var selected = new StackPanel { Spacing = 5 };
        var left = new StackPanel { Spacing = 7 };
        left.Children.Add(new TextBlock { Text = drink ? "已关联候选卡" : "后续卡优先顺序", FontSize = 17 });
        var leftGrid = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(0, 0, 12, 0) };
        leftGrid.Children.Add(left); var leftScroll = new ScrollViewer { Content = selected }; Grid.SetRow(leftScroll, 1); leftGrid.Children.Add(leftScroll);
        columns.Children.Add(leftGrid);
        var right = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto") };
        var filters = new StackPanel { Spacing = 6, Margin = new Thickness(0, 0, 0, 8) };
        var search = new TextBox { Watermark = "搜索卡名" }; filters.Children.Add(search);
        var rarity = new ComboBox { ItemsSource = new[] { "稀有度：全部", "LR", "SSR", "SR", "R", "N" }, SelectedIndex = 0, Width = 175 };
        var plan = new ComboBox { ItemsSource = new[] { "计划：全部", "不限", "感性", "理性", "非凡" }, SelectedIndex = 0, Width = 175 };
        var category = new ComboBox { ItemsSource = drink ? new[] { "分类：A 卡" } : new[] { "分类：全部", "A 卡", "M 卡", "T 卡" }, SelectedIndex = 0, Width = 175, IsEnabled = !drink };
        var origin = new ComboBox { ItemsSource = new[] { "来源：全部", "无", "偶像卡", "支援卡" }, SelectedIndex = 0, Width = 175 };
        foreach (var pair in new[] { new[] { rarity, plan }, new[] { category, origin } })
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
            foreach (var filter in pair) row.Children.Add(filter);
            filters.Children.Add(row);
        }
        right.Children.Add(filters);
        var available = new StackPanel { Spacing = 5 }; var rightScroll = new ScrollViewer { Content = available }; Grid.SetRow(rightScroll, 1); right.Children.Add(rightScroll);
        Grid.SetColumn(right, 1); columns.Children.Add(right); Grid.SetRow(columns, 2); root.Children.Add(columns);
        var navigation = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var previous = new Button { Content = "上一页" }; var next = new Button { Content = "下一页" };
        var count = new TextBlock { VerticalAlignment = VerticalAlignment.Center };
        navigation.Children.Add(previous); navigation.Children.Add(next); navigation.Children.Add(count);
        Grid.SetRow(navigation, 2); right.Children.Add(navigation);
        const int pageSize = 40;
        var page = 0;
        Control Label(string key)
        {
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("42,*") };
            if (images[key]?.Type == JTokenType.Integer)
            {
                var path = Path.Combine(HifLayout.ImageRoot, "hif", "hif_card_icons", $"{(int)images[key]!}.webp");
                if (File.Exists(path)) row.Children.Add(new Image { Source = new Bitmap(path), Width = 36, Height = 48, Stretch = Stretch.Uniform });
            }
            var text = new TextBlock { Text = key, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(text, 1); row.Children.Add(text);
            return row;
        }
        void Render()
        {
            selected.Children.Clear(); available.Children.Clear();
            foreach (var key in _targets.ToArray())
            {
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,32,32,44") };
                row.Children.Add(Label(key));
                foreach (var (text, delta, column) in new[] { ("↑", -1, 1), ("↓", 1, 2) })
                {
                    var button = new Button { Content = text, Padding = new Thickness(4), IsEnabled = _targets.IndexOf(key) + delta >= 0 && _targets.IndexOf(key) + delta < _targets.Count };
                    button.Click += (_, _) => { var index = _targets.IndexOf(key); (_targets[index], _targets[index + delta]) = (_targets[index + delta], _targets[index]); Render(); };
                    Grid.SetColumn(button, column); row.Children.Add(button);
                }
                var remove = new Button { Content = "删除", Padding = new Thickness(3) }; remove.Click += (_, _) => { _targets.Remove(key); Render(); };
                Grid.SetColumn(remove, 3); row.Children.Add(remove); selected.Children.Add(row);
            }
            var filtered = catalog.Where(c => !_targets.Contains((string)c["name"]!)
                && ((string)c["name"]!).Contains(search.Text?.Trim() ?? "", StringComparison.OrdinalIgnoreCase)
                && (rarity.SelectedIndex == 0 || (string?)c["rarity"] == new[] { "", "L", "SSR", "SR", "R", "N" }[rarity.SelectedIndex] || rarity.SelectedIndex == 1 && (string?)c["rarity"] == "LR")
                && (plan.SelectedIndex == 0 || (string?)c["plan"] == new[] { "", "free", "sense", "logic", "anomaly" }[plan.SelectedIndex])
                && (drink || category.SelectedIndex == 0 || (string?)c["type"] == new[] { "", "active", "mental", "trouble" }[category.SelectedIndex])
                && (origin.SelectedIndex switch { 1 => (string?)c["sourceType"] is "default" or "produce", 2 => (string?)c["sourceType"] == "pIdol", 3 => (string?)c["sourceType"] == "support", _ => true }))
                .OrderBy(c => (string?)c["type"] == "trouble" ? 0 : 1)
                .ThenBy(c => (string?)c["rarity"] switch { "N" => 0, "R" => 1, "SR" => 2, "SSR" => 3, "L" or "LR" => 4, _ => 5 })
                .ThenBy(c => (string?)c["sourceType"] switch { "default" or "produce" => 0, "pIdol" => 1, "support" => 2, _ => 3 })
                .ThenBy(c => (string?)c["type"] switch { "active" => 0, "mental" => 1, "trouble" => 2, _ => 3 })
                .ThenBy(c => (string?)c["plan"] switch { "free" => 0, "sense" => 1, "logic" => 2, "anomaly" => 3, _ => 4 })
                .ThenBy(c => (int?)c["id"] ?? int.MaxValue)
                .ToArray();
            var pages = Math.Max(1, (filtered.Length + pageSize - 1) / pageSize);
            page = Math.Clamp(page, 0, pages - 1);
            foreach (var card in filtered.Skip(page * pageSize).Take(pageSize))
            {
                var key = (string)card["name"]!;
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,50") }; row.Children.Add(Label(key));
                var add = new Button { Content = "添加", Padding = new Thickness(5) }; add.Click += (_, _) => { _targets.Add(key); Render(); };
                Grid.SetColumn(add, 1); row.Children.Add(add); available.Children.Add(row);
            }
            if (filtered.Length == 0) available.Children.Add(new TextBlock { Text = "没有符合条件的卡牌" });
            previous.IsEnabled = page > 0; next.IsEnabled = page + 1 < pages;
            count.Text = $"{page + 1}/{pages} 页 · {filtered.Length} 张";
        }
        search.TextChanged += (_, _) => { page = 0; Render(); };
        foreach (var filter in new[] { rarity, plan, category, origin }) filter.SelectionChanged += (_, _) => { page = 0; Render(); };
        previous.Click += (_, _) => { page--; Render(); }; next.Click += (_, _) => { page++; Render(); };
        Render();
        var footer = new StackPanel { Spacing = 5, Margin = new Thickness(0, 10, 0, 0) };
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap }; footer.Children.Add(error);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        var save = new Button { Content = "应用到本次编辑" };
        save.Click += (_, _) =>
        {
            try
            {
                if (!drink && (turns.Value is not { } value || value % 1 != 0)) throw new InvalidOperationException("等待回合必须为0至6的整数");
                JObject? rule = _targets.Count == 0 ? null : new JObject { ["targets"] = new JArray(_targets) };
                if (rule != null && !drink)
                {
                    rule["wait_turns"] = (int)turns.Value!.Value; rule["use_black_vinegar"] = vinegar.IsChecked == true;
                    rule["wait_after_success"] = repeat.IsChecked == true; rule["target_policy"] = HifFollowupConfig.Policies[policy.SelectedIndex];
                }
                apply(rule); close();
            }
            catch (InvalidOperationException exception) { error.Text = exception.Message; }
        };
        var cancel = new Button { Content = "取消" }; cancel.Click += (_, _) => close();
        buttons.Children.Add(save); buttons.Children.Add(cancel); footer.Children.Add(buttons);
        Grid.SetRow(footer, 3); root.Children.Add(footer); Content = root;
    }

    private static Control CardLabel(JObject images, string key, double size)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        if (images[key]?.Type == JTokenType.Integer)
        {
            var path = Path.Combine(HifLayout.ImageRoot, "hif", "hif_card_icons", $"{(int)images[key]!}.webp");
            if (File.Exists(path)) row.Children.Add(new Image { Source = new Bitmap(path), Width = size, Height = size, Stretch = Stretch.Uniform });
        }
        row.Children.Add(new TextBlock { Text = key, VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis });
        return row;
    }

    /// <summary>精简版：有序名单 + 搜索添加；有后续卡时才显示等待与使用方式设置。</summary>
    private Control BuildCompact(string source, bool drink, JObject? saved, Action<JObject?> apply, Action close, JObject[] catalog, JObject images)
    {
        var root = new StackPanel { Spacing = 10 };
        root.Children.Add(new TextBlock
        {
            Text = drink ? "原策略选中以下任一 A 卡时，先用饮料再出牌。" : $"「{source}」打出后，按下列顺序衔接第一张可用的卡。",
            TextWrapping = TextWrapping.Wrap, Opacity = HifTheme.SecondaryOpacity
        });
        var list = new StackPanel { Spacing = 4 };
        root.Children.Add(list);
        var search = new TextBox { Watermark = drink ? "输入卡名添加 A 卡…" : "输入卡名添加后续卡…" };
        root.Children.Add(search);
        var results = new WrapPanel { Orientation = Orientation.Horizontal };
        root.Children.Add(results);

        var turns = new NumericUpDown { Minimum = 0, Maximum = 6, Increment = 1, Value = (decimal?)saved?["wait_turns"] ?? 3, Width = 90 };
        var vinegar = new CheckBox { Content = "使用初星黒酢", IsChecked = (bool?)saved?["use_black_vinegar"] == true };
        var repeat = new CheckBox { Content = "再次遇到起始卡继续等待", IsChecked = (bool?)saved?["wait_after_success"] == true };
        var policy = new ComboBox { ItemsSource = new[] { "正常出牌", "首次组合前保留", "只用于衔接" }, SelectedIndex = Math.Max(0, Array.IndexOf(HifFollowupConfig.Policies, (string?)saved?["target_policy"])), Width = 160 };
        ToolTip.SetTip(turns, "等待后续卡的回合数；0 表示不空过等待，同手可用时仍可衔接");
        ToolTip.SetTip(vinegar, "等待组合时尝试使用一瓶初星黒酢，遵守饮料面板的一战开关和上限");
        ToolTip.SetTip(repeat, "完成一次组合后再遇到起始卡时继续等待；关闭后不再空过，但同手仍可衔接");
        ToolTip.SetTip(policy, "正常出牌：后续卡可单独使用；首次组合前保留：首次组合完成前留给衔接；只用于衔接：始终不单独出牌");
        var settings = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var (label, control) in new (string?, Control)[] { ("等待回合", turns), ("使用方式", policy), (null, vinegar), (null, repeat) })
        {
            var item = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, Margin = new Thickness(0, 0, 16, 6) };
            if (label != null) item.Children.Add(new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center });
            item.Children.Add(control);
            settings.Children.Add(item);
        }
        if (!drink) root.Children.Add(settings);

        void Render()
        {
            list.Children.Clear();
            if (_targets.Count == 0)
                list.Children.Add(new TextBlock { Text = drink ? "还没有关联卡。" : "还没有后续卡。", Opacity = HifTheme.SecondaryOpacity });
            for (var index = 0; index < _targets.Count; index++)
            {
                var key = _targets[index];
                var row = new Grid { ColumnDefinitions = new ColumnDefinitions("24,*,Auto") };
                row.Children.Add(new TextBlock { Text = $"{index + 1}", VerticalAlignment = VerticalAlignment.Center, Opacity = HifTheme.SecondaryOpacity });
                var label = CardLabel(images, key, 32);
                Grid.SetColumn(label, 1); row.Children.Add(label);
                var buttons = new StackPanel { Orientation = Orientation.Horizontal };
                foreach (var (text, delta) in new[] { ("↑", -1), ("↓", 1) })
                {
                    var target = index + delta;
                    var move = new Button { Content = text, Classes = { "Basic" }, Padding = new Thickness(8, 2), IsEnabled = target >= 0 && target < _targets.Count };
                    var from = index;
                    move.Click += (_, _) => { (_targets[from], _targets[target]) = (_targets[target], _targets[from]); Render(); };
                    buttons.Children.Add(move);
                }
                var remove = new Button { Content = "×", Classes = { "Basic" }, Padding = new Thickness(8, 2) };
                ToolTip.SetTip(remove, "移出");
                remove.Click += (_, _) => { _targets.Remove(key); Render(); };
                buttons.Children.Add(remove);
                Grid.SetColumn(buttons, 2); row.Children.Add(buttons);
                list.Children.Add(row);
            }
            settings.IsVisible = !drink && _targets.Count > 0;
            results.Children.Clear();
            var query = (search.Text ?? "").Trim().TrimEnd('+');
            if (query == "") return;
            var matches = catalog.Where(c => !_targets.Contains((string)c["name"]!) && ((string)c["name"]!).Contains(query, StringComparison.OrdinalIgnoreCase))
                .OrderBy(c => (string?)c["rarity"] switch { "L" or "LR" => 0, "SSR" => 1, "SR" => 2, "R" => 3, _ => 4 })
                .Take(8).ToArray();
            if (matches.Length == 0) results.Children.Add(new TextBlock { Text = "没有匹配的卡", Opacity = HifTheme.SecondaryOpacity });
            foreach (var card in matches)
            {
                var key = (string)card["name"]!;
                var add = new Button { Content = CardLabel(images, key, 28), Margin = new Thickness(0, 0, 6, 6), Padding = new Thickness(6, 3) };
                ToolTip.SetTip(add, "加入名单末尾");
                add.Click += (_, _) => { _targets.Add(key); search.Text = ""; Render(); };
                results.Children.Add(add);
            }
        }
        search.TextChanged += (_, _) => Render();
        Render();

        var error = new TextBlock { TextWrapping = TextWrapping.Wrap };
        root.Children.Add(error);
        var footer = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var save = new Button { Content = "应用到本次编辑", Classes = { "Flat" } };
        save.Click += (_, _) =>
        {
            try
            {
                if (!drink && (turns.Value is not { } value || value % 1 != 0)) throw new InvalidOperationException("等待回合必须为0至6的整数");
                JObject? rule = _targets.Count == 0 ? null : new JObject { ["targets"] = new JArray(_targets) };
                if (rule != null && !drink)
                {
                    rule["wait_turns"] = (int)turns.Value!.Value; rule["use_black_vinegar"] = vinegar.IsChecked == true;
                    rule["wait_after_success"] = repeat.IsChecked == true; rule["target_policy"] = HifFollowupConfig.Policies[policy.SelectedIndex];
                }
                apply(rule); close();
            }
            catch (InvalidOperationException exception) { error.Text = exception.Message; }
        };
        var cancel = new Button { Content = "取消", Classes = { "Basic" } };
        cancel.Click += (_, _) => close();
        footer.Children.Add(save); footer.Children.Add(cancel);
        root.Children.Add(footer);
        return root;
    }
}
