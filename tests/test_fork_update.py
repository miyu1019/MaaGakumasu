import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import hif_update as updater
from hif_app import digest_tree, write


class ForkUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).resolve().parents[1] / 'temp'
        temporary.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temporary)
        self.root = Path(self.directory.name) / 'installed'
        self.root.mkdir()
        self.lock = {'version': 'v261008.1', 'repository': updater.REPOSITORY,
                     'upstream_commit': 'original', **{key: 'fixed' for key in updater.COMPONENTS}}
        for name in updater.INSTALL:
            path = self.root / name
            if name in ('upstream', 'extensions/hif', 'tools', 'lang', 'data', 'docs', 'patches'):
                path.mkdir(parents=True)
                (path / 'old.txt').write_text('old')
            else:
                path.write_text('old')
        write(self.root / 'hif-release.json', self.lock)
        write(self.root / 'hif-build-info.json', self.lock)
        (self.root / 'patches/mfaavalonia-v2.14.0-hif.patch').write_text('fixed patch')
        for name in updater.PROTECTED:
            path = self.root / name
            path.mkdir()
            (path / 'private.txt').write_text('personal ' + name)
        self.before = digest_tree(self.root)
        self.candidate = Path(self.directory.name) / 'MaaGakumasu-HIF'
        shutil.copytree(self.root, self.candidate)
        self.incoming = copy.deepcopy(self.lock)
        self.incoming['version'] = 'v261008.2'
        self.incoming['upstream_commit'] = 'owner-synchronized'
        write(self.candidate / 'hif-release.json', self.incoming)
        write(self.candidate / 'hif-build-info.json', self.incoming)
        (self.candidate / 'upstream/old.txt').unlink()
        (self.candidate / 'upstream/new-scenario.txt').write_text('new scenario')
        (self.candidate / 'extensions/hif/new.txt').write_text('new HIF')
        for name in updater.PROTECTED:
            (self.candidate / name / 'private.txt').write_text('package defaults')

    def tearDown(self):
        self.directory.cleanup()

    def package(self):
        path = Path(self.directory.name) / ('MaaGakumasu-HIF-win-x64-' + self.incoming['version'] + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for item in self.candidate.rglob('*'):
                if item.is_file():
                    archive.write(item, 'MaaGakumasu-HIF/' + item.relative_to(self.candidate).as_posix())
        path.with_suffix('.zip.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name)
        return path

    def test_only_own_fork_is_queried_and_missing_release_is_normal(self):
        error = urllib.error.HTTPError('url', 404, 'not found', {}, None)
        with patch.object(updater.urllib.request, 'urlopen', side_effect=error) as request:
            result = updater.update(self.root, check_only=True)
        self.assertEqual(request.call_args.args[0].full_url,
                         'https://api.github.com/repos/miyu1019/MaaGakumasu/releases/latest')
        self.assertIsNone(result['latest'])
        self.assertFalse(result['update_available'])
        self.assertEqual(self.before, digest_tree(self.root))

    def test_frontend_cli_entry_works_with_the_packaged_python(self):
        self.incoming['version'] = json.loads((updater.ROOT / 'hif-release.json').read_text(encoding='utf-8-sig'))['version']
        write(self.candidate / 'hif-release.json', self.incoming)
        result = subprocess.run([sys.executable, str(updater.ROOT / 'tools/hif_app.py'),
                                 'update', '--package', str(self.package())],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['skipped'])

    def test_manual_download_installs_hif_and_synced_scripts_preserves_settings_and_runtime(self):
        package = self.package()
        release = {'tag_name': 'v261008.2', 'assets': [
            {'name': name, 'browser_download_url': name} for name in (package.name, package.name + '.sha256')]}
        def download(url, target):
            shutil.copy2(Path(self.directory.name) / url, target)
        with patch.object(updater, 'latest_release', return_value=release), \
                patch.object(updater, 'download', side_effect=download), \
                patch.object(updater, 'validate_candidate'):
            result = updater.update(self.root)
        self.assertEqual(result['installed'], 'v261008.2')
        self.assertTrue((self.root / 'upstream/new-scenario.txt').exists())
        self.assertFalse((self.root / 'upstream/old.txt').exists())
        self.assertTrue((self.root / 'extensions/hif/new.txt').exists())
        for name in updater.PROTECTED:
            self.assertEqual((self.root / name / 'private.txt').read_text(), 'personal ' + name)
        self.assertEqual((Path(result['backup']) / 'upstream/old.txt').read_text(), 'old')

    def test_install_failure_restores_both_upstream_and_hif(self):
        with self.assertRaisesRegex(RuntimeError, 'Injected failure'):
            updater.install_candidate(self.root, self.candidate, fail_after_swap=True)
        actual = {k: v for k, v in digest_tree(self.root).items() if not k.startswith('backup/')}
        self.assertEqual(self.before, actual)

    def test_wrong_checksum_never_installs(self):
        package = self.package()
        package.with_suffix('.zip.sha256').write_text('0' * 64 + '  ' + package.name)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            updater.update(self.root, package)
        self.assertEqual(self.before, digest_tree(self.root))

    def test_component_or_patch_upgrade_is_rejected(self):
        for key in (*updater.COMPONENTS, 'patch'):
            with self.subTest(key=key):
                altered = copy.deepcopy(self.incoming)
                if key == 'patch':
                    (self.candidate / 'patches/mfaavalonia-v2.14.0-hif.patch').write_text('different')
                else:
                    altered[key] = 'different'
                write(self.candidate / 'hif-release.json', altered)
                write(self.candidate / 'hif-build-info.json', altered)
                with self.assertRaisesRegex(ValueError, '完整运行包'):
                    updater.validate_candidate(self.root, self.candidate)
                self.assertEqual(self.before, digest_tree(self.root))

    def test_existing_or_older_release_is_not_downloaded(self):
        for tag in ('v261008.1', 'v261007.9'):
            with patch.object(updater, 'latest_release', return_value={'tag_name': tag}), \
                    patch.object(updater, 'download') as download:
                self.assertTrue(updater.update(self.root)['skipped'])
                download.assert_not_called()

    def test_mismatched_upstream_provenance_never_installs(self):
        write(self.candidate / 'upstream-provenance.json',
              {'commit': self.incoming['upstream_commit'], 'files': {}})
        with self.assertRaisesRegex(ValueError, '固定提交清单不一致'):
            updater.validate_candidate(self.root, self.candidate)
        self.assertEqual(self.before, digest_tree(self.root))

    def test_missing_release_assets_never_falls_back_to_source_archive(self):
        with patch.object(updater, 'latest_release', return_value={'tag_name': 'v261008.2', 'assets': []}), \
                patch.object(updater, 'download') as download:
            with self.assertRaisesRegex(ValueError, '发布缺少'):
                updater.update(self.root)
            download.assert_not_called()
