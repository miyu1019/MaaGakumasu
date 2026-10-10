using System.Reflection;
using System.Runtime.CompilerServices;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MaaFramework.Binding;
using MaaFramework.Binding.Buffers;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.ViewModels.Pages;

static class LiveViewOffQa
{
    static FieldInfo Field(object value, string name) => value.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!;
    static object? Invoke(object value, string name, params object?[] args) =>
        value.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(value, args);
    static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    static void Pump(Task task)
    {
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!task.IsCompleted)
        {
            Dispatcher.UIThread.RunJobs();
            if (DateTime.UtcNow > deadline) throw new Exception("UI test timed out");
            Thread.Sleep(1);
        }
        task.GetAwaiter().GetResult();
    }

    public static void Run()
    {
        var processor = new MaaProcessor("live-view-off-qa");
        var vm = (TaskQueueViewModel)RuntimeHelpers.GetUninitializedObject(typeof(TaskQueueViewModel));
        Field(vm, "_processorField").SetValue(vm, processor);
        Field(vm, "_liveViewImageCache").SetValue(vm, new WriteableBitmap?[5]);
        Field(vm, "_liveViewRefreshRate").SetValue(vm, 30.0);
        using var timer = new System.Timers.Timer(1000);
        Field(vm, "_liveViewTimer").SetValue(vm, timer);
        using var resource = new MaaResource();
        using var main = new MaaTasker { Resource = resource, Controller = MaaController.Null, DisposeOptions = (DisposeOptions)0 };
        processor.MaaTasker = main;
        processor.InstanceConfiguration.SetValue(ConfigurationKeys.EnableLiveView, false);
        Pump((Task)Invoke(processor, "PrewarmScreenshotTaskerAsync", CancellationToken.None)!);
        Check(Invoke(processor, "GetScreenshotTasker", CancellationToken.None) == null, "Disabled preview created a tasker");
        Check(processor.PostScreencap() == MaaJobStatus.Invalid, "Disabled preview captured a frame");
        vm.ResumeLiveView();
        Check(!timer.Enabled, "Tab activation restarted disabled preview timer");

        vm.EnableLiveView = true;
        Check(timer.Enabled, "Enabling preview did not start its timer");
        var preview = new MaaTasker { Resource = resource, Controller = MaaController.Null, DisposeOptions = (DisposeOptions)0 };
        Field(processor, "_screenshotTasker").SetValue(processor, preview);
        var pending = new TaskCompletionSource<MaaTasker?>();
        Field(processor, "_screenshotTaskerInitTask").SetValue(processor, pending.Task);
        var generation = (long)Field(processor, "_screenshotTaskerGeneration").GetValue(processor)!;
        vm.EnableLiveView = false;
        Check(!timer.Enabled && vm.LiveViewImage == null, "Disabling preview did not stop/clear it");
        Check(Field(processor, "_screenshotTasker").GetValue(processor) == null, "Preview connection was retained");
        Check(ReferenceEquals(processor.MaaTasker, main), "Disabling preview replaced the main controller");
        vm.EnableLiveView = true;
        var latePreview = new MaaTasker { Resource = resource, Controller = MaaController.Null, DisposeOptions = (DisposeOptions)0 };
        pending.SetResult(latePreview);
        Invoke(processor, "PublishInitializedScreenshotTasker", pending.Task, latePreview, generation);
        Check(Field(processor, "_screenshotTasker").GetValue(processor) == null, "Old initialization survived a disable/enable cycle");
        vm.EnableLiveView = false;
        Check(!processor.InstanceConfiguration.GetValue(ConfigurationKeys.EnableLiveView, true), "Disabled setting was not saved");

        // A frame queued before disabling must not repaint the preview afterward.
        using var buffer = new MaaImageBuffer();
        Pump(vm.UpdateLiveViewImageAsync(buffer));
        Check(vm.LiveViewImage == null, "Late frame revived disabled preview");
        Console.WriteLine("Live view off: no prewarm/capture; preview detached; main retained; timer/config/race checks passed.");
    }
}
