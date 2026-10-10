"""Configured attribute order applies to ordinary lessons as well as SP choices."""
import unittest
import ast
from types import SimpleNamespace
from unittest.mock import patch
from test_hif_behavior import PRODUCE, ROOT, _Context


class TrainingPriorityTest(unittest.TestCase):
    def run_lesson(self, first, second, sp_attributes):
        action = PRODUCE.ProduceHIF__ProduceHIFTrainAuto()
        context = _Context({
            'ProduceHIF__ProduceHIFSPFirst' + first: {'enabled': True},
            'ProduceHIF__ProduceHIFSPSecond' + second: {'enabled': True},
        })
        image = object()
        action._get_screenshot = lambda _: image
        action._read_day_counter = lambda *_: 5
        action._training_buttons_ready = lambda *_: True
        reads, clicks, gates = [], [], []
        def recognize(_context, _image, roi):
            attr = next(attr for attr, box in action.SP_ROIS.items() if box == roi)
            reads.append(attr)
            return attr in sp_attributes
        action._get_sp_course = recognize
        action._click_pos = lambda _context, x, y: clicks.append((x, y))
        action._arm_recognition_gate = lambda day, _image, task: gates.append((day, task))
        with patch.object(PRODUCE.time, 'sleep'):
            self.assertTrue(action.run(context, SimpleNamespace(custom_action_param='{}', task_detail=SimpleNamespace(task_id=17))))
        self.assertEqual(gates, [(5, 17)])
        return reads, clicks

    def test_no_sp_uses_first_configured_attribute_for_all_orders(self):
        for first in ('Da', 'Vi', 'Vo'):
            for second in ('Da', 'Vi', 'Vo'):
                if first == second:
                    continue
                with self.subTest(first=first, second=second):
                    reads, clicks = self.run_lesson(first, second, set())
                    self.assertEqual(reads[:2], [first, second])
                    self.assertEqual(set(reads), {'Da', 'Vi', 'Vo'})
                    self.assertEqual(clicks, [PRODUCE.ProduceHIF__ProduceHIFTrainAuto.TRAIN_BUTTONS[first]])

    def test_existing_sp_still_takes_precedence_over_ordinary_first_choice(self):
        for available, chosen in (({'Da', 'Vi'}, 'Da'), ({'Vi', 'Vo'}, 'Vi'), ({'Vo'}, 'Vo')):
            with self.subTest(available=available):
                reads, clicks = self.run_lesson('Da', 'Vi', available)
                self.assertEqual(clicks, [PRODUCE.ProduceHIF__ProduceHIFTrainAuto.SP_POSITIONS[chosen]])
                self.assertEqual(reads[-1], chosen)

    def test_transition_cannot_click_or_arm_gate_on_non_training_page(self):
        for days, ready in (((1, 1), True), ((2, 1), True), ((2, 2), False), ((None, None), True)):
            with self.subTest(days=days, ready=ready):
                action = PRODUCE.ProduceHIF__ProduceHIFTrainAuto()
                values = iter(days)
                action._get_screenshot = lambda *_: object()
                action._read_day_counter = lambda *_: next(values)
                action._training_buttons_ready = lambda *_: ready
                action._arm_recognition_gate = lambda *_: self.fail('Must not block the next action page')
                action._click_pos = lambda *_: self.fail('Must not click stale training coordinates')
                with patch.object(PRODUCE.time, 'sleep'):
                    self.assertTrue(action.run(None, None))

    def test_non_training_day_releases_even_a_same_day_gate(self):
        source = ast.parse((ROOT / 'extensions/hif/agent/hif/reco/produce.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ProduceHIF__ProduceHIFTrainFlagAuto')
        method = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == '_click_gate_active')
        method.decorator_list = []
        namespace = {'ProduceHIF__ProduceHIFTrainAuto': PRODUCE.ProduceHIF__ProduceHIFTrainAuto,
                     'time': PRODUCE.time, 'logger': PRODUCE.logger,
                     'Optional': __import__('typing').Optional}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<training-gate>', 'exec'), namespace)
        action = PRODUCE.ProduceHIF__ProduceHIFTrainAuto
        with patch.object(PRODUCE.time, 'time', return_value=100):
            action._arm_recognition_gate(1, None, 17)
            self.assertFalse(namespace['_click_gate_active'](1, None, 17))
            action._arm_recognition_gate(2, None, 17)
            self.assertTrue(namespace['_click_gate_active'](2, None, 17))
        action._clear_recognition_gate()


if __name__ == '__main__':
    unittest.main()
