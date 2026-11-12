import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import hif_update as updater
from hif_app import write, digest_tree


@unittest.skipUnless(os.name == 'nt', 'Full-package installation is Windows x64 only')
class FullUpdateTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / '旧版 with spaces [2] !'
        self.root.mkdir()
        self.current = {'version': 'v261008.2', 'repository': updater.REPOSITORY, 'upstream_commit': 'original',
                        **{k: 'old' for k in updater.COMPONENTS}}
        self.incoming = {**self.current, 'version': 'v261009.1', 'upstream_commit': 'new',
                         **{k: 'new' for k in updater.COMPONENTS}}
        write(self.root / 'hif-release.json', self.current)
        write(self.root / 'config/config.json', {'theme': 'personal'})
        write(self.root / 'config/hif/cards_priority.json', {'priority_profiles': {'focus': {'card': 9}}})
        write(self.root / 'appsettings.json', {'Instances.List': 'personal', 'Timer.Timer1.Schedule': 'daily'})
        write(self.root / 'resource/mfa_layout.json', {'layout': 'personal'})
        for name in ('hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
            (self.root / 'tools').mkdir(exist_ok=True)
            shutil.copy2(ROOT / 'tools' / name, self.root / 'tools' / name)
        self.candidate = self.base / 'candidate'
        self.candidate.mkdir()
        for name in ('MaaGakumasu.exe', 'python/python.exe', 'tools/validate.py', 'extensions/hif/tasks/produce_hif.json'):
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'new program')
        for name in ('hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
            shutil.copy2(ROOT / 'tools' / name, self.candidate / 'tools' / name)
        (self.candidate / 'upstream').mkdir()
        write(self.candidate / 'hif-release.json', self.incoming)
        write(self.candidate / 'hif-build-info.json', self.incoming)
        write(self.candidate / 'upstream-provenance.json', {'commit': 'new', 'files': {}})
        write(self.candidate / 'MFAAvalonia.runtimeconfig.json', {'runtimeOptions': {'framework': {'name': 'Microsoft.NETCore.App', 'version': '10.0.0'}}})
        write(self.candidate / 'config/config.json', {'theme': 'public'})
        write(self.candidate / 'config/new-feature.json', {'new': True})
        updater.package_manifest(self.candidate, self.incoming['version'])

    def package(self):
        path = self.base / ('MaaGakumasu-HIF-win-x64-' + self.incoming['version'] + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for file in self.candidate.rglob('*'):
                if file.is_file():
                    archive.write(file, 'MaaGakumasu-HIF/' + file.relative_to(self.candidate).as_posix())
        path.with_suffix('.zip.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name)
        return path

    def prepare(self):
        with patch.object(updater, 'runtime_prerequisites'), patch.object(updater.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            return updater.prepare_update(self.root, self.package())

    def test_preparation_accepts_frontend_runtime_changes_and_preserves_personal_bytes(self):
        before = digest_tree(self.root)
        result = self.prepare()
        self.assertEqual(result['state'], 'prepared')
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        self.assertEqual(operation['version'], 'v261009.1')
        self.assertIn('python/python.exe', operation['files'])
        self.assertIn('config/new-feature.json', operation['defaults'])
        self.assertNotIn('config/config.json', operation['files'])
        for name, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest)
        self.assertTrue((Path(operation['stage']) / 'hif_update_runner.ps1').is_file())
        self.assertFalse((self.root / 'backup').exists())

    def test_changed_release_requires_another_confirmation_before_downloading(self):
        with patch.object(updater, 'latest_release', return_value={'tag_name': 'v261009.3'}), \
                patch.object(updater.urllib.request, 'urlopen') as fetch:
            with self.assertRaisesRegex(ValueError, '发布版本已变化'):
                updater.prepare_update(self.root, expected_version='v261009.2')
        fetch.assert_not_called()
        self.assertFalse((self.root / 'temp/hif-update-pending.json').exists())

    def test_manifest_rejects_private_paths_duplicates_and_tampering(self):
        path = self.candidate / updater.MANIFEST
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['files']['config/config.json'] = manifest['defaults']['config/config.json']
        write(path, manifest)
        with self.assertRaisesRegex(ValueError, '个人文件'):
            updater.verify_manifest(self.candidate)
        manifest['files'].pop('config/config.json')
        manifest['files']['maaGakumasu.exe'] = manifest['files']['MaaGakumasu.exe']
        write(path, manifest)
        with self.assertRaisesRegex(ValueError, '重复'):
            updater.verify_manifest(self.candidate)
        manifest['files'].pop('maaGakumasu.exe')
        write(path, manifest)
        (self.candidate / 'MaaGakumasu.exe').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            updater.verify_manifest(self.candidate)

    def test_bad_checksum_unlisted_file_and_insufficient_space_abort_before_backup(self):
        package = self.package()
        package.with_suffix('.zip.sha256').write_text('0' * 64 + '  ' + package.name)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            updater.prepare_update(self.root, package)
        (self.candidate / 'secret.txt').write_bytes(b'not in manifest')
        with self.assertRaisesRegex(ValueError, '清单外'):
            updater.prepare_update(self.root, self.package())
        (self.candidate / 'secret.txt').unlink()
        with patch.object(updater.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)):
            with self.assertRaisesRegex(ValueError, '磁盘空间不足'):
                updater.prepare_update(self.root, self.package())
        self.assertFalse((self.root / 'backup').exists())

    def test_autobuild_channel_prepares_autobuild_package_by_source_commit(self):
        commit = 'e' * 40
        write(self.candidate / 'hif-build-info.json', {**self.incoming, 'build_channel': 'autobuild', 'source_commit': commit})
        updater.package_manifest(self.candidate, self.incoming['version'])
        built = self.package()
        package = built.with_name('MaaGakumasu-HIF-win-x64-autobuild.zip')
        built.rename(package)
        checksum = hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name
        release = {'tag_name': 'autobuild', 'version': self.incoming['version'], 'commit': commit, 'body': '- 测试版说明',
                   'assets': [{'name': n, 'browser_download_url': 'https://example.invalid/' + n} for n in (package.name, package.name + '.sha256')]}
        payloads = {package.name: package.read_bytes(), package.name + '.sha256': checksum.encode('utf-8')}

        def response(request, **kwargs):
            stream = io.BytesIO(payloads[request.full_url.rsplit('/', 1)[-1]])
            stream.headers = {}
            return stream
        identity = self.incoming['version'] + ' · AutoBuild · eeeeeee'
        with patch.object(updater, 'latest_release', return_value=release), patch.object(updater.urllib.request, 'urlopen', side_effect=response), \
                patch.object(updater, 'runtime_prerequisites'), patch.object(updater.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            result = updater.prepare_update(self.root, expected_version=identity, channel='autobuild')
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        self.assertEqual((operation['version'], operation['display_version']), (self.incoming['version'], identity))
        write(self.root / 'hif-build-info.json', {**self.current, 'build_channel': 'autobuild', 'source_commit': commit})
        with patch.object(updater, 'latest_release', return_value=release):
            self.assertTrue(updater.prepare_update(self.root, channel='autobuild')['skipped'])
        (self.root / 'hif-build-info.json').unlink()
        with patch.object(updater, 'latest_release', return_value={**release, 'commit': 'f' * 40}), \
                patch.object(updater.urllib.request, 'urlopen', side_effect=response):
            with self.assertRaisesRegex(ValueError, '源码提交不一致'):
                updater.prepare_update(self.root, channel='autobuild')

    def test_local_autobuild_package_and_reinstall_compare_the_source_commit(self):
        commit = 'e' * 40
        same = {**self.incoming, 'version': self.current['version']}
        write(self.candidate / 'hif-release.json', same)
        write(self.candidate / 'hif-build-info.json', {**same, 'build_channel': 'autobuild', 'source_commit': commit})
        updater.package_manifest(self.candidate, same['version'])
        package = self.package().rename(self.base / updater.AUTOBUILD_PACKAGE)
        package.with_suffix('.zip.sha256').write_text(hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name)

        def prepare(**options):
            with patch.object(updater, 'runtime_prerequisites'), \
                    patch.object(updater.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                return updater.prepare_update(self.root, package, **options)
        operation = json.loads(Path(prepare()['operation']).read_text(encoding='utf-8'))
        self.assertEqual(operation['display_version'], same['version'] + ' · AutoBuild · eeeeeee')
        write(self.root / 'hif-build-info.json', {**self.current, 'build_channel': 'autobuild', 'source_commit': commit})
        self.assertTrue(prepare()['skipped'])
        self.assertEqual(prepare(reinstall=True)['state'], 'prepared')
        package = package.rename(self.base / ('MaaGakumasu-HIF-win-x64-' + same['version'] + '.zip'))
        package.with_suffix('.zip.sha256').write_text(hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name)
        with self.assertRaisesRegex(ValueError, '名称与版本不一致'):
            prepare(reinstall=True)
        package.with_suffix('.zip.sha256').unlink()
        with self.assertRaisesRegex(ValueError, '缺少同名 SHA-256'):
            prepare(reinstall=True)

    def test_latest_release_and_existing_dependencies(self):
        with patch.object(updater, 'latest_release', return_value={'tag_name': 'v261008.2'}):
            self.assertTrue(updater.prepare_update(self.root)['skipped'])
        with patch.object(updater.subprocess, 'check_output', return_value='Microsoft.NETCore.App 10.0.12 [cached]\n'):
            updater.runtime_prerequisites(self.candidate)
        with patch.object(updater.subprocess, 'check_output', return_value='Microsoft.NETCore.App 8.0.12 [cached]\n'):
            with self.assertRaisesRegex(ValueError, '请先安装'):
                updater.runtime_prerequisites(self.candidate)

    def test_streamed_download_and_cancellation_preserve_installed_settings(self):
        package = self.package()
        release = {'tag_name': self.incoming['version'], 'assets': [
            {'name': name, 'browser_download_url': 'https://example.invalid/' + name}
            for name in (package.name, package.name + '.sha256')]}
        payloads = {package.name: package.read_bytes(), package.name + '.sha256': package.with_suffix('.zip.sha256').read_bytes()}
        requests = []
        def response(request, **kwargs):
            name = request.full_url.rsplit('/', 1)[-1]
            requests.append(name)
            stream = io.BytesIO(payloads[name])
            stream.headers = {'Content-Length': str(len(payloads[name]))}
            return stream
        with patch.object(updater, 'latest_release', return_value=release), patch.object(updater.urllib.request, 'urlopen', side_effect=response), \
                patch.object(updater, 'runtime_prerequisites'), patch.object(updater.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            result = updater.prepare_update(self.root)
        self.assertEqual(result['state'], 'prepared')
        # 先尝试分段下载；这里的模拟响应不是 206，于是改为下载并校验完整包。
        self.assertEqual(requests, [package.name, package.name, package.name + '.sha256'])
        self.assertEqual(result['download']['mode'], 'full')
        self.assertIn('不支持分段', result['download']['incremental_error'])
        original = (self.root / 'config/config.json').read_bytes()
        class CancelResponse(io.BytesIO):
            headers = {'Content-Length': '100'}
            def read(inner, size):
                pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
                (Path(pointer['operation']).parent / 'cancel').write_bytes(b'cancel')
                return b'partial download'
        with patch.object(updater, 'latest_release', return_value=release), \
                patch.object(updater.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: CancelResponse(b'partial')):
            with self.assertRaises(InterruptedError):
                updater.prepare_update(self.root)
        self.assertEqual((self.root / 'config/config.json').read_bytes(), original)
        self.assertFalse((self.root / 'backup').exists())
        pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
        self.assertFalse((Path(pointer['operation']).parent / package.name).exists())

    def test_skipped_preparation_removes_its_extraction(self):
        self.incoming = {**self.incoming, 'version': self.current['version']}
        write(self.candidate / 'hif-release.json', self.incoming)
        write(self.candidate / 'hif-build-info.json', self.incoming)
        updater.package_manifest(self.candidate, self.incoming['version'])
        self.assertTrue(self.prepare()['skipped'])
        pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
        stage = Path(pointer['operation']).parent
        self.assertFalse((stage / 'extracted').exists())
        self.assertEqual(json.loads((stage / 'progress.json').read_text(encoding='utf-8'))['state'], 'skipped')

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_progress_window_displays_file_counts_and_startup_wait(self):
        # Load only UI functions, without running an installer or showing a desktop window.
        script = r'''
param($Runner)
$ErrorActionPreference = 'Stop'
$text = [System.IO.File]::ReadAllText($Runner)
$start = $text.IndexOf('$ui = $null')
$end = $text.IndexOf('function Commit-File')
if ($start -lt 0 -or $end -le $start) { throw 'Progress UI block not found' }
. ([scriptblock]::Create($text.Substring($start, $end - $start).Replace('$form.Show()', '$form.CreateControl()')))
$ShowProgress = $true
$Recover = $false
$op = [pscustomobject]@{ id = 'a' * 32; from_version = 'v261105.1'; version = 'v261111.1' }
Open-ProgressWindow
try {
    if ($ui.form.ControlBox -or $ui.close.Visible) { throw 'Installer can be closed during replacement' }
    if ($ui.version.Text -notlike '*v261105.1*v261111.1*') { throw 'Version change missing' }
    Show-UpdateProgress replace '正在替换文件' '3 / 10，5 个未变已跳过' 3 10 'frontend/a.dll'
    if ($ui.step -ne 2 -or $ui.percent -ne 59 -or $ui.percentText.Text -ne '59%') { throw "Overall progress wrong: $($ui.step) $($ui.percent)" }
    if ($ui.detail.Text -notlike '*3 / 10*' -or $ui.file.Text -ne 'frontend/a.dll') { throw 'File progress missing' }
    Show-UpdateProgress start '正在启动新版' '文件已替换，等待新版启动确认。'
    if ($ui.step -ne 3 -or $ui.percent -lt 92 -or $ui.percent -ge 100 -or $ui.action.Text -notlike '*启动新版*') { throw 'Startup verification appears complete' }
    Show-UpdateProgress restore '正在还原程序文件' '1 / 4' 1 4 'frontend/a.dll'
    if (-not $ui.failed -or $ui.step -ne 3 -or $ui.percent -ne 25 -or $ui.title.Text -notlike '*恢复*') { throw 'Restore progress missing' }
} finally { $ui.form.Dispose() }
'''
        check = self.base / 'progress-check.ps1'
        check.write_text(script, encoding='utf-8-sig')
        completed = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File',
                                    str(check), str(ROOT / 'tools/hif_update_runner.ps1')],
                                   capture_output=True, text=True, errors='replace', timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_atomic_copy_retries_briefly_locked_target_and_names_file(self):
        # A handle without delete sharing (e.g. antivirus scanning) makes File.Replace fail until it is closed.
        script = r'''
param($Runner, $Directory)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Runner, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Commit-File', 'Atomic-Copy')) {
    Invoke-Expression $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true).Extent.Text
}
$source = Join-Path $Directory 'source.txt'; $target = Join-Path $Directory 'target.txt'
[System.IO.File]::WriteAllText($source, 'new'); [System.IO.File]::WriteAllText($target, 'old')
$lock = [System.IO.File]::Open($target, 'Open', 'Read', 'Read')
$timer = [System.Timers.Timer]::new(400); $timer.AutoReset = $false
Register-ObjectEvent $timer Elapsed -MessageData $lock -Action { $Event.MessageData.Dispose() } | Out-Null
$timer.Start()
Atomic-Copy $source $target
if ([System.IO.File]::ReadAllText($target) -ne 'new') { throw 'Transient lock was not retried' }
$lock = [System.IO.File]::Open($target, 'Open', 'Read', 'Read')
try { Atomic-Copy $source $target; throw 'Permanent lock did not fail' }
catch { if ($_.Exception.Message -notlike "*$target*") { throw "File name missing: $($_.Exception.Message)" } }
finally { $lock.Dispose() }
if ([System.IO.File]::ReadAllText($target) -ne 'new' -or @(Get-ChildItem -LiteralPath $Directory -Filter '*.tmp').Count) { throw 'Target damaged or temporary file left' }
'''
        check = self.base / 'atomic-copy-check.ps1'
        check.write_text(script, encoding='utf-8-sig')
        work = self.base / 'atomic-copy'
        work.mkdir()
        completed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                    str(check), str(ROOT / 'tools/hif_update_runner.ps1'), str(work)],
                                   capture_output=True, text=True, errors='replace', timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_runner_refuses_tampered_plan_and_candidate_before_backing_up(self):
        result = self.prepare()
        operation_path = Path(result['operation'])
        operation = json.loads(operation_path.read_text(encoding='utf-8'))
        (Path(operation['candidate']) / 'MaaGakumasu.exe').write_bytes(b'tampered')
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(Path(operation['stage']) / 'hif_update_runner.ps1'), '-OperationPath', str(operation_path)]
        result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=30)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('verification failed', result.stderr)
        self.assertFalse((self.root / 'backup').exists())

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_installer_replaces_only_changed_files_and_prunes_finished_updates(self):
        where = Path(os.environ['SystemRoot']) / 'System32/where.exe'
        for directory in (self.root, self.candidate):
            shutil.copy2(where, directory / 'MaaGakumasu.exe')
        (self.root / 'python').mkdir()
        (self.root / 'python/python.exe').write_bytes(b'new program')
        (self.root / 'tools/validate.py').write_bytes(b'old validator')
        (self.root / 'old-only.txt').write_bytes(b'removed in the new version')
        (self.root / 'unlisted-personal-plugin.txt').write_bytes(b'personal plugin')
        updater.package_manifest(self.candidate, self.incoming['version'])
        # The installed manifest is only a list of owned files; hashes come from the installed files themselves.
        owned = ('MaaGakumasu.exe', 'python/python.exe', 'tools/validate.py', 'old-only.txt')
        write(self.root / updater.MANIFEST, {'format': 1, 'version': self.current['version'], 'repository': updater.REPOSITORY,
                                             'files': {name: '0' * 64 for name in owned}, 'defaults': {}})
        stamp = 1_600_000_000
        os.utime(self.root / 'python/python.exe', (stamp, stamp))
        result = self.prepare()
        operation_path = Path(result['operation'])
        operation = json.loads(operation_path.read_text(encoding='utf-8'))
        stage = Path(operation['stage'])
        # A real frontend acknowledges startup; the stand-in executable cannot, so the acknowledgement is written in advance.
        (stage / 'startup.ok').write_text(operation['id'] + ':' + operation['version'], encoding='utf-8')
        updates, backups = self.root / 'temp/hif-updates', self.root / 'backup'

        def earlier_operation(state, age, backup=True):
            identity = uuid.uuid4().hex
            (updates / identity / 'extracted').mkdir(parents=True)
            (updates / identity / 'extracted/leftover.bin').write_bytes(b'x' * 1024)
            if state:
                write(updates / identity / 'operation.json', {'format': 1, 'id': identity, 'state': state})
                os.utime(updates / identity / 'operation.json', (stamp + age, stamp + age))
            if backup:
                (backups / ('hif-update-' + identity) / 'old').mkdir(parents=True)
            return identity
        earlier_operation('complete', 1)
        newer = earlier_operation('complete', 2)
        earlier_operation('rolled_back', 3)
        needs_recovery = earlier_operation('rollback_failed', 4)
        earlier_operation('prepared', 5, backup=False)
        earlier_operation(None, 6, backup=False)
        (backups / ('hif-update-' + uuid.uuid4().hex)).mkdir()
        (backups / 'manual-keep').mkdir()

        completed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                    str(stage / 'hif_update_runner.ps1'), '-OperationPath', str(operation_path)],
                                   capture_output=True, text=True, errors='replace', timeout=120)
        after = json.loads(operation_path.read_text(encoding='utf-8-sig'))
        self.assertEqual(after['state'], 'complete', completed.stdout + completed.stderr)
        backup = Path(after['backup'])
        journal = [json.loads(line)['name'] for line in (backup / 'changes.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        unchanged = ('MaaGakumasu.exe', 'python/python.exe', 'tools/hif_update_runner.ps1', 'tools/export_settings.ps1', 'tools/import_settings.ps1')
        for name in unchanged:
            self.assertNotIn(name, journal)
            self.assertFalse((backup / 'old' / name).exists(), name)
        self.assertEqual(os.stat(self.root / 'python/python.exe').st_mtime, stamp)
        self.assertEqual((after['changed_files'], after['unchanged_files']), (len(journal), 5))
        self.assertEqual((self.root / 'tools/validate.py').read_bytes(), b'new program')
        self.assertEqual((backup / 'old/tools/validate.py').read_bytes(), b'old validator')
        self.assertFalse((self.root / 'old-only.txt').exists())
        self.assertEqual((backup / 'old/old-only.txt').read_bytes(), b'removed in the new version')
        self.assertEqual((self.root / 'unlisted-personal-plugin.txt').read_bytes(), b'personal plugin')
        self.assertEqual(json.loads((self.root / 'config/config.json').read_text(encoding='utf-8-sig')), {'theme': 'personal'})
        self.assertTrue((self.root / 'config/new-feature.json').is_file())
        # Updates that may need recovery and the newest completed one stay; the rest, with their downloads, are removed.
        self.assertEqual({p.name for p in updates.iterdir()}, {operation['id'], newer, needs_recovery})
        self.assertEqual({p.name for p in backups.iterdir() if p.is_dir()},
                         {'hif-update-' + i for i in (operation['id'], newer, needs_recovery)} | {'manual-keep'})
        self.assertEqual(after['cleaned_operations'], 7)
        self.assertFalse((self.root / 'temp/hif-update-pending.json').exists())

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_installation_aborted_before_changes_reopens_the_old_frontend(self):
        # A copy of ping.exe stands in for the frontend: it runs for a moment as the parent, then exits as for installation.
        shutil.copy2(Path(os.environ['SystemRoot']) / 'System32/PING.EXE', self.root / 'MaaGakumasu.exe')
        result = self.prepare()
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        stage = Path(operation['stage'])
        (Path(operation['candidate']) / 'tools/validate.py').write_bytes(b'tampered after preparation')
        for restart in (True, False):
            with self.subTest(restart=restart):
                operation['state'] = 'prepared'
                write(Path(result['operation']), operation)
                (stage / 'commit.ready').write_text(operation['id'], encoding='utf-8')
                (stage / 'runner.ready').unlink(missing_ok=True)
                # 父进程一直运行到安装器核对完身份（写出 runner.ready）再结束，不受机器快慢影响。
                parent = subprocess.Popen([str(self.root / 'MaaGakumasu.exe'), '-n', '120', '127.0.0.1'],
                                          stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    ticks = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                                                     f'(Get-Process -Id {parent.pid}).StartTime.ToUniversalTime().Ticks'], text=True).strip()
                    command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(stage / 'hif_update_runner.ps1'),
                               '-OperationPath', result['operation'], '-ParentId', str(parent.pid), '-ParentStartTicks', ticks]
                    runner = subprocess.Popen(command + ([] if restart else ['-NoRestart']),
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
                    deadline = time.monotonic() + 60
                    while not (stage / 'runner.ready').exists() and runner.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                    parent.kill()
                    stdout, stderr = runner.communicate(timeout=120)
                    completed = subprocess.CompletedProcess(command, runner.returncode, stdout, stderr)
                finally:
                    parent.kill()
                    parent.wait(timeout=30)
                self.assertNotEqual(completed.returncode, 0)
                after = json.loads(Path(result['operation']).read_text(encoding='utf-8-sig'))
                self.assertEqual(after['state'], 'failed', completed.stdout + completed.stderr)
                self.assertIn('verification failed', after['error'])
                self.assertEqual('restored_pid' in after, restart)
                self.assertFalse((self.root / 'backup').exists())

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_late_file_lock_rolls_back_programs_defaults_and_settings(self):
        shutil.copy2(Path(os.environ['SystemRoot']) / 'System32/where.exe', self.root / 'MaaGakumasu.exe')
        (self.root / 'zz-locked.txt').write_bytes(b'old locked file')
        (self.root / 'unlisted-personal-plugin.txt').write_bytes(b'personal plugin')
        (self.candidate / 'zz-locked.txt').write_bytes(b'new version')
        updater.package_manifest(self.candidate, self.incoming['version'])
        before = digest_tree(self.root)
        result = self.prepare()
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        quote = "'" + str(self.root / 'zz-locked.txt').replace("'", "''") + "'"
        locker = subprocess.Popen(['powershell.exe', '-NoProfile', '-Command',
                    '$s=[System.IO.File]::Open(' + quote + ',[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::Read); '
                    "[Console]::WriteLine('READY'); [Console]::ReadLine() | Out-Null; $s.Dispose()"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(locker.stdout.readline().strip(), 'READY')
            completed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(Path(operation['stage']) / 'hif_update_runner.ps1'), '-OperationPath', result['operation'], '-NoRestart'],
                    capture_output=True, text=True, errors='replace', timeout=60)
            self.assertNotEqual(completed.returncode, 0)
            after = json.loads(Path(result['operation']).read_text(encoding='utf-8-sig'))
            self.assertEqual(after['state'], 'rolled_back', completed.stdout + completed.stderr)
            for name, digest in before.items():
                self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest, name)
            self.assertFalse((self.root / 'config/new-feature.json').exists())
            self.assertFalse((self.root / updater.MANIFEST).exists())
            self.assertTrue(Path(after['settings_backup']).is_file())
        finally:
            locker.communicate('\n', timeout=10)

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows required')
    def test_recovery_restores_interrupted_transaction_and_ignores_uncommitted_tail(self):
        shutil.copy2(Path(os.environ['SystemRoot']) / 'System32/where.exe', self.root / 'MaaGakumasu.exe')
        original = (self.root / 'hif-release.json').read_bytes()
        result = self.prepare()
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        backup = Path(operation['backup'])
        backup.mkdir(parents=True)
        export = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(Path(operation['stage']) / 'export_settings.ps1'), '-Root', str(self.root),
                    '-OutputDirectory', str(backup), '-PassThru'], capture_output=True, timeout=30)
        self.assertEqual(export.returncode, 0)
        operation['settings_backup'] = str(next(backup.glob('*.zip')))
        (backup / 'old').mkdir()
        (backup / 'old/hif-release.json').write_bytes(original)
        (backup / 'changes.jsonl').write_text('{"name":"hif-release.json","existed":true}\n{"name":"not-flushed', encoding='utf-8')
        write(self.root / 'hif-release.json', self.incoming)
        write(self.root / 'config/created-during-startup.json', {'new': True})
        operation['state'] = 'installing'
        write(result['operation'], operation)
        completed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(Path(operation['stage']) / 'hif_update_runner.ps1'), '-OperationPath', result['operation'], '-Recover', '-NoRestart'],
                    capture_output=True, text=True, errors='replace', timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual((self.root / 'hif-release.json').read_bytes(), original)
        self.assertFalse((self.root / 'config/created-during-startup.json').exists())


if __name__ == '__main__':
    unittest.main()
