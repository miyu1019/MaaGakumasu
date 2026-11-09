using System.Collections;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using MFAAvalonia.Views.Pages.Hif;
using Newtonsoft.Json.Linq;

/// <summary>出牌编排：旧版后续卡不迁移、选卡与用饮编辑、保存后结构；以及出牌优先级详情的规则编辑。</summary>
static class SequenceQa
{
    static object Field(object instance, string name) => instance.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(instance)!;
    static void Invoke(object instance, string name) => instance.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(instance, null);
    static void Check(bool test, string message) { if (!test) throw new Exception(message); }
    static void Loaded(object window) => Check((bool)Field(window, "_loaded"), ((TextBlock)Field(window, "_status")).Text ?? "Panel not loaded");
    static void Run() { for (var i = 0; i < 10; i++) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(10); } }
    static JObject Rules(HifPriorityPanel panel, string profession) => (JObject)((IDictionary)Field(panel, "_sequences"))[profession]!;
    static void Snapshot(Control panel, string root, string name, bool close = true)
    {
        var window = QaHost.Window(panel);
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
        bitmap.Render(content); bitmap.Save(Path.Combine(root, "debug", name + ".png")); if (close) QaHost.Close(panel);
    }

    static HifLibraryCard Card(HifPriorityPanel panel, string name) => panel.LibraryCards().First(card => card.Name == name);

    public static void Run(string root)
    {
        var cardPath = HifLayout.PersonalPath("cards_priority.json");
        var before = File.ReadAllBytes(cardPath);
        var defaults = JObject.Parse(File.ReadAllText(cardPath));
        Check(defaults[HifPlaySequenceConfig.ConfigKey]?["集中"]?["国民的アイドル+"]?["drinks"]?["waiting"]?[0]?.ToString() == "初星黒酢",
            "Public default sequence missing");

        // 旧版后续卡与全局组合字段不迁移：忽略并提示，保存时去掉。
        var legacy = (JObject)defaults.DeepClone();
        legacy.Remove(HifPlaySequenceConfig.ConfigKey);
        legacy["followup_profiles"] = new JObject { ["集中"] = new JObject { ["国民的アイドル+"] = new JObject {
            ["targets"] = new JArray("脚光+"), ["wait_turns"] = 3, ["use_black_vinegar"] = true,
            ["wait_after_success"] = false, ["target_policy"] = "reserve_first" } } };
        legacy["combo"] = new JObject { ["first"] = "国民的アイドル+", ["second"] = "脚光+" };
        legacy["skip_limit"] = 4;
        File.WriteAllText(cardPath, legacy.ToString());
        var panel = new HifPriorityPanel("集中"); Loaded(panel);
        Check(panel.Incompatible.Contains("后续卡") && panel.Incompatible.Contains("出牌优先级"), "Old follow-ups or combo fields were not reported");
        Check(Rules(panel, "集中").Count == 0, "Old follow-ups were migrated");
        Check(File.ReadAllText(cardPath) == legacy.ToString(), "Loading changed the configuration");

        // 新建组合：选起始卡后接着点卡加入后续卡；起始卡本身和会形成循环的卡不能加入。
        panel.SetMode(false, true);
        QaHost.Show(panel); Run();
        var rows = (StackPanel)Field(panel, "_rows");
        Check(rows.Children.OfType<Button>().Any(b => b.Content as string == "＋ 新建组合"), "Sequence tab missing 新建组合");
        panel.OpenSequencePick("new"); Run();
        const string start = "アピールの基本+";
        Check(panel.PickCard(Card(panel, start)), "Start card was not accepted"); Run();
        Check(panel.SelectedSequence == start && Rules(panel, "集中")[start] != null, "New sequence was not created");
        Check(!panel.PickCard(Card(panel, start)), "Start card was accepted as its own follow-up");
        Check(panel.PickCard(Card(panel, "脚光+")) && panel.PickCard(Card(panel, "存在感+")), "Follow-ups were not added");
        var rule = (JObject)Rules(panel, "集中")[start]!;
        Check(HifPlaySequenceConfig.Targets(rule).SequenceEqual(new[] { "脚光+", "存在感+" }), "Follow-up order wrong");
        var picker = (HifCardPicker)Field(panel, "_cardPicker");
        Check(picker.Visible.Count > 0, "Card picker listed no cards");
        // 「脚光+ → アピールの基本+」会与现有组合形成循环。
        panel.OpenSequencePick("new"); Run();
        Check(panel.PickCard(Card(panel, "脚光+")), "Second start card was not accepted"); Run();
        Check(!panel.PickCard(Card(panel, start)), "Cycle-forming follow-up was accepted");
        panel.OpenSequencePick(""); Run();
        // 回到第一条编排，设置各位置的饮料。
        panel.SelectSequence(start); Run();
        foreach (var (slot, target, drink) in new (string, string?, string)[] {
                     ("before", null, "特製ハツボシエキス"), ("after", null, "初星湯"), ("before", "脚光+", "ブーストエキス"),
                     ("after", "存在感+", "初星水"), ("waiting", null, "初星黒酢"), ("waiting", null, "初星湯") })
        {
            panel.OpenSequencePick("drink", slot, target); Run();
            Check(panel.ToggleSequenceDrink(start, slot, target, drink), $"Drink {drink} was not added to {slot}/{target}");
        }
        Check(panel.ToggleSequenceDrink(start, "waiting", null, "初星湯"), "Drink toggle did not remove");
        Check(!panel.ToggleSequenceDrink(start, "before", "不在候选中的卡", "初星湯"), "Drink was added to a missing candidate");
        // 组合设置在下方，直接修改编辑数据。
        Run();
        var detail = (StackPanel)Field(panel, "_detail");
        var controls = detail.GetLogicalDescendants().ToArray();
        controls.OfType<NumericUpDown>().Single(c => c.Name == "wait_turns").Value = 5;
        controls.OfType<CheckBox>().Single(c => c.Name == "wait_after_success").IsChecked = true;
        var policy = controls.OfType<HifSegmented>().Single(c => c.Name == "target_policy");
        policy.Items[2].RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Run();
        Check(controls.OfType<Button>().Any(b => b.Content as string == "＋ 添加后续卡"), "Chain missing add-follow-up button");
        panel.OpenSequencePick("drink", "after", null); Run();
        Snapshot(panel, root, "sequence-panel", close: false);
        panel.OpenSequencePick("candidate"); Run();
        Snapshot(panel, root, "sequence-card-picker", close: false);
        // 只有前后用饮的单卡编排；空的编排保存时去掉。
        panel.OpenSequencePick("new"); Run();
        Check(panel.PickCard(Card(panel, "存在感+")), "Single-card sequence start was not accepted"); Run();
        panel.OpenSequencePick("drink", "after", null); Run();
        Check(panel.ToggleSequenceDrink("存在感+", "after", null, "ブーストエキス"), "Single-card drink was not added");
        Check(!panel.ToggleSequenceDrink("存在感+", "waiting", null, "初星黒酢"), "Waiting drink was added without follow-ups");
        panel.OpenSequencePick("new"); Run();
        Check(panel.PickCard(Card(panel, "国民的アイドル+")), "Empty sequence start was not accepted"); Run();
        Invoke(panel, "SaveConfig");
        QaHost.Close(panel);
        var saved = JObject.Parse(File.ReadAllText(cardPath));
        Check(saved["followup_profiles"] == null && saved["combo"] == null && saved["skip_limit"] == null, "Old follow-up fields retained after save");
        var savedRules = (JObject)saved[HifPlaySequenceConfig.ConfigKey]!["集中"]!;
        var expected = JObject.Parse("""
            {
              "targets": ["脚光+", "存在感+"], "wait_turns": 5, "wait_after_success": true, "target_policy": "combo_only",
              "drinks": {
                "before": ["特製ハツボシエキス"], "after": ["初星湯"], "waiting": ["初星黒酢"],
                "targets": { "脚光+": { "before": ["ブーストエキス"], "after": [] }, "存在感+": { "before": [], "after": ["初星水"] } }
              }
            }
            """);
        Check(JToken.DeepEquals(savedRules[start], expected), "Saved sequence differs: " + savedRules[start]);
        Check(savedRules["国民的アイドル+"] == null, "Empty sequence was saved");
        Check(HifPlaySequenceConfig.Targets((JObject)savedRules["存在感+"]!).Count == 0
            && savedRules["存在感+"]!["drinks"]!["after"]![0]!.ToString() == "ブーストエキス", "Single-card sequence not saved");
        Check(saved["recognition_profiles"]!["集中"]!.Values<string>().Contains("存在感+"), "Sequence cards missing from recognition");
        var reopened = new HifPriorityPanel("集中"); Loaded(reopened);
        Check(JToken.DeepEquals(Rules(reopened, "集中"), savedRules), "Sequence save/reopen changed the rules");

        // 无法识别的编排（不在目录中的卡、未知饮料）忽略并提示，面板照常打开。
        foreach (var bad in new JObject[] {
                     new() { ["不存在のカード+"] = expected.DeepClone() },
                     new() { [start] = JObject.Parse(expected.ToString().Replace("初星湯", "存在しないドリンク")) } })
        {
            var unknown = (JObject)saved.DeepClone();
            unknown[HifPlaySequenceConfig.ConfigKey]!["全力"] = bad;
            File.WriteAllText(cardPath, unknown.ToString());
            var tolerant = new HifPriorityPanel("集中"); Loaded(tolerant);
            Check(tolerant.Incompatible.Contains("出牌编排") && Rules(tolerant, "全力").Count == 0 && Rules(tolerant, "集中").Count == 2,
                "Unknown sequence content was not ignored");
        }
        File.WriteAllText(cardPath, saved.ToString());

        // 饮料面板：旧「关联卡牌」忽略并提示；所有饮料可选「只在出牌编排中使用」；标出编排引用和不会被喝的原因。
        var drinkPath = HifLayout.PersonalPath("hif_drink_profiles.json");
        var drinkBefore = File.Exists(drinkPath) ? File.ReadAllBytes(drinkPath) : null;
        var drinkConfig = drinkBefore == null ? new JObject { ["schema_version"] = 1, ["profiles"] = new JObject() } : JObject.Parse(File.ReadAllText(drinkPath));
        drinkConfig["profiles"]!["集中"] = new JArray(
            new JObject { ["id"] = 25, ["before_card_targets"] = new JArray("脚光+") },
            new JObject { ["id"] = 28, ["first_battle_enabled"] = false, ["second_battle_enabled"] = true });
        File.WriteAllText(drinkPath, drinkConfig.ToString());
        var drinks = new HifDrinkPanel("集中"); Loaded(drinks);
        Check(drinks.Incompatible.Contains("饮料"), "Old drink-linked cards were not reported");
        var entries = (List<JObject>)((IDictionary)Field(drinks, "_profiles"))["集中"]!;
        Check(entries.All(entry => entry["before_card_targets"] == null), "Old drink-linked cards were kept");
        Check(drinks.SequenceHint("集中", "初星湯") == "一战关闭，只在二战喝" && drinks.SequenceHint("集中", "初星水") == null,
            "Drink hints wrong: " + drinks.SequenceHint("集中", "初星湯"));
        drinks.SequenceDrinkUses = profession => reopened.SequenceDrinkUses(profession);
        drinks.RefreshRows();
        QaHost.Show(drinks); Run();
        var drinkRows = (StackPanel)Field(drinks, "_rows");
        Check(drinkRows.GetLogicalDescendants().OfType<TextBlock>().Any(t => (string?)t.Tag == "sequence-uses" && t.Text == "用于出牌编排 2 处"),
            "Sequence references not shown on drinks");
        var selectors = drinkRows.GetLogicalDescendants().OfType<ComboBox>().Where(c => c.ItemsSource is string[] items && items.Contains("跟随默认")).ToArray();
        Check(selectors.Length == entries.Count && selectors.All(c => ((string[])c.ItemsSource!).Contains("只在出牌编排中使用")),
            "Not every drink offers 只在出牌编排中使用");
        selectors[1].SelectedIndex = 3;
        Invoke(drinks, "Save");
        var savedDrinks = JObject.Parse(File.ReadAllText(drinkPath));
        Check(savedDrinks["profiles"]!["集中"]![1]!["use_timing"]!["mode"]!.ToString() == "combo_only"
            && savedDrinks["profiles"]!["集中"]!.All(entry => entry["before_card_targets"] == null), "Drink timing or old links not saved correctly");
        QaHost.Close(drinks);
        if (drinkBefore == null) File.Delete(drinkPath); else File.WriteAllBytes(drinkPath, drinkBefore);
        QaHost.Close(reopened);
        File.WriteAllBytes(cardPath, before);

        Check(!MFAAvalonia.Extensions.MaaFW.MaaProcessor.Interface!.Option!.ContainsKey("HIF.国民脚光等待回合"), "Removed task option remains exposed");
        foreach (var profession in new[] { "好调", "集中", "好印象", "元気", "全力", "強気" })
        {
            var window = new HifPriorityPanel(profession); Loaded(window);
            var profiles = (IDictionary)Field(window, "_profiles");
            var sourceCards = (List<JObject>)profiles["集中"]!;
            var cards = (List<JObject>)profiles[profession]!;
            var seed = (JObject)sourceCards[0].DeepClone(); var key = (string)seed["key"]!;
            if (cards.All(c => (string?)c["key"] != key)) cards.Add(seed);
            var conditions = (JObject)((IDictionary)Field(window, "_conditionalPriorities"))[profession]!;
            conditions[key] = new JObject { ["priority"] = 1, ["remaining_turns_gte"] = 3, ["remaining_turns_lte"] = 6, ["mode"] = "any" };
            ((HashSet<string>)((IDictionary)Field(window, "_noExtraTurns"))[profession]!).Add(key);
            // 卡表在左，所选卡牌的规则在右侧详情中就地编辑；出牌编排只显示摘要和跳转按钮。
            window.GetType().GetField("_selectedKey", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, key);
            Invoke(window, "RenderCards");
            QaHost.Show(window);
            var cardRow = ((StackPanel)Field(window, "_rows")).Children.OfType<Border>().Select(b => b.Child).OfType<Grid>()
                .Single(row => row.GetLogicalDescendants().OfType<TextBlock>().Any(t => t.Text == key));
            var tags = cardRow.GetLogicalDescendants().OfType<TextBlock>().Select(t => t.Text ?? "").ToArray();
            Check(tags.Any(t => t.Contains("条件优先级") && t.Contains("使用限制")), profession + " card row missing rule tags");
            var cardDetail = (StackPanel)Field(window, "_detail");
            var summary = string.Join(" | ", cardDetail.Children.OfType<Border>().SelectMany(b => b.GetLogicalDescendants().OfType<TextBlock>()).Select(t => t.Text));
            summary += " | " + string.Join(" | ", cardDetail.Children.OfType<Grid>().Single(g => g.Name == "rules").GetLogicalDescendants().OfType<TextBlock>().Select(t => t.Text));
            Check(summary.Contains("剩余回合 ≥ 3") && summary.Contains("有额外回合时不使用") && summary.Contains("出牌编排"), profession + " detail summaries wrong");
            var requested = new List<string>();
            window.SequenceRequested += requested.Add;
            var sequenceButton = cardDetail.GetLogicalDescendants().OfType<Button>().Single(b => b.Content as string is "编辑出牌编排" or "为这张卡新建出牌编排");
            // 两张规则卡片同时显示，各自带编辑内容。
            var ruleCards = cardDetail.Children.OfType<Grid>().Single(g => g.Name == "rules").Children.OfType<Border>().ToArray();
            Check(ruleCards.Select(c => (string?)c.Tag).SequenceEqual(new[] { "条件优先级", "使用限制" }), profession + " rule cards missing");
            foreach (var ruleCard in ruleCards)
                Check(ruleCard.GetLogicalDescendants().OfType<Button>().Any(b => (string?)b.Content == "应用到本次编辑"), profession + " inline editor missing: " + ruleCard.Tag);
            sequenceButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs();
            Check(requested.SequenceEqual(new[] { key }) && window.SelectedSequence == key && Rules(window, profession)[key] != null,
                profession + " detail did not open the card's sequence");
            window.SetMode(false, true);
            Check(((StackPanel)Field(window, "_detail")).GetLogicalDescendants().OfType<Button>().Any(b => b.Content as string == "更换起始卡"),
                profession + " sequence detail missing");
            window.SetMode(false);
            Invoke(window, "RenderCards");
            window.GetType().GetMethod("ShowConditionDialog", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { key, 10m });
            Dispatcher.UIThread.RunJobs();
            var dialog = QaHost.Window(window).OwnedWindows.Single();
            var dialogControls = ((Control)dialog.Content!).GetLogicalDescendants().ToArray();
            Check(dialogControls.OfType<CheckBox>().Any(c => (string?)c.Content == "全力值 <") == (profession == "全力"), "Non-full-power profession has pointer/value restriction");
            var lowerEnabled = dialogControls.OfType<CheckBox>().Single(c => (string?)c.Content == "剩余回合 ≥");
            var lower = ((StackPanel)lowerEnabled.Parent!).Children.OfType<NumericUpDown>().Single();
            var applyButton = dialogControls.OfType<Button>().Single(b => (string?)b.Content == "应用到本次编辑");
            Snapshot(dialog, root, "range-condition-" + profession, false);
            lower.Value = 7; applyButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(dialogControls.OfType<TextBlock>().Any(t => t.Text == "回合下限不能大于上限"), "Reversed interval was accepted");
            lower.Value = 3; applyButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(!((StackPanel)Field(window, "_rows")).GetLogicalDescendants().OfType<CheckBox>().Any(c => (string?)c.Content == "有额外回合时不使用"), "Extra-turn option remains in main card list");
            var showLimits = window.GetType().GetMethod("ShowUseConditionDialog", BindingFlags.Instance | BindingFlags.NonPublic)!;
            showLimits.Invoke(window, new object[] { key }); Dispatcher.UIThread.RunJobs();
            var limitDialog = QaHost.Window(window).OwnedWindows.Single();
            var limitControls = ((Control)limitDialog.Content!).GetLogicalDescendants().ToArray();
            var noExtra = limitControls.OfType<CheckBox>().Single(c => (string?)c.Content == "有额外回合时不使用");
            Check(noExtra.IsChecked == true, "Extra-turn restriction not loaded in use-conditions dialog");
            noExtra.IsChecked = false;
            limitControls.OfType<Button>().Single(b => (string?)b.Content == "取消").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(((HashSet<string>)((IDictionary)Field(window, "_noExtraTurns"))[profession]!).Contains(key), "Canceled restriction edit was applied");
            showLimits.Invoke(window, new object[] { key }); Dispatcher.UIThread.RunJobs();
            limitDialog = QaHost.Window(window).OwnedWindows.Single();
            Snapshot(limitDialog, root, "range-use-limits-" + profession, false);
            ((Control)limitDialog.Content!).GetLogicalDescendants().OfType<Button>().Single(b => (string?)b.Content == "应用到本次编辑").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Invoke(window, "SaveConfig");
            var again = new HifPriorityPanel(profession); Loaded(again);
            var restoredRules = (JObject)((IDictionary)Field(again, "_conditionalPriorities"))[profession]!;
            Check((int?)restoredRules[key]?["remaining_turns_gte"] == 3 && (int?)restoredRules[key]?["remaining_turns_lte"] == 6, "Turn interval lost on save/reopen");
            Check(((HashSet<string>)((IDictionary)Field(again, "_noExtraTurns"))[profession]!).Contains(key), "Extra turn exclusion lost on save/reopen");
            Snapshot(again, root, "range-priority-" + profession);
            QaHost.Close(window);
        }
        File.WriteAllBytes(cardPath, before);
        Console.WriteLine("Play sequences passed: old follow-ups ignored, start/follow-up picking with cycle guard, drink slots, combo settings, save/reopen, unknown content ignored, drink panel links and hints, profession rule editors.");
    }
}
