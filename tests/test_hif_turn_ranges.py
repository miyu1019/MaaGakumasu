"""Profession isolation, inclusive turn intervals, and extra-turn counter rollover."""
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from test_hif_behavior import PRODUCE, ROOT


class TurnRangeTest(unittest.TestCase):
    def test_each_profession_loads_its_own_rule_and_selects_by_inclusive_interval(self):
        config = json.loads((ROOT / 'extensions/hif/defaults/cards_priority.json').read_text(encoding='utf-8'))
        professions = ('好调', '集中', '好印象', '元気', '全力', '強気')
        config['conditional_priority_profiles'] = {p: {'脚光+': {
            'priority': i, 'remaining_turns_gte': 3, 'remaining_turns_lte': 6, 'mode': 'any'
        }} for i, p in enumerate(professions)}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'cards.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            for i, profession in enumerate(professions):
                action = PRODUCE.ProduceHIF__ProduceCardsAuto()
                with patch.object(PRODUCE, 'CARDS_PRIORITY_CONFIG_PATH', str(path)):
                    action._load_config(profession)
                action._hif_recognition = True
                action.combo_wait_enabled = False
                action.priority_index = {'脚光+': 20, '存在感+': 10}
                hand = [{'key': key, 'box': [0, 0, 20, 40], 'conf': 1} for key in action.priority_index]
                for turns in (2, 3, 6, 7, None):
                    action._last_turn_count = turns
                    expected = '脚光+' if turns in (3, 6) else '存在感+'
                    self.assertEqual(action._decide_legacy(None, hand)['key'], expected, (profession, turns))
                self.assertEqual(action.conditional_priority_rules['脚光+']['priority'], i)

    def test_bad_range_and_unknown_turns_do_not_override_base(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.priority_index = {'a': 9}
        for rule, turns in (({'remaining_turns_gte': 6, 'remaining_turns_lte': 3}, 4),
                            ({'remaining_turns_gte': True}, 4), ({'remaining_turns_gte': 3}, None)):
            action.conditional_priority_rules = {'a': {'priority': 0, **rule}}
            self.assertEqual(action._full_power_priority({'key': 'a'}, turns)[1], 9)
        action.conditional_priority_rules = {'a': {'priority': 0, 'remaining_turns_gte': 3}}
        self.assertEqual(action._full_power_priority({'key': 'a'}, 3)[1], 0)

    def test_extra_badge_is_independent_and_requires_plus_and_white_blue_pixels(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        x, y, w, h = action.EXTRA_TURN_ROI
        image[y:y+h, x:x+w] = 255
        image[y+5:y+10, x+5:x+12] = (255, 190, 0)
        for text, expected in (('+1', 1), ('＋ 3', 3), ('8', None), ('+0', None), ('+101', None)):
            calls = []
            context = SimpleNamespace(run_recognition=lambda *args, **kw: calls.append((args, kw)) or SimpleNamespace(
                hit=True, filtered_results=[SimpleNamespace(text=text, box=[x, y, w, h])]))
            with patch.object(PRODUCE, 'np', np):
                self.assertEqual(action._read_extra_turn_count(context, image), expected)
            self.assertEqual(calls[0][1]['pipeline_override']['ProduceHIF__ProduceExtraTurnNumber']['roi'], [103, 126, 46, 28])
        with patch.object(PRODUCE, 'np', np):
            self.assertEqual(action._read_extra_turn_count(None, np.zeros_like(image)), 0)

    def test_rollover_requires_two_frames_and_can_confirm_same_number_skip(self):
        for extra in (1, 2, 3):
            action = PRODUCE.ProduceHIF__ProduceCardsAuto()
            action._hif_recognition = True
            action._last_turn_count = 1
            action._pending_extra_turns = extra
            action._read_extra_turn_count = lambda *_args: 0
            action._looks_like_turn_seven = lambda *_args: False
            context = SimpleNamespace(run_recognition=lambda *_a, **_kw: SimpleNamespace(
                hit=True, filtered_results=[SimpleNamespace(text=str(extra), box=[34, 68, 56, 44])]))
            self.assertIsNone(action._read_turn_count(context, object()))
            self.assertEqual(action._read_turn_count(context, object()), extra)
            self.assertTrue(action._extra_turn_transition)
            skips = []
            action._followups = SimpleNamespace(active='start', skips=1, skipped=lambda: skips.append(True))
            action._followup_skip_turn = 1
            action._observe_followup_turn(extra)
            self.assertEqual(skips, [True])
            self.assertIsNone(action._followup_skip_turn)
            self.assertEqual(action._pending_extra_turns, 0)

    def test_pending_extra_turn_exclusion_applies_to_normal_combo_and_fallback(self):
        from extensions.hif.followups import Followups
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action.combo_wait_enabled = False
        action.no_extra_turn_keys = {'脚光+'}
        action.priority_index = {'脚光+': 0, '存在感+': 8}
        action._followups = Followups({'国民的アイドル+': {'targets': ['脚光+'], 'wait_turns': 0,
            'use_black_vinegar': False, 'wait_after_success': False, 'target_policy': 'normal'}})
        hand = [{'key': k, 'box': [20, 900, 100, 200], 'conf': 1} for k in action.priority_index]
        for pending, confirmed, allowed in ((1, True, False), (3, True, False), (0, True, True), (0, False, False), (None, False, False)):
            action._pending_extra_turns, action._extra_turn_read_confirmed = pending, confirmed
            self.assertEqual(action._allowed_by_use_conditions('脚光+'), allowed)
            self.assertEqual(action._decide(None, hand)['key'], '脚光+' if allowed else '存在感+')
            action._followups.committed('国民的アイドル+', 'start')
            self.assertEqual(action._decide(None, hand)['key'], '脚光+' if allowed else '存在感+')
            action._followups.release()
        action._pending_extra_turns, action._extra_turn_read_confirmed = 1, True
        action._get_card_info = lambda _: (1, 0, 1, hand[0]['box'], hand[0]['box'])
        action._identify_card = lambda *_: ('脚光+', 1)
        action._in_range = lambda *_: True
        skipped = []
        action._skip_round = lambda *_: skipped.append(True)
        action._play_a_card = lambda *_: self.fail('Fallback bypassed pending extra-turn restriction')
        action._play_fallback(None, None, [])
        self.assertEqual(skipped, [True])

    def test_extra_reserve_stays_independent_when_countdown_ocr_fails(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action._pending_extra_turns = 0
        action.no_extra_turn_keys = {'a'}
        action._read_extra_turn_count = lambda *_: 1
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=False, filtered_results=[]))
        self.assertIsNone(action._read_turn_count(context, object()))
        self.assertEqual(action._pending_extra_turns, 1)
        self.assertFalse(action._allowed_by_use_conditions('a'))

    def test_rollover_keeps_reserve_across_failed_ocr_but_requires_two_valid_frames(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action._last_turn_count = 1
        action._pending_extra_turns = 3
        action.no_extra_turn_keys = {'a'}
        action._read_extra_turn_count = lambda *_: 0
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=False, filtered_results=[]))
        self.assertIsNone(action._read_turn_count(context, object()))
        self.assertFalse(action._allowed_by_use_conditions('a'))
        context.run_recognition = lambda *_a, **_k: SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text='3', box=[34, 68, 56, 44])])
        self.assertIsNone(action._read_turn_count(context, object()))
        self.assertEqual(action._read_turn_count(context, object()), 3)
        self.assertTrue(action._allowed_by_use_conditions('a'))


if __name__ == '__main__':
    unittest.main()
