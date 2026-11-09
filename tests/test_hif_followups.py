from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from test_hif_behavior import PRODUCE, COMMON
from extensions.hif.followups import Followups, validate_sequences


def rule(targets=('B', 'C'), before=(), after=(), waiting=(), per=None, **options):
    """规范化后的出牌编排（与 validate_sequences 的输出一致）。"""
    per = per or {}
    return {'targets': list(targets), 'wait_turns': 2, 'wait_after_success': False, 'target_policy': 'normal',
            'drinks': {'before': list(before), 'after': list(after), 'waiting': list(waiting),
                       'targets': {target: {'before': list(per.get(target, {}).get('before', ())),
                                            'after': list(per.get(target, {}).get('after', ()))} for target in targets}},
            **options}


def hand(*keys):
    return [{'key': key, 'box': [100 + i * 200, 884, 180, 250], 'conf': 0.9, 'score': 1}
            for i, key in enumerate(keys)]


class FollowupTests(unittest.TestCase):
    def action(self, rules=None):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.priority_index = {'A': 0, 'C': 1, 'B': 2, 'D': 3}
        action._last_turn_count = 6
        rules = rules if rules is not None else {'A': rule()}
        action._sequences = rules
        action._followups = Followups({key: value for key, value in rules.items() if value['targets']})
        action._hif_priority_source = lambda *_a: 'test'
        return action

    def commit(self, action, decision):
        action._followups.committed(decision['key'], decision.get('followup_role'))

    def test_source_priority_is_unchanged_and_ordered_followup_overrides_normal_rank(self):
        action = self.action()
        decision = action._decide(None, hand('A', 'B', 'C'))
        self.assertEqual((decision['key'], decision['followup_role']), ('A', 'start'))
        self.assertIsNone(action._followups.active)  # recognition/decision is not a committed play
        self.commit(action, decision)
        decision = action._decide(None, hand('C', 'B'))
        self.assertEqual((decision['key'], decision['followup_role'], decision['followup_source']), ('B', 'target', 'A'))
        self.assertNotIn('A', action._followups.successful)
        self.commit(action, decision)
        self.assertIn('A', action._followups.successful)
        self.assertIsNone(action._followups.active)

    def test_single_card_sequences_do_not_start_combos(self):
        action = self.action({'A': rule((), before=('初星湯',))})
        self.assertEqual(action._followups.rules, {})
        decision = action._decide(None, hand('A', 'B'))
        self.assertEqual(decision['key'], 'A')
        self.assertNotIn('followup_role', decision)

    def test_repeated_source_after_success_waits_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                action = self.action({'A': rule(wait_after_success=enabled, waiting=('初星黒酢',))})
                state = action._followups
                state.successful.add('A')
                state.committed('A', 'start')
                calls = []
                action._consume_sequence_drinks = lambda _c, names, *_a: calls.append(names) or False
                decision = action._decide(None, hand('D'))
                self.assertEqual(decision['type'], 'skip' if enabled else 'play')
                self.assertEqual(calls, [['初星黒酢']] if enabled else [])
                self.assertEqual(state.active, 'A' if enabled else None)
                state.committed('A', 'start')
                self.assertEqual(action._decide(None, hand('B'))['key'], 'B')

    def test_wait_limit_releases_without_success_and_new_source_can_try_again(self):
        action = self.action()
        state = action._followups
        state.committed('A', 'start')
        self.assertEqual(action._decide(None, hand('D'))['type'], 'skip')
        action._followup_skip_turn = 6
        action._observe_followup_turn(None)
        action._observe_followup_turn(6)
        self.assertEqual(state.skips, 0)
        action._observe_followup_turn(5)
        self.assertEqual(state.skips, 1)
        action._observe_followup_turn(5)
        self.assertEqual(state.skips, 1)
        action._followup_skip_turn = 5
        action._observe_followup_turn(4)
        self.assertIsNone(state.active)
        self.assertFalse(state.successful)
        state.committed('A', 'start')
        self.assertTrue(state.full_wait)

    def test_target_policies_before_after_success_and_timeout(self):
        for policy in ('normal', 'reserve_first', 'combo_only'):
            for result in ('success', 'timeout'):
                with self.subTest(policy=policy, result=result):
                    action = self.action({'A': rule(target_policy=policy)})
                    state = action._followups
                    self.assertEqual(state.ordinary_allowed('B'), policy == 'normal')
                    state.committed('A', 'start')
                    self.assertEqual(action._decide(None, hand('B'))['followup_role'], 'target')
                    if result == 'success': state.committed('B', 'target')
                    else: state.release()
                    self.assertEqual(state.ordinary_allowed('B'), policy != 'combo_only')
                    self.assertEqual(state.ordinary_allowed(None), policy != 'combo_only')
                    self.assertEqual(Followups(state.rules).ordinary_allowed('B'), policy == 'normal')

    def test_unconfirmed_skip_or_unknown_round_does_not_issue_more_skips(self):
        action = self.action()
        action._followups.committed('A', 'start')
        action._followup_skip_turn = 6
        self.assertEqual(action._decide(None, hand('D'))['type'], 'retry')
        action._observe_followup_turn(5)
        self.assertEqual(action._decide(None, hand('D'))['type'], 'skip')
        action._last_turn_count = None
        self.assertEqual(action._decide(None, hand('D'))['type'], 'retry')
        action._followup_skip_deadline = 0
        with self.assertRaises(PRODUCE.HifDrinkFlowError): action._decide(None, hand('D'))

    def test_target_restrictions_are_not_bypassed_and_repeat_source_keeps_waiting_budget(self):
        action = self.action()
        state = action._followups
        state.committed('A', 'start')
        state.waiting_attempted = True
        state.skips = 1
        action.use_conditions = {'B': [{'remaining_turns_lte': 1}]}
        action._last_turn_count = 5
        self.assertEqual(action._decide(None, hand('B', 'A'))['followup_role'], 'repeat')
        state.committed('A', 'repeat')
        self.assertEqual(state.skips, 0)
        self.assertTrue(state.waiting_attempted)
        self.assertEqual(action._decide(None, hand('B', 'C'))['key'], 'C')

    def test_full_power_presence_conditions_use_real_hand_even_when_finisher_reserved(self):
        action = self.action({'A': rule(('羽ばたけ！+',), target_policy='combo_only')})
        action.profession = '全力'
        action.priority_index['アイドルになります+'] = 0
        decision = action._decide(None, hand('アイドルになります+', '羽ばたけ！+', 'D'))
        self.assertEqual(decision['key'], 'アイドルになります+')
        action.use_conditions = {'羽ばたけ！+': [{'remaining_turns_lte': 1}]}
        action._last_turn_count = 5
        self.assertEqual(action._decide(None, hand('アイドルになります+', '羽ばたけ！+', 'D'))['key'], 'アイドルになります+')

    def test_zero_wait_and_waiting_drinks_once_per_chain_in_order(self):
        action = self.action({'A': rule(wait_turns=0, waiting=('初星黒酢', '初星湯'))})
        action._followups.committed('A', 'start')
        calls = []
        action._consume_battle_drinks = lambda _c, names, *_a, **kw: calls.append((names, action._followup_move_targets, kw)) or False
        decision = action._decide(None, hand('D'))
        self.assertEqual(decision['key'], 'D')
        # 等待用饮按顺序各用一瓶；用饮后的选卡页按后续候选选卡。
        options = {'max_uses': 1, 'remember_absent': True}
        self.assertEqual(calls, [(['初星黒酢'], ['B', 'C'], options), (['初星湯'], ['B', 'C'], options)])
        self.assertIsNone(action._followup_move_targets)
        self.assertIsNone(action._followups.active)
        action._followups.committed('A', 'start')
        action._followups.waiting_attempted = True
        action._decide(None, hand('D'))
        self.assertEqual(len(calls), 2)

    def test_one_active_group_and_target_does_not_start_another_group(self):
        action = self.action({'A': rule(('B',)), 'B': rule(('C',))})
        action._followups.committed('A', 'start')
        self.commit(action, action._decide(None, hand('B', 'C')))
        self.assertIsNone(action._followups.active)
        self.assertEqual(action._followups.successful, {'A'})

    def _battle_context(self, action, state, identify, play, turns=lambda: 9):
        box = [100, 884, 180, 250]
        result = NS(label='cards', box=box, score=0.95)
        job = NS(wait=lambda: NS(get=lambda: object()))
        context = NS(tasker=NS(stopping=False, controller=NS(post_screencap=lambda: job)), get_node_data=lambda *_a: {})
        context.run_recognition = lambda name, *_a, **_kw: NS(
            hit=name in ('ProduceHIF__ProduceRecognitionSkipRound', 'ProduceHIF__ProduceRecognitionCards'), all_results=[result])
        sequences = dict(state.rules)
        action._load_config = lambda *_a: None
        action._load_hif_battle_drink_policy = lambda: None

        def load_sequences():
            action._sequences = getattr(self, 'sequences', sequences)
            action._followups = state
        action._load_hif_sequences = load_sequences
        action._wait_until_playable = lambda *_a, **_kw: True
        action._detect_battle_number = lambda *_a: 1

        def read_turn(*_a):
            action._last_turn_count = turns()
            return action._last_turn_count
        action._read_turn_count = read_turn
        action._is_battle_end = lambda *_a, **_kw: False
        action._handle_move_cards = lambda *_a: False
        action._is_drink_detail_open = lambda *_a: False
        action._process_battle_drink_timing = lambda *_a, **_kw: False
        action._hif_card_boxes = lambda *_a: [(box, 0.95)]
        action._hif_bind_hand = lambda *_a: None
        action._confirm_card_hand = lambda *_a: 'ready'
        action._hif_probe_unknowns = lambda *_a, **_kw: False
        action._identify_card = identify
        action._play_a_card = play
        return context

    def test_battle_loop_commits_only_confirmed_plays_and_clears_on_stop(self):
        action = self.action()
        state = Followups({'A': rule(('B',))})
        attempted = []
        skip_markers = []
        context = None

        def play(*_args):
            attempted.append((state.active, set(state.successful)))
            skip_markers.append(action._followup_skip_turn)
            if len(attempted) == 2: action._followup_skip_turn = 9
            if len(attempted) == 4: context.tasker.stopping = True
            return len(attempted) in (2, 4)
        context = self._battle_context(action, state, lambda *_a: ('A' if state.active is None else 'B', 1), play)
        with patch.object(COMMON.time, 'sleep', return_value=None):
            self.assertTrue(action.run(context, NS(node_name='ProduceHIF__ProduceHIFCardsFlag')))
        self.assertEqual([active for active, _ in attempted], [None, None, 'A', 'A'])
        self.assertTrue(all(not success for _, success in attempted))
        self.assertEqual(state.successful, {'A'})
        self.assertIsNone(skip_markers[2])  # a failed old SKIP cannot count a later card transition
        self.assertIsNone(action._followups)
        self.assertIsNone(action._paired_card)

    def test_battle_loop_uses_after_drinks_only_when_the_same_turn_continues(self):
        for same_turn in (True, False):
            with self.subTest(same_turn=same_turn):
                action = self.action()
                state = Followups({})
                self.sequences = {'A': rule((), after=('ブーストエキス',))}
                turns = [9]
                drinks = []
                context = None

                def play(*_args):
                    if not same_turn:
                        turns[0] = 8  # 打出后回合结束
                    if len(drinks) or len(plays) >= 1:
                        context.tasker.stopping = True
                    plays.append(1)
                    return True
                plays = []
                context = self._battle_context(action, state, lambda *_a: ('A', 1), play, turns=lambda: turns[0])
                action._consume_sequence_drinks = lambda _c, names, label, targets=None: drinks.append((names, label)) or False
                context.tasker.stopping = False
                with patch.object(COMMON.time, 'sleep', return_value=None):
                    action.run(context, NS(node_name='ProduceHIF__ProduceHIFCardsFlag'))
                del self.sequences
                if same_turn:
                    self.assertEqual(drinks, [(['ブーストエキス'], 'HIF出牌编排「A」后置')])
                else:
                    self.assertEqual(drinks, [])
                self.assertIsNone(action._sequence_after)

    def test_strict_policy_blocks_fallback_even_for_unknown_identity(self):
        action = self.action({'A': rule(target_policy='combo_only')})
        action._get_card_info = lambda *_a: (1, 0, 1, [100, 884, 180, 250], [])
        action._skip_round = lambda *_a: None
        for key in ('B', None):
            action._identify_card = lambda *_a: (key, 1)
            action._play_a_card = lambda *_a: self.fail('exclusive target bypassed fallback')
            action._play_fallback(None, object(), [])

    def test_before_drinks_use_in_order_then_lock_fresh_target_until_play(self):
        action = self.action({'C': rule((), before=('特製ハツボシエキス', '初星湯'))})
        calls = []
        action._consume_battle_drinks = lambda _c, names, *_a, **kw: calls.append((names, kw)) or names == ['特製ハツボシエキス']
        decision = action._decide(None, hand('C', 'B'))
        self.assertTrue(action._prepare_sequence_before(None, decision))
        options = {'max_uses': 1, 'remember_absent': True}
        self.assertEqual(calls, [(['特製ハツボシエキス'], options), (['初星湯'], options)])
        self.assertEqual(action._decide(None, hand('B'))['type'], 'retry')
        fresh = action._decide(None, hand('B', 'C'))
        self.assertEqual(fresh['key'], 'C')
        self.assertEqual(fresh['box'][0], 300)
        self.assertFalse(action._prepare_sequence_before(None, fresh))
        self.assertEqual(len(calls), 2)
        action._paired_deadline = 0
        action._abort_drink_flow = lambda *_a: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError('timeout'))
        with self.assertRaises(PRODUCE.HifDrinkFlowError): action._decide(None, hand('B'))

    def test_before_drinks_absent_failed_or_unconfigured_card_plays_normally(self):
        for case in ('absent', 'failed', 'unconfigured'):
            with self.subTest(case=case):
                action = self.action({'B': rule((), before=('初星湯',))})
                calls = []
                action._consume_battle_drinks = lambda _c, names, *_a, **kw: calls.append(names) or False
                if case == 'failed':
                    action._sequence_before_failed.add('B')
                key = 'D' if case == 'unconfigured' else 'B'
                self.assertFalse(action._prepare_sequence_before(None, {'type': 'play', 'key': key}))
                self.assertIsNone(action._paired_card)
                self.assertEqual(len(calls), 1 if case == 'absent' else 0)

    def test_candidate_drinks_follow_the_connection_and_start_drinks_follow_the_start(self):
        action = self.action({'A': rule(('B',), before=('特製ハツボシエキス',), after=('初星湯',),
                                        per={'B': {'before': ['ブーストエキス'], 'after': ['初星水']}})})
        start = action._sequence_play_plan({'key': 'A', 'followup_role': 'start'})
        self.assertEqual((start['before'], start['after'], start['move_targets']),
                         (['特製ハツボシエキス'], ['初星湯'], ['B']))
        target = action._sequence_play_plan({'key': 'B', 'followup_role': 'target', 'followup_source': 'A'})
        self.assertEqual((target['before'], target['after'], target['move_targets']), (['ブーストエキス'], ['初星水'], None))
        self.assertIsNone(action._sequence_play_plan({'key': 'B'}))  # 普通打出不用衔接位置的饮料
        repeat = action._sequence_play_plan({'key': 'A', 'followup_role': 'repeat'})
        self.assertEqual(repeat['before'], ['特製ハツボシエキス'])

    def test_after_drinks_wait_for_the_turn_and_skip_once_it_changes(self):
        action = self.action({'A': rule((), after=('初星湯',))})
        drank = []
        action._consume_sequence_drinks = lambda _c, names, label, targets=None: drank.append(names) or True
        action._last_turn_count = None
        action._schedule_sequence_after({'key': 'A'})
        self.assertIsNone(action._sequence_after)  # 读不到出牌时的回合就不登记
        for turn, extra, expected in ((6, False, True), (5, False, False), (6, True, False)):
            with self.subTest(turn=turn, extra=extra):
                drank.clear()
                action._last_turn_count = 6
                action._schedule_sequence_after({'key': 'A'})
                self.assertIsNone(action._process_sequence_after(None, None))  # 还在等回合数读出
                action._extra_turn_transition = extra
                self.assertEqual(action._process_sequence_after(None, turn), expected)
                self.assertEqual(drank, [['初星湯']] if expected else [])
                self.assertIsNone(action._sequence_after)
        action._extra_turn_transition = False
        action._schedule_sequence_after({'key': 'A'})
        action._sequence_after['deadline'] = 0
        self.assertFalse(action._process_sequence_after(None, None))
        self.assertIsNone(action._sequence_after)

    def test_pair_unusable_releases_lock_and_reselects_or_skips(self):
        for remaining in (hand('B'), []):
            with self.subTest(remaining=remaining):
                action = self.action({})
                action._paired_card = {'type': 'play', 'key': 'C', 'box': hand('C')[0]['box']}
                action._paired_deadline = COMMON.time.monotonic() + 100
                unusable = NS(label='useless', box=hand('C')[0]['box'], score=0.95)
                action._release_unusable_paired_card([unusable])
                self.assertIsNotNone(action._paired_card)
                action._release_unusable_paired_card([unusable])
                self.assertIsNone(action._paired_card)
                self.assertIsNone(action._paired_deadline)
                decision = action._decide(None, remaining)
                self.assertEqual(decision['type'], 'play' if remaining else 'skip')
                if remaining:
                    self.assertEqual(decision['key'], 'B')

    def test_pair_missing_or_other_unusable_card_keeps_lock(self):
        action = self.action({})
        action._paired_card = {'key': 'C', 'box': hand('C')[0]['box']}
        action._paired_deadline = COMMON.time.monotonic() + 100
        target = NS(label='useless', box=hand('C')[0]['box'], score=0.95)
        other = NS(label='useless', box=hand('C', 'B')[1]['box'], score=0.95)
        playable = NS(label='cards', box=target.box, score=0.95)
        for interruption in ([], [other], [target, playable], [NS(label='useless', box=target.box, score=0.4)]):
            action._release_unusable_paired_card([target])
            action._release_unusable_paired_card(interruption)
            action._release_unusable_paired_card([target])
            self.assertIsNotNone(action._paired_card)
            self.assertEqual(action._decide(None, hand('B'))['type'], 'retry')
            action._release_unusable_paired_card([playable])

    def test_pair_timeout_outside_playable_hud_and_unconfirmed_battle_end_stop(self):
        for ending in (False, True):
            with self.subTest(ending=ending):
                action = self.action({})
                captures = []
                context = NS(tasker=NS(stopping=False), get_node_data=lambda *_a: {})

                def capture():
                    captures.append(1)
                    if len(captures) > 2: context.tasker.stopping = True
                    if len(captures) >= 2:
                        action._paired_card = {'key': 'B'}
                        action._paired_deadline = COMMON.time.monotonic() + 100 if ending else 0
                    return object()
                context.tasker.controller = NS(post_screencap=lambda: NS(wait=lambda: NS(get=capture)))
                context.run_recognition = lambda *_a, **_kw: NS(hit=False, all_results=[])
                action._load_config = lambda *_a: None
                action._load_hif_battle_drink_policy = lambda: None
                action._load_hif_sequences = lambda: setattr(action, '_followups', Followups({}))
                action._wait_until_playable = lambda *_a, **_kw: True
                action._detect_battle_number = lambda *_a: 1
                action._is_battle_end = lambda *_a, **_kw: ending
                action._handle_move_cards = lambda *_a: False
                action._is_drink_detail_open = lambda *_a: False
                action._skip_battle_end_animation = lambda *_a: False
                action._abort_drink_flow = lambda *_a: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError('unconfirmed'))
                with patch.object(COMMON.time, 'sleep', return_value=None):
                    self.assertFalse(action.run(context, NS(node_name='ProduceHIF__ProduceHIFCardsFlag')))
                self.assertGreaterEqual(len(captures), 3)
                self.assertIsNone(action._paired_card)

    def test_sequence_drinks_keep_normal_timing_unless_combo_only(self):
        action = self.action({'A': rule((), before=('特製ハツボシエキス', '初星黒酢'))})
        action._drink_imported_names = ['特製ハツボシエキス', '初星黒酢']
        action._drink_default_timing = {'mode': 'remaining_turn', 'turn': 4}
        action._drink_specific_timings = {'初星黒酢': {'mode': 'combo_only'}}
        due = action._due_battle_drink_names(4)
        self.assertIn('特製ハツボシエキス', due)
        self.assertNotIn('初星黒酢', due)
        self.assertNotIn('初星黒酢', action._due_battle_drink_names(9, first_turn=True))
        self.assertTrue(action._valid_battle_drink_timing({'mode': 'combo_only'}, allow_combo_only=True))
        self.assertFalse(action._valid_battle_drink_timing({'mode': 'combo_only', 'turn': 4}, allow_combo_only=True))

    def test_validation_normalizes_and_rejects_bad_sequences(self):
        cards, drinks = {'A', 'B', 'C'}, {'初星湯', '初星水'}

        def raw(targets=('B',), drinks_value=None, **options):
            value = {'targets': list(targets), 'wait_turns': 2, 'wait_after_success': False, 'target_policy': 'normal', **options}
            if drinks_value is not None:
                value['drinks'] = drinks_value
            return value
        invalid = [
            {'A': raw(('A',))}, {'A': raw(('B', 'B'))}, {'A': raw(('missing',))},
            {'A': raw(('B',)), 'B': raw(('A',))}, {'A': raw(wait_turns=True)}, {'A': raw(wait_turns=7)},
            {'A': raw(wait_after_success=1)}, {'A': raw(target_policy='invalid')},
            {'A': raw(drinks_value={'before': ['未知饮料']})}, {'A': raw(drinks_value={'before': ['初星湯', '初星湯']})},
            {'A': raw(drinks_value={'targets': {'C': {'before': ['初星湯']}}})},
            {'A': raw(drinks_value={'targets': {'B': {'waiting': ['初星湯']}}})},
            {'A': raw((), drinks_value={'waiting': ['初星湯']})}, {'A': raw(())},
            {'A': raw(drinks_value={'later': []})}, {'A': raw(drinks_value=['初星湯'])},
        ]
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                validate_sequences(rules, cards, drinks)
        result = validate_sequences({'A': raw(('B',)), 'B': raw(('C',)), 'C': raw((), drinks_value={'after': ['初星水']})},
                                    cards, drinks)
        self.assertEqual(result['A']['drinks'], {'before': [], 'after': [], 'waiting': [], 'targets': {'B': {'before': [], 'after': []}}})
        self.assertEqual(result['C']['drinks']['after'], ['初星水'])
        self.assertEqual(result['C']['targets'], [])


if __name__ == '__main__': unittest.main()
