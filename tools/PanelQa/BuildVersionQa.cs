using System;
using System.IO;
using MFAAvalonia.Helper;
using MFAAvalonia.ViewModels.Windows;
using MFAAvalonia.ViewModels.UsersControls.Settings;

internal static class BuildVersionQa
{
    public static void Run(string root)
    {
        var path = Path.Combine(root, "hif-build-info.json");
        var original = File.ReadAllBytes(path);
        const string version = "v261014.1";
        void Check(bool condition) { if (!condition) throw new Exception("Build version display mismatch"); }
        try
        {
            File.WriteAllText(path, "{\"build_channel\":\"stable\"}");
            Check(HifLayout.BuildVersionDisplay(version) == version);
            File.WriteAllText(path, "{\"build_channel\":\"nightly\",\"source_commit\":\"247a83b123\",\"built_at\":\"2026-10-14 04:00:00 +08:00\"}");
            var expected = version + " · AutoBuild 测试版 · 247a83b";
            Check(HifLayout.BuildVersionDisplay(version) == expected);
            var model = new VersionUpdateSettingsUserControlModel { ResourceVersion = version };
            Check(model.ResourceVersion == version && model.ResourceVersionDisplay == expected);
            var title = new RootViewModel();
            title.ShowResourceVersion(version);
            Check(title.ResourceVersion == expected);
            File.WriteAllText(path, "{\"build_channel\":\"nightly\",\"built_at\":\"2026-10-15 04:00:00 +08:00\",\"source_commit\":\"247a83b123\"}");
            Check(HifLayout.BuildVersionDisplay(version) == expected);
            File.WriteAllText(path, "{\"build_channel\":\"nightly\",\"source_commit\":\"9dc212e456\"}");
            Check(HifLayout.BuildVersionDisplay(version) == version + " · AutoBuild 测试版 · 9dc212e");
            File.WriteAllText(path, "{");
            Check(HifLayout.BuildVersionDisplay(version) == version);
            File.Delete(path);
            Check(HifLayout.BuildVersionDisplay(version) == version);
        }
        finally { File.WriteAllBytes(path, original); }
        Console.WriteLine("Build version display passed: stable, AutoBuild identity, title/settings, legacy metadata and invalid/missing metadata.");
    }
}
