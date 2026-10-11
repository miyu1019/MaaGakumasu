import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_hif_behavior import PRODUCE, ROOT


class DrinkLimitsTest(unittest.TestCase):
    def load(self, entries, policy=None):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.profession = "集中"
        config = {"profiles": {"集中": entries}}
        if policy is not None:
            config["first_battle_policy_profiles"] = {"集中": policy}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory) / "drinks.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(PRODUCE, "HIF_DRINK_PROFILES_PATH", str(path)):
                action._load_hif_battle_drink_policy()
        action.hif_battle_number = 1
        return action

    def consume(self, action, inventory, wanted, fail_use=False, **kwargs):
        selected = [0]
        action._battle_drink_boxes = lambda _: [[i * 40, 0, 20, 20] for i in range(len(inventory))]
        def open_detail(_, position, label):
            selected[0] = position[0] // 40
            return True
        action._open_drink_detail = open_detail
        action._read_battle_drink_identity = lambda _: (inventory[selected[0]], "")
        action._use_drink = lambda _: False if fail_use else inventory.pop(selected[0]) is not None
        action._close_drink_detail = lambda _: True
        action._wait_for_battle_drink_return = lambda _: True
        action._wait_for_drink_list_stable = lambda *args, **kw: True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            return action._consume_battle_drinks(
                SimpleNamespace(tasker=SimpleNamespace(stopping=False)), wanted, "test", **kwargs
            )

    def test_single_kind_limit_persists_across_timing_and_combo_batches(self):
        action = self.load([{"id": 28, "first_battle_max_uses": 1}])
        inventory = ["初星湯", "初星湯", "初星水"]
        self.assertTrue(self.consume(action, inventory, ["初星湯"]))
        self.assertEqual(inventory, ["初星湯", "初星水"])
        self.assertFalse(self.consume(action, inventory, ["初星湯"], max_uses=1, verify_post_use=False))
        action.hif_battle_number = 2
        self.assertTrue(self.consume(action, inventory, ["初星湯"]))
        self.assertEqual(inventory, ["初星水"])

    def test_total_limit_sums_different_kinds_across_batches(self):
        action = self.load([], {"max_uses": 2})
        inventory = ["初星湯", "初星水", "初星湯"]
        self.assertTrue(self.consume(action, inventory, ["初星湯"], max_uses=1, verify_post_use=False))
        self.assertTrue(self.consume(action, inventory, ["初星水", "初星湯"]))
        self.assertEqual(inventory, ["初星湯"])
        self.assertEqual(action._drink_used_counts, {"初星湯": 1, "初星水": 1})
        self.assertFalse(self.consume(action, inventory, ["初星湯"]))
        action.hif_battle_number = 2
        self.assertTrue(self.consume(action, inventory, ["初星湯"]))

    def test_unconfigured_limit_is_per_kind_and_does_not_override_imported(self):
        action = self.load([{"id": 28}], {"unconfigured_max_uses": 1})
        inventory = ["初星湯", "初星湯", "初星水", "初星水"]
        self.consume(action, inventory, ["初星湯", "初星水"])
        self.assertEqual(inventory, ["初星水"])
        self.assertEqual(action._drink_used_counts, {"初星湯": 2, "初星水": 1})

    def test_individual_switches_and_unconfigured_switch(self):
        action = self.load([
            {"id": 28, "first_battle_enabled": False, "second_battle_enabled": True},
            {"id": 14, "first_battle_enabled": True, "second_battle_enabled": False},
        ], {"enabled": False})
        inventory = ["初星湯", "初星水", "センブリソーダ"]
        self.consume(action, inventory, list(inventory))
        self.assertEqual(inventory, ["初星湯", "初星水"])
        action.hif_battle_number = 2
        inventory.append("センブリソーダ")
        self.consume(action, inventory, list(inventory))
        self.assertEqual(inventory, ["センブリソーダ"])

    def test_legacy_disabled_and_default_both_enabled(self):
        action = self.load([{"id": 28}, {"id": 14, "disabled": True}])
        for battle in (1, 2):
            action.hif_battle_number = battle
            inventory = ["初星湯", "初星湯", "初星水", "センブリソーダ"]
            self.consume(action, inventory, list(inventory))
            self.assertEqual(inventory, ["センブリソーダ"])

    def test_zero_and_unknown_battle_do_not_bypass_limits(self):
        for policy in ({"max_uses": 0}, {"unconfigured_max_uses": 0}):
            action = self.load([], policy)
            action.hif_battle_number = None
            self.assertFalse(self.consume(action, ["初星湯"], ["初星湯"]))
        action = self.load([{"id": 28, "first_battle_max_uses": 0}])
        self.assertFalse(self.consume(action, ["初星湯"], ["初星湯"]))

    def test_failed_use_does_not_count(self):
        action = self.load([])
        with self.assertRaises(PRODUCE.HifDrinkFlowError):
            self.consume(action, ["初星湯"], ["初星湯"], fail_use=True)
        self.assertEqual(action._drink_used_counts, {})

    def test_new_battle_resets_shared_counts(self):
        action = self.load([])
        context = SimpleNamespace(get_node_data=lambda _: {}, tasker=SimpleNamespace(
            stopping=True, controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())))))
        with patch.object(action, "_load_config"), patch.object(action, "_load_hif_battle_drink_policy"), \
             patch.object(action, "_load_hif_followups"), patch.object(action, "_wait_until_playable"), \
             patch.object(action, "_detect_battle_number", return_value=1), \
             patch.object(PRODUCE.time, "sleep", return_value=None):
            action._drink_used_counts = {"初星湯": 2, "初星水": 1}
            self.assertTrue(action.run(context, SimpleNamespace(node_name="ProduceHIF__ProduceHIFCardsFlag")))
        self.assertEqual(action._drink_used_counts, {})

    def test_invalid_limits_fail_closed(self):
        for value in (True, 1.5, -2, 5, 100, "1", None):
            with self.subTest(value=value):
                for action in (self.load([{"id": 28, "first_battle_max_uses": value}]),
                               self.load([], {"max_uses": value})):
                    self.assertEqual(action._drink_default_timing, {"mode": "never"})
                    self.assertFalse(action._drink_first_battle_policy["enabled"])
                    action.hif_battle_number = 2
                    self.assertFalse(self.consume(action, [action.BROWN_TARGET_NAME], [action.BROWN_TARGET_NAME]))

    def test_acquisition_and_purchase_follow_new_switches(self):
        entries = [
            {"id": 28, "disabled": True, "first_battle_enabled": False,
             "second_battle_enabled": True, "purchase_enabled": True},
            {"id": 14, "disabled": False, "first_battle_enabled": False,
             "second_battle_enabled": False, "purchase_enabled": True},
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory) / "drinks.json"
            path.write_text(json.dumps({"profiles": {"集中": entries}}), encoding="utf-8")
            context = SimpleNamespace(get_node_data=lambda _: {})
            with patch.object(PRODUCE, "HIF_DRINK_PROFILES_PATH", str(path)):
                self.assertEqual(PRODUCE._hif_drink_priority_names(context), ["初星湯"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, purchase_only=True), ["初星湯"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, disabled_only=True), ["センブリソーダ"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, purchase_only=True, include_disabled=True),
                                 ["初星湯", "センブリソーダ"])


if __name__ == "__main__":
    unittest.main()
