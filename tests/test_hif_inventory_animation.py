from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_hif_behavior import PRODUCE


def row(text, x, y, w=100, h=30):
    return SimpleNamespace(text=text, box=[x, y, w, h])


def detail(y):
    return [row('Pドリンク詳細', 53, y), row('リカバリドリンク', 191, y + 86, 232),
            row('捨てる', 572, y + 82, 66), row('キャンセル', 166, y + 303, 145),
            row('使う', 489, y + 298, 63)]


def confirmation(y, name='リカバリドリンク'):
    return [row(f'本当に{name}を捨てますか？', 60, y - 100, 450),
            row('はい', 493, y, 60), row('いいえ', 194, y, 85)]


class InventoryAnimationTest(unittest.TestCase):
    def setup_replay(self, frames):
        action = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        state = {'index': -1, 'clock': 10.0, 'clicks': []}
        def screen():
            state['index'] += 1
            return min(state['index'], len(frames) - 1)
        controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=screen)))
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False,
            post_stop=lambda: self.fail('Recoverable animation must not stop the task')))
        action._inventory_popup_results = lambda _c, image: frames[image]
        action._is_receive_drink_screen = lambda *_: True
        action._click = lambda _c, pos, **_kw: state['clicks'].append((state['index'], pos))
        return action, context, state

    def clock(self, state):
        return patch.multiple(PRODUCE.time, monotonic=lambda: state['clock'],
            sleep=lambda seconds: state.__setitem__('clock', state['clock'] + seconds))

    def test_same_name_while_detail_slides_does_not_finish_opening(self):
        action, context, state = self.setup_replay([detail(903), detail(841), detail(841), detail(841)])
        with self.clock(state):
            result = action._inventory_detail(context, (62, 1210))
        self.assertEqual(result['name'], 'リカバリドリンク')
        self.assertEqual(state['index'], 3)
        self.assertEqual(result['discard'][1], 923)

    def test_cancel_waits_for_stable_button_and_title_disappears_before_next_bottle(self):
        title_only = [row('Pドリンク詳細', 53, 600)]
        action, context, state = self.setup_replay([detail(903), detail(841), detail(841), detail(841),
                                                  title_only, title_only, title_only, [], [], []])
        with self.clock(state):
            self.assertTrue(action._cancel_inventory_detail(context))
        self.assertEqual(state['clicks'], [(3, (238, 1159))])
        self.assertEqual(state['index'], 9)

    def test_yes_waits_for_position_stability_and_retries_same_dialog_when_first_click_misses(self):
        frames = [confirmation(1165)] + [confirmation(1145)] * 10 + [[], [], []]
        action, context, state = self.setup_replay(frames)
        action._inventory_detail = lambda *_: {'name': 'リカバリドリンク', 'discard': [572, 923, 66, 27]}
        action._bar_filled_count = lambda image: 3 if image >= 11 else 4
        with self.clock(state):
            self.assertTrue(action._discard_inventory_drink(context, (62, 1210), 'リカバリドリンク'))
        yes = [(index, pos) for index, pos in state['clicks'] if pos[0] == 523]
        self.assertEqual(len(yes), 2)
        self.assertGreaterEqual(yes[0][0], 3)
        self.assertTrue(all(pos == (523, 1160) for _, pos in yes))

    def test_confirm_not_applied_twice_cancels_and_preserves_inventory_instead_of_stopping(self):
        action, context, state = self.setup_replay([confirmation(1145)] * 30)
        action._inventory_detail = lambda *_: {'name': 'リカバリドリンク', 'discard': [572, 923, 66, 27]}
        action._cancel_inventory_detail = lambda *_: True
        action._bar_filled_count = lambda _: 4
        with self.clock(state):
            self.assertFalse(action._discard_inventory_drink(context, (62, 1210), 'リカバリドリンク'))
        positions = [pos for _, pos in state['clicks']]
        self.assertEqual(positions.count((523, 1160)), 2)
        self.assertEqual(positions[-1], (236, 1160))

    def test_callback_returns_false_when_state_cannot_be_confirmed(self):
        action = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        with patch.object(action, '_run_receive', side_effect=PRODUCE.HifDrinkFlowError('unconfirmed')):
            self.assertFalse(action.run(None, None))


if __name__ == '__main__':
    unittest.main()
