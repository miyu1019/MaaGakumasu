using System.Collections;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Themes.Fluent;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json.Linq;
using Microsoft.Extensions.DependencyInjection;

var root = Path.GetFullPath(args[0]);
var app = AppBuilder.Configure<Application>().UsePlatformDetect().SetupWithoutStarting();
Application.Current!.Styles.Add(new FluentTheme());
typeof(AppPaths).GetProperty("DataRoot")!.GetSetMethod(true)!.Invoke(null, [root]);
typeof(AppPaths).GetField("_initialized", BindingFlags.NonPublic | BindingFlags.Static)!.SetValue(null, true);
foreach (var (name, value) in new[] { ("_configDirectory", "config"), ("_logsDirectory", "logs"), ("_tempDirectory", "temp") })
    typeof(AppPaths).GetField(name, BindingFlags.NonPublic | BindingFlags.Static)!.SetValue(null, Path.Combine(root, value));
Directory.CreateDirectory(Path.Combine(root, "debug"));
MaaProcessor.ReadInterface();
Directory.CreateDirectory(Path.Combine(AppContext.BaseDirectory, "runtimes", "win-x64", "native", "plugins"));
var engineVersion = MaaFramework.Binding.Interop.Native.MaaUtility.MaaVersion();
if (!engineVersion.Contains("5.12.3")) throw new Exception("Unexpected packaged native engine: " + engineVersion);
Console.WriteLine("Native MaaFramework version: " + engineVersion);
if (args.Contains("--repair-only")) { RepairQa.Run(root); return; }
if (args.Contains("--first-run-only"))
{
    var scan = typeof(MaaProcessorManager).GetMethod("ScanAllInstanceFiles", BindingFlags.NonPublic | BindingFlags.Static)!;
    var ids = (List<string>)scan.Invoke(null, null)!;
    if (!ids.SequenceEqual(new[] { "hif" })) throw new Exception("First run must contain only the public HIF instance");
    var config = new MFAAvalonia.Configuration.InstanceConfiguration("hif");
    if (config.GetValue("InstanceName", "") != "一键培育") throw new Exception("Unexpected initial instance name");
    var tasks = config.GetValue("TaskItems", new List<MaaInterface.MaaInterfaceTask>());
    if (tasks.Count != 1 || tasks[0].Entry != "ProduceHIF") throw new Exception("Unexpected initial task list");
    var firstRunExpected = JObject.Parse(File.ReadAllText(Path.Combine(root, "extensions/hif/first-run/config/instances/hif.json")));
    var selected = JArray.FromObject(tasks[0].Option!);
    foreach (var option in (JArray)firstRunExpected["TaskItems"]![0]!["option"]!)
    {
        var actual = selected.Single(item => (string?)item["name"] == (string?)option["name"]);
        foreach (var property in ((JObject)option).Properties())
            if (!JToken.DeepEquals(actual[property.Name], property.Value))
                throw new Exception("Initial HIF option differs: " + property.Name);
    }
    var layout = JObject.Parse(File.ReadAllText(Path.Combine(root, "resource/mfa_layout.json")));
    var grid = new MFAAvalonia.Views.UserControls.Dashboard.DashboardCardGrid { GridId = "TaskQueueView" };
    foreach (var name in new[] { "settings", "task_list", "task_desc", "live_view", "log" })
        grid.Children.Add(new MFAAvalonia.Views.UserControls.Dashboard.DashboardCard { CardId = name });
    var load = grid.GetType().GetMethod("EnsureLayoutsLoaded", BindingFlags.NonPublic | BindingFlags.Instance)!;
    load.Invoke(grid, null);
    foreach (var card in grid.Children.OfType<MFAAvalonia.Views.UserControls.Dashboard.DashboardCard>())
    {
        var cell = layout[card.CardId]!;
        if (card.GridRow != (int)cell["row"]! || card.GridColumn != (int)cell["col"]!
            || card.GridRowSpan != (int)cell["row_span"]! || card.GridColumnSpan != (int)cell["col_span"]!)
            throw new Exception("Initial dashboard layout differs: " + card.CardId);
    }
    Console.WriteLine("First-run native parser passed: one cultivation instance, only HIF, saved options and dashboard layout.");
    return;
}
if (args.Contains("--agent-demand-only"))
{
    Application.Current!.Styles.Add(new SukiUI.SukiTheme());
    var services = new ServiceCollection();
    services.AddSingleton<MFAAvalonia.ViewModels.UsersControls.Settings.GuiSettingsUserControlModel>();
    typeof(MFAAvalonia.App).GetProperty("Services")!.GetSetMethod(true)!.Invoke(null, [services.BuildServiceProvider()]);
    using var resource = new MaaFramework.Binding.MaaResource();
    using var tasker = new MaaFramework.Binding.MaaTasker
    {
        Resource = resource, Controller = MaaFramework.Binding.MaaController.Null,
        DisposeOptions = (MaaFramework.Binding.DisposeOptions)0
    };
    var processor = new MaaProcessor("agent-demand-qa") { MaaTasker = tasker };
    var ensure = typeof(MaaProcessor).GetMethod("EnsureTaskAgentAsync", BindingFlags.NonPublic | BindingFlags.Instance)!;
    var stop = typeof(MaaProcessor).GetMethod("StopTaskAgent", BindingFlags.NonPublic | BindingFlags.Instance)!;
    var contexts = (List<AgentContext>)Field(processor, "_agentContexts");
    void StartAgent(CancellationToken token, Action? whileStarting = null)
    {
        var startup = (Task)ensure.Invoke(processor, [tasker, token])!;
        var deadline = DateTime.UtcNow.AddSeconds(20);
        while (!startup.IsCompleted)
        {
            Avalonia.Threading.Dispatcher.UIThread.RunJobs();
            whileStarting?.Invoke();
            if (DateTime.UtcNow > deadline) throw new Exception("Agent demand check timed out");
            Thread.Sleep(10);
        }
        startup.GetAwaiter().GetResult();
    }
    void StopAgent() => stop.Invoke(processor, null);
    try
    {
        if (contexts.Count != 0) throw new Exception("Idle instance started an Agent");
        using var canceled = new CancellationTokenSource();
        canceled.Cancel();
        try { StartAgent(canceled.Token); throw new Exception("Canceled request started an Agent"); }
        catch (OperationCanceledException) { }
        if (contexts.Count != 0) throw new Exception("Canceled request leaked an Agent");
        StartAgent(CancellationToken.None);
        contexts = (List<AgentContext>)Field(processor, "_agentContexts");
        if (contexts.Count != 1 || contexts[0].Config!.ChildArgs!.Contains("--hif-only"))
            throw new Exception("Upstream task did not start exactly one upstream Agent");
        var processId = contexts[0].Process!.Id;
        StartAgent(CancellationToken.None);
        if (contexts.Count != 1 || contexts[0].Process!.Id != processId)
            throw new Exception("Adjacent upstream tasks restarted the Agent");
        StopAgent();
        if (contexts.Count != 0 || (bool)Field(processor, "_agentStarted"))
            throw new Exception("Switch/end cleanup retained an Agent");
        var originalAgents = MaaProcessor.Interface!.Agent;
        var failureScript = Path.Combine(root, "temp", "agent-failure.py");
        File.WriteAllText(failureScript, "import os\nfrom pathlib import Path\nPath('temp/failed-agent-pids.txt').open('a').write(str(os.getpid())+'\\n')\nraise SystemExit(7)\n");
        try
        {
            MaaProcessor.Interface.Agent = [new MaaInterface.MaaInterfaceAgent
            {
                ChildExec = "./python/python.exe", ChildArgs = [failureScript], Timeout = 1
            }];
            try { StartAgent(CancellationToken.None); throw new Exception("Failed startup was accepted"); }
            catch (InvalidOperationException) { }
            if (((List<AgentContext>)Field(processor, "_agentContexts")).Count != 0
                || (bool)Field(processor, "_agentStarted")) throw new Exception("Failed startup retained an Agent");
            foreach (var pid in File.ReadAllLines(Path.Combine(root, "temp", "failed-agent-pids.txt")))
            {
                try
                {
                    using var remaining = System.Diagnostics.Process.GetProcessById(int.Parse(pid));
                    if (!remaining.HasExited) throw new Exception("Failed startup leaked a process");
                }
                catch (ArgumentException) { }
            }
        }
        finally { MaaProcessor.Interface.Agent = originalAgents; }
        var canceledPidFile = Path.Combine(root, "temp", "canceled-agent-pid.txt");
        File.Delete(canceledPidFile);
        File.WriteAllText(failureScript, "import os,time\nfrom pathlib import Path\nPath('temp/canceled-agent-pid.txt').write_text(str(os.getpid()))\ntime.sleep(60)\n");
        try
        {
            MaaProcessor.Interface.Agent = [new MaaInterface.MaaInterfaceAgent
            {
                ChildExec = "./python/python.exe", ChildArgs = [failureScript], Timeout = 1
            }];
            using var canceledStartup = new CancellationTokenSource();
            try
            {
                StartAgent(canceledStartup.Token, () =>
                {
                    if (File.Exists(canceledPidFile)) canceledStartup.Cancel();
                });
                throw new Exception("Canceled startup was accepted");
            }
            catch (OperationCanceledException) { }
            if (!File.Exists(canceledPidFile)
                || ((List<AgentContext>)Field(processor, "_agentContexts")).Count != 0)
                throw new Exception("Startup cancellation was not exercised or retained an Agent");
            try
            {
                using var remaining = System.Diagnostics.Process.GetProcessById(int.Parse(File.ReadAllText(canceledPidFile)));
                if (!remaining.HasExited) throw new Exception("Canceled startup leaked a process");
            }
            catch (ArgumentException) { }
        }
        finally { MaaProcessor.Interface.Agent = originalAgents; }
        StartAgent(CancellationToken.None);
        contexts = (List<AgentContext>)Field(processor, "_agentContexts");
        if (contexts.Count != 1 || contexts[0].Process!.Id == processId)
            throw new Exception("Upstream Agent did not restart after switching back");
        Console.WriteLine("Demand loading passed: idle/canceled=0, upstream=1, reuse, switch cleanup, startup failure and restart.");
    }
    finally { StopAgent(); }
    tasker.Dispose();
    return;
}
if (args.Contains("--agent-cleanup-only"))
{
    // A controller-free native lifecycle test; never connects to or clicks the game.
    foreach (var alreadyExited in new[] { false, true })
    {
        using var resource = new MaaFramework.Binding.MaaResource();
        using var tasker = new MaaFramework.Binding.MaaTasker
        {
            Resource = resource, Controller = MaaFramework.Binding.MaaController.Null,
            DisposeOptions = (MaaFramework.Binding.DisposeOptions)0
        };
        var client = MaaFramework.Binding.MaaAgentClient.Create(resource);
        client.Tasker = tasker;
        var start = new System.Diagnostics.ProcessStartInfo(Path.Combine(root, "python", "python.exe"))
        {
            WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        start.ArgumentList.Add(Path.Combine(root, "tools", "agent_entry.py"));
        start.ArgumentList.Add("--hif-only");
        start.ArgumentList.Add(client.Id ?? throw new Exception("Missing Agent identifier"));
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        if (!client.LinkStart(start, timeout.Token)) throw new Exception("Fixture Agent failed to connect");
        var process = client.AgentServerProcess;
        var processId = process.Id;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (alreadyExited)
        {
            process.Kill(true);
            process.WaitForExit();
        }
        AgentHelper.KillAllAgents([new AgentContext { Client = client, Process = process }]);
        tasker.Dispose(); // Formerly threw InvalidOperationException and replaced the successful result.
        try
        {
            using var remaining = System.Diagnostics.Process.GetProcessById(processId);
            if (!remaining.HasExited) throw new Exception("Agent process leaked");
        }
        catch (ArgumentException) { }
        await Task.WhenAll(stdout, stderr);
        Console.WriteLine($"Agent cleanup passed (already exited: {alreadyExited}); native disposal completed; process exited.");
    }
    return;
}
var hifTask = MaaProcessor.Interface!.Task!.Single(t => t.Name == "HIF培育");
if (hifTask.Entry != "ProduceHIF" || hifTask.Option!.Any(o => o.Name == "HIF.培育难度" || o.Name == "HIF.培育偶像" || o.Name == "HIF.跳过选择偶像"))
    throw new Exception("Native task import failed");
if (MaaProcessor.Interface.Task!.Single(t => t.Name == "开始培育").Entry != "Produce")
    throw new Exception("Upstream task changed");
if (args.Contains("--new-scenario-only"))
{
    if (!MaaProcessor.Interface.Option!["培育难度"].Cases!.Any(c => c.Name == "QA新增剧本"))
        throw new Exception("New upstream scenario was not loaded by native front end");
    Console.WriteLine("New upstream scenario and isolated HIF task loaded by native front-end parser.");
    return;
}

object Field(object instance, string name) => instance.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(instance)!;
void Save(object instance, string method) => instance.GetType().GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(instance, null);
void Loaded(object window)
{
    if (!(bool)Field(window, "_loaded")) throw new Exception("Panel failed to load: " + window.GetType().Name + " " + ((TextBlock)Field(window, "_status")).Text);
}
void Snapshot(Window window, string name)
{
    var content = (Control)window.Content!;
    content.Measure(new Size(1000, 700));
    content.Arrange(new Rect(0, 0, 1000, 700));
    // Constructors and disk round trips are asserted here; visual layout is unchanged.
}

var priority = new HifPriorityWindow("集中");
Loaded(priority);
var unknown = (IDictionary)Field(priority, "_unknownPriorities");
var expected = (decimal)unknown["集中"]! + 1;
unknown["集中"] = expected;
((HashSet<string>)Field(priority, "_changedProfessions")).Add("集中");
Save(priority, "SaveConfig");
var reopened = new HifPriorityWindow("集中");
Loaded(reopened);
if ((decimal)((IDictionary)Field(reopened, "_unknownPriorities"))["集中"]! != expected)
    throw new Exception("Priority save/reopen failed");
Snapshot(reopened, "priority");
foreach (var (target, swap, name) in new[] { (true, false, "target"), (false, true, "swap") })
{
    var window = new HifPriorityWindow("集中", target, swap);
    Loaded(window);
    var field = target ? "_targetProfiles" : "_swapOutProfiles";
    var selected = (List<JObject>)((IDictionary)Field(window, field))["集中"]!;
    var catalog = (JArray)Field(window, "_targetCatalog");
    var candidate = catalog.OfType<JObject>().First(card => selected.All(item => (string?)item["name"] != (string?)card["name"]));
    selected.Insert(0, (JObject)candidate.DeepClone());
    var expectedNames = selected.Select(card => (string?)card["name"]).ToArray();
    Save(window, "SaveConfig");
    var again = new HifPriorityWindow("集中", target, swap);
    Loaded(again);
    var actualNames = ((List<JObject>)((IDictionary)Field(again, field))["集中"]!).Select(card => (string?)card["name"]).ToArray();
    if (!expectedNames.SequenceEqual(actualNames)) throw new Exception(name + " save/reopen or ordering failed");
    Snapshot(again, name);
}

var drink = new HifDrinkWindow("集中");
Loaded(drink);
((IDictionary)Field(drink, "_defaultTimings"))["集中"] = new JObject { ["mode"] = "remaining_turn", ["turn"] = 7 };
Save(drink, "Save");
var drinkAgain = new HifDrinkWindow("集中");
Loaded(drinkAgain);
if ((int?)((JObject)((IDictionary)Field(drinkAgain, "_defaultTimings"))["集中"]!)["turn"] != 7)
    throw new Exception("Drink save/reopen failed");
Snapshot(drinkAgain, "drinks");

var custom = new HifCustomCardWindow("集中");
Loaded(custom);
((IDictionary)((IDictionary)Field(custom, "_profiles"))["集中"]!)[381] = new HashSet<int> { 65 };
Save(custom, "Save");
var customAgain = new HifCustomCardWindow("集中");
Loaded(customAgain);
var cards = (IDictionary)((IDictionary)Field(customAgain, "_profiles"))["集中"]!;
if (!((HashSet<int>)cards[381]!).Contains(65)) throw new Exception("Customization save/reopen failed");
Snapshot(customAgain, "customization");
Console.WriteLine("All five panels loaded and passed save/reopen, including target/swap ordering.");
