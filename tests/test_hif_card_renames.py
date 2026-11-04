import json
import pathlib
import tempfile
import unittest

from test_hif_behavior import PRODUCE, COMMON, hif_patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'extensions/hif/catalog'
OLD, NEW = '愛をこめて+', '愛を込めて+'


class CardRenameTest(unittest.TestCase):
    def test_rename_table_points_from_retired_names_to_catalog_names(self):
        renames = json.loads((CATALOG / 'hif_card_renames.json').read_text(encoding='utf-8'))
        priority = json.loads((CATALOG / 'hif_priority_card_catalog.json').read_text(encoding='utf-8'))
        images = json.loads((CATALOG / 'hif_priority_card_images.json').read_text(encoding='utf-8'))
        targets = {card['name'] for card in json.loads((CATALOG / 'hif_target_card_catalog.json').read_text(encoding='utf-8'))}
        self.assertTrue(renames)
        for old, new in renames.items():
            self.assertNotIn(old, priority)
            self.assertNotIn(old, targets)
            self.assertIn(new, priority)
            self.assertIn(new, images)
            self.assertIn(new, targets)
            self.assertNotIn(new, renames, '更名表不能链式改名')
        # OCR 词是卡名前缀，不得与旧全名相同，否则会被一并改名。
        self.assertFalse({entry['name'] for entry in priority.values()} & set(renames))

    def test_every_card_field_is_renamed_and_ocr_words_are_kept(self):
        config = {
            'priority_profiles': {'集中': [{'key': OLD, 'template': '', 'priority': 3, 'name': '愛を'}]},
            'recognition_profiles': {'集中': [OLD, '脚光+']},
            'followup_profiles': {'集中': {OLD: {'targets': ['脚光+']}, '国民的アイドル+': {'targets': [OLD]}}},
            'conditional_priority_profiles': {'集中': {OLD: {'priority': 1, 'mode': 'any'}}},
            'use_condition_profiles': {'集中': {OLD: [{'remaining_turns_lte': 2}]}},
            'hold_priority_profiles': {'強気': {'ordered': False, 'cards': [OLD]}},
            'no_extra_turn_profiles': {'集中': [OLD]},
            'preferred_acquisition_profiles': {'集中': [{'name': '初めての場所'}]},
            'swap_out_priority_profiles': {'集中': ['夢と現境界線+']},
            'swap_out_options_profiles': {'集中': {'月明りに包まれて+': {'prefer_uncustomized': True}}},
            'profiles': {'集中': [{'id': 25, 'before_card_targets': [OLD]}]},
        }
        renamed = COMMON._hif_rename_cards(config, COMMON._hif_card_renames())
        text = json.dumps(renamed, ensure_ascii=False)
        for old in ('愛をこめて+', '初めての場所"', '夢と現境界線+', '月明りに包まれて+'):
            self.assertNotIn(old, text)
        self.assertEqual(renamed['priority_profiles']['集中'][0], {'key': NEW, 'template': '', 'priority': 3, 'name': '愛を'})
        self.assertEqual(renamed['followup_profiles']['集中'], {NEW: {'targets': ['脚光+']}, '国民的アイドル+': {'targets': [NEW]}})
        self.assertEqual(renamed['preferred_acquisition_profiles']['集中'], [{'name': '初めての場所+'}])
        self.assertEqual(renamed['profiles']['集中'][0]['before_card_targets'], [NEW])
        self.assertEqual(list(renamed['use_condition_profiles']['集中']), [NEW])

    def test_existing_official_entries_win_over_retired_duplicates(self):
        config = {
            'priority_profiles': {'集中': [{'key': NEW, 'priority': 1}, {'key': OLD, 'priority': 9}]},
            'recognition_profiles': {'集中': [OLD, NEW]},
            'use_condition_profiles': {'集中': {OLD: [{'remaining_turns_lte': 9}], NEW: [{'remaining_turns_lte': 1}]}},
        }
        renamed = COMMON._hif_rename_cards(config, {OLD: NEW})
        self.assertEqual(renamed['priority_profiles']['集中'], [{'key': NEW, 'priority': 1}])
        self.assertEqual(renamed['recognition_profiles']['集中'], [NEW])
        self.assertEqual(renamed['use_condition_profiles']['集中'], {NEW: [{'remaining_turns_lte': 1}]})

    def test_unreadable_rename_table_leaves_config_unchanged(self):
        with hif_patch('HIF_CARD_RENAMES_PATH', str(ROOT / 'temp/missing-renames.json')):
            self.assertEqual(COMMON._hif_card_renames(), {})

    def test_agent_keeps_priority_and_followups_configured_under_retired_name(self):
        config = json.loads((ROOT / 'extensions/hif/defaults/cards_priority.json').read_text(encoding='utf-8'))
        config['priority_profiles']['集中'].append({'key': OLD, 'template': '', 'priority': 0.5, 'name': '愛を'})
        config['use_condition_profiles']['集中'] = {OLD: [{'remaining_turns_lte': 2}]}
        config['followup_profiles']['集中'][OLD] = {'targets': ['脚光+'], 'wait_turns': 2, 'use_black_vinegar': False,
                                                  'wait_after_success': False, 'target_policy': 'normal'}
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / 'cards_priority.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            with hif_patch('CARDS_PRIORITY_CONFIG_PATH', str(path)):
                action._load_config('集中')
                action._load_hif_followups()
        # 游戏内显示正式名，OCR 读到正式名时仍命中原有优先级与使用限制。
        self.assertEqual(action._hif_name_key('愛を込めて+'), NEW)
        self.assertEqual(action.priority_index[NEW], 0.5)
        self.assertEqual(action.use_conditions[NEW], [{'remaining_turns_lte': 2}])
        self.assertIn(NEW, action._followups.rules)


if __name__ == '__main__':
    unittest.main()
