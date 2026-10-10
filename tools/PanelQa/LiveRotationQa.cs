using System.Diagnostics;
using System.Reflection;
using MaaFramework.Binding;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json.Linq;

static class LiveRotationQa
{
    static string Shell(JToken device, params string[] command)
    {
        var start = new ProcessStartInfo(device["AdbPath"]!.Value<string>()!)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true
        };
        foreach (var arg in new[] { "-s", device["AdbSerial"]!.Value<string>()!, "shell" }.Concat(command))
            start.ArgumentList.Add(arg);
        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEnd();
        var error = process.StandardError.ReadToEnd();
        process.WaitForExit();
        if (process.ExitCode != 0) throw new Exception("ADB rotation command failed: " + error);
        return output.Trim();
    }

    public static void Run(string[] args)
    {
        var configIndex = Array.IndexOf(args, "--device-config");
        if (configIndex < 0) throw new ArgumentException("--device-config is required; no device settings are embedded in this test");
        var device = JObject.Parse(File.ReadAllText(args[configIndex + 1]))["AdbDevice"]!;
        using var controller = new MaaAdbController(device["AdbPath"]!.Value<string>()!, device["AdbSerial"]!.Value<string>()!,
            AdbScreencapMethods.EmulatorExtras, AdbInputMethods.EmulatorExtras, device["Config"]!.Value<string>()!,
            Path.Combine(AppContext.BaseDirectory, "MaaAgentBinary"));
        if (controller.LinkStart().Wait() != MaaJobStatus.Succeeded) throw new Exception("Cannot connect main SDK controller");
        using var resource = new MaaResource();
        using var tasker = new MaaTasker { Controller = controller, Resource = resource, DisposeOptions = (DisposeOptions)0 };
        var processor = new MaaProcessor("live-rotation-qa");
        processor.MaaTasker = tasker;
        processor.InstanceConfiguration.SetValue(ConfigurationKeys.EnableLiveView, false);
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        typeof(MaaProcessor).GetField("_activeHifTasker", flags)!.SetValue(processor, tasker);
        var callback = typeof(MaaProcessor).GetMethod("HandleHifControllerCallback", flags)!;
        // Observe real main-controller callbacks through the production recovery handler.
        controller.Callback += (sender, value) => callback.Invoke(processor, [sender, value]);
        var portrait = 0;
        var landscape = 0;
        var failed = 0;
        var watch = Stopwatch.StartNew();
        var originalAccel = Shell(device, "settings", "get", "system", "accelerometer_rotation");
        var originalRotation = Shell(device, "settings", "get", "system", "user_rotation");
        var originalFixed = Shell(device, "cmd", "window", "fixed-to-user-rotation");
        var originalIgnore = Shell(device, "cmd", "window", "get-ignore-orientation-request").Contains(" true ") ? "true" : "false";
        var stage = 0;
        Console.WriteLine("READY: EmulatorExtras main controller, preview disabled; no cultivation task started.");
        try
        {
        while (watch.Elapsed.TotalSeconds < 45)
        {
            if (stage == 0 && watch.Elapsed.TotalSeconds >= 3)
            {
                Shell(device, "cmd", "window", "set-ignore-orientation-request", "true");
                Shell(device, "cmd", "window", "fixed-to-user-rotation", "enabled");
                Shell(device, "cmd", "window", "user-rotation", "lock", "1");
                Console.WriteLine($"ROTATE {watch.Elapsed.TotalSeconds:F2}s landscape");
                stage++;
            }
            if (stage == 1 && watch.Elapsed.TotalSeconds >= 20)
            {
                Shell(device, "cmd", "window", "user-rotation", "lock", "0");
                Console.WriteLine($"ROTATE {watch.Elapsed.TotalSeconds:F2}s portrait");
                stage++;
            }
            var status = controller.Screencap().Wait();
            if (status != MaaJobStatus.Succeeded) { failed++; continue; }
            using var image = processor.GetImage(controller, false);
            if (image == null || !image.TryGetRawData(out _, out var width, out var height, out _))
                throw new Exception("A successful screenshot did not contain an image");
            if (width < height) portrait++;
            if (width > height) landscape++;
            Console.WriteLine($"FRAME {watch.Elapsed.TotalSeconds:F2}s {width}x{height}");
            if (processor.IsHifScreenshotRecoveryExpired) throw new Exception("Main screenshot recovery expired");
            if (typeof(MaaProcessor).GetField("_screenshotTasker", flags)!.GetValue(processor) != null)
                throw new Exception("Disabled preview created another controller");
            Thread.Sleep(150);
        }
        }
        finally
        {
            Shell(device, "cmd", "window", "user-rotation", "lock", originalRotation);
            Shell(device, "cmd", "window", "fixed-to-user-rotation", originalFixed);
            Shell(device, "cmd", "window", "set-ignore-orientation-request", originalIgnore);
            Shell(device, "settings", "put", "system", "user_rotation", originalRotation);
            Shell(device, "settings", "put", "system", "accelerometer_rotation", originalAccel);
            if (originalAccel == "1") Shell(device, "cmd", "window", "user-rotation", "free");
            Console.WriteLine("RESTORED original Android rotation settings");
        }
        typeof(MaaProcessor).GetField("_activeHifTasker", flags)!.SetValue(processor, null);
        Console.WriteLine($"RESULT portrait={portrait} landscape={landscape} failed={failed} preview=none task=not-started");
        if (portrait == 0 || landscape == 0) throw new Exception("Both rotation orientations were not captured");
    }
}
