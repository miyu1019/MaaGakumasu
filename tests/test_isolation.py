import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import compose, digest_tree, extract_package, install_composition, migrate_config, read, update, write
from validate import validate


def frontend_text(relative):
    # Native checks are complemented by compiling/applying the complete patch.
    return (ROOT / 'patches/mfaavalonia-v2.14.0-hif.patch').read_text(encoding='utf-8')


class IsolationTests(unittest.TestCase):
    def fixture(self, root):
        interface = {'interface_version': 2, 'version': 'v1', 'import': ['./tasks/produce.json'],
                     'resource': [{'name': '官服', 'path': ['./resource/base']}],
                     'languages': {'zh-CN': 'lang/zh-CN.json'}, 'task': []}
        write(root / 'upstream/interface.json', interface)
        write(root / 'upstream/tasks/produce.json', {'task': [{'name': '开始培育', 'entry': 'Produce'}]})
        write(root / 'upstream/lang/zh-CN.json', {'培育说明': 'original'})
        write(root / 'upstream/resource/base/pipeline/Produce.json', {'Produce': {'next': []}})
        (root / 'upstream/agent').mkdir()
        (root / 'upstream/agent/main.py').write_text('VALUE = 1\n')
        write(root / 'extensions/hif/tasks/produce_hif.json', {'task': [
            {'name': 'HIF培育', 'entry': 'ProduceHIF', 'option': ['HIF.培育次数'], 'default_check': False},
            {'name': 'HIF培育-zh_CN', 'entry': 'ProduceHIF', 'option': ['HIF.培育次数'], 'default_check': False}],
            'option': {'HIF.培育次数': {'type': 'select', 'cases': [{'name': '1'}]}}})
        write(root / 'extensions/hif/namespace.json', {'options': {'培育次数': 'HIF.培育次数'}})
        write(root / 'extensions/hif/resource/base/pipeline/HIF.json', {'ProduceHIF': {'next': []}})
        write(root / 'config/hif/cards_priority.json', {'personal': 27})
        for folder in ('libs', 'python', 'runtimes', 'temp', 'config/instances'):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / 'libs/panel.dll').write_bytes(b'custom front end')
        install_composition(root)

    def package(self, root, incremental=False, incompatible=False):
        package = root / 'package.zip'
        interface = read(root / 'upstream/interface.json')
        interface.update(version='v2', interface_version=3 if incompatible else 2)
        with zipfile.ZipFile(package, 'w') as archive:
            for path in (root / 'upstream').rglob('*'):
                if path.is_file() and path.name != 'interface.json':
                    archive.write(path, path.relative_to(root / 'upstream').as_posix())
            archive.writestr('interface.json', json.dumps(interface))
            archive.writestr('resource/base/pipeline/Extra.json', json.dumps({'NewScenario': {'next': []}}))
            archive.writestr('config/hif/cards_priority.json', '{"personal": 999}')
            archive.writestr('libs/panel.dll', b'upstream front end')
            if incremental:
                archive.writestr('changes.json', json.dumps({'deleted_files': ['resource/base/pipeline/Obsolete.json']}))
        return package

    def run_update(self, root, package, **kwargs):
        def check_candidate(command, **_):
            candidate = Path(command[command.index('--upstream') + 1])
            validate(root, candidate)
        with patch('hif_app.subprocess.run', side_effect=check_candidate):
            return update(root, package, **kwargs)

    def test_actual_graph_and_upstream_byte_identity(self):
        result = validate(ROOT)
        self.assertEqual(result['tasks'], 2)
        expected = read(ROOT / 'upstream-provenance.json')['files']
        self.assertEqual(digest_tree(ROOT / 'upstream'), expected)

    def test_compose_preserves_original_task_definitions_and_help(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            before = digest_tree(root / 'upstream')
            interface, languages = compose(root)
            self.assertIn('./upstream/tasks/produce.json', interface['import'])
            self.assertEqual(languages['zh-CN.json']['培育说明'], 'original')
            self.assertEqual(digest_tree(root / 'upstream'), before)

    def test_migration_preserves_values_order_check_and_ordinary_task(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            ordinary = {'name': '开始培育-zh_CN', 'entry': 'Produce', 'option': [{'name': '培育难度', 'index': 0}]}
            old = {'name': '开始培育', 'entry': 'Produce', 'default_check': True,
                   'remark': 'my task', 'display_name_override': 'custom', 'option': [
                       {'name': '培育难度', 'index': 2}, {'name': '培育次数', 'index': 6},
                       {'name': '培育偶像', 'index': 4}, {'name': '跳过选择偶像', 'index': 1}]}
            write(root / 'config/instances/a.json', {'TaskItems': [old, ordinary]})
            self.assertEqual(migrate_config(root), 1)
            tasks = read(root / 'config/instances/a.json')['TaskItems']
            self.assertEqual(tasks[1], ordinary)
            self.assertTrue(tasks[0]['default_check'])
            self.assertEqual(tasks[0]['remark'], 'my task')
            self.assertEqual(tasks[0]['display_name_override'], 'custom')
            self.assertEqual(tasks[0]['option'], [{'name': 'HIF.培育次数', 'index': 6}])
            self.assertEqual(migrate_config(root), 0)
            self.assertTrue((root / 'backup/config-before-hif-migration/a.json').is_file())

    def test_full_update_protects_extension_configuration_and_panel(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            protected = {p: digest_tree(root / p) for p in ('extensions', 'config', 'libs')}
            result = self.run_update(root, self.package(root))
            self.assertEqual(result['installed'], 'v2')
            self.assertTrue((root / 'upstream/resource/base/pipeline/Extra.json').exists())
            self.assertEqual(protected, {p: digest_tree(root / p) for p in protected})

    def test_incremental_deletions_stay_in_upstream(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            write(root / 'upstream/resource/base/pipeline/Obsolete.json', {})
            package = self.package(root, incremental=True)
            # The deleted item must not be present in an incremental payload.
            self.run_update(root, package)
            # A package containing a deleted file re-adds it by design; verify a realistic delta.
            with zipfile.ZipFile(package) as archive:
                contents = {n: archive.read(n) for n in archive.namelist() if not n.endswith('Obsolete.json')}
            with zipfile.ZipFile(package, 'w') as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            self.run_update(root, package)
            self.assertFalse((root / 'upstream/resource/base/pipeline/Obsolete.json').exists())

    def test_failed_update_rolls_back_upstream_and_composition(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            before = digest_tree(root / 'upstream')
            interface = (root / 'interface.json').read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'Injected failure'):
                self.run_update(root, self.package(root), fail_after_swap=True)
            self.assertEqual(digest_tree(root / 'upstream'), before)
            self.assertEqual((root / 'interface.json').read_bytes(), interface)

    def test_incompatible_package_retains_installed_version(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            before = digest_tree(root / 'upstream')
            with self.assertRaises(ValueError):
                self.run_update(root, self.package(root, incompatible=True))
            self.assertEqual(digest_tree(root / 'upstream'), before)

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            package = root / 'bad.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('../config/hif/cards_priority.json', '{}')
            with self.assertRaises(ValueError):
                extract_package(package, root / 'extract')

    def test_native_updater_cannot_run_automatically(self):
        code = frontend_text('Helper/VersionChecker.cs')
        self.assertIn('if (HifLayout.IsIndependent) return Task.CompletedTask;', code)
        self.assertIn('await HifLayout.RunManualUpdateAsync("update", localPackagePath)', code)

    def test_native_executor_uses_separate_hif_models_and_agent(self):
        code = frontend_text('Extensions/MaaFW/MaaProcessor.cs')
        self.assertIn('HifLayout.ResourcePaths(ViewModel?.CurrentResource)', code)
        self.assertIn('ChildArgs = [.. c.ChildArgs ?? [], "--hif-only"]', code)
        self.assertIn('using var resource = new MaaResource(paths);', code)
        bootstrap = (ROOT / 'tools/agent_entry.py').read_text(encoding='utf-8')
        self.assertIn("if '--hif-only' in sys.argv:", bootstrap)

    def test_update_rejects_resource_rename_that_would_hide_hif(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as d:
            root = Path(d)
            self.fixture(root)
            hif = read(root / 'extensions/hif/tasks/produce_hif.json')
            hif['task'][0]['resource'] = ['官服']
            write(root / 'extensions/hif/tasks/produce_hif.json', hif)
            interface = read(root / 'upstream/interface.json')
            interface['resource'][0]['name'] = 'renamed'
            write(root / 'upstream/interface.json', interface)
            with self.assertRaisesRegex(ValueError, 'identifiers changed'):
                compose(root)


if __name__ == '__main__':
    unittest.main()
