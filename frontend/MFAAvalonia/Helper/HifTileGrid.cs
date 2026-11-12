using Avalonia;
using Avalonia.Controls.Primitives;
using System;

namespace MFAAvalonia.Helper;

/// <summary>
/// 卡片网格：按可用宽度决定每行张数，卡片等宽并铺满整行，右侧不留空白。
/// 窗口变宽或变窄时每行张数随之变化；卡片不少于 <see cref="MinTileWidth"/>。
/// </summary>
public sealed class HifTileGrid : UniformGrid
{
    /// <summary>卡牌名单卡片的最小宽度：获取与定制、授业换出卡共用，两页每行张数一致。</summary>
    public const double CardTileWidth = 340;

    public double MinTileWidth { get; init; } = 296;

    protected override Size MeasureOverride(Size availableSize)
    {
        Columns = double.IsInfinity(availableSize.Width)
            ? Math.Max(1, Children.Count)
            : Math.Max(1, (int)(availableSize.Width / MinTileWidth));
        return base.MeasureOverride(availableSize);
    }
}
