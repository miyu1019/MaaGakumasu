using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using SukiUI.Controls;
using System;

namespace MFAAvalonia.Helper;

/// <summary>HIF 编辑面板共用的非模态说明窗口。</summary>
public sealed class HifPanelHelp
{
    public SukiWindow? ActiveWindow { get; private set; }

    public Button CreateButton(Window owner, string title, string text)
    {
        var button = new Button { Content = "使用说明" };
        button.Click += (_, _) => Show(owner, title, text);
        return button;
    }

    public void Show(Window owner, string title, string text)
    {
        if (ActiveWindow != null)
        {
            ActiveWindow.Activate();
            return;
        }
        var window = new SukiWindow
        {
            Title = title, Width = 660, Height = 650,
            MinWidth = 460, MinHeight = 350, WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        var root = new Grid { Margin = new Thickness(18), RowDefinitions = new RowDefinitions("Auto,*,Auto"), RowSpacing = 12 };
        var heading = new StackPanel { Spacing = 4 };
        heading.Children.Add(new TextBlock { Text = title, FontSize = 21, FontWeight = FontWeight.Bold });
        heading.Children.Add(new TextBlock { Text = "可保持打开，同时编辑配置。", FontSize = 13, Foreground = Brushes.Gray });
        root.Children.Add(heading);
        var sections = new StackPanel { Spacing = 10 };
        foreach (var paragraph in text.Split("\n\n", StringSplitOptions.RemoveEmptyEntries))
        {
            var lines = paragraph.Split('\n', 2);
            var section = new StackPanel { Spacing = 6 };
            if (lines.Length == 2)
                section.Children.Add(new TextBlock { Text = lines[0], FontSize = 16, FontWeight = FontWeight.Bold, TextWrapping = TextWrapping.Wrap });
            section.Children.Add(new TextBlock { Text = lines[^1], FontSize = 14, LineHeight = 22, TextWrapping = TextWrapping.Wrap });
            sections.Children.Add(new Border
            {
                Child = section, Padding = new Thickness(12), CornerRadius = new CornerRadius(7),
                BorderBrush = Brushes.LightGray, BorderThickness = new Thickness(1)
            });
        }
        var scroll = new ScrollViewer { Content = sections };
        Grid.SetRow(scroll, 1); root.Children.Add(scroll);
        var close = new Button { Content = "关闭说明", HorizontalAlignment = HorizontalAlignment.Right };
        close.Click += (_, _) => window.Close();
        Grid.SetRow(close, 2); root.Children.Add(close);
        window.Content = root;
        window.Closed += (_, _) => ActiveWindow = null;
        ActiveWindow = window;
        window.Show(owner);
    }
}
