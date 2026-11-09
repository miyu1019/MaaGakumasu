using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using MFAAvalonia.Helper;
using System;
using System.Collections.Generic;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>卡库可以加入的名单。Acquisition 为「获取与定制」：优先获取名单与技能卡定制名单在同一标签中编辑。</summary>
public enum HifLibraryTarget
{
    Priority,
    Acquisition,
    SwapOut
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

    private readonly List<string> _incompatible = new();

    /// <summary>加载时忽略的旧版本配置所属模块；页面顶部汇总提示，保存成功后清除。</summary>
    public IReadOnlyList<string> Incompatible => _incompatible;

    /// <summary>配置含旧版本格式的内容：忽略该部分并记下模块名供页面提示，不报错。</summary>
    protected void NoteIncompatible(string module)
    {
        if (_incompatible.Contains(module)) return;
        _incompatible.Add(module);
        using var _ = LoggerHelper.PushContext(source: "UI", operation: "HifStrategyPage");
        LoggerHelper.Warning($"HIF 配置不兼容：{module}含旧版本格式的内容，已忽略");
    }

    /// <summary>请求在「卡库」中为指定名单挑选卡牌。</summary>
    public event Action<HifLibraryTarget>? LibraryRequested;

    protected bool RequestLibrary(HifLibraryTarget target)
    {
        if (LibraryRequested == null) return false;
        LibraryRequested(target);
        return true;
    }

    /// <summary>名单为空时显示的「打开卡库添加」：跳到卡库并选好名单；名单有卡后不再显示。</summary>
    protected Button LibraryButton(HifLibraryTarget target)
    {
        var button = new Button { Content = "打开卡库添加", Classes = { "Flat" }, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(8, 0) };
        button.Click += (_, _) => RequestLibrary(target);
        return button;
    }

    public bool HasUnsavedChanges => SaveButton.IsEnabled;

    /// <summary>保存未保存的修改；保存失败时返回 false，面板状态栏显示原因。</summary>
    public bool TrySave()
    {
        if (HasUnsavedChanges) SaveCore();
        if (HasUnsavedChanges) return false;
        // 保存时已去掉被忽略的旧内容。
        _incompatible.Clear();
        return true;
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
