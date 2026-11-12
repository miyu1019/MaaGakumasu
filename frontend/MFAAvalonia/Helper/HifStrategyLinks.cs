using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Views.Pages.Hif;
using System;

namespace MFAAvalonia.Helper;

/// <summary>任务设置中「HIF培育」的策略入口：一个按钮，跳转到「HIF 策略」页并切到任务所选职业。</summary>
public static class HifStrategyLinks
{
    public const string Tag = "hif-strategy-links";

    /// <summary>旧任务选项名对应的策略面板；不是策略选项时返回 null。</summary>
    public static HifStrategyTab? TabOf(string? legacyOptionName) => legacyOptionName switch
    {
        "HIF战斗出牌优先级配置" => HifStrategyTab.Priority,
        "HIF优先获取卡配置" => HifStrategyTab.Customization,
        "HIF授业优先换出卡配置" => HifStrategyTab.SwapOut,
        "HIF饮料配置" => HifStrategyTab.Drinks,
        "HIF自定义卡配置" => HifStrategyTab.Customization,
        _ => null
    };

    public static Control Create(Func<string> profession, Action<Control> bindIdleEnabled)
    {
        // 与其他任务选项同样的一行：名称在左，按钮在右；页面打开后停在上次所在的标签。
        var button = new Button { Content = "HIF 策略", Padding = new Thickness(16, 4), VerticalAlignment = VerticalAlignment.Center };
        ToolTip.SetTip(button, "打开「HIF 策略」页，按「角色职业」所选职业编辑出牌优先级、获取与定制、授业换出卡、出牌编排和饮料；也可从左侧导航进入。");
        bindIdleEnabled(button);
        button.Click += (_, _) =>
        {
            var selected = profession();
            LoggerHelper.UserAction("打开 HIF 策略页", $"职业={selected}", operation: "HifStrategyPage");
            HifStrategyView.Navigate(selected);
        };
        var section = new DockPanel { Margin = new Thickness(10, 8), Tag = Tag };
        DockPanel.SetDock(button, Dock.Right);
        section.Children.Add(button);
        section.Children.Add(new TextBlock { Text = "策略配置", VerticalAlignment = VerticalAlignment.Center });
        return section;
    }
}
