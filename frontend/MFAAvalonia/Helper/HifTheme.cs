using Avalonia;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>HIF 面板的主题色：边框和分隔线跟随当前浅色/深色主题。</summary>
public static class HifTheme
{
    /// <summary>卡片与分隔线的浅色描边；按创建时的主题取色，面板重绘时随主题更新。</summary>
    public static IBrush LineBrush
    {
        get
        {
            var app = Application.Current;
            if (app != null && app.TryGetResource("SukiLightBorderBrush", app.ActualThemeVariant, out var value))
            {
                if (value is IBrush brush) return brush;
                if (value is Color color) return new SolidColorBrush(color);
            }
            return Brushes.LightGray;
        }
    }

    /// <summary>次要说明文字的透明度。</summary>
    public const double SecondaryOpacity = 0.65;
}
