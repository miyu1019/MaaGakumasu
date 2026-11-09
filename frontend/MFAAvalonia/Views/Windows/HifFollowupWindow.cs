using Avalonia.Controls;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;
using System;

namespace MFAAvalonia.Views.Windows;

/// <summary>Ordered follow-up targets shared by card combinations and pre-card drinks.</summary>
public sealed class HifFollowupWindow : SukiWindow
{
    private readonly HifPanelHelp _help = new();

    public HifFollowupWindow(string source, bool drink, JObject? saved, Action<JObject?> apply)
    {
        Title = drink ? "饮料关联卡牌" : $"后续卡 - {source}";
        Width = 860; Height = 740; MinWidth = 800; MinHeight = 600;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Content = new HifFollowupEditor(source, drink, saved, apply, Close, _help);
    }
}
