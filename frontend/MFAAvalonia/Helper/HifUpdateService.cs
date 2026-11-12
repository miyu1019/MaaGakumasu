using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Views.Pages.Hif;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;
using SukiUI.Dialogs;
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
    private static bool _confirmationOpen;
    public static bool IsChecking { get; private set; }
    public static bool IsCommitting { get => _isCommitting; private set => _isCommitting = value; }
    public static bool SuppressStartup { get; } = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("HIF_UPDATE_OPERATION"));
    public static bool IsVerifyingStartup { get; private set; } = SuppressStartup;
    public static string Status { get; private set; } = "HIF 完整包更新";
    public static bool CanCancel => _cancellation != null && _closingOperation == null;
    public static event Action? Changed;
    private static string PendingFile => Path.Combine(AppPaths.TempDirectory, "hif-update-pending.json");
    private static string Channel => ConfigurationManager.Current.GetValue(ConfigurationKeys.HifUpdateChannelIndex, 1) == 0 ? "autobuild" : "stable";

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
        using var reader = new StreamReader(new FileStream(full, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete));
        var op = JObject.Parse(reader.ReadToEnd());
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
        if (!OperatingSystem.IsWindows())
        {
            SetStatus("HIF 运行包只提供 Windows x64，完整包更新不可用。");
            return;
        }
        try
        {
            var restartOperation = Environment.GetEnvironmentVariable("HIF_UPDATE_OPERATION");
            if (!string.IsNullOrEmpty(restartOperation))
            {
                var restored = Environment.GetEnvironmentVariable("HIF_UPDATE_RESTORED") == "1";
                Environment.SetEnvironmentVariable("HIF_UPDATE_OPERATION", null);
                Environment.SetEnvironmentVariable("HIF_UPDATE_RESTORED", null);
                if (restored)
                {
                    // 旧版已恢复或根本没改动：不需要启动确认，先解除限制，读取结果失败也不影响运行任务。
                    IsVerifyingStartup = false;
                    _lastCheck = DateTime.UtcNow;
                    StartCheckTimer();
                    var result = ReadOperation(restartOperation);
                    if (result.Value<string>("state") == "failed")
                    {
                        SetStatus("更新未安装，当前版本未改动：" + result.Value<string>("error"));
                        ToastHelper.Info("HIF 更新未安装", Status, 15000);
                    }
                    else
                    {
                        SetStatus("更新失败，已恢复旧版和个人配置。备份与诊断记录已保留。");
                        ToastHelper.Info("HIF 更新已恢复", Status, 15000);
                    }
                    return;
                }
                // 新版启动确认：确认前不运行任务；确认失败时安装器超时回滚并结束本进程。
                var op = ReadOperation(restartOperation);
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
                IsVerifyingStartup = false;
                _lastCheck = DateTime.UtcNow;
                StartCheckTimer();
                return;
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
        if (automatic && !OperatingSystem.IsWindows()) return;
        if (_confirmationOpen) return;
        if (!automatic && _pending != null) { await RequestUpdateAsync(); return; }
        if (automatic && (_cancellation != null || _pending != null || IsCommitting)) return;
        if (automatic && !ShouldCheckAutomatically(ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableCheckVersion, true),
                ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableAutoUpdateResource, false), DateTime.UtcNow, _lastCheck)) return;
        if (!await CheckGate.WaitAsync(0))
        {
            if (automatic) return;
            SetStatus("正在检查 HIF 更新，请稍候…");
            await CheckGate.WaitAsync();
        }
        try
        {
            IsChecking = true;
            SetStatus("正在检查 HIF 更新…");
            _lastCheck = DateTime.UtcNow;
            var data = await RunToolAsync("check", null, CancellationToken.None);
            if (_cancellation != null || _pending != null) return;
            HandleCheckResult(data, automatic);
        }
        catch (Exception error)
        {
            LoggerHelper.Error("HIF 检查更新失败", error);
            SetStatus("检查失败：" + error.Message);
            if (!automatic)
            {
                ToastHelper.Error("HIF 检查更新失败", error.Message, 10);
            }
        }
        finally
        {
            IsChecking = false;
            DispatcherHelper.PostOnMainThread(() => Changed?.Invoke());
            CheckGate.Release();
        }
    }

    private static void HandleCheckResult(JObject data, bool automatic)
    {
        if (data.Value<bool>("update_available"))
        {
            SetStatus("发现 HIF 新版 " + data.Value<string>("latest") + "，请查看更新说明。");
            ShowUpdateConfirmation(data, automatic && ConfigurationManager.Current.GetValue(ConfigurationKeys.EnableAutoUpdateResource, false));
        }
        else
        {
            SetStatus("无更新，当前 HIF 版本为 " + data.Value<string>("installed") + "。");
            if (!automatic) ToastHelper.Info("HIF 版本检查", Status, 10);
        }
    }

    public static async Task RequestUpdateAsync()
    {
        if (_cancellation != null || IsCommitting || _confirmationOpen) return;
        if (_pending != null)
        {
            var op = ReadOperation(_pending.Value<string>("operation")!);
            ShowUpdateConfirmation(new JObject
            {
                ["installed"] = MaaProcessor.Interface?.Version,
                ["latest"] = op.Value<string>("display_version") ?? op.Value<string>("version"),
                ["release_notes"] = op.Value<string>("release_notes")
            });
            return;
        }
        await CheckAsync();
    }

    private static void ShowUpdateConfirmation(JObject data, bool automaticInstall = false)
    {
        DispatcherHelper.PostOnMainThread(() =>
        {
            if (_confirmationOpen || _cancellation != null || IsCommitting) return;
            _confirmationOpen = true;
            var latest = data.Value<string>("latest");
            var notes = new Markdown.Avalonia.Full.MarkdownScrollViewer
            {
                Markdown = string.IsNullOrWhiteSpace(data.Value<string>("release_notes"))
                    ? "此版本未提供更新说明。" : data.Value<string>("release_notes"),
                MaxHeight = 340,
                MaxWidth = 620
            };
            var content = new StackPanel { Spacing = 12, MaxWidth = 620 };
            content.Children.Add(new TextBlock
            {
                Text = $"当前版本：{data.Value<string>("installed")}\n最新版本：{latest}\n更新说明：",
                TextWrapping = Avalonia.Media.TextWrapping.Wrap
            });
            content.Children.Add(notes);
            content.Children.Add(new TextBlock
            {
                Text = automaticInstall ? "自动更新已开启：后台下载后等待所有实例任务结束再安装，可取消本次更新。"
                    : _pending == null ? "是否下载并更新？安装会等待所有实例任务结束。" : "已下载此版本，是否继续更新？",
                TextWrapping = Avalonia.Media.TextWrapping.Wrap
            });
            var builder = Instances.DialogManager.CreateDialog().WithTitle(automaticInstall ? "HIF 自动更新说明" : "HIF 更新确认").WithContent(content)
                .OnDismissed(dialog => { notes.Dispose(); _confirmationOpen = false; });
            if (automaticInstall)
                builder.WithActionButton("取消更新", dialog => Cancel(), true)
                    .WithActionButton("关闭说明", dialog => { }, true);
            else
                builder.WithActionButton("取消", dialog => SetStatus("已取消更新，当前版本保留。"), true)
                    .WithActionButton("更新", dialog => { _ = UpdateAsync(expectedVersion: latest); }, true);
            var shown = builder.TryShow();
            if (!shown)
            {
                notes.Dispose();
                _confirmationOpen = false;
                SetStatus("请关闭当前对话框后再次点击更新。");
            }
            else if (automaticInstall) _ = UpdateAsync(expectedVersion: latest);
        });
    }

    internal static bool ShouldCheckAutomatically(bool check, bool install, DateTime now, DateTime last)
        => (check || install) && now - last >= TimeSpan.FromHours(6);

    /// <summary>重新安装渠道里不低于当前版本的最新完整包；安装器只替换缺失或内容不同的文件，用于修复程序文件。</summary>
    public static void RequestReinstall()
    {
        if (_cancellation != null || IsCommitting || IsVerifyingStartup) return;
        DispatcherHelper.PostOnMainThread(() =>
        {
            if (_confirmationOpen || _cancellation != null || IsCommitting) return;
            _confirmationOpen = true;
            var shown = Instances.DialogManager.CreateDialog().WithTitle("重新安装 HIF").WithContent(new TextBlock
                {
                    Text = "重新下载当前更新渠道里不低于当前版本的最新完整包，只替换缺失或被改动的程序文件，个人配置保留。安装会等待所有实例任务结束。",
                    TextWrapping = Avalonia.Media.TextWrapping.Wrap, MaxWidth = 620
                })
                .OnDismissed(dialog => _confirmationOpen = false)
                .WithActionButton("取消", dialog => { }, true)
                .WithActionButton("重新安装", dialog => { _ = UpdateAsync(reinstall: true); }, true)
                .TryShow();
            if (!shown)
            {
                _confirmationOpen = false;
                SetStatus("请关闭当前对话框后再次点击重新安装。");
            }
        });
    }

    private static async Task<JObject> RunToolAsync(string command, string? package, CancellationToken token, string? expectedVersion = null, bool reinstall = false)
    {
        var info = new ProcessStartInfo
        {
            FileName = Path.Combine(AppPaths.DataRoot, "python", OperatingSystem.IsWindows() ? "python.exe" : "bin/python3"), WorkingDirectory = AppPaths.DataRoot,
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8, StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        info.Environment["PYTHONUTF8"] = "1";
        // 版本更新设置里的 GitHub Token 只用于 api.github.com，提高匿名接口的限流额度。
        var gitHubToken = SimpleEncryptionHelper.Decrypt(ConfigurationManager.Current.GetValue(ConfigurationKeys.GitHubToken, string.Empty)).Trim();
        if (gitHubToken.Length > 0) info.Environment["HIF_GITHUB_TOKEN"] = gitHubToken;
        info.ArgumentList.Add(Path.Combine(AppPaths.DataRoot, "tools", "hif_app.py"));
        info.ArgumentList.Add(command);
        if (package != null) { info.ArgumentList.Add("--package"); info.ArgumentList.Add(package); }
        if (expectedVersion != null) { info.ArgumentList.Add("--expected-version"); info.ArgumentList.Add(expectedVersion); }
        if (reinstall) info.ArgumentList.Add("--reinstall");
        info.ArgumentList.Add("--channel");
        info.ArgumentList.Add(Channel);
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
                // 增量下载只取与已安装文件不同的部分，体积常不足 1 MB，保留一位小数。
                "downloading" when data.Value<string>("mode") == "incremental" => "正在下载变化的文件 "
                    + Megabytes(data.Value<long>("downloaded")) + " / " + Megabytes(data.Value<long>("total")) + " MB",
                "downloading" => "正在下载完整包 " + (data.Value<long>("downloaded") / 1048576) + " MB"
                    + (data.Value<long>("total") > 0 ? " / " + (data.Value<long>("total") / 1048576) + " MB" : ""),
                "inspecting" => "正在读取新版文件清单",
                "comparing" => $"正在比对已安装文件 {data.Value<long>("compared")} / {data.Value<long>("total")}",
                "verifying" => "正在校验安装包", "extracting" => "正在解压准备新版", "validating" => "正在检查新版运行环境",
                _ => "正在准备 HIF 更新"
            };
            SetStatus(label);
        }
        catch (IOException) { }
        catch (Newtonsoft.Json.JsonException) { }
    }

    private static string Megabytes(long bytes) => (bytes / 1048576.0).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture);

    private static void LogDownload(JObject? download)
    {
        if (download == null) return;
        if (download.Value<string>("mode") == "incremental")
            LoggerHelper.Info($"HIF 更新：增量下载 {download.Value<long>("downloaded_files")} 个文件，共 {Megabytes(download.Value<long>("downloaded"))} MB"
                + $"（完整包 {Megabytes(download.Value<long>("package_size"))} MB），复用 {download.Value<long>("reused_files")} 个已安装文件。");
        else
            LoggerHelper.Info($"HIF 更新：使用完整包 {Megabytes(download.Value<long>("package_size"))} MB"
                + (download.Value<string>("incremental_error") is { Length: > 0 } reason ? "；未增量下载：" + reason : "。"));
    }

    public static bool AllInstancesIdle() => MaaProcessor.Processors.All(p =>
        p.TaskQueue.Count == 0 && !p.IsTaskRunActive && !p.IsConnecting && !p.IsIndependentHifRunning
        && p.MaaTasker?.IsRunning != true && p.MaaTasker?.IsStopping != true
        && p.ViewModel?.IsRunning != true && p.ViewModel?.Idle != false);

    private static bool HasStrategyEditor() => HifStrategyView.HasUnsavedChanges;

    public static async Task UpdateAsync(string? package = null, string? expectedVersion = null, bool reinstall = false)
    {
        if (!OperatingSystem.IsWindows())
        {
            SetStatus("HIF 运行包只提供 Windows x64，完整包更新不可用。");
            ToastHelper.Info("HIF 更新", Status, 10);
            return;
        }
        if (!await UpdateGate.WaitAsync(0)) return;
        _cancellation = new CancellationTokenSource();
        var token = _cancellation.Token;
        try
        {
            if (_pending == null || package != null)
            {
                SetStatus("正在准备 HIF 更新");
                _pending = await RunToolAsync("prepare-update", package, token, expectedVersion, reinstall);
                if (_pending.Value<bool>("skipped"))
                {
                    _pending = null;
                    SetStatus(reinstall ? "更新渠道里没有不低于当前版本的完整包，未重新安装。" : "当前已是最新 HIF 版本。");
                    return;
                }
                LogDownload(_pending["download"] as JObject);
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
                { SetStatus("等待「HIF 策略」页保存或放弃未保存的修改后安装。"); await Task.Delay(1000, token); continue; }
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
            foreach (var arg in new[] { "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(stage, "hif_update_runner.ps1"), "-ShowProgress",
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
            SetStatus("正在退出并安装，请查看更新进度窗口；完成后会自动启动，请勿手动打开程序。");
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
