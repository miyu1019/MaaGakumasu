import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/export_settings.ps1'


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class ExportSettingsTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / '程序 with spaces [2] !'
        self.root.mkdir()
        self.settings = {
            'config/config.json': b'\xef\xbb\xbf{"theme":"blue"}\r\n',
            'config/instances/hif.json': '{"name":"一键培育","connection":"private"}'.encode(),
            'config/hif/cards_priority.json': b'{"priority_profiles":{"focus":{"card":7}}}',
            'config/hif/hif_drink_profiles.json': b'{"max_owned":0}',
            'config/hif/hif_custom_card_selection.json': b'{"profiles":{}}',
            'config/cards_priority.json': b'{"legacy":true}',
            'config/hif/职业 [2] !.json': b'{"custom":true}',
            'config/empty-file.json': b'',
            'appsettings.json': b'{"Instances.List":["hif"],"Timer.Timer1.Schedule":"daily"}',
            'resource/mfa_layout.json': b'{"layout":"personal"}',
        }
        for name, data in {**self.settings, 'debug/run.log': b'log', 'resource/template.png': b'image',
                           'backup/old.zip': b'previous backup', 'MaaGakumasu.exe': b'program'}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def run_script(self, *args):
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(SCRIPT), '-Root', str(self.root), *args],
                              capture_output=True, text=True, errors='replace', timeout=30)

    def test_export_preserves_source_and_all_settings_bytes_and_hashes(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for _ in range(2):
            result = self.run_script()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archives = list((self.root / 'backup').glob('maa-settings-*.zip'))
        self.assertEqual(len(archives), 2)
        for path in archives:
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), set(self.settings) | {'backup-manifest.json', '恢复说明.txt'})
                manifest = json.loads(archive.read('backup-manifest.json'))
                self.assertEqual(set(manifest['files']), set(self.settings))
                for name, data in self.settings.items():
                    self.assertEqual(archive.read(name), data)
                    self.assertEqual(manifest['files'][name], {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
                self.assertIn('appsettings.json', archive.read('恢复说明.txt').decode('utf-8'))
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse(list(self.root.rglob('*.partial')))

    def test_legacy_config_without_optional_files_and_custom_output(self):
        (self.root / 'appsettings.json').unlink()
        (self.root / 'resource/mfa_layout.json').unlink()
        output = Path(self.directory.name) / 'backups elsewhere'
        result = self.run_script('-OutputDirectory', str(output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(next(output.glob('*.zip'))) as archive:
            self.assertIn('config/cards_priority.json', archive.namelist())
            self.assertNotIn('appsettings.json', archive.namelist())
            self.assertNotIn('resource/mfa_layout.json', archive.namelist())

    def test_empty_or_missing_config_does_not_create_archive(self):
        for path in (self.root / 'config').rglob('*'):
            if path.is_file():
                path.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('empty', result.stderr)
        empty_root = Path(self.directory.name) / 'no config'
        empty_root.mkdir()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(SCRIPT), '-Root', str(empty_root)], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.root.rglob('maa-settings-*')))

    def test_output_inside_config_is_rejected_without_creating_it(self):
        output = self.root / 'config/new-backup'
        result = self.run_script('-OutputDirectory', str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_batch_wrapper_exports_from_its_own_directory(self):
        shutil.copy2(ROOT / 'export_settings_导出设置.bat', self.root)
        (self.root / 'tools').mkdir()
        shutil.copy2(SCRIPT, self.root / 'tools/export_settings.ps1')
        result = subprocess.run(['cmd.exe', '/c', str(self.root / 'export_settings_导出设置.bat'), '--no-pause'],
                                capture_output=True, text=True, errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list((self.root / 'backup').glob('maa-settings-*.zip'))), 1)

    def test_config_junction_is_rejected_without_exporting_external_data(self):
        outside = Path(self.directory.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'private.json'
        sentinel.write_bytes(b'external data')
        link = self.root / 'config/external'
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        created = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                  'New-Item -ItemType Junction -Path ' + quote(link) +
                                  ' -Target ' + quote(outside) + ' | Out-Null'],
                                 capture_output=True, text=True, errors='replace', timeout=30)
        if created.returncode and 'Access is denied' in created.stderr:
            self.skipTest('This environment does not permit creating junctions')
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        try:
            result = self.run_script()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(self.root.rglob('maa-settings-*.zip')))
            self.assertEqual(sentinel.read_bytes(), b'external data')
        finally:
            link.rmdir()

    def make_backup(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return next((self.root / 'backup').glob('maa-settings-*.zip'))

    def destination(self):
        target = Path(self.directory.name) / '新版 with spaces [3] !'
        (target / 'config').mkdir(parents=True)
        (target / 'config/config.json').write_bytes(b'{"newer_theme":"green"}')
        (target / 'config/new-setting.json').write_bytes(b'{"new_default":true}')
        (target / 'interface.json').write_bytes(b'{"task_options":"new definitions"}')
        return target

    def run_import(self, target, archive):
        return subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                               '-File', str(ROOT / 'tools/import_settings.ps1'), '-Root', str(target),
                               '-ArchivePath', str(archive)],
                              capture_output=True, text=True, errors='replace', timeout=30)

    def test_import_round_trip_backs_up_destination_and_keeps_new_definitions(self):
        archive = self.make_backup()
        target = self.destination()
        result = self.run_import(target, archive)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, data in self.settings.items():
            self.assertEqual((target / name).read_bytes(), data, name)
        self.assertEqual((target / 'config/new-setting.json').read_bytes(), b'{"new_default":true}')
        self.assertEqual((target / 'interface.json').read_bytes(), b'{"task_options":"new definitions"}')
        with zipfile.ZipFile(next((target / 'backup').glob('maa-settings-*.zip'))) as backup:
            self.assertEqual(backup.read('config/config.json'), b'{"newer_theme":"green"}')
        self.assertFalse(list(target.rglob('*.tmp')))

    def test_import_rejects_tampering_and_traversal_before_changes(self):
        original = self.make_backup()
        target = self.destination()
        bad = Path(self.directory.name) / 'bad.zip'
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(bad, 'w') as archive:
            for name in source.namelist():
                archive.writestr(name, b'tampered' if name == 'config/config.json' else source.read(name))
        result = self.run_import(target, bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('verification failed', result.stderr)
        with zipfile.ZipFile(bad, 'w') as archive:
            name = 'config/../../outside.json'
            data = b'outside'
            archive.writestr(name, data)
            archive.writestr('backup-manifest.json', json.dumps({'format': 1, 'files': {
                name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}))
        result = self.run_import(target, bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid backup path', result.stderr)
        self.assertEqual((target / 'config/config.json').read_bytes(), b'{"newer_theme":"green"}')
        self.assertFalse((target / 'backup').exists())

    def test_import_rolls_back_written_files_when_a_later_file_is_locked(self):
        archive = self.make_backup()
        target = self.destination()
        layout = target / 'resource/mfa_layout.json'
        layout.parent.mkdir()
        layout.write_bytes(b'{"layout":"new"}')
        quote = "'" + str(layout).replace("'", "''") + "'"
        command = ('$locked = [System.IO.File]::Open(' + quote +
                   ', [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read); '
                   "[Console]::WriteLine('READY'); [Console]::ReadLine() | Out-Null; $locked.Dispose()")
        locker = subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', command],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(locker.stdout.readline().strip(), 'READY')
            result = self.run_import(target, archive)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('original setting files restored', result.stderr)
            self.assertEqual((target / 'config/config.json').read_bytes(), b'{"newer_theme":"green"}')
            self.assertFalse((target / 'appsettings.json').exists())
            self.assertFalse((target / 'config/hif/cards_priority.json').exists())
            self.assertEqual(layout.read_bytes(), b'{"layout":"new"}')
            self.assertEqual(len(list((target / 'backup').glob('maa-settings-*.zip'))), 1)
            self.assertFalse(list(target.rglob('*.tmp')))
        finally:
            locker.communicate('\n', timeout=10)

    def test_import_batch_accepts_dragged_zip_path(self):
        archive = self.make_backup()
        target = self.destination()
        (target / 'tools').mkdir()
        for name in ('export_settings.ps1', 'import_settings.ps1'):
            shutil.copy2(ROOT / 'tools' / name, target / 'tools' / name)
        shutil.copy2(ROOT / 'import_settings_导入设置.bat', target)
        command = f'cmd.exe /d /s /c ""{target / "import_settings_导入设置.bat"}" "{archive}" --no-pause"'
        result = subprocess.run(command,
                                capture_output=True, text=True, errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((target / 'config/hif/cards_priority.json').read_bytes(), self.settings['config/hif/cards_priority.json'])


if __name__ == '__main__':
    unittest.main()
