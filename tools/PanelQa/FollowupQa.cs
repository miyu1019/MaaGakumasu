using System.Collections;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Media.Imaging;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Windows;
using Newtonsoft.Json.Linq;

static class FollowupQa
{
    static object Field(object instance, string name) => instance.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(instance)!;
    static void Invoke(object instance, string name) => instance.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(instance, null);
    static void Check(bool test, string message) { if (!test) throw new Exception(message); }
    static void Loaded(object window) => Check((bool)Field(window, "_loaded"), ((TextBlock)Field(window, "_status")).Text ?? "Panel not loaded");
    static void Snapshot(Window window, string root, string name, bool close = true)
    {
        window.Position = new PixelPoint(-10000, -10000);
        window.Show(); Dispatcher.UIThread.RunJobs();
        var original = (Control)window.Content!;
        window.Content = null;
        var content = new Border { Background = Brushes.White, Child = original };
        window.Content = content;
        content.Measure(new Size(window.Width - 36, window.Height - 60));
        content.Arrange(new Rect(0, 0, window.Width - 36, window.Height - 60));
        for (var frame = 0; frame < 20; frame++) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(16); }
        using var bitmap = new RenderTargetBitmap(new PixelSize((int)content.Bounds.Width, (int)content.Bounds.Height), new Vector(96, 96));
        bitmap.Render(content); bitmap.Save(Path.Combine(root, "debug", name + ".png")); if (close) window.Close();
    }
    public static void Run(string root)
    {
        var cardPath = HifLayout.PersonalPath("cards_priority.json");
        var before = File.ReadAllBytes(cardPath);
        var legacy = JObject.Parse(File.ReadAllText(cardPath));
        legacy.Remove("followup_profiles"); legacy["combo"] = new JObject { ["first"] = "国民的アイドル+", ["second"] = "脚光+" }; legacy["skip_limit"] = 4;
        File.WriteAllText(cardPath, legacy.ToString());
        var instancePath = Path.Combine(root, "config", "instances", "followup-conflict.json");
        var tasks = new JArray();
        foreach (var wait in new[] { 1, 5 }) tasks.Add(new JObject { ["entry"] = "ProduceHIF", ["option"] = new JArray(
            new JObject { ["name"] = HifFollowupConfig.OldWait, ["data"] = new JObject { ["combo_wait_turns"] = wait.ToString() } }) });
        File.WriteAllText(instancePath, new JObject { ["TaskItems"] = tasks, ["private"] = "keep" }.ToString());
        var panel = new HifPriorityWindow("集中"); Loaded(panel);
        var choices = (IDictionary)Field(panel, "_migrationChoices");
        Check(choices.Contains("集中"), "Old wait conflict was lost");
        Invoke(panel, "SaveConfig");
        Check(File.ReadAllText(cardPath) == legacy.ToString(), "Conflict wrote config before choice");
        var rules = (JObject)((IDictionary)Field(panel, "_followups"))["集中"]!;
        var migrated = (JObject)rules["国民的アイドル+"]!;
        migrated["wait_turns"] = 5; migrated["wait_after_success"] = true; migrated["target_policy"] = "combo_only";
        migrated["targets"] = new JArray("脚光+", "存在感+"); choices.Remove("集中");
        Invoke(panel, "SaveConfig");
        Check((int?)JObject.Parse(File.ReadAllText(cardPath))["followup_profiles"]?["集中"]?["国民的アイドル+"]?["wait_turns"] == 5, "Group save failed");
        var savedConfig = JObject.Parse(File.ReadAllText(cardPath));
        Check(savedConfig["combo"] == null && savedConfig["skip_limit"] == null, "Legacy global combo fields retained after migration");
        Check(HifFollowupConfig.LegacyWaits().Count == 0, "Old task waits retained");
        Check((string?)JObject.Parse(File.ReadAllText(instancePath))["private"] == "keep", "Private instance field changed");
        Check(File.Exists(Path.Combine(root, "backup/followups-before-migration/config/hif/cards_priority.json")), "Missing migration backup");
        var reopened = new HifPriorityWindow("集中"); Loaded(reopened);
        var restored = (JObject)((IDictionary)Field(reopened, "_followups"))["集中"]!;
        Check(JToken.DeepEquals(restored, rules), "Follow-up save/reopen changed candidate order or flags");
        Snapshot(reopened, root, "followup-priority-panel");
        JObject? applied = null;
        var picker = new HifFollowupWindow("国民的アイドル+", false, migrated, value => applied = value);
        Check(((Control)picker.Content!).GetLogicalDescendants().OfType<ComboBox>().Count() == 5, "Follow-up import filters are missing");
        var category = ((Control)picker.Content!).GetLogicalDescendants().OfType<ComboBox>()
            .Single(c => ((IEnumerable<string>)c.ItemsSource!).First() == "分类：全部");
        category.SelectedIndex = 2;
        var targetCatalog = JArray.Parse(File.ReadAllText(HifLayout.CatalogPath("hif_target_card_catalog.json")))
            .OfType<JObject>().ToDictionary(c => (string)c["name"]!);
        foreach (var add in ((Control)picker.Content!).GetLogicalDescendants().OfType<Button>().Where(b => (string?)b.Content == "添加"))
        {
            var key = ((Grid)add.Parent!).GetLogicalDescendants().OfType<TextBlock>().Single().Text!;
            Check((string?)targetCatalog[key]["type"] == "mental", "M filter included an A card");
        }
        category.SelectedIndex = 0;
        Check(((Control)picker.Content!).GetLogicalDescendants().OfType<Button>().Count(b => (string?)b.Content == "添加") <= 40, "Picker did not page available cards");
        var controls = ((Control)picker.Content!).GetLogicalDescendants().OfType<Button>().ToArray();
        Snapshot(picker, root, "followup-card-dialog");
        controls.First(b => (string?)b.Content == "↓").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        controls.Single(b => (string?)b.Content == "应用到本次编辑").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Check(applied != null && (string?)applied["targets"]?[0] == "存在感+" && (bool?)applied["wait_after_success"] == true, "Picker edits or apply callback failed");
        var cleared = false;
        var clearPicker = new HifFollowupWindow("国民的アイドル+", false, new JObject { ["targets"] = new JArray("脚光+") }, value => cleared = value == null);
        var clearControls = ((Control)clearPicker.Content!).GetLogicalDescendants().OfType<Button>().ToArray();
        clearControls.Single(b => (string?)b.Content == "删除").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        clearControls.Single(b => (string?)b.Content == "应用到本次编辑").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Check(cleared, "Clearing all candidates did not disable rule");
        var drinks = new HifDrinkWindow("集中"); Loaded(drinks);
        var imported = (List<JObject>)((IDictionary)Field(drinks, "_profiles"))["集中"]!;
        var drink = imported.FirstOrDefault(d => (int?)d["id"] == 25);
        if (drink == null) { drink = new JObject { ["id"] = 25 }; imported.Add(drink); }
        drink["before_card_targets"] = new JArray("脚光+", "存在感+");
        Invoke(drinks, "Save");
        Check(((TextBlock)Field(drinks, "_status")).Text!.Contains("无效"), "Mental card was accepted by A-only drink");
        drink["before_card_targets"] = new JArray("脚光+", "アピールの基本+");
        Invoke(drinks, "Save");
        var drinkAgain = new HifDrinkWindow("集中"); Loaded(drinkAgain);
        var savedDrink = ((List<JObject>)((IDictionary)Field(drinkAgain, "_profiles"))["集中"]!).Single(d => (int?)d["id"] == 25);
        Check(JToken.DeepEquals(savedDrink["before_card_targets"], drink["before_card_targets"]), "Drink association save/reopen failed");
        Snapshot(drinkAgain, root, "followup-drink-panel");
        Snapshot(new HifFollowupWindow("特製ハツボシエキス", true, new JObject { ["targets"] = drink["before_card_targets"]!.DeepClone() }, _ => { }), root, "followup-drink-dialog");
        Check(!MFAAvalonia.Extensions.MaaFW.MaaProcessor.Interface!.Option!.ContainsKey(HifFollowupConfig.OldWait), "Removed task option remains exposed");
        foreach (var profession in new[] { "好调", "集中", "好印象", "元気", "全力", "強気" })
        {
            var window = new HifPriorityWindow(profession); Loaded(window);
            var profiles = (IDictionary)Field(window, "_profiles");
            var sourceCards = (List<JObject>)profiles["集中"]!;
            var cards = (List<JObject>)profiles[profession]!;
            var seed = (JObject)sourceCards[0].DeepClone(); var key = (string)seed["key"]!;
            if (cards.All(c => (string?)c["key"] != key)) cards.Add(seed);
            var conditions = (JObject)((IDictionary)Field(window, "_conditionalPriorities"))[profession]!;
            conditions[key] = new JObject { ["priority"] = 1, ["remaining_turns_gte"] = 3, ["remaining_turns_lte"] = 6, ["mode"] = "any" };
            ((HashSet<string>)((IDictionary)Field(window, "_noExtraTurns"))[profession]!).Add(key);
            Invoke(window, "RenderCards");
            window.Position = new PixelPoint(-10000, -10000); window.Show(); Dispatcher.UIThread.RunJobs();
            Check(((Control)window.Content!).GetLogicalDescendants().OfType<Button>().Any(b => (string?)b.Content == "条件优先级✓"), profession + " missing conditional priority entry");
            var cardRow = ((StackPanel)Field(window, "_rows")).Children.OfType<Border>().Select(b => b.Child).OfType<Grid>().First();
            var followupButton = cardRow.Children.OfType<Button>().Single(b => ((string?)b.Content)?.StartsWith("后续卡") == true);
            var useButton = cardRow.Children.OfType<Button>().Single(b => ((string?)b.Content) is "使用限制" or "限制✓");
            Check(Grid.GetRow(followupButton) == Grid.GetRow(useButton), "Follow-up entry occupies an extra card row");
            window.GetType().GetMethod("ShowConditionDialog", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { key, 10m });
            Dispatcher.UIThread.RunJobs();
            var dialog = window.OwnedWindows.Single();
            var dialogControls = ((Control)dialog.Content!).GetLogicalDescendants().ToArray();
            Check(dialogControls.OfType<CheckBox>().Any(c => (string?)c.Content == "全力值 <") == (profession == "全力"), "Non-full-power profession has pointer/value restriction");
            var lowerEnabled = dialogControls.OfType<CheckBox>().Single(c => (string?)c.Content == "剩余回合 ≥");
            var lower = ((StackPanel)lowerEnabled.Parent!).Children.OfType<NumericUpDown>().Single();
            var applyButton = dialogControls.OfType<Button>().Single(b => (string?)b.Content == "应用到本次编辑");
            Snapshot(dialog, root, "range-condition-" + profession, false);
            lower.Value = 7; applyButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(dialogControls.OfType<TextBlock>().Any(t => t.Text == "回合下限不能大于上限"), "Reversed interval was accepted");
            lower.Value = 3; applyButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(!((Control)window.Content!).GetLogicalDescendants().OfType<CheckBox>().Any(c => (string?)c.Content == "有额外回合时不使用"), "Extra-turn option remains in main card list");
            var showLimits = window.GetType().GetMethod("ShowUseConditionDialog", BindingFlags.Instance | BindingFlags.NonPublic)!;
            showLimits.Invoke(window, new object[] { key }); Dispatcher.UIThread.RunJobs();
            var limitDialog = window.OwnedWindows.Single();
            var limitControls = ((Control)limitDialog.Content!).GetLogicalDescendants().ToArray();
            var noExtra = limitControls.OfType<CheckBox>().Single(c => (string?)c.Content == "有额外回合时不使用");
            Check(noExtra.IsChecked == true, "Extra-turn restriction not loaded in use-conditions dialog");
            noExtra.IsChecked = false;
            limitControls.OfType<Button>().Single(b => (string?)b.Content == "取消").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(((HashSet<string>)((IDictionary)Field(window, "_noExtraTurns"))[profession]!).Contains(key), "Canceled restriction edit was applied");
            showLimits.Invoke(window, new object[] { key }); Dispatcher.UIThread.RunJobs();
            limitDialog = window.OwnedWindows.Single();
            Snapshot(limitDialog, root, "range-use-limits-" + profession, false);
            ((Control)limitDialog.Content!).GetLogicalDescendants().OfType<Button>().Single(b => (string?)b.Content == "应用到本次编辑").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Invoke(window, "SaveConfig");
            var again = new HifPriorityWindow(profession); Loaded(again);
            var restoredRules = (JObject)((IDictionary)Field(again, "_conditionalPriorities"))[profession]!;
            Check((int?)restoredRules[key]?["remaining_turns_gte"] == 3 && (int?)restoredRules[key]?["remaining_turns_lte"] == 6, "Turn interval lost on save/reopen");
            Check(((HashSet<string>)((IDictionary)Field(again, "_noExtraTurns"))[profession]!).Contains(key), "Extra turn exclusion lost on save/reopen");
            Snapshot(again, root, "range-priority-" + profession);
            window.Close();
        }
        File.WriteAllBytes(cardPath, before);
        Console.WriteLine("Follow-up panels passed migration, import filtering/paging, profession intervals, reversed bounds, pending extra-turn exclusion and save/reopen.");
    }
}
