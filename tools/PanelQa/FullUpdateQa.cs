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
        CheckUpdateConfirmation();
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

    private static void CheckUpdateConfirmation()
    {
        var handle = typeof(HifUpdateService).GetMethod("HandleCheckResult", BindingFlags.Static | BindingFlags.NonPublic)!;
        SukiUI.Dialogs.ISukiDialog? shown = null;
        int count = 0;
        Instances.DialogManager.OnDialogShown += (_, e) => { shown = e.Dialog; count++; };
        ConfigurationManager.Current.SetValue(ConfigurationKeys.EnableAutoUpdateResource, false);
        var data = Newtonsoft.Json.Linq.JObject.Parse("""
            {"installed":"v261009.1","latest":"v261009.2","update_available":true,"release_notes":"## 更新说明\n- 保留个人策略"}
            """);
        foreach (var automatic in new[] { false, true })
        {
            handle.Invoke(null, [data, automatic]);
            Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            if (shown?.Content is not Avalonia.Controls.StackPanel panel) throw new Exception("Update confirmation was not shown");
            var versions = ((Avalonia.Controls.TextBlock)panel.Children[0]).Text!;
            if (!versions.Contains("v261009.1") || !versions.Contains("v261009.2")) throw new Exception("Confirmation lost version numbers");
            var notes = panel.Children[1].GetType().GetProperty("Markdown")!.GetValue(panel.Children[1]) as string;
            if (notes != (string?)data["release_notes"]) throw new Exception("Release notes were not shown");
            if (typeof(HifUpdateService).GetField("_cancellation", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null) != null)
                throw new Exception("Download started before confirmation");
            ((Avalonia.Controls.Button)shown.ActionButtons[0]).RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Avalonia.Controls.Button.ClickEvent));
            Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            if (!HifUpdateService.Status.Contains("已取消")) throw new Exception("Cancel did not preserve the current version");
        }
        data["update_available"] = false;
        handle.Invoke(null, [data, false]);
        Avalonia.Threading.Dispatcher.UIThread.RunJobs();
        if (count != 2 || !HifUpdateService.Status.Contains("无更新")) throw new Exception("No-update result incorrectly opened a confirmation");
        Console.WriteLine("Update confirmation passed: manual and automatic checks show versions/notes, no download before confirmation, cancel, and no-update feedback.");
    }
}
