import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from test_hif_behavior import PRODUCE, ROOT


class UnconfiguredPreferenceTests(unittest.TestCase):
    def action(self, profession="集中"):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action._load_config(profession)
        action.priority_index = {}
        action.conditional_priority_rules = {}
        action._hif_card_types = {"M": "mental", "A": "active"}
        action._last_turn_count = 5
        return action

    def test_modes_and_phase_boundary_in_both_decision_paths(self):
        hand = [{"key": "M", "box": [1, 900, 100, 100], "conf": 0.1},
                {"key": "A", "box": [2, 900, 100, 100], "conf": 0.9}]
        for profession in ("集中", "全力"):
            action = self.action(profession)
            for mode, turns, expected in (("none", 5, "A"), ("mental", 5, "M"), ("active", 5, "A"),
                                          ("remaining_turns", 4, "M"), ("remaining_turns", 3, "A"),
                                          ("remaining_turns", None, "A")):
                with self.subTest(profession=profession, mode=mode, turns=turns):
                    action.unconfigured_card_preference = {"mode": mode}
                    action._last_turn_count = turns
                    self.assertEqual(action._decide_legacy(None, hand)["key"], expected)
            action.unconfigured_card_preference = {"mode": "remaining_turns", "early": "active", "late": "mental"}
            action._last_turn_count = 2
            self.assertEqual(action._decide_legacy(None, hand)["key"], "M")

    def test_explicit_priorities_and_use_conditions_are_preserved(self):
        action = self.action("全力")
        action.unconfigured_card_preference = {"mode": "mental", "prefer_upgraded": True}
        hand = [{"key": "M", "box": [1, 900, 100, 100], "conf": 0.1, "upgraded": True},
                {"key": "A", "box": [2, 900, 100, 100], "conf": 0.9}]
        action.priority_index = {"A": 1}
        self.assertEqual(action._decide_full_power(hand)["key"], "A")
        action.priority_index = {"A": action.unknown_priority}
        self.assertEqual(action._decide_full_power(hand)["key"], "A")
        action.priority_index = {}
        action.use_conditions = {"M": [{"remaining_turns_lte": 3}]}
        self.assertEqual(action._decide_full_power(hand)["key"], "A")

    def test_upgrade_uses_actual_title_and_not_canonical_key(self):
        action = self.action()
        action._hif_card_types = {"M": "mental", "A": "active"}
        for text, complete, expected in (("M+", False, True), ("M++", False, True), ("M ＋", False, True),
                                         ("M", True, False), ("M", False, None), ("", True, None)):
            self.assertIs(action._hif_title_upgraded(text, complete=complete), expected)
        hand = [{"key": "M+", "box": [1, 900, 100, 100], "conf": 0.9, "upgraded": False},
                {"key": "M+", "box": [2, 900, 100, 100], "conf": 0.1, "upgraded": True},
                {"key": "A+", "box": [3, 900, 100, 100], "conf": 0.8, "upgraded": True}]
        action.unconfigured_card_preference = {"mode": "mental", "prefer_upgraded": True}
        self.assertEqual(action._decide_legacy(None, hand)["box"], hand[1]["box"])
        action.unconfigured_card_preference = {"mode": "mental"}
        self.assertEqual(action._decide_legacy(None, hand)["box"], hand[0]["box"])
        action.unconfigured_card_preference = {"prefer_upgraded": True}
        self.assertEqual(action._decide_legacy(None, hand)["box"], hand[2]["box"])
        action.priority_index = {"M+": 1}
        self.assertEqual(action._decide_legacy(None, hand)["box"], hand[1]["box"])
        # 组合已选定卡名后，同名选择也应使用实际强化标记。
        with patch.object(action, "_decide_card", return_value={"type": "play", "key": "M+", "box": hand[0]["box"],
                                                               "followup_role": "target"}):
            selected = action._decide(None, hand)
            self.assertEqual(selected["box"], hand[1]["box"])
            self.assertEqual(selected["followup_role"], "target")

    def test_old_invalid_and_profession_scoped_configuration(self):
        config = json.loads((ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8"))
        config["unconfigured_card_preference_profiles"] = {"集中": {"mode": "mental", "prefer_upgraded": True},
                                                           "全力": {"mode": "invalid"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cards_priority.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                action = self.action()
                self.assertEqual(action.unconfigured_card_preference["mode"], "mental")
                action._load_config("全力")
                self.assertEqual(action.unconfigured_card_preference, {})
                action._load_config("好调")
                self.assertEqual(action.unconfigured_card_preference, {})
                self.assertIn("mental", action._hif_card_types.values())
                self.assertIn("active", action._hif_card_types.values())

    def test_ocr_preserves_actual_plus_and_hand_cache_resets(self):
        action = self.action()
        action._hif_name_index = {"M": ["M+"]}
        box = [10, 900, 150, 230]
        for text, expected in (("M+", True), ("M", False), ("M＋＋", True)):
            result = SimpleNamespace(text=text, box=[0, 0, 80, 20])
            context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(hit=True, filtered_results=[result]))
            with patch.object(action, "_hif_full_name_visible", return_value=True):
                self.assertEqual(action._identify_by_name(context, object(), box), "M+")
            self.assertIs(action._hif_last_upgraded, expected)
        action._hif_bind_hand([(box, 0.9)])
        hand = [{"key": "M+", "box": box, "conf": 0.9, "upgraded": True}]
        action._confirm_card_hand(hand)
        action._confirm_card_hand(hand)
        self.assertTrue(action._hif_detail_upgraded[0])
        action._reset_hif_recognition()
        self.assertEqual(action._hif_detail_upgraded, {})


if __name__ == "__main__":
    unittest.main()
