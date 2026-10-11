import unittest
import json
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from tests.test_hif_behavior import PRODUCE, _Context


class ExchangeCustomizationTest(unittest.TestCase):
    def test_per_card_option_combinations_and_fallback(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto

        def run_case(cards, rules, targets):
            clicks = []
            controller = SimpleNamespace(post_click=lambda x, y: (
                clicks.append((x, y)) or SimpleNamespace(wait=lambda: None)))
            context = _Context({})
            context.tasker = SimpleNamespace(controller=controller)
            action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
            action.CARD_GRID = [(10, 10), (20, 20)]
            action._replacement_targets = lambda _context: targets
            action._swap_out_options = lambda _context: rules
            action._wait_stable_name = lambda _context: (clicks[-1], cards[action.CARD_GRID.index(clicks[-1])][0])
            seen_marks = []
            action._customized_card = lambda pos: (
                seen_marks.append(cards[action.CARD_GRID.index(pos)][1]) or cards[action.CARD_GRID.index(pos)][1])
            with patch.object(swap, '_delete_card_exchange_count', 0), patch.object(PRODUCE.time, 'sleep'), \
                    patch.object(action, '_confirm_exchange', return_value=True) as confirm, \
                    patch.object(PRODUCE, 'logger') as logs:
                result = action.run(context, None)
                self.assertEqual(confirm.call_count, int(result))
                if None in seen_marks:
                    self.assertTrue(any('定制标记未确认，按非定制卡处理' in call.args[0]
                                        for call in logs.info.call_args_list))
                if True in seen_marks:
                    self.assertTrue(any('有定制标记' in call.args[0] for call in logs.info.call_args_list))
                return result, clicks[-1]

        for prefer, allow, position in ((True, True, (20, 20)), (False, True, (10, 10)),
                                         (True, False, (20, 20)), (False, False, (20, 20))):
            with self.subTest(prefer=prefer, allow=allow):
                self.assertEqual(run_case([('スポットライト+', True), ('スポットライト+', False)],
                                          {'スポットライト': (prefer, allow)}, ['スポットライト', '基本']),
                                 (True, position))
                self.assertEqual(run_case([('スポットライト+', None), ('スポットライト+', True)],
                                          {'スポットライト': (prefer, allow)}, ['スポットライト', '基本']),
                                 (True, (10, 10)))
        self.assertEqual(run_case([('スポットライト+', True)] * 2, {}, ['スポットライト', '基本']),
                         (True, (10, 10)))  # 旧配置两个开关均默认开启。
        self.assertFalse(run_case([('スポットライト+', True)] * 2,
                                  {'スポットライト': (True, False)}, ['スポットライト', '基本'])[0])
        # 禁止某张卡的定制版，不能绕过名单通过兜底换出；另一张卡的开关独立。
        self.assertEqual(run_case([('スポットライト+', True), ('魅惑の視線+', True)],
                                  {'スポットライト': (True, False), '魅惑の視線': (True, True)}, ['基本']),
                         (True, (20, 20)))
        self.assertEqual(run_case([('スポットライト+', True), ('スポットライト+', False)],
                                  {}, ['不存在的卡', '基本']), (True, (20, 20)))

    def test_old_config_defaults_and_profession_specific_explicit_values(self):
        config = {'swap_out_options_profiles': {
            '集中': {'スポットライト+': {'prefer_uncustomized': False, 'allow_customized': False},
                     '魅惑の視線+': {'allow_customized': True}},
            '全力': {'スポットライト+': {'prefer_uncustomized': True, 'allow_customized': True}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'cards_priority.json'
            action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
            with patch.object(PRODUCE, 'CARDS_PRIORITY_CONFIG_PATH', str(path)), \
                    patch.object(PRODUCE.ProduceHIF__ProduceCardsAuto, '_load_hif_profession', return_value='集中'):
                path.write_text('{}', encoding='utf-8')
                self.assertEqual(action._swap_out_options(_Context({})), {})
                path.write_text(json.dumps(config), encoding='utf-8')
                self.assertEqual(action._swap_out_options(_Context({})),
                                 {'スポットライト': (False, False), '魅惑の視線': (True, True)})
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), config)

    def test_marker_ignores_icons_costs_and_other_cards(self):
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        # 蓝色属性图标，以及卡面之外的绿色费用/其他卡定制标记。
        image[362:394, 92:124] = (255, 180, 0)
        cv2.circle(image, (155, 385), 14, (40, 200, 20), -1)
        cv2.circle(image, (140, 870), 14, (40, 200, 20), -1)
        with patch.object(PRODUCE, 'cv2', cv2), patch.object(PRODUCE, 'np', np):
            detector = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto._customized_card
            self.assertFalse(detector(image))
            for number in (1, 2, 3):
                marked = image.copy()
                cv2.circle(marked, (108, 378), 13, (40, 200, 20), -1)
                cv2.putText(marked, str(number), (102, 385), cv2.FONT_HERSHEY_SIMPLEX,
                            0.55, (255, 255, 255), 2)
                self.assertTrue(detector(marked))
            self.assertIsNone(detector(None))
            self.assertIsNone(detector(image[:720]))

    def test_same_name_customized_card_is_skipped_and_fallback_is_guarded(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        for mode in (swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE, swap.SECOND_SWAP_CONSULT_DELETE_NODE):
            for marks, expected, last in (([True, False], True, (20, 20)),
                                          ([True, True], False, (20, 20)),
                                          ([None, None], True, (10, 10)),
                                          ([False, True], False, (10, 10))):
                with self.subTest(mode=mode, marks=marks):
                    clicks = []
                    controller = SimpleNamespace(post_click=lambda x, y: (
                        clicks.append((x, y)) or SimpleNamespace(wait=lambda: None)))
                    context = _Context({mode: {'enabled': True}})
                    context.tasker = SimpleNamespace(controller=controller)
                    action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                    action.CARD_GRID = [(10, 10), (20, 20)]
                    action._replacement_targets = lambda _context: ['スポットライト', '基本']
                    action._swap_out_options = lambda _context: {'スポットライト': (True, False)}
                    action._wait_stable_name = lambda _context: (clicks[-1], 'スポットライト+')
                    # 最后一种情况模拟复核时详情变成了带豆卡。
                    action._customized_card = lambda pos: (
                        marks[1] if marks == [False, True] and len(clicks) > 1
                        else marks[action.CARD_GRID.index(pos)])
                    with patch.object(swap, '_delete_card_exchange_count', 1), \
                            patch.object(swap, '_delete_card_a', 'スポットライト'), \
                            patch.object(PRODUCE.time, 'sleep'), \
                            patch.object(action, '_confirm_exchange', return_value=True) as confirm:
                        self.assertEqual(action.run(context, None), expected)
                        self.assertEqual(confirm.call_count, int(expected))
                        self.assertEqual(clicks[-1], last)


if __name__ == '__main__':
    unittest.main()
