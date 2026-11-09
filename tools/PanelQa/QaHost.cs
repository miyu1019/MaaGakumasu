using System.Runtime.CompilerServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Threading;

/// <summary>策略面板现在嵌入「HIF 策略」页；验证时放进屏幕外的窗口里显示、截图和弹出对话框。</summary>
internal static class QaHost
{
    static readonly ConditionalWeakTable<Control, Window> Hosts = new();

    public static Window Window(Control panel)
    {
        if (panel is Window window) return window;
        if (Hosts.TryGetValue(panel, out var host)) return host;
        host = new Window
        {
            Width = 1000, Height = 700, Content = panel, ShowInTaskbar = false,
            WindowStartupLocation = WindowStartupLocation.Manual, Position = new PixelPoint(-10000, -10000)
        };
        Hosts.Add(panel, host);
        return host;
    }

    public static Window Show(Control panel)
    {
        var host = Window(panel);
        host.Show();
        Dispatcher.UIThread.RunJobs();
        return host;
    }

    public static void Close(Control panel)
    {
        if (panel is Window window) window.Close();
        else if (Hosts.TryGetValue(panel, out var host)) { host.Close(); Hosts.Remove(panel); }
    }

    /// <summary>面板或窗口的根内容（面板为 UserControl 的 Content）。</summary>
    public static Control Root(Control panel) => (Control)(panel is Window window ? window.Content! : ((UserControl)panel).Content!);
}
