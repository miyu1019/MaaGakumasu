using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Views.Pages.Hif;
using System;

namespace MFAAvalonia.Helper;

/// <summary>任务设置中「HIF培育」的策略入口：跳转到「HIF 策略」页的对应面板。</summary>
public static class HifStrategyLinks
{
    public const string Tag = "hif-strategy-links";

    private static readonly (HifStrategyTab Tab, string Label)[] Links =
    [
        (HifStrategyTab.Priority, "出牌优先级"),
        (HifStrategyTab.Acquisition, "优先获取卡"),
        (HifStrategyTab.SwapOut, "授业换出卡"),
        (HifStrategyTab.Followups, "后续卡"),
        (HifStrategyTab.Drinks, "饮料"),
        (HifStrategyTab.Customization, "技能卡定制")
    ];

    /// <summary>旧任务选项名对应的策略面板；不是策略选项时返回 null。</summary>
    public static HifStrategyTab? TabOf(string? legacyOptionName) => legacyOptionName switch
    {
        "HIF战斗出牌优先级配置" => HifStrategyTab.Priority,
        "HIF优先获取卡配置" => HifStrategyTab.Acquisition,
        "HIF授业优先换出卡配置" => HifStrategyTab.SwapOut,
        "HIF饮料配置" => HifStrategyTab.Drinks,
        "HIF自定义卡配置" => HifStrategyTab.Customization,
        _ => null
    };

    public static Control Create(Func<string> profession, Action<Control> bindIdleEnabled)
    {
        var section = new StackPanel { Spacing = 6, Margin = new Thickness(10, 8), Tag = Tag };
        section.Children.Add(new TextBlock { Text = "HIF 策略", FontWeight = FontWeight.SemiBold });
        section.Children.Add(new TextBlock
        {
            Text = "按「角色职业」所选职业编辑，也可从左侧「HIF 策略」页进入。", FontSize = 12, Opacity = 0.7, TextWrapping = TextWrapping.Wrap
        });
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var (tab, label) in Links)
        {
            var button = new Button { Content = label, Padding = new Thickness(12, 4), Margin = new Thickness(0, 0, 6, 6) };
            bindIdleEnabled(button);
            button.Click += (_, _) =>
            {
                var selected = profession();
                LoggerHelper.UserAction("打开 HIF 策略页", $"职业={selected}, 面板={label}", operation: "HifStrategyPage");
                HifStrategyView.Navigate(selected, tab);
            };
            buttons.Children.Add(button);
        }
        section.Children.Add(buttons);
        return section;
    }
}
