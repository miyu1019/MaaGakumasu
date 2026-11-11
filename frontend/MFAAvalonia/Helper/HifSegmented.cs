using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using System;
using System.Collections.Generic;
using System.Linq;

namespace MFAAvalonia.Helper;

/// <summary>分段选择：一排胶囊按钮，选中项高亮。点击只发出请求，由调用方决定是否切换（例如先确认未保存修改）。</summary>
public sealed class HifSegmented : Border
{
    private readonly List<Button> _items = new();

    /// <summary>用户点击某一项；参数为序号。</summary>
    public event Action<int>? SelectionRequested;

    public int SelectedIndex { get; private set; } = -1;

    public IReadOnlyList<Button> Items => _items;

    public HifSegmented(IEnumerable<string> labels)
    {
        CornerRadius = new CornerRadius(10);
        Padding = new Thickness(3);
        HorizontalAlignment = HorizontalAlignment.Left;
        VerticalAlignment = VerticalAlignment.Center;
        this[!BackgroundProperty] = new DynamicResourceExtension("SukiPrimaryColor5");
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2 };
        foreach (var (label, index) in labels.Select((label, index) => (label, index)))
        {
            var button = new Button
            {
                Content = label, Padding = new Thickness(14, 5), CornerRadius = new CornerRadius(8),
                Classes = { "Basic" }, MinHeight = 0
            };
            button.Click += (_, _) => SelectionRequested?.Invoke(index);
            _items.Add(button);
            row.Children.Add(button);
        }
        Child = row;
    }

    public void Select(int index)
    {
        SelectedIndex = index;
        Highlight(i => i == index);
    }

    /// <summary>多选筛选用：同时高亮多项，<see cref="SelectedIndex"/> 取第一项。</summary>
    public void SelectMany(IReadOnlyCollection<int> indexes)
    {
        SelectedIndex = indexes.Count == 0 ? -1 : indexes.Min();
        Highlight(indexes.Contains);
    }

    private void Highlight(Func<int, bool> selected)
    {
        for (var i = 0; i < _items.Count; i++)
        {
            var button = _items[i];
            if (selected(i))
            {
                button[!TemplatedControl.BackgroundProperty] = new DynamicResourceExtension("SukiCardBackground");
                button[!TemplatedControl.ForegroundProperty] = new DynamicResourceExtension("SukiPrimaryColor");
                button.FontWeight = FontWeight.SemiBold;
            }
            else
            {
                button.ClearValue(TemplatedControl.BackgroundProperty);
                button.ClearValue(TemplatedControl.ForegroundProperty);
                button.FontWeight = FontWeight.Normal;
            }
        }
    }
}
