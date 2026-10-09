using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace MFAAvalonia.Helper;

public static class IconHelper
{
    private static readonly Lazy<Bitmap> LazyIcon = new(() => LoadIconWithFallback(stream => new Bitmap(stream), CreateEmptyImage));
    private static readonly Lazy<WindowIcon> LazyWindowIcon = new(() => LoadIconWithFallback(stream => new WindowIcon(stream), () => new WindowIcon(Icon)));
    public static Bitmap Icon => LazyIcon.Value;
    public static WindowIcon WindowIcon => LazyWindowIcon.Value;

    private static T LoadIconWithFallback<T>(Func<Stream, T> load, Func<T> fallback)
    {
        try
        {
            var dataDirectory = AppPaths.DataRoot;
            var installDirectory = AppPaths.InstallRoot;

            // 尝试从 interface.json 同目录读取 icon 字段
            var interfacePath = AppPaths.InterfaceJsonPath;
            if (File.Exists(interfacePath))
            {
                var interfaceIcon = TryGetInterfaceIcon(interfacePath);
                if (!string.IsNullOrWhiteSpace(interfaceIcon))
                {
                    var interfaceIconPath = Path.Combine(dataDirectory, interfaceIcon);
                    if (File.Exists(interfaceIconPath))
                    {
                        using var fileStream = File.OpenRead(interfaceIconPath);
                        return load(fileStream);
                    }
                }
            }

            // 尝试从执行目录加载
            var iconPath = Path.Combine(installDirectory, "Assets", "logo.ico");
            if (!File.Exists(iconPath))
                iconPath = Path.Combine(installDirectory, "assets", "logo.ico");
            if (File.Exists(iconPath))
            {
                using var fileStream = File.OpenRead(iconPath);
                return load(fileStream);
            }

            // 尝试从嵌入资源加载
            var uri = new Uri("avares://MFAAvalonia.Core/Assets/logo.ico");
            if (AssetLoader.Exists(uri))
            {
                using var assets = AssetLoader.Open(uri);
                return load(assets);
            }

            LoggerHelper.Warning("未找到内嵌图标资源");
            return fallback();
        }
        catch (Exception ex)
        {
            LoggerHelper.Error($"图标加载失败: {ex}");
            return fallback();
        }
    }

    private static string? TryGetInterfaceIcon(string interfacePath)
    {
        try
        {
            var json = JsonHelper.LoadJson<JObject>(interfacePath, null);
            return json?["icon"]?.ToString();
        }
        catch
        {
            return null;
        }
    }

    private static Bitmap CreateEmptyImage()
    {
        // 创建 1x1 透明位图
        var size = new PixelSize(1, 1);
        var dpi = new Vector(96, 96);
        var format = PixelFormat.Rgba8888;

        var writeableBitmap = new WriteableBitmap(size, dpi, format);

        using var buffer = writeableBitmap.Lock();

        var pixels = new byte[buffer.Size.Width * buffer.Size.Height * 4];

        for (var i = 0; i < pixels.Length; i += 4)
        {
            pixels[i] = 0; // R
            pixels[i + 1] = 0; // G
            pixels[i + 2] = 0; // B
            pixels[i + 3] = 0; // A
        }

        Marshal.Copy(pixels, 0, buffer.Address, pixels.Length);


        return writeableBitmap;
    }
}
