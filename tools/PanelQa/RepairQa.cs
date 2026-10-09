using System.Diagnostics;
using System.Collections.ObjectModel;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MaaFramework.Binding;
using MaaFramework.Binding.Buffers;
using MaaFramework.Binding.Custom;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Other;
using MFAAvalonia.ViewModels.Pages;
using Microsoft.Extensions.DependencyInjection;
using Newtonsoft.Json.Linq;

static class RepairQa
{
    static FieldInfo F(object obj, string name) => obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!;
    static void Set(object obj, string name, object? value) => F(obj, name).SetValue(obj, value);
    static object Invoke(object obj, string name, params object?[] args) => obj.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(obj, args)!;
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
    public static void Run(string root)
    {
        var services = new ServiceCollection();
        services.AddSingleton<MFAAvalonia.ViewModels.UsersControls.Settings.GuiSettingsUserControlModel>();
        services.AddSingleton<SukiUI.Toasts.ISukiToastManager, SukiUI.Toasts.SukiToastManager>();
        typeof(MFAAvalonia.App).GetProperty("Services")!.GetSetMethod(true)!.Invoke(null, [services.BuildServiceProvider()]);
        Avalonia.Application.Current!.Styles.Add(new SukiUI.SukiTheme());
        var processor = new MaaProcessor("repair-qa");
        var vm = (TaskQueueViewModel)RuntimeHelpers.GetUninitializedObject(typeof(TaskQueueViewModel));
        Set(vm, "_processorField", processor);
        Set(vm, "_liveViewImageCache", new WriteableBitmap?[5]);
        Set(vm, "_liveViewFpsWindowStart", DateTime.UtcNow);
        Set(vm, "_taskItemViewModels", new ObservableCollection<DragItemViewModel>());
        Set(vm, "_currentResources", new ObservableCollection<MaaInterface.MaaInterfaceResource>());
        Set(vm, "_currentResource", "官服");
        var loader = new TaskLoader(MaaProcessor.Interface, vm);
        var hif = new InstanceConfiguration("hif").GetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask>()).Single();
        var originals = MaaProcessor.Interface!.Task!;
        List<DragItemViewModel> rows = [new(hif.Clone()) { OwnerViewModel = vm }];
        var history = new List<string> { hif.Name + TaskLoader.NEW_SEPARATOR + hif.Entry };
        List<DragItemViewModel> Sync()
        {
            object?[] args = [history, rows, originals];
            var result = ((List<DragItemViewModel>, List<DragItemViewModel>))Invoke(loader, "SynchronizeTaskItems", args);
            history = (List<string>)args[0]!;
            Check(result.Item2.Count == 0, "Existing HIF was removed");
            return result.Item1;
        }
        var originalOptions = JToken.FromObject(rows[0].InterfaceItem!.Option!);
        foreach (var option in originalOptions)
            if (option["sub_options"]?.Type == JTokenType.Null) option["sub_options"] = new JArray();
        rows = Sync();
        Check(rows.Count == 1 && rows[0].InterfaceItem!.Entry == "ProduceHIF", "Original tasks were auto-added");
        var synchronizedOptions = JToken.FromObject(rows[0].InterfaceItem!.Option!);
        foreach (JObject option in originalOptions)
        {
            var actual = synchronizedOptions.Single(o => (string?)o["name"] == (string?)option["name"]);
            foreach (var property in option.Properties())
            {
                if (property.Value.Type == JTokenType.Null || property.Name == "sub_options" && !property.Value.Any()) continue;
                Check(JToken.DeepEquals(property.Value, actual[property.Name]), "HIF saved option changed: " + property.Name);
            }
        }
        processor.InstanceConfiguration.SetValues(new Dictionary<string, object>
        {
            ["CurrentTasks"] = history, ["TaskItems"] = rows.Select(r => r.InterfaceItem!).ToList()
        });
        rows = new InstanceConfiguration("repair-qa").GetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask>()).Select(t => new DragItemViewModel(t) { OwnerViewModel = vm }).ToList();
        rows = Sync();
        Check(rows.Count == 1, "Reload added tasks");
        var extra = originals.First(t => t.Entry != "ProduceHIF");
        rows.Add(new(extra.Clone()) { OwnerViewModel = vm });
        rows = Sync();
        processor.InstanceConfiguration.SetValue("TaskItems", rows.Select(r => r.InterfaceItem!).ToList());
        Check(new InstanceConfiguration("repair-qa").GetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask>()).Count == 2, "Manual task did not persist");
        var definitions = new ObservableCollection<DragItemViewModel>();
        var firstResourceLoad = false; // Resource initialization is separate from task restoration.
        loader.LoadTasks(originals, definitions, ref firstResourceLoad);
        Check(vm.TaskItemViewModels.Count(t => t.InterfaceItem != null) == 2, "Full task load changed manually saved list");
        processor.InstanceConfiguration.SetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask> { hif.Clone() });
        loader.LoadTasks(originals, definitions, ref firstResourceLoad);
        Check(vm.TaskItemViewModels.Count(t => t.InterfaceItem != null) == 1, "Full reload added unselected tasks");
        processor.InstanceConfiguration.SetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask>());
        loader.LoadTasks(originals, definitions, ref firstResourceLoad);
        Check(!vm.TaskItemViewModels.Any(t => t.InterfaceItem != null), "Empty saved list was repopulated");
        Console.WriteLine("Task synchronization: restart, definition reload, HIF options and manual addition passed.");

        var path = Path.Combine(root, "temp", "atomic-save-qa.json");
        JsonHelper.SaveJson(path, new JObject { ["sequence"] = -1 });
        Parallel.For(0, 40, n =>
        {
            JsonHelper.SaveJson(path, new JObject { ["sequence"] = n, ["payload"] = new string('x', 1000) });
            Check(JsonHelper.LoadJson(path, new JObject(), errorHandle: null)!["sequence"] != null, "Concurrent read observed invalid JSON");
            processor.InstanceConfiguration.SetValue("concurrent-" + n, n + 1);
        });
        var saved = new InstanceConfiguration("repair-qa");
        for (int n = 0; n < 40; n++) Check(saved.GetValue("concurrent-" + n, -1) == n + 1, "Concurrent instance option was lost");
        using (var fileLock = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            File.WriteAllText(path + ".probe", "{}");
            try { File.Move(path + ".probe", path, true); }
            catch (Exception ex) { Console.WriteLine("Locked replace exception: " + ex.GetType().Name); }
            finally { if (File.Exists(path + ".probe")) File.Delete(path + ".probe"); }
            using var writeStarted = new ManualResetEventSlim();
            var delayedSave = Task.Run(() => { writeStarted.Set(); JsonHelper.SaveJson(path, new JObject { ["sequence"] = 99 }); });
            writeStarted.Wait(); Thread.Sleep(150); fileLock.Dispose(); delayedSave.GetAwaiter().GetResult();
        }
        Check((int)JsonHelper.LoadJson(path, new JObject(), errorHandle: null)!["sequence"]! == 99, "Short file lock did not recover");
        var before = File.ReadAllBytes(path);
        if (OperatingSystem.IsWindows())
        {
            using var fileLock = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            JsonHelper.SaveJson(path, new JObject { ["sequence"] = 100 });
        }
        else
        {
            // Unix allows replacing an open file; deny directory writes to exercise a real save failure.
            var directory = Path.GetDirectoryName(path)!;
            var permissions = File.GetUnixFileMode(directory);
            try
            {
                File.SetUnixFileMode(directory, permissions & ~(UnixFileMode.UserWrite | UnixFileMode.GroupWrite | UnixFileMode.OtherWrite));
                JsonHelper.SaveJson(path, new JObject { ["sequence"] = 100 });
            }
            finally { File.SetUnixFileMode(directory, permissions); }
        }
        Check(before.SequenceEqual(File.ReadAllBytes(path)), "Final save failure changed original file");
        Check(!Directory.GetFiles(Path.GetDirectoryName(path)!, "atomic-save-qa.json.*.tmp").Any(), "Temporary file leaked");
        Dispatcher.UIThread.RunJobs();
        Console.WriteLine("Atomic save: concurrent read/write, option preservation and platform-specific save failure passed.");

        using var resource = new MaaResource();
        using var tasker = new MaaTasker { Resource = resource, Controller = MaaController.Null, DisposeOptions = (DisposeOptions)0 };
        processor.MaaTasker = tasker;
        Set(processor, "_activeHifTasker", tasker);
        Set(processor, "_hifLastScreenshotTicks", Stopwatch.GetTimestamp());
        Check(!(bool)Invoke(processor, "ShouldScreencapForLiveView"), "HIF live view can capture");
        Check((bool)typeof(MaaProcessor).GetProperty("UseSeparateScreenshotTasker", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(processor)!, "Independent live view cannot use preview controller");
        Check((bool)Invoke(processor, "HasReusableMainTasker"), "Rotation can rebuild active main controller");
        Set(processor, "_hifLastScreenshotTicks", Stopwatch.GetTimestamp() - 29 * Stopwatch.Frequency);
        Check(!processor.IsHifScreenshotRecoveryExpired, "A normal action delay was treated as a rotation failure");
        Set(processor, "_hifScreenshotFailureTicks", Stopwatch.GetTimestamp() - 29 * Stopwatch.Frequency);
        Check(!processor.IsHifScreenshotRecoveryExpired, "Rotation grace ended early");
        Set(processor, "_hifScreenshotFailureTicks", Stopwatch.GetTimestamp() - 31 * Stopwatch.Frequency);
        Check(processor.IsHifScreenshotRecoveryExpired, "Rotation grace exceeded 30 seconds");
        object Event(string message, string details)
        {
            var type = typeof(MaaProcessor).GetMethod("HandleCallBack")!.GetParameters()[1].ParameterType;
            var lastType = type.GetConstructors().Single().GetParameters()[2].ParameterType;
            return Activator.CreateInstance(type, message, details, Enum.GetValues(lastType).GetValue(0)!)!;
        }
        Invoke(processor, "HandleHifControllerCallback", null, Event("Controller.Action.Succeeded", "{\"action\":\"screencap\"}"));
        Check(!processor.IsHifScreenshotRecoveryExpired, "Recovered frame did not reset grace");
        var starting = Event("Node.Recognition.Starting", "{\"name\":\"qa\",\"reco_id\":987654321,\"focus\":null}");
        typeof(MaaProcessor).GetMethod("HandleCallBack")!.Invoke(processor, [null, starting]);
        var looksLikePath = typeof(FocusHandler).GetMethod("LooksLikeFilePath", BindingFlags.Static | BindingFlags.NonPublic)!;
        Check(!(bool)looksLikePath.Invoke(null, ["[color=#fff]消息[/color]"])!, "Color markup treated as a path");
        Console.WriteLine("Live view: independent preview enabled; main controller retained; 30-second main recovery and callback passed.");

        foreach (var size in new[] { (720, 1280), (1280, 720), (720, 1280) })
        {
            var pixels = new byte[size.Item1 * size.Item2 * 3];
            Array.Fill(pixels, (byte)128);
            var data = Marshal.AllocHGlobal(pixels.Length);
            try
            {
                Marshal.Copy(pixels, 0, data, pixels.Length);
                var buffer = new MaaImageBuffer();
                Check(buffer.TrySetRawData(data, size.Item1, size.Item2, 16), "Cannot construct rotation frame");
                Pump(vm.UpdateLiveViewImageAsync(buffer));
                Check(vm.LiveViewImage?.PixelSize == new PixelSize(size.Item1, size.Item2), "Rotation frame has stale dimensions");
            }
            finally { Marshal.FreeHGlobal(data); }
        }
        Pump(vm.UpdateLiveViewImageAsync(null));
        Check(vm.LiveViewImage == null, "Cleared frame retained old image");
        processor.StopHifForScreenshotTimeout();
        processor.StopHifForScreenshotTimeout();
        Check((int)F(processor, "_hifScreenshotRecoveryFailed").GetValue(processor)! == 1, "Timeout did not stop HIF");
        Set(processor, "_activeHifTasker", null);
        Console.WriteLine("Rotation: portrait/landscape/portrait buffers, expired image clearing and bounded stop passed.");
    }
}
