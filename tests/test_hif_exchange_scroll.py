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
