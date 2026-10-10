import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_hif_behavior import PRODUCE, ROOT


class RewardSceneTest(unittest.TestCase):
    def test_lesson_does_not_set_count_or_click_on_unknown_or_wrong_day(self):
        lesson = PRODUCE.ProduceHIF__ProduceHIFLessonAuto()
        lesson._get_screenshot = lambda _: object()
        lesson._click_pos = lambda *_: self.fail('Clicked before confirmed lesson day')
        for day in (None, 0, 1, 2, 4, 5, 7):
            PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index = 2
            lesson._read_day_counter = lambda *_: day
            self.assertFalse(lesson.run(None, None))
            self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index, 2)
        days = iter((6, 3))
        lesson._read_day_counter = lambda *_: next(days)
        with patch.object(PRODUCE.time, 'sleep', return_value=None):
            self.assertFalse(lesson.run(None, None))
        self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index, 2)
        lesson._read_day_counter = lambda *_: 6
        lesson._lesson_buttons_ready = lambda *_: False
        with patch.object(PRODUCE.time, 'sleep', return_value=None):
            self.assertFalse(lesson.run(None, None))
        self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index, 2)

    def test_lesson_recognition_needs_date_and_three_button_columns(self):
        source = ast.parse((ROOT / 'extensions/hif/agent/hif/reco/produce.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ProduceHIF__ProduceHIFLessonFlagAuto')
        node.decorator_list = []
        namespace = {'CustomRecognition': type('CustomRecognition', (), {'AnalyzeArg': object, 'AnalyzeResult': SimpleNamespace}),
            'Context': object, 'Union': __import__('typing').Union, 'Optional': __import__('typing').Optional, 'RectType': object,
            'ProduceHIF__ProduceHIFLessonAuto': SimpleNamespace(_recognition_gate_active=lambda _: False,
                LESSON_BUTTONS=[(202, 1045), (377, 1045), (533, 1045)], RECOGNITION_GATE_ROI=[0, 850, 720, 300],
                _lesson_buttons_ready=PRODUCE.ProduceHIF__ProduceHIFLessonAuto._lesson_buttons_ready),
            'ProduceHIF__ProduceHIFTrainFlagAuto': SimpleNamespace(_read_day_counter=lambda *_: None), 'logger': PRODUCE.logger}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<lesson-recognition>', 'exec'), namespace)
        analyzer = namespace[node.name]()
        for day, xs, expected in ((None, (180, 350, 510), False), (6, (180,), False),
                                 (6, (180, 350, 510), True), (3, (180, 350, 510), True), (5, (180, 350, 510), False)):
            namespace['ProduceHIF__ProduceHIFTrainFlagAuto']._read_day_counter = lambda *_: day
            context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=True,
                filtered_results=[SimpleNamespace(box=[x, 1030, 50, 30]) for x in xs]))
            self.assertEqual(analyzer.analyze(context, SimpleNamespace(image=object())).box is not None, expected)

    def test_supply_reads_name_line_without_type_or_effects_and_accepts_missing_upgrade_mark(self):
        result = lambda text, x, y, w=180: SimpleNamespace(text=text, box=[x, y, w, 30])
        rows = [result('D', 130, 490, 20), result('私がスター', 210, 490), result('好印象消費2', 200, 540)]
        name = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto._supply_name_from_results(rows)
        self.assertEqual(name, '私がスター')
        self.assertTrue(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, '私がスター+'))
        self.assertFalse(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, '私がスター候補+'))
        merged = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto._supply_name_from_results([
            result('D私がスター', 125, 490), result('好印象消費2', 200, 540)])
        self.assertEqual(merged, '私がスター')

    def test_supply_title_does_not_fall_back_to_effect_or_right_side_cost(self):
        result = lambda text, box: SimpleNamespace(text=text, box=box)
        parse = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto._supply_name_from_results
        self.assertEqual(parse([result('体力消費4', [128, 553, 146, 30])]), '')
        self.assertEqual(parse([result('好印象+4', [136, 552, 179, 30]),
                               result('消費', [580, 500, 40, 30])]), '')
        for name, box in (('やる気は満点', [266, 495, 183, 38]),
                          ('ラブリーウインク', [239, 501, 237, 33]),
                          ('止められない想い', [239, 500, 238, 32])):
            self.assertEqual(parse([result(name, box), result('体力消費4', [128, 553, 146, 30])]), name)

    def test_replacement_protects_only_purchase_checked_drinks(self):
        choose = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto._replacement_slot
        self.assertEqual(choose(['low', 'a', 'low', 'b'], 'a', ['a', 'b']), 2)
        self.assertEqual(choose(['bad', 'a', 'other', 'b'], 'a', ['a', 'b'], ['bad']), 0)
        self.assertEqual(choose(['a', 'b', 'a', 'b'], 'a', ['a', 'b']), 3)
        self.assertIsNone(choose(['a', 'b', 'a', 'b'], 'a', ['a', 'b'], protected_names=['a', 'b']))
        self.assertEqual(choose(['a', 'b', 'a', 'b'], 'b', ['a', 'b'], protected_names=['b']), 2)
        self.assertEqual(choose(['bad', 'a', 'other', 'b'], 'a', ['a', 'b'], ['bad'], ['bad', 'b']), 2)
        self.assertEqual(choose(['a', 'a', 'a', 'a'], 'b', ['a', 'b']), 3)
        self.assertIsNone(choose(['low'] * 4, 'unknown', ['a']))
        self.assertIsNone(choose(['low', '', 'low', 'a'], 'a', ['a']))

    def test_inventory_replacement_reads_purchase_protection_including_disabled(self):
        action = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        held = iter(['bad', 'a', 'b', 'a'])
        action._inventory_detail = lambda *_: {'name': next(held)}
        action._cancel_inventory_detail = lambda *_: True
        action._discard_inventory_drink = lambda _c, _pos, name: name == 'a'
        context = object()
        with patch.object(PRODUCE, '_hif_drink_priority_names', return_value=['bad', 'b']) as names:
            self.assertTrue(action._make_inventory_room(context, 'b', ['a', 'b'], ['bad']))
        names.assert_called_once_with(context, purchase_only=True, include_disabled=True)

    def test_full_inventory_run_discards_then_reselects_target_before_receive(self):
        action = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        image = object()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image)))))
        events = []
        action._is_receive_drink_screen = lambda *_: True
        action._wait_for_bar_stable = lambda *_: (image, 4)
        def select(*_):
            action._supply_target_name = 'target'
            return (360, 885)
        action._select_priority_supply_drink = select
        action._make_inventory_room = lambda *_: events.append('discard-and-confirm') or True
        action._click = lambda _c, pos, **_k: events.append(pos)
        action._read_supply_drink_ocr = lambda *_: 'target'
        action._wait_for_receive_button = lambda _: [330, 1080, 50, 30]
        with patch.object(PRODUCE, '_hif_drink_priority_names', side_effect=[['target'], []]):
            self.assertTrue(action.run(context, None))
        self.assertEqual(events, ['discard-and-confirm', (360, 885), (355, 1095)])

    def test_failed_discard_does_not_repeat_or_receive(self):
        action = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        action.INVENTORY_ACTION_TIMEOUT = 0
        action._inventory_detail = lambda *_: {'name': 'low', 'discard': [560, 900, 80, 30]}
        clicks, stops = [], []
        action._click = lambda _c, pos, **_k: clicks.append(pos)
        context = SimpleNamespace(tasker=SimpleNamespace(post_stop=lambda: stops.append(True)))
        with self.assertRaises(PRODUCE.HifDrinkFlowError):
            action._discard_inventory_drink(context, (146, 1210), 'low')
        self.assertEqual(clicks, [(600, 915)])
        self.assertEqual(stops, [True])


if __name__ == '__main__':
    unittest.main()
