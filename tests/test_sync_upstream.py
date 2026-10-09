import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import sync_upstream as syncer
import hif_build as builder
from hif_app import digest_tree, read, write


class UpstreamSyncTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.root = Path(self.temporary.name)
        self.git('init', '--quiet')
        (self.root / 'agent/custom').mkdir(parents=True)
        (self.root / 'agent/main.py').write_text('# original agent\n')
        (self.root / 'agent/custom/__init__.py').write_text('')
        write(self.root / 'assets/interface.json', {'interface_version': 2, 'version': 'v1',
              'import': ['./tasks/produce.json'], 'resource': [
                  {'name': name, 'path': ['./resource/base']} for name in ('官服', 'DMM', '插件版汉化', 'DMM插件汉化版')],
              'languages': {'zh-CN': './lang/zh-CN.json'}})
        write(self.root / 'assets/tasks/produce.json', {'task': [{'name': '开始培育', 'entry': 'Produce'}]})
        write(self.root / 'assets/lang/zh-CN.json', {})
        write(self.root / 'assets/resource/base/pipeline/Produce.json', {'Produce': {'action': 'DoNothing'}})
        (self.root / 'requirements.txt').write_text('maafw\nloguru\n')
        for name in syncer.PROTECTED:
            path = self.root / name
            if name.endswith('.txt'):
                path.write_text('fixed runtime dependencies')
            else:
                path.mkdir(parents=True)
                (path / 'retained.json').write_text('{"private": "retained"}')
        self.git('add', '.')
        self.commit('original')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()
        write(self.root / 'assets/interface.json', {**read(self.root / 'assets/interface.json'), 'version': 'v2'})
        write(self.root / 'assets/resource/base/pipeline/NewScenario.json', {'NewScenario': {'action': 'DoNothing'}})
        (self.root / 'agent/new.py').write_text('# new upstream code\n')
        self.git('add', 'assets', 'agent')
        self.commit('new scenario')
        self.target = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('checkout', '--quiet', '--detach', self.base)
        lock = {'upstream_commit': self.base, 'version': 'v261008.1',
                'framework_version': '5.12.3', 'python_version': '3.12.9'}
        write(self.root / 'hif-release.json', lock)
        self.runtime = self.root / 'runtime-fixture'
        (self.runtime / 'python').mkdir(parents=True)
        (self.runtime / 'python/python.exe').write_bytes(b'test executable; never started')
        write(self.runtime / 'hif-release.json', lock)
        self.before = self.snapshot()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return syncer.git(self.root, *args)

    def commit(self, message):
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--quiet', '-m', message)

    def snapshot(self):
        return (digest_tree(self.root / 'agent'), digest_tree(self.root / 'assets'),
                (self.root / 'hif-release.json').read_bytes() if (self.root / 'hif-release.json').exists() else None,
                syncer.fingerprints(self.root))

    def run_sync(self, apply=False):
        return syncer.sync(self.root, self.target, self.runtime, apply=apply, fetch=False)

    @patch.object(syncer, 'check_candidate')
    def test_preview_never_changes_source_or_settings(self, _check):
        result = self.run_sync()
        self.assertFalse(result['applied'])
        self.assertIn('A\tagent/new.py', result['changes'])
        self.assertEqual(self.before, self.snapshot())

    @patch.object(syncer, 'check_candidate')
    def test_apply_updates_only_upstream_and_lock_with_backup(self, _check):
        index = (self.root / '.git/index').read_bytes()
        result = self.run_sync(apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual(read(self.root / 'hif-release.json')['upstream_commit'], self.target)
        self.assertEqual(read(self.root / 'hif-release.json')['version'], 'v261008.1')
        self.assertEqual(read(self.root / 'assets/interface.json')['version'], 'v2')
        self.assertTrue((self.root / 'agent/new.py').exists())
        self.assertEqual(self.before[3], syncer.fingerprints(self.root))
        self.assertEqual(self.before[0], digest_tree(Path(result['backup']) / 'agent'))
        self.assertEqual(index, (self.root / '.git/index').read_bytes())
        # Newly introduced files are not staged by the tool, but match the new fixed source.
        checked = self.run_sync()
        self.assertTrue(checked['compatible'])
        self.assertFalse(checked['applied'])

    @patch.object(syncer, 'check_candidate', side_effect=ValueError('incompatible interface'))
    def test_incompatible_candidate_retains_all_files(self, _check):
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            self.run_sync(apply=True)
        self.assertEqual(self.before, self.snapshot())

    @patch.object(syncer, 'check_candidate')
    def test_dirty_upstream_is_not_overwritten(self, check):
        (self.root / 'agent/main.py').write_text('user change')
        with self.assertRaisesRegex(ValueError, '本地修改'):
            self.run_sync(apply=True)
        check.assert_not_called()
        self.assertEqual((self.root / 'agent/main.py').read_text(), 'user change')

    @patch.object(syncer, 'check_candidate')
    def test_swap_failure_restores_old_source_and_lock(self, _check):
        with patch.object(syncer, 'write', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.run_sync(apply=True)
        self.assertEqual(self.before, self.snapshot())

    def test_concurrent_hif_or_manifest_edit_prevents_apply(self):
        for path in ('extensions/hif/retained.json', 'hif-release.json'):
            with self.subTest(path=path):
                original = (self.root / path).read_bytes()
                with patch.object(syncer, 'check_candidate', side_effect=lambda *_: (self.root / path).write_text('{}')):
                    with self.assertRaisesRegex(RuntimeError, '修改'):
                        self.run_sync(apply=True)
                (self.root / path).write_bytes(original)
                self.assertEqual(self.before, self.snapshot())

    def test_existing_commit_build_needs_no_network(self):
        with patch.object(builder, 'run') as fetch:
            builder.ensure_upstream_commit(self.root, self.base)
            fetch.assert_not_called()

    def test_missing_commit_fetches_only_pinned_official_commit(self):
        commit = 'f' * 40
        with patch.object(builder, 'run') as fetch:
            builder.ensure_upstream_commit(self.root, commit)
            self.assertEqual(fetch.call_args.args[0][-2:],
                             ['https://github.com/SuperWaterGod/MaaGakumasu.git', commit])
        with self.assertRaises(ValueError):
            builder.ensure_upstream_commit(self.root, 'main')


if __name__ == '__main__':
    unittest.main()
