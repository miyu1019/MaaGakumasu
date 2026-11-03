import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from tests.test_hif_behavior import PRODUCE, COMMON, DECISION, hif_patch

CHOOSE = "ProduceHIF__ProduceRecognitionChooseMoveCards"
MOVE = "ProduceHIF__ProduceMoveCards"


class HoldPageTest(unittest.TestCase):
    def _action(self, names, profession="全力", keys=(), ordered=False, low=True, rows_per_scroll=(147,)):
        """names 按点击坐标与当前偏移给卡名；rows_per_scroll 为每次下滑的实测位移，用尽后视为到底。"""
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.profession = profession
        action.hold_priority_keys = list(keys)
        action.hold_priority_ordered = ordered
        clicks, tasks, swipes = [], [], []
        tasker = SimpleNamespace(stopping=False)
        tasker.controller = SimpleNamespace(
            post_click=lambda x, y: clicks.append((x, y)) or SimpleNamespace(wait=lambda: None))
        context = SimpleNamespace(
            tasker=tasker,
            run_recognition=lambda name, *_a, **_k: SimpleNamespace(hit=name == CHOOSE),
            run_task=tasks.append,
        )
        action._move_has_panel = lambda _image: low
        downs = iter(rows_per_scroll)

        def scroll(_context, direction):
            delta = next(downs, 0) if direction > 0 else -action._move_offset
            swipes.append(direction)
            action._move_offset += delta
            return delta

        action._move_scroll = scroll
        # 卡名按内容位置（点击 y + 当前偏移）给出，模拟滚动后同一张卡换了屏幕坐标。
        action._read_move_title_stable = lambda _context: names.get(
            (clicks[-1][0], clicks[-1][1] + action._move_offset), "其他卡+")
        action._wait_for_move_enabled = lambda _context: True
        return action, context, clicks, tasks, swipes

    def _run(self, action, context):
        with patch.object(COMMON.time, "sleep", return_value=None):
            return action._handle_move_cards(context, object())

    def test_unordered_hold_keeps_first_found_and_stops_scanning(self):
        names = {(286, 569): "わたしは、風！+", (433, 569): "羽ばたけ！+"}
        action, context, clicks, tasks, swipes = self._action(names, keys=["羽ばたけ！+", "わたしは、風！+"])
        action._hold_source = "モチベ+"
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks, [(139, 569), (286, 569), (286, 569)])
        self.assertEqual(swipes, [])
        self.assertEqual(tasks, [MOVE])
        self.assertIsNone(action._hold_source)

    def test_ordered_hold_scrolls_whole_deck_and_returns_to_best_card(self):
        names = {(286, 569): "わたしは、風！+", (433, 569 + 4 * 147): "羽ばたけ！+"}
        action, context, clicks, tasks, swipes = self._action(
            names, keys=["羽ばたけ！", "わたしは、風！"], ordered=True)
        self.assertTrue(self._run(action, context))
        # 首屏四行，下滑一行后只扫新露出的第五行，找到第一顺位即停，直接点选（已在可点范围）。
        self.assertEqual(len(clicks), 16 + 3 + 1)
        self.assertEqual(clicks[-1], (433, 569 + 4 * 147 - 147))
        self.assertEqual(swipes, [1])
        self.assertEqual(tasks, [MOVE])

    def test_ordered_hold_without_top_target_scrolls_back_for_lower_rank(self):
        names = {(286, 569): "わたしは、風！+"}
        action, context, clicks, tasks, swipes = self._action(
            names, keys=["羽ばたけ！", "わたしは、風！"], ordered=True, rows_per_scroll=(147, 147))
        self.assertTrue(self._run(action, context))
        self.assertEqual(swipes, [1, 1, 1, -1])
        self.assertEqual(clicks[-1], (286, 569))
        self.assertEqual(tasks, [MOVE])

    def test_empty_hold_list_selects_first_card_without_scanning(self):
        action, context, clicks, tasks, swipes = self._action({}, profession="強気")
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks, [(139, 569)])
        self.assertEqual((tasks, swipes), ([MOVE], []))

    def test_old_layout_scans_visible_grid_without_scrolling(self):
        names = {(580, 604): "羽ばたけ！+"}
        action, context, clicks, tasks, swipes = self._action(names, keys=["羽ばたけ！"], low=False)
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks, action.MOVE_GRID[:8] + [(580, 604)])
        self.assertEqual((tasks, swipes), ([MOVE], []))

    def test_honryou_selects_two_with_second_tap_and_cancels_when_none_found(self):
        names = {(139, 569): "羽ばたけ！+", (433, 716): "わたしは、風！+"}
        action, context, clicks, tasks, _ = self._action(names, keys=["羽ばたけ！", "わたしは、風！"])
        action._hold_source = action.HOLD_MULTI_SOURCE
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks[-4:], [(139, 569), (139, 569), (433, 716), (433, 716)])
        self.assertEqual(tasks, [MOVE])

        action, context, clicks, tasks, _ = self._action({}, keys=["羽ばたけ！"], rows_per_scroll=())
        action._hold_source = action.HOLD_MULTI_SOURCE
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks[-1], action.HOLD_CANCEL_POS)
        self.assertEqual(tasks, [])

        action, context, clicks, tasks, _ = self._action({})
        action._hold_source = action.HOLD_MULTI_SOURCE
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks, [(139, 569), (139, 569)])
        self.assertEqual(tasks, [MOVE])

    def test_green_tea_uses_hold_list_in_any_profession_and_vinegar_keeps_spotlight_search(self):
        names = {(286, 569): "羽ばたけ！+", (433, 569): "脚光+"}
        action, context, clicks, tasks, _ = self._action(names, profession="集中", keys=["羽ばたけ！"])
        action._move_drink_source = action.HOLD_GREEN_TEA
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks[-1], (286, 569))
        self.assertIsNone(action._move_drink_source)

        action, context, clicks, tasks, _ = self._action(names, profession="全力", keys=["羽ばたけ！"])
        action._move_drink_source = action.BROWN_TARGET_NAME
        self.assertTrue(self._run(action, context))
        self.assertEqual(clicks[-1], (433, 569))

    def test_followup_search_scrolls_to_candidate(self):
        names = {(580, 569 + 5 * 147): "国民的アイドル+"}
        action, context, clicks, tasks, swipes = self._action(names, rows_per_scroll=(147, 147))
        action._followup_move_targets = ["国民的アイドル+"]
        self.assertTrue(self._run(action, context))
        self.assertEqual(swipes, [1, 1])
        self.assertEqual(clicks[-1], (580, 569 + 5 * 147 - 2 * 147))
        self.assertEqual(tasks, [MOVE])

    def test_mismatched_title_after_reselect_does_not_move(self):
        names = {(286, 569): "羽ばたけ！+"}
        action, context, clicks, tasks, _ = self._action(names, keys=["羽ばたけ！"])
        reads = iter(["其他卡", "羽ばたけ！+", "其他卡"])
        action._read_move_title_stable = lambda _context: next(reads)
        self.assertFalse(self._run(action, context))
        self.assertEqual(tasks, [])

    def test_panel_probe_distinguishes_layouts(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        panel = np.full((1280, 720, 3), 232, dtype=np.uint8)
        self.assertTrue(action._move_has_panel(panel))
        cards = panel.copy()
        cards[400:520, 80:200] = 30
        self.assertFalse(action._move_has_panel(cards))

    def test_hold_list_shift_is_measurable(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto
        rng = np.random.default_rng(5)
        canvas = np.full((1280 + 147 * 6, 720, 3), 40, dtype=np.uint8)
        for row in range(8):
            for x in action.MOVE_COLUMN_X:
                face = cv2.resize(rng.integers(0, 255, (6, 6, 3), dtype=np.uint8), (116, 116),
                                  interpolation=cv2.INTER_NEAREST)
                top = action.MOVE_ROW0_Y + row * action.MOVE_ROW_PITCH - 58
                canvas[top:top + 116, x - 58:x + 58] = face

        def frame(offset):
            image = np.full((1280, 720, 3), 40, dtype=np.uint8)
            _, y0, _, y1 = action.MOVE_LIST_VIEW
            image[y0:y1] = canvas[y0 + offset:y1 + offset]
            return image

        start, end = action.MOVE_SWIPE_SPAN
        x0, y0, x1, y1 = action.MOVE_LIST_VIEW
        self.assertLessEqual(2 * (start - end), (y1 - y0) - 120 - 10)
        with hif_patch('cv2', cv2), hif_patch('np', np):
            for delta in (120, 147, 200):
                self.assertEqual(COMMON._hif_list_scroll_shift(frame(0), frame(delta), 1, action.MOVE_LIST_VIEW), delta)

    def test_hold_priority_config_parsing(self):
        parse = DECISION.BattleDecisionMixin._parse_hold_priority
        cfg = {"hold_priority_profiles": {"全力": {"ordered": True, "cards": ["羽ばたけ！+", "羽ばたけ！+", "", 3]},
                                          "集中": {"ordered": True, "cards": ["脚光+"]}}}
        self.assertEqual(parse(cfg, "全力"), (["羽ばたけ！+"], True))
        self.assertEqual(parse(cfg, "集中"), ([], False))
        self.assertEqual(parse({}, "強気"), ([], False))
        self.assertEqual(parse({"hold_priority_profiles": {"強気": {"ordered": "yes", "cards": []}}}, "強気"), ([], False))


if __name__ == "__main__":
    unittest.main()
