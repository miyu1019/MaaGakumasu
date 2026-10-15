using System.Reflection;
using Avalonia;
using Avalonia.Data;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Views.UserControls.Dashboard;
using Newtonsoft.Json.Linq;

static class TaskDescriptionQa
{
    public static void Run(string root)
    {
        var settings = new GuiSettingsUserControlModel();
        Application.Current!.Resources["FullScreenGeometry"] = Geometry.Parse("M0,0 L1,1");
        Application.Current.Resources["FullScreenReturnGeometry"] = Geometry.Parse("M0,0 L1,1");
        if (!settings.ShowTaskDescription) throw new Exception("Task description must default to visible");
        var grid = new DashboardCardGrid { Columns = 3, Rows = 2 };
        var log = new DashboardCard { CardId = "log" };
        var description = new DashboardCard { CardId = "task_desc", GridColumn = 1 };
        description.Bind(DashboardCard.IsVisibleProperty, new Binding(nameof(settings.ShowTaskDescription)) { Source = settings });
        grid.Children.Add(log);
        grid.Children.Add(description);
        void Layout()
        {
            Dispatcher.UIThread.RunJobs();
            grid.Measure(new Size(900, 600));
            grid.Arrange(new Rect(0, 0, 900, 600));
        }
        Layout();
        var position = (description.GridColumn, description.GridRow);
        settings.ShowTaskDescription = false;
        Layout();
        if (description.IsVisible) throw new Exception("Disabling task description did not hide the card");
        var hidden = (System.Collections.IDictionary)typeof(DashboardCardGrid)
            .GetField("_hiddenCards", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(grid)!;
        if (!hidden.Contains(description)) throw new Exception("Hidden task description still occupies dashboard cells");
        var saved = JObject.Parse(File.ReadAllText(Path.Combine(root, "config/config.json")));
        if (saved[ConfigurationKeys.ShowTaskDescription]?.Value<bool>() != false)
            throw new Exception("Task description preference was not saved");
        ConfigurationManager.Current.SetConfig(saved.ToObject<Dictionary<string, object>>()!);
        if (new GuiSettingsUserControlModel().ShowTaskDescription)
            throw new Exception("Task description preference was lost after reload");
        settings.ShowTaskDescription = true;
        Layout();
        if (!description.IsVisible || hidden.Contains(description)
            || position != (description.GridColumn, description.GridRow))
            throw new Exception("Re-enabling task description did not restore its position");
        foreach (var enabled in new[] { false, true })
        {
            log.IsMaximized = true;
            typeof(DashboardCardGrid).GetMethod("ApplyMaximize", BindingFlags.NonPublic | BindingFlags.Instance)!
                .Invoke(grid, [log]);
            settings.ShowTaskDescription = enabled;
            log.IsMaximized = false;
            typeof(DashboardCardGrid).GetMethod("RestoreMaximize", BindingFlags.NonPublic | BindingFlags.Instance)!
                .Invoke(grid, [log]);
            var deadline = DateTime.UtcNow.AddMilliseconds(400);
            while (DateTime.UtcNow < deadline)
            {
                Dispatcher.UIThread.RunJobs();
                Thread.Sleep(5);
            }
            Layout();
            if (description.IsVisible != enabled)
                throw new Exception("Maximize/restore overwrote the task description preference");
        }
        Console.WriteLine("Task description: default, live toggle, freed cells, save/reload, restored position and maximize/restore passed.");
    }
}
