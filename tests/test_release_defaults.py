import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import initialize_defaults, migrate_personal_files


class PublicDefaultsTests(unittest.TestCase):
    def test_no_other_profession_strategies_are_published(self):
        data = json.loads((ROOT / 'extensions/hif/defaults/cards_priority.json').read_text(encoding='utf-8'))
        for field in ('priority_profiles', 'preferred_acquisition_profiles', 'swap_out_priority_profiles',
                      'recognition_profiles', 'use_condition_profiles'):
            for profession, entries in data[field].items():
                if profession != '集中':
                    self.assertFalse(entries, (field, profession))
        self.assertTrue(data['priority_profiles']['集中'])
        self.assertEqual(data['conditional_priority_profiles'], {'全力': {}})
        self.assertEqual(set(data['unknown_priority_profiles']), {'集中'})
        drinks = json.loads((ROOT / 'extensions/hif/defaults/hif_drink_profiles.json').read_text(encoding='utf-8'))
        self.assertTrue(drinks['profiles']['集中'])
        for profession, entries in drinks['profiles'].items():
            if profession != '集中':
                self.assertFalse(entries)

    def test_templates_do_not_overwrite_existing_personal_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            root = Path(directory)
            target = root / 'extensions/hif/defaults'
            target.mkdir(parents=True)
            for name in ('cards_priority.json', 'hif_drink_profiles.json'):
                (target / name).write_text('{"public": true}')
            personal = root / 'config/hif/cards_priority.json'
            personal.parent.mkdir(parents=True)
            personal.write_text('{"personal": 123}')
            initialize_defaults(root)
            self.assertEqual(personal.read_text(), '{"personal": 123}')
            self.assertEqual(json.loads((personal.parent / 'hif_drink_profiles.json').read_text()), {'public': True})
            initialize_defaults(root)
            self.assertEqual(personal.read_text(), '{"personal": 123}')

    def test_safe_first_run_options(self):
        options = json.loads((ROOT / 'extensions/hif/tasks/produce_hif.json').read_text(encoding='utf-8'))['option']
        for key, expected in {'角色职业': '集中', '使用体力药': 'No', '跳过准备阶段': 'No', '培育次数': '1'}.items():
            self.assertEqual(options['HIF.' + key]['default_case'], expected)

    def test_public_instance_contains_only_hif_and_no_connection(self):
        root = ROOT / 'extensions/hif/first-run'
        instance = json.loads((root / 'config/instances/hif.json').read_text(encoding='utf-8'))
        self.assertEqual(instance['InstanceName'], '一键培育')
        self.assertEqual(instance['CurrentTasks'], ['HIF培育<|||>ProduceHIF'])
        self.assertEqual([task['entry'] for task in instance['TaskItems']], ['ProduceHIF'])
        self.assertFalse(any('Device' in key or 'Preset' in key for key in instance))
        ui = json.loads((root / 'config/config.json').read_text(encoding='utf-8'))
        self.assertFalse(any('Position' in key for key in ui))
        layout = json.loads((root / 'resource/mfa_layout.json').read_text(encoding='utf-8'))
        occupied = set()
        for name in ('settings', 'task_list', 'task_desc', 'live_view', 'log'):
            card = layout[name]
            for row in range(card['row'], card['row'] + card['row_span']):
                for col in range(card['col'], card['col'] + card['col_span']):
                    self.assertNotIn((row, col), occupied, name)
                    self.assertLess(row, layout['rows'])
                    self.assertLess(col, layout['columns'])
                    occupied.add((row, col))

    def test_legacy_personal_values_and_image_paths_are_migrated_once(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            root = Path(directory)
            legacy = root / 'config/cards_priority.json'
            legacy.parent.mkdir()
            value = {'priority_profiles': {'集中': [{'key': 'user card', 'priority': 19, 'template': 'cards/test.png'}]},
                     'swap_out_priority_profiles': {'全力': ['personal card']}}
            legacy.write_text(json.dumps(value))
            image = root / 'extensions/hif/resource/base/image/hif/cards/test.png'
            image.parent.mkdir(parents=True)
            image.write_bytes(b'fixture')
            migrate_personal_files(root)
            target = root / 'config/hif/cards_priority.json'
            migrated = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(migrated['priority_profiles']['集中'][0]['template'], 'hif/cards/test.png')
            self.assertEqual(migrated['priority_profiles']['集中'][0]['priority'], 19)
            self.assertEqual(migrated['swap_out_priority_profiles'], value['swap_out_priority_profiles'])
            self.assertEqual(json.loads((root / 'backup/personal-before-hif-migration/cards_priority.json').read_text()), value)
            self.assertEqual(migrate_personal_files(root), 0)


if __name__ == '__main__':
    unittest.main()
