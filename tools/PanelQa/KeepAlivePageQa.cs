using Avalonia.Controls;
using Avalonia.Threading;
using Avalonia.VisualTree;
using SukiUI.Controls;

/// <summary>常驻页面：主页与 HIF 策略同时常驻、互不挤占；关闭常驻后离开即释放；普通页面仍正常卸载。</summary>
static class KeepAlivePageQa
{
    public static void Run()
    {
        var home = new Border { Name = "Home" };
        var strategy = new Border { Name = "Strategy" };
        var other = new Border { Name = "Other" };
        int homeLoaded = 0, strategyLoaded = 0, strategyUnloaded = 0, otherUnloaded = 0;
        home.Loaded += (_, _) => homeLoaded++;
        strategy.Loaded += (_, _) => strategyLoaded++;
        strategy.Unloaded += (_, _) => strategyUnloaded++;
        other.Unloaded += (_, _) => otherUnloaded++;
        var homeItem = new SukiSideMenuItem { Header = "Home", KeepContentAlive = true, PageContent = home };
        var strategyItem = new SukiSideMenuItem { Header = "Strategy", KeepContentAlive = true, PageContent = strategy };
        var otherItem = new SukiSideMenuItem { Header = "Other", PageContent = other };
        var menu = new SukiSideMenu();
        menu.Items.Add(homeItem);
        menu.Items.Add(strategyItem);
        menu.Items.Add(otherItem);
        QaHost.Show(menu);
        try
        {
            Settle();
            Expect(homeLoaded == 1 && Shown(home), "home not shown at start");
            for (var round = 0; round < 3; round++)
            {
                Select(menu, strategyItem);
                Expect(Shown(strategy) && Attached(home) && !Shown(home), "strategy not shown over hidden home");
                Select(menu, otherItem);
                Expect(Shown(other) && Attached(home) && Attached(strategy) && !Shown(strategy), "kept pages detached by other page");
                Select(menu, homeItem);
                Expect(Shown(home) && !Attached(other) && otherUnloaded == round + 1, "other page not unloaded");
            }
            Expect(homeLoaded == 1 && strategyLoaded == 1 && strategyUnloaded == 0, $"reloaded home={homeLoaded} strategy={strategyLoaded}");

            // 快速连切：动画未结束就切回，页面仍应可见且可点击。
            menu.SelectedItem = strategyItem;
            Dispatcher.UIThread.RunJobs();
            menu.SelectedItem = homeItem;
            Settle();
            Expect(Interactive(home) && !Shown(strategy), "home broken after rapid switch");

            // 隐藏时关闭常驻：立即释放；再次进入按普通页面加载，离开后卸载。
            strategyItem.KeepContentAlive = false;
            Dispatcher.UIThread.RunJobs();
            Expect(!Attached(strategy) && strategyUnloaded == 1, "hidden strategy not released when disabled");
            Select(menu, strategyItem);
            Expect(Interactive(strategy) && strategyLoaded == 2, "strategy not shown as normal page");
            Select(menu, homeItem);
            Expect(!Attached(strategy) && strategyUnloaded == 2 && Attached(home), "normal strategy page not unloaded");

            // 显示时重新开启、显示时关闭：离开后才释放。
            strategyItem.KeepContentAlive = true;
            Select(menu, strategyItem);
            Select(menu, homeItem);
            Expect(Attached(strategy) && strategyLoaded == 3, "strategy not kept after re-enabling");
            Select(menu, strategyItem);
            strategyItem.KeepContentAlive = false;
            Dispatcher.UIThread.RunJobs();
            Expect(Interactive(strategy), "visible strategy released while shown");
            Select(menu, homeItem);
            Expect(!Attached(strategy) && Interactive(home), "strategy not released after leaving");
        }
        finally { QaHost.Close(menu); }
        Console.WriteLine("Keep-alive pages passed: home and strategy kept together, toggle releases on leave, other pages unload, rapid switching recovers.");
    }

    static bool Attached(Control c) => c.GetVisualRoot() != null;
    static bool Shown(Control c) => Attached(c) && c.IsEffectivelyVisible;

    static bool Interactive(Control c)
    {
        var presenter = c.FindAncestorOfType<Avalonia.Controls.Presenters.ContentPresenter>();
        return Shown(c) && presenter is { IsHitTestVisible: true } && presenter.Opacity > 0.99;
    }

    static void Select(SukiSideMenu menu, SukiSideMenuItem item)
    {
        menu.SelectedItem = item;
        Settle();
    }

    static void Settle()
    {
        var until = DateTime.UtcNow.AddMilliseconds(600);
        while (DateTime.UtcNow < until) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(15); }
    }

    static void Expect(bool ok, string message)
    {
        if (!ok) throw new Exception("Keep-alive page: " + message);
    }
}
