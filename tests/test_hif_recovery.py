import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_hif_behavior import PRODUCE, COMMON, hif_patch
from extensions.hif.recovery import RecoveryState


class RecoveryBudgetTest(unittest.TestCase):
    def setUp(self):
        PRODUCE._hif_recovery.reset(None)

    def test_only_business_progress_or_new_task_resets_budget(self):
        state = RecoveryState()
        argv = NS(task_detail=NS(task_id=1))
        with patch.object(COMMON.time, 'monotonic', return_value=0):
            state.bind(argv).progress(('行动日期', 6))
            self.assertTrue(state.defer())
        with patch.object(COMMON.time, 'monotonic', return_value=31):
            state.bind(argv).progress(('行动日期', 6))
            self.assertFalse(state.defer())
            state.pending = '提交结果待确认'
            state.progress(('行动日期', 3))
            self.assertFalse(state.defer())
            state.pending = None
            state.progress(('行动日期', 3))
            self.assertTrue(state.defer())
            state.bind(NS(task_detail=NS(task_id=2)))
            self.assertIsNone(state.deadline)
            self.assertIsNone(state.step)

    def test_network_wait_does_not_consume_budget(self):
        state = RecoveryState()
        with patch.object(COMMON.time, 'monotonic', return_value=0):
            self.assertTrue(state.defer())
        state.paused += 60
        with patch.object(COMMON.time, 'monotonic', return_value=70):
            self.assertTrue(state.defer())
        with patch.object(COMMON.time, 'monotonic', return_value=91):
            self.assertFalse(state.defer())

    def test_invalid_action_exhausts_budget_without_clicking(self):
        action = PRODUCE.ProduceHIF__ProduceHIFRestAuto()
        action._get_screenshot = lambda _: object()
        action._read_day_counter = lambda *_: None
        action._click_pos = lambda *_: self.fail('Unconfirmed page clicked')
        context = NS(tasker=NS(stopping=False))
        argv = NS(task_detail=NS(task_id=73), node_name='Rest')
        PRODUCE._hif_recovery.reset(None)
        with patch.object(COMMON.time, 'monotonic', return_value=0), patch.object(COMMON.time, 'sleep'):
            self.assertTrue(action.run(context, argv))
        with patch.object(COMMON.time, 'monotonic', return_value=31), hif_patch('_hif_failure', return_value=False) as failure:
            self.assertFalse(action.run(context, argv))
            failure.assert_called_once()

    def test_stop_does_not_wait_or_click(self):
        with patch.object(COMMON.time, 'sleep') as sleep:
            self.assertTrue(PRODUCE._hif_defer(NS(tasker=NS(stopping=True)), None, 'stop'))
            sleep.assert_not_called()

    def test_read_only_result_requires_three_consecutive_samples(self):
        seen = []
        frames = iter([True, False, True, True, True])
        with hif_patch('_hif_nonbattle_screencap', side_effect=lambda _: next(frames)), \
                patch.object(COMMON.time, 'sleep') as sleep:
            self.assertTrue(PRODUCE._hif_observe_result(NS(tasker=NS(stopping=False)), lambda image: seen.append(image) or image))
        self.assertEqual(seen, [True, False, True, True, True])
        self.assertTrue(all(call.args[0] >= .25 for call in sleep.call_args_list))

    def test_result_unknown_exhausts_budget_without_clicking(self):
        clock = [0]
        context = NS(tasker=NS(stopping=False))
        with patch.object(COMMON.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(COMMON.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)), \
                hif_patch('_hif_nonbattle_screencap', return_value=object()):
            self.assertFalse(PRODUCE._hif_observe_result(context, lambda _: False))
        self.assertEqual(clock[0], 10)

    def test_bad_combo_downgrades_but_preserves_card_restrictions(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.profession = '集中'
        action.use_conditions = {'A': [{'remaining_turns_lte': 2}]}
        action._drink_before_targets = []
        from unittest.mock import mock_open
        import json
        configs = [{'followup_profiles': {'集中': {'A': {}}}}, {'A': {'name': 'A'}}]
        with patch('builtins.open', mock_open()), patch.object(json, 'load', side_effect=configs):
            action._load_hif_followups()
        self.assertEqual(action._followups.rules, {})
        self.assertEqual(action.use_conditions, {'A': [{'remaining_turns_lte': 2}]})

    def test_missing_basic_config_is_not_a_combo_downgrade(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        with patch('builtins.open', side_effect=OSError('missing')):
            with self.assertRaises(PRODUCE.HifDrinkFlowError):
                action._load_config('集中')

    def test_paired_timeout_releases_only_after_safe_confirmation(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._paired_card = {'key': 'A'}
        action._paired_deadline = 0
        action._is_drink_detail_open = lambda *_: False
        action._is_battle_end = lambda *_: False
        action._read_turn_count = lambda *_: 5
        context = NS(tasker=NS(stopping=False), run_recognition=lambda *_: NS(hit=True))
        with hif_patch('_hif_nonbattle_screencap', return_value=object()), patch.object(COMMON.time, 'sleep'):
            action._recover_paired_drink(context)
        self.assertIsNone(action._paired_card)
        self.assertTrue(action._paired_drink_disabled)
        self.assertIn('特製ハツボシエキス', action._drink_disabled_names)

    def test_pending_transaction_cannot_reenter_or_recover(self):
        context = NS(tasker=NS(stopping=False))
        argv = NS(node_name='Entry')
        PRODUCE._hif_recovery.pending = '已提交用饮'
        with hif_patch('_hif_failure', return_value=False), \
                hif_patch('_hif_nonbattle_screencap') as capture:
            self.assertFalse(PRODUCE.ProduceHIF__ProduceHIFEntryObserveAuto().run(context, argv))
            self.assertFalse(PRODUCE.ProduceHIF__ProduceHIFEntryRecoveryAuto().run(context, argv))
            capture.assert_not_called()

    def test_watchdog_counts_only_business_progress(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._last_turn_count = 9
        action._battle_confirmed_plays = 0
        action._drink_used_counts = {}
        action._battle_progress_signature = None
        action._battle_progress_at = 0
        action._battle_recovery_deadline = None
        context = NS(tasker=NS(stopping=False))
        for now in (0, 119, 120, 149):
            with patch.object(COMMON.time, 'monotonic', return_value=now):
                self.assertFalse(action._check_battle_progress(context, object()))
        with patch.object(COMMON.time, 'monotonic', return_value=150):
            with self.assertRaises(PRODUCE.HifDrinkFlowError):
                action._check_battle_progress(context, object())
        action._battle_confirmed_plays = 1
        with patch.object(COMMON.time, 'monotonic', return_value=151):
            self.assertFalse(action._check_battle_progress(context, object()))
        self.assertIsNone(action._battle_recovery_deadline)

    def test_network_capture_and_retry_time_are_both_excluded(self):
        clock, frames = [0], iter(['network', 'network', 'normal'])
        def capture():
            clock[0] += 20
            return next(frames)
        context = NS(tasker=NS(stopping=False, controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=capture)))))
        with patch.object(COMMON.time, 'monotonic', side_effect=lambda: clock[0]), \
                hif_patch('_hif_retry_network', side_effect=lambda _ctx, image: image == 'network'):
            self.assertTrue(PRODUCE._hif_recovery.defer())
            self.assertEqual(PRODUCE._hif_nonbattle_screencap(context), 'normal')
            self.assertTrue(PRODUCE._hif_recovery.defer())
            self.assertEqual(PRODUCE._hif_recovery.paused, 60)

    def test_exchange_delay_never_replays_submit(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        clicks = []
        action._click_text = lambda *_: clicks.append('change')
        action._wait_exchange_closed = lambda _: False
        action._exchange_page_visible = lambda _ctx, image: image == 0
        frames = iter([0, 1, 2, 3])
        with hif_patch('_hif_nonbattle_screencap', side_effect=lambda _: next(frames)), \
                hif_patch('_hif_safe_scene', return_value='known-next'), \
                patch.object(COMMON.time, 'sleep'), \
                patch.object(PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto, 'finish_delete_card_exchange') as finish:
            self.assertTrue(action._confirm_exchange(NS(tasker=NS(stopping=False)), object()))
        self.assertEqual(clicks, ['change'])
        finish.assert_called_once()
        self.assertIsNone(PRODUCE._hif_recovery.pending)

    def test_delayed_drink_result_requires_count_drop_without_another_use(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._drink_disabled_names = set()
        action._drink_imported_names = ['A']
        action._drink_used_counts = {}
        action._battle_drink_limit_reached = lambda _: False
        action._wait_for_drink_list_stable = lambda *_a, **_k: True
        action._battle_drink_boxes = lambda _ctx, image=None: [[10, 10, 10, 10]] * (2 if image in (None, 0) else 1)
        action._open_drink_detail = lambda *_: True
        action._read_battle_drink_identity = lambda _: ('A', 'A')
        uses = []
        action._use_drink = lambda _: uses.append('A') or True
        action._wait_for_battle_drink_return = lambda _: False
        action._is_drink_detail_open = lambda *_: False
        context = NS(tasker=NS(stopping=False), run_recognition=lambda *_: NS(hit=True))
        frames = iter([0, 1, 2, 3])
        with hif_patch('_hif_nonbattle_screencap', side_effect=lambda _: next(frames)), patch.object(COMMON.time, 'sleep'):
            self.assertTrue(action._consume_battle_drinks(context, ['A'], 'test', max_uses=1))
        self.assertEqual(uses, ['A'])
        self.assertEqual(action._drink_used_counts, {'A': 1})
        self.assertIsNone(PRODUCE._hif_recovery.pending)


if __name__ == '__main__':
    unittest.main()
