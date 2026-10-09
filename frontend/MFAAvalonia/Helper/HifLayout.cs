using Newtonsoft.Json.Linq;
using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

/// <summary>Local composition owns HIF data; manual updates use this Fork's HIF Releases.</summary>
public static class HifLayout
{
    public static bool IsIndependent => File.Exists(Path.Combine(AppPaths.DataRoot, "extensions", "hif", "tasks", "produce_hif.json"));
    public static string ImageRoot => Path.Combine(AppPaths.DataRoot, "extensions", "hif", "resource", "base", "image");
    public static string PersonalPath(string name) => Path.Combine(AppPaths.DataRoot, "config", "hif", name);
    public static string CatalogPath(string name) => Path.Combine(AppPaths.DataRoot, "extensions", "hif", "catalog", name);
    public static string? LegacyOptionName(string? name) => name?.StartsWith("HIF.", StringComparison.Ordinal) == true ? name[4..] : name;
    public static System.Collections.Generic.List<string> ResourcePaths(string? resource)
    {
        var variants = JObject.Parse(File.ReadAllText(Path.Combine(AppPaths.DataRoot, "extensions", "hif", "resource-variants.json")));
        var layers = variants[resource ?? "官服"]?.ToObject<string[]>()
            ?? throw new InvalidOperationException("HIF 不支持所选资源");
        var paths = new System.Collections.Generic.List<string>();
        foreach (var layer in layers)
            paths.Add(Path.Combine(AppPaths.DataRoot, "extensions", "hif", "resource", layer));
        return paths;
    }

    public static async Task RunManualUpdateAsync(string command, string? package = null)
    {
        var info = new ProcessStartInfo
        {
            FileName = Path.Combine(AppPaths.DataRoot, "python", "python.exe"),
            WorkingDirectory = AppPaths.DataRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        info.Environment["PYTHONUTF8"] = "1";
        info.ArgumentList.Add(Path.Combine(AppPaths.DataRoot, "tools", "hif_app.py"));
        info.ArgumentList.Add(command);
        if (package != null)
        {
            info.ArgumentList.Add("--package");
            info.ArgumentList.Add(package);
        }
        try
        {
            using var process = Process.Start(info) ?? throw new IOException("无法启动手动更新工具");
            var output = process.StandardOutput.ReadToEndAsync();
            var error = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            var result = await output;
            var diagnostics = await error;
            if (process.ExitCode != 0)
                throw new IOException(diagnostics.Trim().Split('\n')[^1]);
            LoggerHelper.Info($"HIF 手动更新：{result.Trim()}");
            var data = Newtonsoft.Json.Linq.JObject.Parse(result.Trim().Split('\n')[^1]);
            var message = command == "check"
                ? data["latest"]?.Type == JTokenType.Null ? $"当前 HIF：{data["installed"]}；本 Fork 尚未发布 HIF 运行包。"
                : $"当前 HIF：{data["installed"]}；最新发布：{data["latest"]}"
                : data["skipped"]?.Value<bool>() == true ? "已是最新 HIF 版本，HIF 配置保持原样。"
                : "HIF 配置已保留，请重启前台加载新版本。";
            DispatcherHelper.PostOnMainThread(() => ToastHelper.Info(
                command == "check" ? "HIF 版本检查" : "HIF 更新完成",
                message, 10000));
        }
        catch (Exception error)
        {
            LoggerHelper.Error("HIF 手动更新失败，原版本已保留", error);
            DispatcherHelper.PostOnMainThread(() => ToastHelper.Info("HIF 更新失败", $"原版本已保留。{error.Message}", 15000));
        }
    }
}
