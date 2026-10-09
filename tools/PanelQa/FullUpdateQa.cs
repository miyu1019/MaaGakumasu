using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Other;
using Newtonsoft.Json;
using System.Reflection;

static class FullUpdateQa
{
    public static void Run()
    {
        var check = typeof(HifUpdateService).GetMethod("ShouldCheckAutomatically", BindingFlags.Static | BindingFlags.NonPublic)!;
        var now = DateTime.UtcNow;
        bool Should(bool enabled, bool install, double hours) => (bool)check.Invoke(null, [enabled, install, now, now.AddHours(-hours)])!;
        if (Should(false, false, 100) || Should(true, false, 5.99) || !Should(true, false, 6) || !Should(false, true, 6))
            throw new Exception("Automatic check switches or interval incorrect");
        _ = MaaProcessorManager.Instance;
        var first = new MaaProcessor("update-first");
        var second = new MaaProcessor("update-background");
        MaaProcessor.Processors.Clear();
        MaaProcessor.Processors.Add(first);
        MaaProcessor.Processors.Add(second);
        if (!HifUpdateService.AllInstancesIdle()) throw new Exception("Empty instances must be idle");
        second.TaskQueue.Enqueue(new MFATask { Name = "background queue" });
        if (HifUpdateService.AllInstancesIdle()) throw new Exception("Background queued task was ignored");
        second.TaskQueue.Clear();
        var active = typeof(MaaProcessor).GetField("_isTaskRunActive", BindingFlags.Instance | BindingFlags.NonPublic)!;
        active.SetValue(second, 1);
        if (HifUpdateService.AllInstancesIdle()) throw new Exception("Executing task with empty queue was ignored");
        active.SetValue(second, 0);
        typeof(HifUpdateService).GetProperty(nameof(HifUpdateService.IsCommitting))!.GetSetMethod(true)!.Invoke(null, [true]);
        var begin = typeof(MaaProcessor).GetMethod("TryBeginTaskRun", BindingFlags.Instance | BindingFlags.NonPublic)!;
        if ((bool)begin.Invoke(second, null)!) throw new Exception("A new task crossed the installation barrier");
        var lockedFile = Path.Combine(AppPaths.ConfigDirectory, "save-failure-qa.json");
        File.WriteAllText(lockedFile, "{\"old\":true}");
        using (var locked = File.Open(lockedFile, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            try { JsonHelper.SaveJson(lockedFile, new { changed = true }); throw new Exception("Save failure did not abort installation"); }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
        }
        if (File.ReadAllText(lockedFile) != "{\"old\":true}") throw new Exception("Failed save changed the original file");
        typeof(HifUpdateService).GetProperty(nameof(HifUpdateService.IsCommitting))!.GetSetMethod(true)!.Invoke(null, [false]);
        var definition = JsonConvert.DeserializeObject<MaaInterface>("""
            {"option":{"old":{"cases":[{"name":"off"},{"name":"on"}],"default_case":"off"},
                        "new":{"cases":[{"name":"off"},{"name":"on"}],"default_case":"on"}}}
            """)!;
        var old = new DragItemViewModel(new MaaInterface.MaaInterfaceTask { Name = "task", Entry = "entry",
            Option = [new() { Name = "old", Index = 1 }, new() { Name = "removed", Index = 0 }] });
        var updated = new MaaInterface.MaaInterfaceTask { Name = "task", Entry = "entry",
            Option = [new() { Name = "old" }, new() { Name = "new" }] };
        var loader = new TaskLoader(definition, null!);
        typeof(TaskLoader).GetMethod("UpdateOptions", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(loader, [old, updated]);
        var options = old.InterfaceItem!.Option!;
        if (options.Count != 2 || options.Single(o => o.Name == "old").Index != 1 || options.Single(o => o.Name == "new").Index != 1)
            throw new Exception("Task update failed to preserve choice and initialize the new default");
        Console.WriteLine("Native full-update policy passed: switches/6-hour interval, background queues, active tasks, start barrier, save failure, new option defaults.");
    }
}
