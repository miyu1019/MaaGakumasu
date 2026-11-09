using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using MFAAvalonia.Helper;
using System;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>卡库可以加入的名单。</summary>
public enum HifLibraryTarget
{
    Priority,
    Acquisition,
    SwapOut,
    Followups,
    Customization
}

/// <summary>卡库中的一张卡。PriorityOnly 表示只在出牌优先级目录中（旧条目或 T 卡），不能加入优先获取卡。</summary>
public sealed record HifLibraryCard(int Id, string Name, string Rarity, string Plan, string Type, string Source, bool PriorityOnly);

/// <summary>「HIF 策略」页中的一个编辑面板：职业由页面统一切换，保存状态由页面汇总。</summary>
public abstract class HifStrategyPanel : UserControl
{
    public static readonly string[] Professions = ["好调", "集中", "好印象", "元気", "全力", "強気"];

    /// <summary>面板名称，用于日志和说明窗口标题。</summary>
    public string Title { get; protected set; } = "";

    /// <summary>未保存状态变化时触发。</summary>
    public event EventHandler? UnsavedChanged;

    protected abstract Button SaveButton { get; }

    protected abstract void SaveCore();

    public abstract string Profession { get; set; }

    /// <summary>页面「使用说明」抽屉显示的说明：每段第一行为标题，段落之间空一行。</summary>
    public virtual string HelpText => "";

    /// <summary>请求在「卡库」中为指定名单挑选卡牌；页面订阅后不再弹出导入窗口。</summary>
    public event Action<HifLibraryTarget>? LibraryRequested;

    protected bool RequestLibrary(HifLibraryTarget target)
    {
        if (LibraryRequested == null) return false;
        LibraryRequested(target);
        return true;
    }

    public bool HasUnsavedChanges => SaveButton.IsEnabled;

    /// <summary>保存未保存的修改；保存失败时返回 false，面板状态栏显示原因。</summary>
    public bool TrySave()
    {
        if (HasUnsavedChanges) SaveCore();
        return !HasUnsavedChanges;
    }

    /// <summary>对话框和文件选择器的所属窗口。</summary>
    protected Window OwnerWindow => TopLevel.GetTopLevel(this) as Window ?? Instances.RootView;

    protected HifStrategyPanel()
    {
        // 子类字段初始化先于本构造函数，保存按钮此时已存在。
        SaveButton.Classes.Add("Flat");
        SaveButton.PropertyChanged += (_, e) =>
        {
            if (e.Property == IsEnabledProperty) UnsavedChanged?.Invoke(this, EventArgs.Empty);
        };
    }

    /// <summary>底部状态栏：只显示状态文字。保存按钮在页面顶部，由页面调用 <see cref="TrySave"/>；面板自己的保存按钮只记录未保存状态。</summary>
    protected static Grid Footer(TextBlock status, Button save)
    {
        var footer = new Grid { Margin = new Thickness(0, 12, 0, 0) };
        status.TextWrapping = Avalonia.Media.TextWrapping.Wrap;
        status.VerticalAlignment = VerticalAlignment.Center;
        status.Opacity = HifTheme.SecondaryOpacity;
        footer.Children.Add(status);
        return footer;
    }
}
