import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from test_hif_behavior import PRODUCE, ROOT
from extensions.hif.followups import Followups, legacy_rule, validate_rules

sys.path.insert(0, str(ROOT / 'tools'))
from hif_followup_migration import migrate_followups


def rule(targets=('B', 'C'), **options):
    return {'targets': list(targets), 'wait_turns': 2, 'use_black_vinegar': False,
            'wait_after_success': False, 'target_policy': 'normal', **options}


def hand(*keys):
    return [{'key': key, 'box': [100 + i * 200, 884, 180, 250], 'conf': 0.9, 'score': 1}
            for i, key in enumerate(keys)]


class FollowupTests(unittest.TestCase):
    def action(self, rules=None):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action.combo_first = action.combo_second = None
        action.priority_index = {'A': 0, 'C': 1, 'B': 2, 'D': 3}
        action._last_turn_count = 6
        action._followups = Followups(rules if rules is not None else {'A': rule()})
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
        self.assertEqual((decision['key'], decision['followup_role']), ('B', 'target'))
        self.assertNotIn('A', action._followups.successful)
        self.commit(action, decision)
        self.assertIn('A', action._followups.successful)
        self.assertIsNone(action._followups.active)

    def test_repeated_source_after_success_waits_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                action = self.action({'A': rule(wait_after_success=enabled, use_black_vinegar=True)})
                state = action._followups
                state.successful.add('A')
                state.committed('A', 'start')
                calls = []
                action._drink_brown_bottle = lambda *_a: calls.append('vinegar') or False
                decision = action._decide(None, hand('D'))
                self.assertEqual(decision['type'], 'skip' if enabled else 'play')
                self.assertEqual(calls, ['vinegar'] if enabled else [])
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

    def test_target_restrictions_are_not_bypassed_and_repeat_source_keeps_vinegar_budget(self):
        action = self.action()
        state = action._followups
        state.committed('A', 'start')
        state.vinegar_attempted = True
        state.skips = 1
        action.use_conditions = {'B': [{'remaining_turns_lte': 1}]}
        action._last_turn_count = 5
        self.assertEqual(action._decide(None, hand('B', 'A'))['followup_role'], 'repeat')
        state.committed('A', 'repeat')
        self.assertEqual(state.skips, 0)
        self.assertTrue(state.vinegar_attempted)
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

    def test_zero_wait_and_vinegar_only_once_per_chain(self):
        action = self.action({'A': rule(wait_turns=0, use_black_vinegar=True)})
        action._followups.committed('A', 'start')
        calls = []
        action._consume_battle_drinks = lambda *_a, **_kw: calls.append(action._followup_move_targets) or False
        decision = action._decide(None, hand('D'))
        self.assertEqual(decision['key'], 'D')
        self.assertEqual(calls, [['B', 'C']])
        self.assertIsNone(action._followups.active)

    def test_one_active_group_and_target_does_not_start_another_group(self):
        action = self.action({'A': rule(('B',)), 'B': rule(('C',))})
        action._followups.committed('A', 'start')
        self.commit(action, action._decide(None, hand('B', 'C')))
        self.assertIsNone(action._followups.active)
        self.assertEqual(action._followups.successful, {'A'})

    def test_battle_loop_commits_only_confirmed_plays_and_clears_on_stop(self):
        action = self.action()
        state = Followups({'A': rule(('B',))})
        box = [100, 884, 180, 250]
        result = NS(label='cards', box=box, score=0.95)
        job = NS(wait=lambda: NS(get=lambda: object()))
        context = NS(tasker=NS(stopping=False, controller=NS(post_screencap=lambda: job)), get_node_data=lambda *_a: {})
        context.run_recognition = lambda name, *_a, **_kw: NS(
            hit=name in ('ProduceHIF__ProduceRecognitionSkipRound', 'ProduceHIF__ProduceRecognitionCards'), all_results=[result])
        action._load_config = lambda *_a: None
        action._load_hif_battle_drink_policy = lambda: None
        action._load_hif_followups = lambda: setattr(action, '_followups', state)
        action._wait_until_playable = lambda *_a, **_kw: True
        action._detect_battle_number = lambda *_a: 1
        action._read_turn_count = lambda *_a: 9
        action._is_battle_end = lambda *_a, **_kw: False
        action._handle_move_cards = action._handle_star_get = lambda *_a: False
        action._is_drink_detail_open = lambda *_a: False
        action._process_battle_drink_timing = lambda *_a, **_kw: False
        action._hif_card_boxes = lambda *_a: [(box, 0.95)]
        action._hif_bind_hand = lambda *_a: None
        action._confirm_card_hand = lambda *_a: 'ready'
        action._hif_probe_unknowns = lambda *_a: False
        action._identify_card = lambda *_a: ('A' if state.active is None else 'B', 1)
        attempted = []
        skip_markers = []
        def play(*_args):
            attempted.append((state.active, set(state.successful)))
            skip_markers.append(action._followup_skip_turn)
            if len(attempted) == 2: action._followup_skip_turn = 9
            if len(attempted) == 4: context.tasker.stopping = True
            return len(attempted) in (2, 4)
        action._play_a_card = play
        with patch.object(PRODUCE.time, 'sleep', return_value=None):
            self.assertTrue(action.run(context, NS(node_name='ProduceHIF__ProduceHIFCardsFlag')))
        self.assertEqual([active for active, _ in attempted], [None, None, 'A', 'A'])
        self.assertTrue(all(not success for _, success in attempted))
        self.assertEqual(state.successful, {'A'})
        self.assertIsNone(skip_markers[2])  # a failed old SKIP cannot count a later card transition
        self.assertIsNone(action._followups)
        self.assertIsNone(action._paired_card)

    def test_strict_policy_blocks_fallback_even_for_unknown_identity(self):
        action = self.action({'A': rule(target_policy='combo_only')})
        action._get_card_info = lambda *_a: (1, 0, 1, [100, 884, 180, 250], [])
        action._skip_round = lambda *_a: None
        for key in ('B', None):
            action._identify_card = lambda *_a: (key, 1)
            action._play_a_card = lambda *_a: self.fail('exclusive target bypassed fallback')
            action._play_fallback(None, object(), [])

    def test_paired_drink_uses_one_then_locks_fresh_target_until_play(self):
        action = self.action({})
        action._drink_before_targets = ['B', 'C']
        calls = []
        action._consume_battle_drinks = lambda *_a, **kw: calls.append(kw) or True
        decision = action._decide(None, hand('C', 'B'))
        self.assertTrue(action._prepare_paired_drink(None, decision))
        self.assertEqual(calls, [{'max_uses': 1}])
        self.assertEqual(action._decide(None, hand('B'))['type'], 'retry')
        fresh = action._decide(None, hand('B', 'C'))
        self.assertEqual(fresh['key'], 'C')
        self.assertEqual(fresh['box'][0], 300)
        self.assertFalse(action._prepare_paired_drink(None, fresh))
        self.assertEqual(len(calls), 1)
        action._paired_deadline = 0
        action._abort_drink_flow = lambda *_a: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError('timeout'))
        with self.assertRaises(PRODUCE.HifDrinkFlowError): action._decide(None, hand('B'))

    def test_pair_disabled_absent_or_active_combo_does_not_change_normal_play(self):
        for case in ('absent', 'disabled', 'combo'):
            action = self.action()
            action._drink_before_targets = ['B']
            calls = []
            action._consume_battle_drinks = lambda *_a, **kw: calls.append(kw) or False
            if case == 'disabled': action._drink_disabled_names.add('特製ハツボシエキス')
            if case == 'combo': action._followups.committed('A', 'start')
            self.assertFalse(action._prepare_paired_drink(None, {'type': 'play', 'key': 'B'}))
            self.assertEqual(len(calls), 1 if case == 'absent' else 0)

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
                        action._paired_deadline = PRODUCE.time.monotonic() + 100 if ending else 0
                    return object()
                context.tasker.controller = NS(post_screencap=lambda: NS(wait=lambda: NS(get=capture)))
                context.run_recognition = lambda *_a, **_kw: NS(hit=False, all_results=[])
                action._load_config = lambda *_a: None
                action._load_hif_battle_drink_policy = lambda: None
                action._load_hif_followups = lambda: setattr(action, '_followups', Followups({}))
                action._wait_until_playable = lambda *_a, **_kw: True
                action._detect_battle_number = lambda *_a: 1
                action._is_battle_end = lambda *_a, **_kw: ending
                action._handle_move_cards = action._handle_star_get = lambda *_a: False
                action._is_drink_detail_open = lambda *_a: False
                action._skip_battle_end_animation = lambda *_a: False
                action._abort_drink_flow = lambda *_a: (_ for _ in ()).throw(PRODUCE.HifDrinkFlowError('unconfirmed'))
                with patch.object(PRODUCE.time, 'sleep', return_value=None):
                    self.assertFalse(action.run(context, NS(node_name='ProduceHIF__ProduceHIFCardsFlag')))
                self.assertEqual(len(captures), 2)
                self.assertIsNone(action._paired_card)

    def test_bound_drink_is_excluded_from_timed_batch(self):
        action = self.action({})
        action._drink_imported_names = ['特製ハツボシエキス']
        action._drink_default_timing = {'mode': 'remaining_turn', 'turn': 4}
        action._drink_before_targets = ['B']
        self.assertEqual(action._due_battle_drink_names(4), [])
        action._drink_before_targets = []
        self.assertEqual(action._due_battle_drink_names(4), ['特製ハツボシエキス'])

    def test_validation_rejects_cycles_duplicates_self_unknown_and_bad_values(self):
        invalid = [{'A': rule(('A',))}, {'A': rule(('B','B'))}, {'A': rule(('missing',))},
                   {'A': rule(('B',)), 'B': rule(('A',))}, {'A': rule(wait_turns=True)},
                   {'A': rule(wait_turns=7)}, {'A': rule(use_black_vinegar=1)}, {'A': rule(target_policy='invalid')}]
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ValueError): validate_rules(rules, {'A', 'B', 'C'})
        validate_rules({'A': rule(('B',)), 'B': rule(('C',))}, {'A','B','C'})


class MigrationTests(unittest.TestCase):
    def fixture(self, root, values):
        config = {'combo': {'first':'A','second':'B'}, 'skip_limit':4,
                  'priority_profiles': {'集中':[{'key':'A'}, {'key':'B'}]}, 'private': {'keep':True}}
        path = root / 'config/hif/cards_priority.json'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps(config), encoding='utf-8')
        directory = root / 'config/instances'; directory.mkdir(parents=True)
        for i, value in enumerate(values):
            (directory / f'{i}.json').write_text(json.dumps({'connection':'keep', 'TaskItems':[
                {'entry':'ProduceHIF','option':[{'name':'HIF.国民脚光等待回合','data':{'combo_wait_turns':str(value)}}]},
                {'entry':'Produce','option':[{'name':'other','data':{'keep':True}}]}]}), encoding='utf-8')
        return path

    def test_migration_backs_up_and_is_idempotent_and_keeps_private_values(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            root = Path(directory); path = self.fixture(root, [2])
            before = path.read_bytes()
            self.assertTrue(migrate_followups(root)['changed'])
            after = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(after['followup_profiles']['集中']['A']['wait_turns'], 2)
            self.assertEqual(after['followup_profiles']['集中']['A']['target_policy'], 'reserve_first')
            self.assertEqual(after['private'], {'keep':True})
            self.assertNotIn('combo', after)
            self.assertNotIn('skip_limit', after)
            self.assertEqual((root/'backup/followups-before-migration/config/hif/cards_priority.json').read_bytes(), before)
            data = json.loads((root/'config/instances/0.json').read_text(encoding='utf-8'))
            self.assertEqual(data['connection'], 'keep')
            self.assertEqual(data['TaskItems'][0]['option'], [])
            self.assertEqual(data['TaskItems'][1]['option'][0]['name'], 'other')
            self.assertFalse(migrate_followups(root)['changed'])

    def test_conflicting_waits_do_not_write_until_explicit_choice(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            root = Path(directory); path = self.fixture(root, [1, 5])
            before = path.read_bytes()
            self.assertEqual(migrate_followups(root)['conflicts'], {'集中':[1,5]})
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root/'backup').exists())
            self.assertTrue(migrate_followups(root, {'集中':5})['changed'])
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['followup_profiles']['集中']['A']['wait_turns'], 5)

    def test_empty_profile_disables_legacy_and_fallback_wait_is_three(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            root = Path(directory); path = self.fixture(root, [])
            config = json.loads(path.read_text(encoding='utf-8')); config['followup_profiles']={'集中':{}}
            path.write_text(json.dumps(config))
            migrate_followups(root)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['followup_profiles']['集中'], {})
        self.assertEqual(legacy_rule({'combo':{'first':'A','second':'B'}, 'priority_profiles':{'集中':[{'key':'A'},{'key':'B'}]}}, '集中')['A']['wait_turns'], 3)


if __name__ == '__main__': unittest.main()
