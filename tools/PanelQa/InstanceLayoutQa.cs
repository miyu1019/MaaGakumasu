using System.Reflection;
using System.Security.Cryptography;
using Avalonia;
using Avalonia.Data;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Views.UserControls.Dashboard;
using Newtonsoft.Json.Linq;

static class InstanceLayoutQa
{
    static object? Invoke(object target, string name, params object[] args) => target.GetType()
        .GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(target, args);
    static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    static void Layout(DashboardCardGrid grid)
    {
        Dispatcher.UIThread.RunJobs();
        grid.Measure(new Size(1200, 800));
        grid.Arrange(new Rect(0, 0, 1200, 800));
    }

    public static void Run(string root)
    {
        Application.Current!.Resources["FullScreenGeometry"] = Geometry.Parse("M0,0 L1,1");
        Application.Current.Resources["FullScreenReturnGeometry"] = Geometry.Parse("M0,0 L1,1");
        const string key = ConfigurationKeys.DashboardCardGridLayout + ".TaskQueueView";
        var settings = new GuiSettingsUserControlModel { ShowTaskDescription = false };
        var defaults = new List<DashboardCardLayout>
        {
            new() { Id = "task_list", Col = 0, Row = 0, ColSpan = 4, RowSpan = 6, ExpandedColSpan = 4, ExpandedRowSpan = 6 },
            new() { Id = "log", Col = 4, Row = 0, ColSpan = 4, RowSpan = 6, ExpandedColSpan = 4, ExpandedRowSpan = 6 },
            new() { Id = "task_desc", Col = 8, Row = 0, ColSpan = 4, RowSpan = 3, ExpandedColSpan = 4, ExpandedRowSpan = 3 },
            new() { Id = "live_view", Col = 8, Row = 3, ColSpan = 4, RowSpan = 3, ExpandedColSpan = 4, ExpandedRowSpan = 3 }
        };
        ConfigurationManager.Current.SetValue(key, defaults);
        ConfigurationManager.Current.SetValue(ConfigurationKeys.TaskQueueDashboardLayout + ".TaskQueueView",
            new { Rows = 8, Columns = 12, Spacing = 10 });
        ConfigurationManager.Current.SetValue(ConfigurationKeys.DashboardCardGridResourceLayoutHash + ".TaskQueueView",
            Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(root, "resource/mfa_layout.json")))));
        ConfigurationManager.SetLiveViewEnabled(false);

        DashboardCardGrid Create(InstanceConfiguration owner)
        {
            var result = new DashboardCardGrid { GridId = "TaskQueueView", Columns = 12, Rows = 8 };
            foreach (var item in defaults)
            {
                var card = new DashboardCard { CardId = item.Id };
                if (item.Id == "task_desc") card.Bind(DashboardCard.IsVisibleProperty,
                    new Binding(nameof(settings.ShowTaskDescription)) { Source = settings });
                if (item.Id == "live_view") card.IsVisible = ConfigurationManager.GetLiveViewEnabled();
                result.Children.Add(card);
            }
            result.UseInstanceLayout(owner);
            Invoke(result, "EnsureLayoutsLoaded");
            Layout(result);
            return result;
        }
        DashboardCard Card(DashboardCardGrid grid, string id) => grid.Children.OfType<DashboardCard>().Single(c => c.CardId == id);
        var daily = new InstanceConfiguration("layout-daily-qa");
        var cultivation = new InstanceConfiguration("layout-cultivation-qa");
        var grid = Create(daily);
        Check(Card(grid, "task_list").GridRowSpan == 6, "Existing global layout was not inherited");
        Card(grid, "task_list").GridRowSpan = 8;
        Card(grid, "log").GridRowSpan = 2;
        Card(grid, "task_desc").GridRow = 1;
        Invoke(grid, "SaveLayouts");
        grid.UseInstanceLayout(cultivation);
        Check(Card(grid, "task_list").GridRowSpan == 6, "New instance inherited another instance's edits");
        grid.Rows = 10;
        Card(grid, "task_list").GridRowSpan = 2;
        Card(grid, "log").GridRowSpan = 10;
        Card(grid, "task_desc").GridRow = 0;
        Card(grid, "task_desc").GridRowSpan = 2;
        Card(grid, "task_list").IsCollapsed = true;
        Card(grid, "task_list").ExpandedRowSpan = 2;
        Invoke(grid, "SaveLayouts");
        grid.UseInstanceLayout(daily);
        Check(grid.Rows == 8 && Card(grid, "task_list").GridRowSpan == 8 && Card(grid, "log").GridRowSpan == 2,
            "Switching lost the daily layout or grid dimensions");
        settings.ShowTaskDescription = true;
        Layout(grid);
        Check(Card(grid, "task_desc").GridRow == 1, "Hidden card restored a different instance's position");
        settings.ShowTaskDescription = false;
        Card(grid, "log").IsMaximized = true;
        grid.UseInstanceLayout(cultivation);
        Layout(grid);
        Check(!Card(grid, "log").IsMaximized && grid.Rows == 10 && Card(grid, "log").GridRowSpan == 10,
            "Switching during maximize corrupted the new layout");
        var deadline = DateTime.UtcNow.AddMilliseconds(350);
        while (DateTime.UtcNow < deadline) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(5); }
        grid.UseInstanceLayout(daily);
        Check(Card(grid, "log").GridRowSpan == 2, "Canceled animation overwrote another instance");
        var reopened = Create(new InstanceConfiguration("layout-cultivation-qa"));
        Check(reopened.Rows == 10 && Card(reopened, "log").GridRowSpan == 10, "Restart lost instance dimensions/layout");
        Check(Card(reopened, "task_list").IsCollapsed && Card(reopened, "task_list").ExpandedRowSpan == 2,
            "Restart lost the collapsed card's expanded size");
        Check(!Card(reopened, "task_desc").IsVisible, "Closed task description was reopened");
        settings.ShowTaskDescription = true;
        Layout(grid); Layout(reopened);
        Check(Card(grid, "task_desc").IsVisible && Card(grid, "task_desc").GridRow == 1
            && Card(reopened, "task_desc").IsVisible && Card(reopened, "task_desc").GridRow == 0
            && Card(reopened, "task_desc").GridRowSpan == 2, "Global description toggle lost per-instance layout");
        var notifications = new List<bool>();
        void Toggle(bool value)
        {
            notifications.Add(value);
            Card(grid, "live_view").IsVisible = value;
            Card(reopened, "live_view").IsVisible = value;
        }
        ConfigurationManager.LiveViewEnabledChanged += Toggle;
        try
        {
            daily.SetValue(ConfigurationKeys.EnableLiveView, false);
            using var previewA = new TaskQueueViewModel("layout-preview-a-qa");
            using var previewB = new TaskQueueViewModel("layout-preview-b-qa");
            previewA.EnableLiveView = true;
            Check(previewB.EnableLiveView, "Another instance retained its disabled preview state");
            var processor = new MaaProcessor("layout-daily-qa");
            Check((bool)typeof(MaaProcessor).GetProperty("IsLiveViewEnabled", BindingFlags.NonPublic | BindingFlags.Instance)!
                .GetValue(processor)!, "Processor used a stale per-instance preview setting");
            previewB.EnableLiveView = false;
            Check(!previewA.EnableLiveView, "Another instance retained its enabled preview state");
            Check(notifications.SequenceEqual(new[] { true, false }) && !Card(grid, "live_view").IsVisible
                && !Card(reopened, "live_view").IsVisible, "Global preview switch did not reach all instances");
        }
        finally { ConfigurationManager.LiveViewEnabledChanged -= Toggle; }
        Check(JToken.DeepEquals(JToken.FromObject(defaults), JToken.FromObject(ConfigurationManager.Current.GetValue(key, new List<DashboardCardLayout>()))),
            "Instance layout edits overwrote the global migration source");
        Console.WriteLine("Instance layouts: migration, independent size/position, switch, restart, hidden panels, global toggles and maximize race passed.");
    }
}
