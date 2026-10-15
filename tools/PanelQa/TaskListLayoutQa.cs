using System.Xml.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Views.UserControls.Dashboard;

static class TaskListLayoutQa
{
    public static void Run(string root)
    {
        Application.Current!.Resources["FullScreenGeometry"] = Geometry.Parse("M0,0 L1,1");
        Application.Current.Resources["FullScreenReturnGeometry"] = Geometry.Parse("M0,0 L1,1");
        var source = XDocument.Load(Path.Combine(root, "frontend/MFAAvalonia/Views/Pages/TaskQueueView.axaml"));
        var element = source.Descendants().Single(e => e.Name.LocalName == "ListBox"
            && e.Attributes().Any(a => a.Name.LocalName == "Name" && a.Value == "TaskListBox"));
        var minimum = (double?)element.Attribute("MinHeight") ?? 0;
        var list = new ListBox
        {
            MinHeight = minimum, Margin = new Thickness(0, 3, 0, 0),
            ItemsSource = new[] { "资源设置", "启动游戏", "HIF培育", "其他任务" },
            ItemTemplate = new FuncDataTemplate<string>((name, _) => new TextBlock { Text = name, Height = 40 }),
            ItemsPanel = new FuncTemplate<Panel?>(() => new StackPanel())
        };
        list.Classes.Add("Fluent");
        var content = new Grid();
        content.Children.Add(list);
        var card = new DashboardCard { CardId = "task_list", TitleContent = new TextBlock { Text = "任务列表" }, Content = content };
        var host = new Border { Child = card, Width = 600, Height = 220 };
        var canvas = new Canvas();
        canvas.Children.Add(host);
        var window = new Window { Content = canvas, Width = 700, Height = 500 };
        window.Show();
        try
        {
            foreach (var height in new[] { 220.0, 160.0, 120.0, 300.0 })
            {
                host.Height = height;
                canvas.Measure(new Size(700, 500));
                canvas.Arrange(new Rect(0, 0, 700, 500));
                Dispatcher.UIThread.RunJobs();
                var first = list.ContainerFromIndex(0)!;
                var header = card.GetVisualDescendants().OfType<Border>().Single(c => c.Name == "HeaderArea");
                var top = first.TranslatePoint(default, card)!.Value.Y;
                var headerBottom = header.TranslatePoint(new Point(0, header.Bounds.Height), card)!.Value.Y;
                var scroll = list.GetVisualDescendants().OfType<ScrollViewer>().First();
                Console.WriteLine($"Task list height={height}: first={top}, headerBottom={headerBottom}, viewport={scroll.Viewport.Height}, extent={scroll.Extent.Height}");
                if (top < headerBottom) throw new Exception("First task is obscured by the card header");
                if (list.Bounds.Height > content.Bounds.Height + 0.5)
                    throw new Exception("Task list exceeds its available content height");
                scroll.ScrollToEnd();
                Dispatcher.UIThread.RunJobs();
                var last = list.ContainerFromIndex(3)!;
                var bottom = last.TranslatePoint(new Point(0, last.Bounds.Height), card)!.Value.Y;
                var viewportBottom = scroll.TranslatePoint(new Point(0, scroll.Bounds.Height), card)!.Value.Y;
                if (bottom > viewportBottom + 0.5)
                    throw new Exception("Last task is unreachable by scrolling");
                scroll.Offset = default;
                Dispatcher.UIThread.RunJobs();
            }
        }
        finally { window.Close(); }
        Console.WriteLine("Task list small-window layout passed: first task visible and scrolling constrained to the card.");
    }
}
