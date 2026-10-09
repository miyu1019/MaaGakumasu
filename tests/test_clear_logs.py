from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/clear_logs.ps1'


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class ClearLogsTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'runtime with spaces'
        self.root.mkdir()
        self.removable = ['logs/odd [2] !.log', 'logs/nested/old.log.zip',
                          'debug/custom/agent.log', 'debug/card.png', 'debug/a/b/photo.webp']
        self.protected = ['logs/logo.png', 'debug/verification.json', 'debug/other.zip',
                          'resource/template.png', 'config/hif/cards_priority.json',
                          'config/config.json', 'backup/old.log',
                          'config/cards_priority.before_visual_editor.json',
                          'config/hif/cards_priority.before_visual_editor.json']
        for name in self.removable + self.protected:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserve or remove exactly this file')

    def run_script(self, *args):
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(SCRIPT), '-Root', str(self.root), *args],
                              capture_output=True, text=True, errors='replace', timeout=30)

    def test_preview_and_cleanup_preserve_settings_and_non_diagnostic_files(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = self.run_script('-DryRun')
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn('5 files would be deleted', preview.stdout)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.removable:
            self.assertFalse((self.root / name).exists(), name)
        for name in self.protected:
            self.assertEqual((self.root / name).read_bytes(), b'preserve or remove exactly this file', name)

    def test_optional_backup_flag_removes_only_editor_snapshots(self):
        result = self.run_script('-PriorityBackup')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.protected:
            self.assertEqual((self.root / name).exists(), not name.endswith('before_visual_editor.json'), name)

    def test_locked_log_is_kept_and_other_files_are_cleaned(self):
        path = self.root / self.removable[0]
        with path.open('rb'):
            result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(path.exists())
        self.assertFalse((self.root / self.removable[1]).exists())
        self.assertTrue((self.root / 'config/hif/cards_priority.json').exists())

    def test_batch_wrapper_keeps_legacy_flags(self):
        shutil.copy2(ROOT / 'clear_logs_清除日志.bat', self.root)
        (self.root / 'tools').mkdir()
        shutil.copy2(SCRIPT, self.root / 'tools/clear_logs.ps1')
        result = subprocess.run(['cmd.exe', '/c', str(self.root / 'clear_logs_清除日志.bat'),
                                 '--dry-run', '--priority-backup', '--no-pause'],
                                capture_output=True, text=True, errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('7 files would be deleted', result.stdout)
        self.assertTrue((self.root / self.removable[0]).exists())

    def test_junction_does_not_expose_external_logs_to_cleanup(self):
        outside = Path(self.directory.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.log'
        sentinel.write_bytes(b'outside the cleanup scope')
        link = self.root / 'debug/external'
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        created = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                  'New-Item -ItemType Junction -Path ' + quote(link) +
                                  ' -Target ' + quote(outside) + ' | Out-Null'],
                                 capture_output=True, text=True, errors='replace', timeout=30)
        if created.returncode and 'Access is denied' in created.stderr:
            self.skipTest('This environment does not permit creating junctions')
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sentinel.read_bytes(), b'outside the cleanup scope')


if __name__ == '__main__':
    unittest.main()
