using System.Reflection;
using Avalonia;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Views.UserControls.Dashboard;

static class DashboardMaximizeQa
{
    static void Invoke(DashboardCardGrid grid, string method, DashboardCard card) =>
        typeof(DashboardCardGrid).GetMethod(method, BindingFlags.Instance | BindingFlags.NonPublic)!
            .Invoke(grid, [card]);

    static void FinishAnimation(DashboardCardGrid grid)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(400);
        while (DateTime.UtcNow < deadline)
        {
            Dispatcher.UIThread.RunJobs();
            Thread.Sleep(5);
        }
        grid.Measure(new Size(900, 600));
        grid.Arrange(new Rect(0, 0, 900, 600));
    }

    public static void Run()
    {
        Application.Current!.Resources["FullScreenGeometry"] = Geometry.Parse("M0,0 L1,1");
        Application.Current.Resources["FullScreenReturnGeometry"] = Geometry.Parse("M0,0 L1,1");
        foreach (var targetId in new[] { "log", "task_desc" })
        foreach (var enabled in new[] { false, true })
        {
            var grid = new DashboardCardGrid { Columns = 3, Rows = 2 };
            var target = new DashboardCard { CardId = targetId };
            var live = new DashboardCard { CardId = "live_view", GridColumn = 1, IsVisible = enabled };
            var hidden = new DashboardCard { CardId = "hidden", GridColumn = 2, IsVisible = false };
            grid.Children.Add(target);
            grid.Children.Add(live);
            grid.Children.Add(hidden);
            FinishAnimation(grid);
            var bounds = live.Bounds;
            target.IsMaximized = true;
            Invoke(grid, "ApplyMaximize", target);
            FinishAnimation(grid);
            if (live.IsVisible != enabled || hidden.IsVisible)
                throw new Exception("Maximizing changed panel visibility");
            target.IsMaximized = false;
            Invoke(grid, "RestoreMaximize", target);
            FinishAnimation(grid);
            if (live.IsVisible != enabled || hidden.IsVisible || live.Bounds != bounds
                || live.GridColumn != 1 || live.GridRow != 0)
                throw new Exception("Restoring changed visibility or layout");

            // A live-view setting changed during maximization must remain authoritative.
            target.IsMaximized = true;
            Invoke(grid, "ApplyMaximize", target);
            FinishAnimation(grid);
            live.IsVisible = !enabled;
            target.IsMaximized = false;
            Invoke(grid, "RestoreMaximize", target);
            FinishAnimation(grid);
            if (live.IsVisible != !enabled || hidden.IsVisible)
                throw new Exception("Restoring overwrote the current live-view setting");
        }
        Console.WriteLine("Dashboard maximize/restore preserves visibility, live-view setting and layout.");
    }
}
