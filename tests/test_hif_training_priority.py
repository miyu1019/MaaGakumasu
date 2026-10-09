"""Configured attribute order applies to ordinary lessons as well as SP choices."""
import unittest
from types import SimpleNamespace
from test_hif_behavior import PRODUCE, _Context


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
        reads, clicks, gates = [], [], []
        def recognize(_context, _image, roi):
            attr = next(attr for attr, box in action.SP_ROIS.items() if box == roi)
            reads.append(attr)
            return attr in sp_attributes
        action._get_sp_course = recognize
        action._click_pos = lambda _context, x, y: clicks.append((x, y))
        action._arm_recognition_gate = lambda day, _image, task: gates.append((day, task))
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


if __name__ == '__main__':
    unittest.main()
