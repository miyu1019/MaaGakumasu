import hashlib
import io
import json
import os
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
        write(self.root / 'hif-release.json', self.lock)
        write(self.root / 'hif-build-info.json', self.lock)
        self.before = digest_tree(self.root)

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def response(value):
        stream = io.BytesIO(json.dumps(value).encode('utf-8'))
        stream.headers = {}
        return stream

    def autobuild_identity(self, base='v261008.1', commit='b' * 40, notes='- 修复识别'):
        return {'tag': 'autobuild', 'version': base, 'commit': commit, 'package': updater.AUTOBUILD_PACKAGE, 'notes': notes}

    def autobuild_release(self):
        return {'tag_name': 'autobuild', 'body': '正文不再用于判断身份', 'assets': [
            {'name': name, 'browser_download_url': 'https://github.com/download/' + name}
            for name in (updater.AUTOBUILD_PACKAGE, updater.AUTOBUILD_PACKAGE + '.sha256', updater.AUTOBUILD_INFO)]}

    def test_only_own_fork_is_queried_and_missing_release_is_normal(self):
        error = urllib.error.HTTPError('url', 404, 'not found', {}, None)
        with patch.object(updater.urllib.request, 'urlopen', side_effect=error) as request:
            result = updater.check(self.root)
        self.assertEqual(request.call_args.args[0].full_url,
                         'https://api.github.com/repos/miyu1019/MaaGakumasu/releases/latest')
        self.assertIsNone(result['latest'])
        self.assertFalse(result['update_available'])
        self.assertEqual(self.before, digest_tree(self.root))

    def test_frontend_cli_entry_works_with_the_packaged_python(self):
        # A local package of the installed version is verified, extracted and skipped without touching the program.
        repository = updater.ROOT
        current = json.loads((repository / 'hif-release.json').read_text(encoding='utf-8-sig'))
        # The package carries the installed identity: an AutoBuild runtime (as in CI) is matched by its source commit.
        commit = updater.installed_autobuild_commit(repository)
        info = {**current, 'build_channel': updater.AUTOBUILD_TAG, 'source_commit': commit} if commit else current
        package = Path(self.directory.name) / ('MaaGakumasu-HIF-win-x64-' + (updater.AUTOBUILD_TAG if commit else current['version']) + '.zip')
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('MaaGakumasu-HIF/hif-release.json', json.dumps(current))
            archive.writestr('MaaGakumasu-HIF/hif-build-info.json', json.dumps(info))
        package.with_suffix('.zip.sha256').write_text(hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name)
        pointer = repository / 'temp/hif-update-pending.json'
        original_pointer = pointer.read_bytes() if pointer.exists() else None
        result = subprocess.run([sys.executable, str(repository / 'tools/hif_app.py'), 'prepare-update', '--package', str(package)],
                                capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'})
        stage = Path(json.loads(pointer.read_text(encoding='utf-8'))['operation']).parent
        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])['skipped'])
            self.assertFalse((stage / 'extracted').exists())
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if not any(stage.parent.iterdir()):
                stage.parent.rmdir()
            if original_pointer is None:
                pointer.unlink()
            else:
                pointer.write_bytes(original_pointer)

    def test_rate_limited_api_uses_stable_public_release_and_description(self):
        page = io.BytesIO(b'<nav>ignore navigation</nav><div class="markdown-body"><h2>Notes</h2>'
                          b'<p>Keep settings &amp; choices.</p><div><ul><li>New option</li></ul></div></div>'
                          b'<div>ignore assets navigation</div>')
        page.geturl = lambda: 'https://github.com/miyu1019/MaaGakumasu/releases/tag/v261009.1'
        error = urllib.error.HTTPError('url', 403, 'rate limited', {'X-RateLimit-Remaining': '0'}, io.BytesIO())
        with patch.object(updater.urllib.request, 'urlopen', side_effect=[error, page]) as request:
            result = updater.latest_release()
        self.assertEqual(request.call_args_list[1].args[0].full_url, 'https://github.com/miyu1019/MaaGakumasu/releases/latest')
        self.assertEqual(result['tag_name'], 'v261009.1')
        self.assertIn('Keep settings & choices.', result['body'])
        self.assertIn('- New option', result['body'])
        self.assertNotIn('ignore', result['body'])
        asset = next(a for a in result['assets'] if a['name'].endswith('.zip.sha256'))
        self.assertEqual(asset['browser_download_url'],
                         'https://github.com/miyu1019/MaaGakumasu/releases/download/v261009.1/MaaGakumasu-HIF-win-x64-v261009.1.zip.sha256')

    def test_public_release_fallback_rejects_other_repositories(self):
        for url in ('https://github.com/other/project/releases/tag/v261009.1',
                    'https://github.com/miyu1019/MaaGakumasu/releases/tag/not-a-release',
                    'https://example.com/miyu1019/MaaGakumasu/releases/tag/v261009.1'):
            page = io.BytesIO(b'')
            page.geturl = lambda: url
            with self.subTest(url=url), patch.object(updater.urllib.request, 'urlopen', return_value=page):
                with self.assertRaises(ValueError):
                    updater.release_from_public_page()

    def test_non_rate_limit_permission_error_is_reported(self):
        error = urllib.error.HTTPError('url', 403, 'forbidden', {}, io.BytesIO(b'Forbidden'))
        with patch.object(updater.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError):
                updater.latest_release()
        self.assertEqual(request.call_count, 1)

    def test_token_is_sent_only_to_the_github_api_and_rejection_is_explained(self):
        with patch.dict(os.environ, {'HIF_GITHUB_TOKEN': ' secret-token '}), \
                patch.object(updater.urllib.request, 'urlopen',
                             side_effect=[self.response(self.autobuild_release()), self.response(self.autobuild_identity())]) as request:
            updater.latest_release('autobuild')
        api, asset = (call.args[0] for call in request.call_args_list)
        self.assertEqual(api.get_header('Authorization'), 'Bearer secret-token')
        self.assertIsNone(asset.get_header('Authorization'))
        error = urllib.error.HTTPError('url', 401, 'bad credentials', {}, io.BytesIO())
        with patch.dict(os.environ, {'HIF_GITHUB_TOKEN': 'expired'}), patch.object(updater.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'Token'):
                updater.latest_release()
        with patch.dict(os.environ, {'HIF_GITHUB_TOKEN': ''}):
            self.assertIsNone(updater.github_api('latest').get_header('Authorization'))

    def test_check_returns_release_notes_without_downloading(self):
        release = {'tag_name': 'v261008.2', 'body': '## 更新说明\n保留个人策略'}
        with patch.object(updater, 'latest_release', return_value=release), patch.object(updater.urllib.request, 'urlopen') as fetch:
            result = updater.check(self.root)
        self.assertTrue(result['update_available'])
        self.assertEqual(result['release_notes'], release['body'])
        fetch.assert_not_called()

    def test_autobuild_channel_reads_identity_file_and_compares_source_commit(self):
        with patch.object(updater.urllib.request, 'urlopen',
                          side_effect=[self.response(self.autobuild_release()), self.response(self.autobuild_identity())]) as request:
            result = updater.check(self.root, channel='autobuild')
        self.assertEqual([call.args[0].full_url for call in request.call_args_list],
                         ['https://api.github.com/repos/miyu1019/MaaGakumasu/releases/tags/autobuild',
                          'https://github.com/download/' + updater.AUTOBUILD_INFO])
        self.assertTrue(result['update_available'])
        self.assertEqual(result['latest'], 'v261008.1 · AutoBuild · bbbbbbb')
        self.assertEqual(result['release_notes'], '- 修复识别')
        write(self.root / 'hif-build-info.json', {**self.lock, 'build_channel': 'autobuild', 'source_commit': 'b' * 40})

        def described(**identity):
            return {**self.autobuild_release(), 'version': identity.get('base', 'v261008.1'), 'commit': identity.get('commit', 'b' * 40)}
        with patch.object(updater, 'latest_release', return_value=described()):
            result = updater.check(self.root, channel='autobuild')
        self.assertFalse(result['update_available'])
        self.assertEqual(result['installed'], 'v261008.1 · AutoBuild · bbbbbbb')
        with patch.object(updater, 'latest_release', return_value=described(base='v261007.1', commit='c' * 40)):
            self.assertFalse(updater.check(self.root, channel='autobuild')['update_available'])
        with patch.object(updater, 'latest_release', return_value=described(commit='c' * 40)):
            self.assertTrue(updater.check(self.root, channel='autobuild')['update_available'])

    def test_autobuild_release_needs_a_valid_identity_file(self):
        release = {**self.autobuild_release(), 'assets': self.autobuild_release()['assets'][:2]}
        with self.assertRaisesRegex(ValueError, updater.AUTOBUILD_INFO):
            updater.describe_autobuild(release)
        for broken in ({'commit': 'short'}, {'package': 'other.zip'}, {'tag': 'nightly'}):
            with self.subTest(broken=broken), \
                    patch.object(updater.urllib.request, 'urlopen', return_value=self.response({**self.autobuild_identity(), **broken})):
                with self.assertRaisesRegex(ValueError, '身份文件无效'):
                    updater.describe_autobuild(self.autobuild_release())

    def test_rate_limited_autobuild_reads_the_fixed_download_address(self):
        error = urllib.error.HTTPError('url', 429, 'rate limited', {}, io.BytesIO())
        with patch.object(updater.urllib.request, 'urlopen', side_effect=[error, self.response(self.autobuild_identity(commit='d' * 40))]) as request:
            result = updater.latest_release('autobuild')
        base = 'https://github.com/miyu1019/MaaGakumasu/releases/download/autobuild/'
        self.assertEqual(request.call_args_list[1].args[0].full_url, base + updater.AUTOBUILD_INFO)
        self.assertEqual((result['version'], result['commit'], result['body']), ('v261008.1', 'd' * 40, '- 修复识别'))
        self.assertEqual({a['name']: a['browser_download_url'] for a in result['assets']}[updater.AUTOBUILD_PACKAGE],
                         base + updater.AUTOBUILD_PACKAGE)
        missing = urllib.error.HTTPError('url', 404, 'not found', {}, io.BytesIO())
        with patch.object(updater.urllib.request, 'urlopen', side_effect=[error, missing]):
            self.assertIsNone(updater.latest_release('autobuild'))

    def test_installed_autobuild_can_switch_back_to_the_stable_release_of_its_base_version(self):
        write(self.root / 'hif-build-info.json', {**self.lock, 'build_channel': 'autobuild', 'source_commit': 'b' * 40})
        for tag, expected in (('v261008.1', True), ('v261008.2', True), ('v261007.9', False)):
            with self.subTest(tag=tag), patch.object(updater, 'latest_release', return_value={'tag_name': tag}):
                self.assertEqual(updater.check(self.root)['update_available'], expected)

    def test_newer_rules_for_stable_autobuild_and_reinstall(self):
        cases = [
            (('v261008.2', None, 'v261008.1', None), True),
            (('v261008.1', None, 'v261008.1', None), False),
            (('v261008.1', 'a' * 40, 'v261008.1', None), True),
            (('v261008.1', 'a' * 40, 'v261008.1', 'a' * 40), False),
            (('v261008.1', None, 'v261008.1', 'a' * 40), True),
            (('v261007.9', 'c' * 40, 'v261008.1', 'a' * 40), False),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(updater.is_newer(*arguments), expected)
        self.assertTrue(updater.is_newer('v261008.1', None, 'v261008.1', None, reinstall=True))
        self.assertTrue(updater.is_newer('v261008.1', 'a' * 40, 'v261008.1', 'a' * 40, reinstall=True))
        self.assertFalse(updater.is_newer('v261007.9', None, 'v261008.1', None, reinstall=True))

    def test_missing_release_assets_never_falls_back_to_source_archive(self):
        with patch.object(updater, 'latest_release', return_value={'tag_name': 'v261008.2', 'assets': []}), \
                patch.object(updater.urllib.request, 'urlopen') as download:
            with self.assertRaisesRegex(ValueError, '发布缺少'):
                updater.prepare_update(self.root)
            download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
