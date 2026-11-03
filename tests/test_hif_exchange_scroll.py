import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from tests.test_hif_behavior import PRODUCE, _Context, COMMON, hif_patch


def _face(rng):
    """卡面用放大的色块，保留类似插画的大块结构（逐像素噪声模糊后会变平）。"""
    return cv2.resize(rng.integers(0, 255, (6, 6, 3), dtype=np.uint8), (116, 116), interpolation=cv2.INTER_NEAREST)


def _deck_image(offset, rows=5, seed=7):
    """按实机交换页几何绘制卡组：行距146、卡高116；中间两行的中间两列用同一卡面模拟重复卡。"""
    rng = np.random.default_rng(seed)
    repeated = _face(rng)
    exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
    canvas = np.full((1280 + 146 * rows, 720, 3), 40, dtype=np.uint8)
    for row in range(rows):
        for column, x in enumerate(exchange.COLUMN_X):
            face = repeated if row in (1, 2) and column in (1, 2) else _face(rng)
            top = exchange.ROW_CENTER_Y0 + row * exchange.ROW_PITCH - 58
            canvas[top:top + 116, x - 58:x + 58] = face
    image = np.full((1280, 720, 3), 40, dtype=np.uint8)
    _, y0, _, y1 = exchange.LIST_VIEW
    image[y0:y1] = canvas[y0 + offset:y1 + offset]
    return image


def _similar_deck_image(offset, rows=6, seed=3):
    """卡底费用、M 标和边框各卡相同，末行只有一张卡（2026-11-02 实机卡组：21张，滚到底偏移≈395）。"""
    rng = np.random.default_rng(seed)
    exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
    bottom = cv2.resize(rng.integers(0, 255, (3, 6, 3), dtype=np.uint8), (116, 80), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((1280 + 146 * rows, 720, 3), 40, dtype=np.uint8)
    for row in range(rows):
        for column, x in enumerate(exchange.COLUMN_X):
            if row == rows - 1 and column:
                continue
            face = _face(rng)
            face[36:] = bottom
            top = exchange.ROW_CENTER_Y0 + row * exchange.ROW_PITCH - 58
            canvas[top:top + 116, x - 58:x + 58] = face
    image = np.full((1280, 720, 3), 40, dtype=np.uint8)
    _, y0, _, y1 = exchange.LIST_VIEW
    image[y0:y1] = canvas[y0 + offset:y1 + offset]
    return image


class ExchangeScrollTest(unittest.TestCase):
    def _action(self, names_by_pos, offsets, stopping_after=None):
        """names_by_pos 按点击坐标给卡名；offsets 为每次下滑的实测位移，用尽后视为到底。"""
        clicks, swipes = [], []
        tasker = SimpleNamespace(stopping=False)

        def post_click(x, y):
            clicks.append((x, y))
            if stopping_after is not None and len(clicks) >= stopping_after:
                tasker.stopping = True
            return SimpleNamespace(wait=lambda: None)

        tasker.controller = SimpleNamespace(post_click=post_click)
        context = _Context({})
        context.tasker = tasker
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action._swap_out_options = lambda _context: {}
        action._customized_card = lambda _image: False
        action._wait_stable_name = lambda _context: (object(), names_by_pos.get(clicks[-1], "其他卡+"))
        downs = iter(offsets)

        def scroll(_context, direction):
            delta = next(downs, 0) if direction > 0 else -action._list_offset
            swipes.append(direction)
            action._list_offset += delta
            return delta

        action._scroll_list = scroll
        return action, context, clicks, swipes

    def test_scroll_shift_matches_measured_rows_including_repeated_cards(self):
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
        with hif_patch('cv2', cv2), hif_patch('np', np):
            for start, delta in ((0, 103), (0, 146), (0, 292), (60, 200)):
                with self.subTest(start=start, delta=delta):
                    before, after = _deck_image(start), _deck_image(start + delta)
                    self.assertEqual(exchange._scroll_shift(before, after, 1), delta)
                    self.assertEqual(exchange._scroll_shift(after, before, -1), -delta)
            still = _deck_image(103)
            self.assertEqual(exchange._scroll_shift(still, still.copy(), 1), 0)
            blank = np.full((1280, 720, 3), 40, dtype=np.uint8)
            self.assertIsNone(exchange._scroll_shift(blank, blank.copy(), 1))

    def test_scroll_beyond_measurable_range_is_not_misread_as_one_row_less(self):
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
        with hif_patch('cv2', cv2), hif_patch('np', np):
            for delta in (146, 250, 300):
                with self.subTest(delta=delta):
                    self.assertEqual(exchange._scroll_shift(_similar_deck_image(0), _similar_deck_image(delta), 1), delta)
            # 旧实现把实际395测成249：漏扫一整行，回点落到モチベ+。
            for delta in (360, 395):
                with self.subTest(delta=delta):
                    self.assertIsNone(exchange._scroll_shift(_similar_deck_image(0), _similar_deck_image(delta), 1))

    def test_swipe_stays_within_measurable_shift(self):
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
        start, end = exchange.SWIPE_SPAN
        x0, y0, x1, y1 = exchange.LIST_VIEW
        measurable = (y1 - y0) - exchange.SHIFT_TEMPLATE_HEIGHT - 10
        self.assertLessEqual(2 * (start - end), measurable)  # 连同惯性也留在可测范围内

    def test_empty_cell_detects_background_only(self):
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto
        with hif_patch('cv2', cv2), hif_patch('np', np):
            image = _similar_deck_image(395)
            self.assertFalse(exchange._empty_cell(image, (139, 1032)))
            self.assertTrue(exchange._empty_cell(image, (285, 1032)))
            self.assertFalse(exchange._empty_cell(object(), (285, 1032)))

    def test_empty_slots_in_last_row_are_not_recorded_with_stale_name(self):
        names = {(139, 1032): "眠気"}
        action, context, clicks, _swipes = self._action(names, [103])
        empties = {(285, 1032), (432, 1032), (580, 1032)}
        action._replacement_targets = lambda _context: ["眠気", action.TARGET]
        # 空位不换选中卡，详情仍是上一张；末张眠気是定制版(版本1)，空位若被记录会以版本0胜出。
        action._wait_stable_name = lambda _context: (
            clicks[-1], "眠気" if clicks[-1] in empties else names.get(clicks[-1], "其他卡+"))
        action._customized_card = lambda image: image == (139, 1032)
        action._empty_cell = lambda _image, pos: pos in empties
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto, '_delete_card_exchange_count', 0), \
                patch.object(COMMON.time, 'sleep'), \
                patch.object(action, '_confirm_exchange', return_value=True) as confirm:
            self.assertTrue(action.run(context, None))
        confirm.assert_called_once()
        self.assertFalse(empties & set(clicks))  # 空位点前即跳过，不点开
        self.assertEqual(clicks[-1], (139, 1032))

    def test_delete_page_skips_empty_cells(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        image = np.full((1280, 720, 3), 30, dtype=np.uint8)
        rng = np.random.default_rng(0)
        for x, y in action.CARD_GRID[:7]:
            image[y - 50:y + 50, x - 50:x + 50] = rng.integers(0, 255, (100, 100, 3), dtype=np.uint8)
        with hif_patch('cv2', cv2), hif_patch('np', np):
            self.assertEqual(action._selectable_slots(image), list(range(7)))
            self.assertEqual(action._selectable_slots(object()), list(range(len(action.CARD_GRID))))

    def test_unconfirmed_scroll_returns_to_top_before_clicking_by_position(self):
        for still_after, expected in ((2, 0), (None, None)):
            with self.subTest(still_after=still_after):
                action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                context = _Context({})
                context.tasker = SimpleNamespace(stopping=False)
                action._list_offset = 146
                frame = np.zeros((1280, 720, 3), dtype=np.uint8)
                swipes, checks = [], []
                action._list_image = lambda _context: frame
                action._settled_list_image = lambda _context: frame
                action._swipe_list = lambda _context, direction: swipes.append(direction) or True
                action._scroll_shift = lambda *_args: None
                action._save_scroll_images = lambda *_args: None

                def still(*_args):
                    checks.append(1)
                    return still_after is not None and len(checks) >= still_after

                action._list_still = still
                self.assertIsNone(action._scroll_list(context, 1))
                self.assertEqual(action._list_offset, expected)
                self.assertEqual(swipes[0], 1)
                self.assertTrue(all(direction == -1 for direction in swipes[1:]))
                if expected is None:
                    self.assertIsNone(action._ensure_visible(context, (0, (139, 990))))
                else:
                    self.assertEqual(action._ensure_visible(context, (0, (139, 990))), (139, 990))

    def test_visible_rows_follow_measured_offset(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action._list_offset = 0
        self.assertEqual(action._visible_rows(), [(0, 697), (1, 843), (2, 989)])
        # 实机截图：列表滚到底偏移103，第一行只露出底边，不点。
        action._list_offset = 103
        self.assertEqual(action._visible_rows(), [(1, 740), (2, 886), (3, 1032)])

    def test_basic_card_below_first_screen_is_found_after_scrolling(self):
        action, context, clicks, swipes = self._action({(285, 1032): "魅せ方の基本+"}, [103])
        action._replacement_targets = lambda _context: [action.TARGET]
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto, '_delete_card_exchange_count', 0), \
                patch.object(COMMON.time, 'sleep'), \
                patch.object(action, '_confirm_exchange', return_value=True) as confirm:
            self.assertTrue(action.run(context, None))
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual(clicks[:12], action.CARD_GRID)
        self.assertEqual(clicks[12:], [(139, 1032), (285, 1032)])  # 只点新露出的第4行
        self.assertEqual(swipes, [1])

    def test_first_card_fallback_scrolls_back_to_top_and_rechecks_name(self):
        for reselected, expected in (("夏夜に咲く思い出+", True), ("スポットライト+", False)):
            with self.subTest(reselected=reselected):
                names = {(139, 697): "夏夜に咲く思い出+"}
                action, context, clicks, swipes = self._action(names, [103])
                action._replacement_targets = lambda _context: [action.TARGET]
                original = action._wait_stable_name
                action._wait_stable_name = lambda _context: (
                    (object(), reselected) if len(clicks) > 16 else original(_context))
                with patch.object(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto, '_delete_card_exchange_count', 0), \
                        patch.object(COMMON.time, 'sleep'), \
                        patch.object(action, '_confirm_exchange', return_value=True) as confirm:
                    self.assertEqual(action.run(context, None), expected)
                self.assertEqual(confirm.call_count, int(expected))
                self.assertEqual(swipes, [1, 1, -1])  # 扫到底后先回到顶部再点第一格
                self.assertEqual(clicks[-1], (139, 697))

    def test_stop_request_leaves_exchange_page_untouched(self):
        action, context, clicks, swipes = self._action({}, [103], stopping_after=3)
        action._replacement_targets = lambda _context: [action.TARGET]
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto, '_delete_card_exchange_count', 0), \
                patch.object(COMMON.time, 'sleep'), \
                patch.object(action, '_confirm_exchange', return_value=True) as confirm:
            self.assertFalse(action.run(context, None))
        confirm.assert_not_called()
        self.assertEqual(len(clicks), 3)
        self.assertEqual(swipes, [])


if __name__ == '__main__':
    unittest.main()
