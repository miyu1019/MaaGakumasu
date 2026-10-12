"""Consult entry must finish reward events before treating the screen as a shop."""
import ast
from types import SimpleNamespace as NS
import typing
import unittest
from unittest.mock import patch
from test_hif_behavior import PRODUCE, ROOT


class ConsultEntryTest(unittest.TestCase):
    def setUp(self):
        PRODUCE._hif_recovery.reset(None)
        self.action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        self.action._shop_finish_guard_active = lambda: False
        self.clock = [0.0]
        self.scene = 'card_event'
        self.taps, self.tasks = [], []
        self.context = NS(tasker=NS(stopping=False),
            run_recognition=self.recognize, run_task=self.run_task)
        self.action._click = self.click
        self.action._shop_visible = lambda _ctx, image: image == 'shop'

    def recognize(self, node, image):
        return NS(hit=node == image)

    def run_task(self, node, pipeline_override):
        self.tasks.append((node, pipeline_override))
        self.scene = 'shop'
        return NS(status=NS(succeeded=True))

    def click(self, _ctx, pos, **_kw):
        self.taps.append((self.scene, pos))
        if len(self.taps) == 3:
            self.scene = 'drink_event'
        if len(self.taps) == 6:
            self.scene = 'ProduceHIF__ProduceHIFDrinkFullFlag'

    def sleep(self, duration):
        self.clock[0] += duration

    def wait(self):
        with patch.object(PRODUCE, '_hif_nonbattle_screencap', side_effect=lambda _: self.scene), \
                patch.object(PRODUCE.time, 'monotonic', side_effect=lambda: self.clock[0]), \
                patch.object(PRODUCE.time, 'sleep', side_effect=self.sleep):
            return self.action._wait_for_shop_ready(self.context)

    def test_card_and_drink_events_then_full_page_are_handled_before_shop(self):
        self.assertTrue(self.wait())
        self.assertEqual([scene for scene, _pos in self.taps], ['card_event'] * 3 + ['drink_event'] * 3)
        self.assertTrue(all(pos == (10, 20) for _scene, pos in self.taps))
        self.assertEqual(len(self.tasks), 1)
        node, override = self.tasks[0]
        self.assertEqual(override[node]['next'], [])
        self.assertEqual(override[node]['on_error'], [])
        self.assertGreaterEqual(self.clock[0], 15)

    def test_all_receive_pages_reuse_existing_actions_without_skip_taps(self):
        for suffix in ('DrinkFullKeepFlag', 'DrinkFullConfirmFlag', 'DrinkFullFlag', 'DrinkFlag'):
            self.setUp()
            node = 'ProduceHIF__ProduceHIF' + suffix
            self.scene = node
            self.assertTrue(self.wait())
            self.assertEqual(self.taps, [])
            self.assertEqual([name for name, _ in self.tasks], [node])

    def test_no_optional_actions_still_wait_for_shop_before_finish(self):
        finished = []
        self.action._buy_drinks_enabled = lambda _: False
        self.action._delete_card_enabled = lambda _: False
        self.action._finish_consult = lambda _: finished.append(self.scene)
        with patch.object(PRODUCE, '_hif_nonbattle_screencap', side_effect=lambda _: self.scene), \
                patch.object(PRODUCE.time, 'monotonic', side_effect=lambda: self.clock[0]), \
                patch.object(PRODUCE.time, 'sleep', side_effect=self.sleep):
            self.assertTrue(self.action.run_shop(self.context, wait_for_ready=True))
        self.assertEqual(finished, ['shop'])

    def test_stop_in_event_wait_prevents_more_clicks_or_shop_actions(self):
        self.sleep = lambda _duration: setattr(self.context.tasker, 'stopping', True)
        self.assertFalse(self.wait())
        self.assertEqual(len(self.taps), 1)
        self.assertEqual(self.tasks, [])

    def test_failed_receive_action_stops_without_animation_clicks(self):
        self.scene = 'ProduceHIF__ProduceHIFDrinkFullFlag'
        self.context.run_task = lambda *_a, **_k: NS(status=NS(succeeded=False))
        self.assertFalse(self.wait())
        self.assertEqual(self.taps, [])

    def test_submitted_receive_page_is_observed_without_replaying_action(self):
        self.scene = 'ProduceHIF__ProduceHIFDrinkFullFlag'
        def receive(node, pipeline_override):
            self.tasks.append((node, pipeline_override))
            return NS(status=NS(succeeded=True))
        self.context.run_task = receive
        self.assertFalse(self.wait())
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.taps, [])

    def test_entry_timeout_does_not_restart_budget_after_taps(self):
        self.action._click = lambda _ctx, pos, **_kw: self.taps.append(pos)
        self.assertFalse(self.wait())
        self.assertGreaterEqual(self.clock[0], 30)
        self.assertLess(self.clock[0], 30.3)
        self.assertLessEqual(len(self.taps), 30)

    def test_shop_identity_requires_both_title_and_exchange_instruction(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        for title, detail in ((True, False), (False, True), (False, False), (True, True)):
            action._read_ocr = lambda _ctx, _img, roi, _exp: ('hit' if (title if roi == action.SHOP_TITLE_ROI else detail) else '')
            self.assertEqual(action._shop_visible(self.context, object()), title and detail)

    def test_shop_recovery_recognition_uses_same_identity_gate(self):
        source = ast.parse((ROOT / 'extensions/hif/agent/hif/reco/produce.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ProduceHIF__ProduceHIFConsultShopFlagAuto')
        node.decorator_list = []
        namespace = {'CustomRecognition': type('CustomRecognition', (), {'AnalyzeArg': object, 'AnalyzeResult': NS}),
            'Context': object, 'Union': typing.Union, 'Optional': typing.Optional, 'RectType': object,
            'ProduceHIF__ProduceHIFConsultAuto': PRODUCE.ProduceHIF__ProduceHIFConsultAuto, 'logger': PRODUCE.logger}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<consult-recognition>', 'exec'), namespace)
        for visible in (False, True):
            with patch.object(PRODUCE.ProduceHIF__ProduceHIFConsultAuto, '_shop_finish_guard_active', return_value=False), \
                    patch.object(PRODUCE.ProduceHIF__ProduceHIFConsultAuto, '_shop_visible', return_value=visible):
                result = namespace[node.name]().analyze(self.context, NS(image=object()))
                self.assertEqual(result.box is not None, visible)
