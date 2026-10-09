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
        self.assertEqual(requests, [package.name, package.name + '.sha256'])
        original = (self.root / 'config/config.json').read_bytes()
        class CancelResponse(io.BytesIO):
            headers = {'Content-Length': '100'}
            def read(inner, size):
                pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
                (Path(pointer['operation']).parent / 'cancel').write_bytes(b'cancel')
                return b'partial download'
        with patch.object(updater, 'latest_release', return_value=release), \
                patch.object(updater.urllib.request, 'urlopen', return_value=CancelResponse(b'partial')):
            with self.assertRaises(InterruptedError):
                updater.prepare_update(self.root)
        self.assertEqual((self.root / 'config/config.json').read_bytes(), original)
        self.assertFalse((self.root / 'backup').exists())

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
