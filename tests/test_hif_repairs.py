import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_hif_behavior import PRODUCE


class HifRepairTests(unittest.TestCase):
    def action(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action._hif_log_detection = lambda *_args: None
        action._read_battle_drink_identity = lambda *_args: ('ブーストエキス', '')
        action._read_turn_count = lambda *_args: 6
        return action

    def context(self):
        job = NS(wait=lambda: NS(get=lambda: object()))
        return NS(tasker=NS(stopping=False, controller=NS(post_screencap=lambda: job)))

    def test_cancel_retries_with_compatibility_and_stops_after_recovery(self):
        action, context = self.action(), self.context()
        action._find_drink_detail_actions = lambda *_args: {'cancel': [1, 1, 1, 1], 'use': [2, 2, 1, 1]}
        clicks = []
        action._click_drink_detail_action = lambda _ctx, name, **kw: clicks.append((name, kw['compatibility']))
        action._wait_for_drink_detail = lambda *_args, **_kw: False
        action._abort_drink_flow = lambda _ctx, reason: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError(reason))
        with self.assertRaises(PRODUCE.HifDrinkFlowError):
            action._close_drink_detail(context)
        self.assertEqual(clicks, [('cancel', False), ('cancel', True), ('cancel', True)])

    def test_battle_cancel_late_close_does_not_click_a_third_time(self):
        action, context = self.action(), self.context()
        action._find_drink_detail_actions = lambda *_: {'cancel': [1, 1, 1, 1]}
        clicks, waits = [], []
        action._click_drink_detail_action = lambda *_a, **_k: clicks.append(True)
        def wait(*_a, **kw):
            waits.append(kw['timeout'])
            return len(waits) == 3
        action._wait_for_drink_detail = wait
        self.assertTrue(action._close_drink_detail(context))
        self.assertEqual(len(clicks), 2)
        self.assertEqual(waits[-1], 10)

    def test_cancel_second_attempt_can_recover_and_already_closed_never_clicks(self):
        action, context = self.action(), self.context()
        action._find_drink_detail_actions = lambda *_args: {'cancel': [1, 1, 1, 1], 'use': [2, 2, 1, 1]}
        clicks = []
        action._click_drink_detail_action = lambda _ctx, name, **kw: clicks.append(kw['compatibility'])
        outcomes = iter([False, True])
        action._wait_for_drink_detail = lambda *_args, **_kw: next(outcomes)
        self.assertTrue(action._close_drink_detail(context))
        self.assertEqual(clicks, [False, True])
        action._find_drink_detail_actions = lambda *_args: {}
        action._wait_for_drink_detail = lambda *_args, **_kw: True
        self.assertTrue(action._close_drink_detail(context))
        self.assertEqual(len(clicks), 2)

    def test_use_checks_dialog_again_before_retry(self):
        action, context = self.action(), self.context()
        frames = iter([{'use': [1, 1, 1, 1], 'cancel': [2, 2, 1, 1]}, {}])
        action._find_drink_detail_actions = lambda *_args: next(frames)
        clicks = []
        action._click_drink_detail_action = lambda _ctx, name, **kw: clicks.append(name)
        closed = iter([False, True])
        action._wait_for_drink_detail = lambda *_args, **_kw: next(closed)
        self.assertTrue(action._use_drink(context))
        self.assertEqual(clicks, ['use'])

    def test_closed_dialog_requires_absent_buttons_and_restored_page(self):
        for buttons, restored, expected in [({}, False, False), ({'cancel': [1, 1, 1, 1]}, True, False), ({}, True, True)]:
            with self.subTest(buttons=buttons, restored=restored):
                action, context = self.action(), self.context()
                action._find_drink_detail_actions = lambda *_args: buttons
                context.run_recognition = lambda *_args: NS(hit=restored)
                ticks = iter([0, .1, .2, 1.1])
                with patch.object(PRODUCE.time, 'time', side_effect=lambda: next(ticks)), patch.object(PRODUCE.time, 'sleep'):
                    self.assertEqual(action._wait_for_drink_detail(context, False, 1), expected)

    def test_only_unusable_cards_skip_after_two_stable_frames_not_during_dialog(self):
        action, context = self.action(), self.context()
        context.run_recognition = lambda name, *_args: NS(hit=name.endswith('SkipRound'))
        action._is_drink_detail_open = lambda *_args: False
        hand = [NS(label='useless', box=[268, 884, 184, 250], score=.957)]
        with patch.object(PRODUCE.time, 'time', side_effect=[100, 101]):
            self.assertFalse(action._hif_empty_hand(context, None, hand))
            self.assertTrue(action._hif_empty_hand(context, None, hand))
        action._reset_hif_recognition()
        action._is_drink_detail_open = lambda *_args: True
        with patch.object(PRODUCE.time, 'time', side_effect=[100, 101]):
            self.assertFalse(action._hif_empty_hand(context, None, hand))
            self.assertFalse(action._hif_empty_hand(context, None, hand))

    def test_dialog_failure_is_reported_as_failed_action(self):
        action = self.action()
        action._run_battle = lambda *_args: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError('cancel failed'))
        self.assertFalse(action.run(None, None))

    def test_changed_dialog_cannot_receive_compatibility_click(self):
        action, context = self.action(), self.context()
        action._find_drink_detail_actions = lambda *_args: {'use': [1, 1, 1, 1], 'cancel': [2, 2, 1, 1]}
        identities = iter([('ブーストエキス', ''), ('センブリソーダ', '')])
        action._read_battle_drink_identity = lambda *_args: next(identities)
        action._wait_for_drink_detail = lambda *_args, **_kw: False
        clicks = []
        action._click_drink_detail_action = lambda _ctx, name, **kw: clicks.append(name)
        action._abort_drink_flow = lambda _ctx, reason: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError(reason))
        with self.assertRaises(PRODUCE.HifDrinkFlowError):
            action._use_drink(context)
        self.assertEqual(clicks, ['use'])

    def test_unknown_hand_timeout_cannot_skip_in_drink_dialog(self):
        action, context = self.action(), self.context()
        action._hif_empty_started_at = 0
        action._is_drink_detail_open = lambda *_args: True
        with patch.object(PRODUCE.time, 'time', return_value=100):
            self.assertFalse(action._hif_empty_hand(context, object(), []))
        self.assertIsNone(action._hif_empty_started_at)

    def test_button_disappearing_before_click_never_uses_blind_coordinate(self):
        for compatibility in (False, True):
            action, context = self.action(), self.context()
            action._find_drink_detail_actions = lambda *_args: {}
            context.tasker.controller.post_click = lambda *_args: self.fail('Clicked after dialog buttons disappeared')
            self.assertFalse(action._click_drink_detail_action(context, 'use', compatibility=compatibility))

    def test_unusable_cards_require_stable_battle_hud(self):
        action, context = self.action(), self.context()
        context.run_recognition = lambda name, *_args: NS(hit=name.endswith('SkipRound'))
        action._is_drink_detail_open = lambda *_args: False
        hand = [NS(label='useless', box=[268, 884, 184, 250], score=.957)]
        action._read_turn_count = lambda *_args: None
        with patch.object(PRODUCE.time, 'time', side_effect=[100, 101]):
            self.assertFalse(action._hif_empty_hand(context, None, hand))
            self.assertFalse(action._hif_empty_hand(context, None, hand))

    def test_template_region_keeps_full_size_inside_image(self):
        for box, expected_x in [([18, 884, 180, 250], 0), ([514, 884, 182, 248], 480)]:
            with self.subTest(box=box):
                action, context = self.action(), self.context()
                action.cards_list = [{'key': 'a', 'template': 'a.png'}]
                action._identify_by_name = lambda *_args: None
                regions = []
                def reco(name, image, pipeline_override):
                    regions.append(pipeline_override[name]['roi'])
                    return NS(hit=False)
                context.run_recognition = reco
                action._identify_card(context, NS(shape=(1280, 720, 3)), box)
                self.assertEqual(regions, [[expected_x, 864, 240, box[3]+40]])

    def test_drink_return_has_no_fixed_three_second_sleep(self):
        action, context = self.action(), self.context()
        context.run_recognition = lambda name, *_args: NS(hit=name.endswith('SkipRound'))
        action._is_drink_detail_open = lambda *_args: False
        sleeps = []
        with patch.object(PRODUCE.time, 'sleep', side_effect=sleeps.append):
            self.assertTrue(action._wait_for_battle_drink_return(context))
        self.assertEqual(sleeps, [action.DRINK_LIST_STABLE_POLL_INTERVAL])

    def test_drink_bar_requires_reduced_count_before_stability(self):
        for counts, expected in [([4, 4, 3, 3, 3], True), ([4] * 30, False)]:
            with self.subTest(counts=counts[:5]):
                action, context = self.action(), self.context()
                frames = iter(counts)
                seen = []
                def recognize(*_args):
                    count = next(frames)
                    seen.append(count)
                    return NS(hit=True, filtered_results=[NS(box=[i*100, 1100, 50, 50]) for i in range(count)])
                context.run_recognition = recognize
                now = [0.0]
                def sleep(seconds): now[0] += seconds
                with patch.object(PRODUCE.time, 'time', side_effect=lambda: now[0]), patch.object(PRODUCE.time, 'sleep', side_effect=sleep):
                    self.assertEqual(action._wait_for_drink_list_stable(context, after_use=False, expected_count=3), expected)
                if expected:
                    self.assertEqual(seen, [4, 4, 3, 3, 3])
                    self.assertLess(now[0], 3)

    def test_selected_card_index_uses_stable_slot_when_neighbor_missing(self):
        action, context = self.action(), self.context()
        boxes = [([20+i*125, 884, 140, 250], .95) for i in range(5)]
        action._hif_bind_hand(boxes)
        missing = [boxes[0], *boxes[2:]]
        action._hif_has_select = lambda _ctx, _img, box: box == boxes[4][0]
        self.assertEqual(action._hif_selected_index(context, object(), missing), 4)

    def test_play_confirmed_target_is_not_blocked_by_missing_neighbor(self):
        action, context = self.action(), self.context()
        boxes = [([20+i*125, 884, 140, 250], .95) for i in range(5)]
        action._hif_bind_hand(boxes)
        missing = [boxes[0], *boxes[2:]]
        action._hif_frame_boxes = lambda *_args: missing
        clicks = []
        context.tasker.controller.post_click = lambda x, y: clicks.append((x, y)) or NS(job_id=1, succeeded=True, wait=lambda: None)
        context.run_recognition = lambda *_args: NS(hit=True)
        action._hif_has_select = lambda _ctx, _img, box: not clicks and box == boxes[4][0]
        action._hif_panel_top = lambda *_args: 584 if not clicks else None
        action._wait_until_playable = lambda *_args, **_kw: True
        with patch.object(PRODUCE.time, 'sleep'):
            self.assertTrue(action._play_hif_card(context, boxes[4][0]))
        self.assertEqual(clicks, [(590, 1009)])
