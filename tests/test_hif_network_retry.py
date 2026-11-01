import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_hif_behavior import PRODUCE, ROOT, COMMON, hif_patch


class NetworkRetryTests(unittest.TestCase):
    def test_non_image_frames_do_not_trigger_network_recognition(self):
        for frame in ({}, {"screen": "shop"}, None, object()):
            self.assertFalse(PRODUCE._hif_network_dialog_possible(frame))

    def test_exit_retry_survives_exhausted_animation_taps_and_preserves_state(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._last_turn_count = 1
        action._battle_transition_taps = action.BATTLE_TRANSITION_MAX_TAPS
        action._battle_transition_misses = 5
        clicks = []
        context = NS(
            tasker=NS(stopping=False, controller=NS(
                post_click=lambda x, y: NS(wait=lambda: clicks.append((x, y))))),
            run_recognition=lambda *_: NS(hit=True, box=[470, 1140, 120, 35]),
        )
        with hif_patch('_hif_network_dialog_possible', return_value=True), patch.object(COMMON.time, 'sleep') as sleep:
            action._skip_battle_end_animation(context, object())
        self.assertEqual(clicks, [(530, 1157)])
        self.assertEqual(action._battle_transition_taps, action.BATTLE_TRANSITION_MAX_TAPS)
        self.assertEqual(action._last_turn_count, 1)
        self.assertEqual(action._battle_transition_misses, 0)
        sleep.assert_called_once_with(3)
        context.tasker.stopping = True
        action._skip_battle_end_animation(context, object())
        self.assertEqual(len(clicks), 1)

    def test_nonbattle_retries_until_recovered_then_returns_fresh_screen(self):
        frames = iter(['error', 'error_again', 'shop'])
        clicks = []
        context = NS(
            tasker=NS(stopping=False, controller=NS(
                post_screencap=lambda: NS(wait=lambda: NS(get=lambda: next(frames))),
                post_click=lambda x, y: NS(wait=lambda: clicks.append((x, y))))),
            run_recognition=lambda _, image: NS(hit=image != 'shop', box=[470, 1140, 120, 35]),
        )
        with hif_patch('_hif_network_dialog_possible', return_value=True), patch.object(COMMON.time, 'sleep'):
            self.assertEqual(PRODUCE._hif_nonbattle_screencap(context), 'shop')
        self.assertEqual(clicks, [(530, 1157)] * 2)

    def test_nonbattle_does_not_click_non_network_dialog_or_after_stop(self):
        for hit, stopping in ((False, False), (True, True)):
            context = NS(tasker=NS(stopping=stopping), run_recognition=lambda *_: NS(hit=hit))
            with hif_patch('_hif_network_dialog_possible', return_value=True):
                self.assertFalse(PRODUCE._hif_retry_network(context, object()))

    def test_final_transition_keeps_tapping_beyond_three(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._last_turn_count = 1
        action._battle_transition_taps = action._battle_transition_misses = 0
        action._last_battle_transition_tap = 0.0
        clicks = []
        context = NS(
            tasker=NS(stopping=False, controller=NS(
                post_click=lambda x, y: NS(wait=lambda: clicks.append((x, y))))),
            run_recognition=lambda *_a, **_k: NS(hit=False),
        )
        clock = [100.0]
        with hif_patch('_hif_retry_network', return_value=False), patch.object(COMMON.time, 'monotonic', lambda: clock[0]):
            for _ in range(3 * (action.BATTLE_TRANSITION_MAX_TAPS + 2)):
                clock[0] += 1
                action._skip_battle_end_animation(context, object())
        self.assertGreater(action.BATTLE_TRANSITION_MAX_TAPS, 3)
        self.assertEqual(clicks, [(360, 640)] * action.BATTLE_TRANSITION_MAX_TAPS)

    def test_normal_turn_does_not_check_network(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._last_turn_count = 2
        context = NS(run_recognition=lambda *_: self.fail('Normal battle checked network'))
        action._skip_battle_end_animation(context, object())

    def test_pipeline_requires_error_and_retry_and_clicks_retry_box(self):
        nodes = json.loads((ROOT / 'extensions/hif/resource/base/pipeline/HIF.json').read_text(encoding='utf-8'))
        retry = nodes['ProduceHIF__ProduceNetworkRetry']
        self.assertEqual(retry['recognition'], 'And')
        self.assertEqual(len(retry['all_of']), 2)
        self.assertEqual(retry['box_index'], 1)
        self.assertEqual(nodes['ProduceHIF__ProduceEntryHIF']['next'][0], '[JumpBack]ProduceHIF__ProduceNetworkRetry')
        for name, node in nodes.items():
            children = node.get('next', [])
            if not children:
                continue
            names = [child.split(']')[-1] for child in children if isinstance(child, str)]
            def is_direct(child):
                reco = nodes.get(child, {}).get('recognition', 'DirectHit')
                return (reco.get('type') if isinstance(reco, dict) else reco) == 'DirectHit'
            if any(not is_direct(child) for child in names):
                self.assertEqual(children[0], '[JumpBack]ProduceHIF__ProduceNetworkRetry', name)


if __name__ == '__main__':
    unittest.main()
