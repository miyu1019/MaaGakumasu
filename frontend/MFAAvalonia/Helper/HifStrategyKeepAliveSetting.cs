using MFAAvalonia.Configuration;
using System;

namespace MFAAvalonia.Helper;

/// <summary>「允许 HIF 策略常驻」设置：默认开启，切换后立即应用到左侧导航。</summary>
public static class HifStrategyKeepAliveSetting
{
    public static bool Enabled => ConfigurationManager.Current.GetValue(ConfigurationKeys.HifStrategyKeepAlive, true);

    public static event Action<bool>? Changed;

    public static void Apply(bool enabled) => Changed?.Invoke(enabled);
}
