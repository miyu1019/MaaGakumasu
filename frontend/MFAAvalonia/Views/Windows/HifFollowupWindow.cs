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

/// <summary>Ordered follow-up targets shared by card combinations and pre-card drinks.</summary>
public sealed class HifFollowupWindow : SukiWindow
{
    private readonly List<string> _targets;
    public HifFollowupWindow(string source, bool drink, JObject? saved, Action<JObject?> apply)
    {
        Title = drink ? "饮料关联卡牌" : $"后续卡 - {source}";
        Width = 860; Height = 740; MinWidth = 800; MinHeight = 600;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        _targets = (saved?["targets"] as JArray ?? new JArray()).Values<string>().Select(s => s!).ToList();
        var priority = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_catalog.json"), Encoding.UTF8));
        var catalog = JArray.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_target_card_catalog.json"), Encoding.UTF8))
            .OfType<JObject>().Where(c => priority[(string)c["name"]!] != null && (drink ? (string?)c["type"] == "active" : (string?)c["name"] != source)).ToArray();
        var images = JObject.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_priority_card_images.json"), Encoding.UTF8));
        var root = new Grid { Margin = new Thickness(18), RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto") };
        root.Children.Add(new TextBlock { Text = drink
            ? "原策略选中任一关联 A 卡时，先喝一瓶再出牌；不改变卡牌优先级。"
            : "起始卡确认打出后，按左侧顺序衔接第一张可用卡。所有卡仍遵守使用限制。", TextWrapping = TextWrapping.Wrap });
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
            settings.Children.Add(new TextBlock { Text = "0 回合不空过；关闭再次等待时，同手仍可衔接。「只用于衔接」在完成或超时后也禁止单独出牌。", TextWrapping = TextWrapping.Wrap, Foreground = Brushes.Gray });
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
                apply(rule); Close();
            }
            catch (InvalidOperationException exception) { error.Text = exception.Message; }
        };
        var cancel = new Button { Content = "取消" }; cancel.Click += (_, _) => Close();
        buttons.Children.Add(save); buttons.Children.Add(cancel); footer.Children.Add(buttons);
        Grid.SetRow(footer, 3); root.Children.Add(footer); Content = root;
    }
}
