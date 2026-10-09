using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

/// <summary>Preparation may run alongside tasks; only the installation barrier blocks new work.</summary>
public static class HifUpdateService
{
    private static readonly SemaphoreSlim CheckGate = new(1, 1);
    private static readonly SemaphoreSlim UpdateGate = new(1, 1);
    private static CancellationTokenSource? _cancellation;
    private static DateTime _lastCheck = DateTime.MinValue;
    private static DispatcherTimer? _timer;
    private static JObject? _pending;
    private static JObject? _closingOperation;
    private static int _initialized;
    private static long _saveFailures;
    private static volatile bool _isCommitting;
    public static bool IsCommitting { get => _isCommitting; private set => _isCommitting = value; }
    public static bool SuppressStartup { get; } = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("HIF_UPDATE_OPERATION"));
    public static bool IsVerifyingStartup { get; private set; } = SuppressStartup;
    public static string Status { get; private set; } = "HIF 完整包更新";
    public static bool CanCancel => _cancellation != null && _closingOperation == null;
    public static event Action? Changed;
    private static string PendingFile => Path.Combine(AppPaths.TempDirectory, "hif-update-pending.json");

    private static void SetStatus(string text)
    {
        DispatcherHelper.RunOnMainThread(() =>
        {
            if (Status != text) LoggerHelper.Info("HIF 更新状态：" + text);
            Status = text;
            Changed?.Invoke();
        });
    }

    private static JObject ReadOperation(string path)
    {
        var full = Path.GetFullPath(path);
        var op = JObject.Parse(File.ReadAllText(full));
        var id = op.Value<string>("id") ?? "";
        if (!System.Text.RegularExpressions.Regex.IsMatch(id, "^[a-f0-9]{32}$")
            || !string.Equals(Path.GetFullPath(op.Value<string>("root") ?? ""), AppPaths.DataRoot, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(full, Path.Combine(AppPaths.TempDirectory, "hif-updates", id, "operation.json"), StringComparison.OrdinalIgnoreCase))
            throw new IOException("更新操作不属于当前程序目录。");
        return op;
    }

    public static async Task InitializeAsync()
    {
        if (!HifLayout.IsIndependent || Interlocked.Exchange(ref _initialized, 1) != 0) return;
        try
        {
            var restartOperation = Environment.GetEnvironmentVariable("HIF_UPDATE_OPERATION");
            if (!string.IsNullOrEmpty(restartOperation))
            {
                var restored = Environment.GetEnvironmentVariable("HIF_UPDATE_RESTORED") == "1";
                Environment.SetEnvironmentVariable("HIF_UPDATE_OPERATION", null);
                Environment.SetEnvironmentVariable("HIF_UPDATE_RESTORED", null);
                var op = ReadOperation(restartOperation);
                if (restored)
                {
                    SetStatus("更新失败，已恢复旧版和个人配置。备份与诊断记录已保留。");
                    ToastHelper.Info("HIF 更新已恢复", Status, 15000);
                }
                else
                {
                    var release = JObject.Parse(File.ReadAllText(Path.Combine(AppPaths.DataRoot, "hif-release.json")));
                    if (release.Value<string>("version") != op.Value<string>("version") || MaaProcessor.Interface?.Task == null
                        || MaaProcessorManager.Instance.Current.ViewModel == null) throw new IOException("新版实例或任务定义尚未加载。");
                    foreach (var file in Directory.EnumerateFiles(AppPaths.ConfigDirectory, "*.json", SearchOption.AllDirectories))
                        JToken.Parse(File.ReadAllText(file));
                    var stage = Path.GetDirectoryName(restartOperation)!;
                    File.WriteAllText(Path.Combine(stage, "startup.ok"), op.Value<string>("id") + ":" + op.Value<string>("version"));
                    SetStatus("HIF 新版启动验证通过，配置已恢复。本次重启不自动运行任务。");
                    for (var attempt = 0; attempt < 480; attempt++)
                    {
                        if (ReadOperation(restartOperation).Value<string>("state") == "complete") break;
                        await Task.Delay(250);
                    }
                    if (ReadOperation(restartOperation).Value<string>("state") != "complete") throw new IOException("安装程序尚未确认成功。");
                }
                IsVerifyingStartup = false;
                _lastCheck = DateTime.UtcNow;
                StartCheckTimer();
                return;
            }
            if (!ConfigurationManager.Current.GetValue("Hif.FullUpdateSettingsInitialized", false))
            {
                var settings = Instances.VersionUpdateSettingsUserControlModel;
                settings.EnableCheckVersion = true;
                settings.EnableAutoUpdateResource = false;
                settings.EnableAutoUpdateMFA = false;
                ConfigurationManager.Current.SetValue("Hif.FullUpdateSettingsInitialized", true);
                ToastHelper.Info("HIF 更新设置", "已启用自动检查更新；自动安装保持关闭，可在版本更新设置中修改。", 10000);
            }
            if (File.Exists(PendingFile))
            {
                var pointer = JObject.Parse(File.ReadAllText(PendingFile));
                var path = pointer.Value<string>("operation");
                if (path != null && File.Exists(path))
                {
                    var op = ReadOperation(path);
                    if (op.Value<string>("state") == "prepared")
                    {
                        _pending = pointer;
                        SetStatus("新版已下载；点击更新继续安装。上次退出未安装。");
                        ToastHelper.Info("HIF 更新待安装", Status, 15000);
                    }
                    else if (op.Value<string>("state") is "installing" or "starting" or "rollback_failed" or "recovering")
                    {
                        SetStatus("上次更新未完成，请退出 Maa 后运行“恢复更新”。");
                        ToastHelper.Info("HIF 更新需要恢复", Status, 15000);
                        return;
                    }
                }
            }
            StartCheckTimer();
            if (AppRuntime.Args.TryGetValue("hif-update-package", out var localPackage))
            {
                await UpdateAsync(Path.GetFullPath(localPackage));
                return;
            }
            if (_pending == null) await CheckAsync(true);
        }
        catch (Exception error)
        {
            LoggerHelper.Error("HIF 更新初始化失败", error);
            SetStatus("HIF 更新初始化失败：" + error.Message);
        }
    }

    private static void StartCheckTimer()
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromHours(6) };
        _timer.Tick += async (_, _) => await CheckAsync(true);
        _timer.Start();
    }

    public static async Task CheckAsync(bool automatic = false)
    {
        if (IsVerifyingStartup || !HifLayout.IsIndependent) return;
        if (automatic && (_cancellation != null || _pending != null || IsCommitting)) return;
        if (automatic && !ShouldCheckAutomatically(ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableCheckVersion, true),
                ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableAutoUpdateResource, false), DateTime.UtcNow, _lastCheck)) return;
        if (!await CheckGate.WaitAsync(0)) return;
        try
        {
            _lastCheck = DateTime.UtcNow;
            var data = await RunToolAsync("check", null, CancellationToken.None);
            if (_cancellation != null || _pending != null) return;
            if (data.Value<bool>("update_available"))
            {
                SetStatus("发现 HIF 新版 " + data.Value<string>("latest") + "，点击更新下载并安装。");
                if (automatic && ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableAutoUpdateResource, false))
                    _ = UpdateAsync();
                else ToastHelper.Info("HIF 发现新版", Status, 10000);
            }
            else if (!automatic)
            {
                SetStatus(data["latest"]?.Type == JTokenType.Null ? "本 Fork 尚未发布运行包。" : "当前已是最新 HIF 版本。");
                ToastHelper.Info("HIF 版本检查", Status, 10000);
            }
        }
        catch (Exception error)
        {
            LoggerHelper.Error("HIF 检查更新失败", error);
            if (!automatic) SetStatus("检查失败：" + error.Message);
        }
        finally { CheckGate.Release(); }
    }

    internal static bool ShouldCheckAutomatically(bool check, bool install, DateTime now, DateTime last)
        => (check || install) && now - last >= TimeSpan.FromHours(6);

    private static async Task<JObject> RunToolAsync(string command, string? package, CancellationToken token)
    {
        var info = new ProcessStartInfo
        {
            FileName = Path.Combine(AppPaths.DataRoot, "python", "python.exe"), WorkingDirectory = AppPaths.DataRoot,
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8, StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        info.Environment["PYTHONUTF8"] = "1";
        info.ArgumentList.Add(Path.Combine(AppPaths.DataRoot, "tools", "hif_app.py"));
        info.ArgumentList.Add(command);
        if (package != null) { info.ArgumentList.Add("--package"); info.ArgumentList.Add(package); }
        using var process = Process.Start(info) ?? throw new IOException("无法启动更新准备工具。");
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        try
        {
            while (!process.HasExited)
            {
                token.ThrowIfCancellationRequested();
                if (command == "prepare-update") ShowPreparationProgress();
                await Task.Delay(250, token);
            }
            await process.WaitForExitAsync(token);
            var result = await output;
            var diagnostics = await errors;
            if (process.ExitCode != 0) throw new IOException(diagnostics.Trim().Split('\n').LastOrDefault());
            return JObject.Parse(result.Trim().Split('\n').Last());
        }
        catch
        {
            if (!process.HasExited) { process.Kill(true); await process.WaitForExitAsync(); }
            throw;
        }
    }

    private static void ShowPreparationProgress()
    {
        try
        {
            if (!File.Exists(PendingFile)) return;
            var pointer = JObject.Parse(File.ReadAllText(PendingFile));
            var directory = Path.GetDirectoryName(pointer.Value<string>("operation"));
            if (directory == null) return;
            var data = JObject.Parse(File.ReadAllText(Path.Combine(directory, "progress.json")));
            var label = data.Value<string>("state") switch
            {
                "downloading" => "正在下载新版 " + (data.Value<long>("downloaded") / 1048576) + " MB"
                    + (data.Value<long>("total") > 0 ? " / " + (data.Value<long>("total") / 1048576) + " MB" : ""),
                "verifying" => "正在校验安装包", "extracting" => "正在解压准备新版", "validating" => "正在检查新版运行环境",
                _ => "正在准备 HIF 完整包"
            };
            SetStatus(label);
        }
        catch (IOException) { }
        catch (Newtonsoft.Json.JsonException) { }
    }

    public static bool AllInstancesIdle() => MaaProcessor.Processors.All(p =>
        p.TaskQueue.Count == 0 && !p.IsTaskRunActive && !p.IsConnecting && !p.IsIndependentHifRunning
        && p.MaaTasker?.IsRunning != true && p.MaaTasker?.IsStopping != true
        && p.ViewModel?.IsRunning != true && p.ViewModel?.Idle != false);

    private static bool HasStrategyEditor() => Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop
        && desktop.Windows.Any(w => w is HifPriorityWindow or HifDrinkWindow or HifCustomCardWindow);

    public static async Task UpdateAsync(string? package = null)
    {
        if (!await UpdateGate.WaitAsync(0)) return;
        _cancellation = new CancellationTokenSource();
        var token = _cancellation.Token;
        try
        {
            if (_pending == null || package != null)
            {
                SetStatus("正在准备 HIF 完整包");
                _pending = await RunToolAsync("prepare-update", package, token);
                if (_pending.Value<bool>("skipped")) { _pending = null; SetStatus("当前已是最新 HIF 版本。"); return; }
            }
            var operationPath = _pending.Value<string>("operation") ?? throw new IOException("缺少安装操作。");
            var operation = ReadOperation(operationPath);
            if (operation.Value<string>("state") != "prepared") throw new IOException("下载包已失效，请重新下载。");
            while (true)
            {
                token.ThrowIfCancellationRequested();
                if (!DispatcherHelper.RunOnMainThread(AllInstancesIdle))
                { SetStatus("新版已下载，等待所有实例的任务队列结束。无限循环任务需手动停止。"); await Task.Delay(1000, token); continue; }
                if (DispatcherHelper.RunOnMainThread(HasStrategyEditor))
                { SetStatus("等待策略窗口保存并关闭后安装。"); await Task.Delay(1000, token); continue; }
                var stayedIdle = true;
                for (var seconds = 10; seconds > 0; seconds--)
                {
                    SetStatus($"{seconds} 秒后安装并重启，可取消。");
                    await Task.Delay(1000, token);
                    if (!DispatcherHelper.RunOnMainThread(() => AllInstancesIdle() && !HasStrategyEditor())) { stayedIdle = false; break; }
                }
                if (!stayedIdle) continue;
                var acquired = DispatcherHelper.RunOnMainThread(() =>
                {
                    IsCommitting = true;
                    if (!AllInstancesIdle() || HasStrategyEditor()) { IsCommitting = false; return false; }
                    Instances.RootViewModel.IsUpdating = true;
                    return true;
                });
                if (acquired) break;
            }
            await DispatcherHelper.RunOnMainThreadAsync(() =>
            {
                _saveFailures = JsonHelper.SaveFailureCount;
                MaaProcessorManager.Instance.PersistCurrentSelection();
                foreach (var processor in MaaProcessor.Processors.ToList()) processor.ViewModel?.PersistConfigurationState();
                Instances.RootView.SaveWindowSizeAndPositionImmediately();
                foreach (var config in ConfigurationManager.Configs) ConfigurationManager.SaveConfiguration(config.Name);
                if (JsonHelper.SaveFailureCount != _saveFailures) throw new IOException("配置保存失败，更新已中止。");
            });
            token.ThrowIfCancellationRequested();
            var stage = Path.GetDirectoryName(operationPath)!;
            using var parent = Process.GetCurrentProcess();
            var runnerInfo = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = stage };
            foreach (var arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(stage, "hif_update_runner.ps1"),
                         "-OperationPath", operationPath, "-ParentId", parent.Id.ToString(), "-ParentStartTicks", parent.StartTime.ToUniversalTime().Ticks.ToString() })
                runnerInfo.ArgumentList.Add(arg);
            using var runner = Process.Start(runnerInfo) ?? throw new IOException("无法启动独立安装程序。");
            for (var attempt = 0; !File.Exists(Path.Combine(stage, "runner.ready")); attempt++)
            {
                if (runner.HasExited || attempt >= 40) throw new IOException("独立安装程序未就绪，当前程序保留。");
                await Task.Delay(250, token);
            }
            _closingOperation = operation;
            Changed?.Invoke();
            SetStatus("正在退出并安装，配置将自动备份。");
            await DispatcherHelper.RunOnMainThreadAsync(() => Instances.ShutdownApplication());
        }
        catch (OperationCanceledException) { SetStatus(_pending == null ? "更新下载已取消。" : "安装已取消，下载包保留，可点击更新继续。"); }
        catch (Exception error) { LoggerHelper.Error("HIF 更新失败", error); SetStatus("更新失败，当前版本保留：" + error.Message); }
        finally
        {
            if (_closingOperation == null)
            {
                IsCommitting = false;
                DispatcherHelper.RunOnMainThread(() => Instances.RootViewModel.IsUpdating = false);
            }
            _cancellation?.Dispose();
            _cancellation = null;
            DispatcherHelper.PostOnMainThread(() => Changed?.Invoke());
            UpdateGate.Release();
        }
    }

    public static void Cancel() { if (CanCancel) _cancellation?.Cancel(); }

    public static void BeforeClosed()
    {
        _timer?.Stop();
        if (_closingOperation == null) { _cancellation?.Cancel(); return; }
        if (JsonHelper.SaveFailureCount != _saveFailures) throw new IOException("退出时保存失败，禁止安装。");
        var stage = _closingOperation.Value<string>("stage")!;
        File.WriteAllText(Path.Combine(stage, "commit.ready"), _closingOperation.Value<string>("id"));
    }
}
