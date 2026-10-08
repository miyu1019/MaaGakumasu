import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import hif_build
import hif_update
from hif_app import write, compose, digest_tree, extract_package, python_executable


class LinuxReleaseTests(unittest.TestCase):
    def test_linux_composition_resolves_case_without_changing_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / 'hif-build-info.json', {'platform': 'linux-x64'})
            write(root / 'upstream/interface.json', {'interface_version': 2, 'import': ['./tasks/Shutdown.json'],
                  'resource': [{'name': '官服', 'path': ['./resource/base']}], 'task': []})
            write(root / 'upstream/tasks/shutdown.json', {'task': []})
            (root / 'upstream/agent').mkdir()
            (root / 'upstream/agent/main.py').write_text('')
            write(root / 'extensions/hif/tasks/produce_hif.json', {'task': []})
            before = digest_tree(root / 'upstream')
            interface, _ = compose(root)
            self.assertEqual(interface['import'][0], './upstream/tasks/shutdown.json')
            self.assertEqual(interface['agent']['child_exec'], './python/bin/python3')
            self.assertEqual(before, digest_tree(root / 'upstream'))

    def test_linux_archive_can_be_relocated_and_keeps_executable_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            (runtime / 'python/bin').mkdir(parents=True)
            binary = runtime / 'python/bin/python3.12'
            binary.write_bytes(b'python fixture')
            binary.chmod(0o755)
            (runtime / 'python/bin/python3').symlink_to('python3.12')
            (runtime / 'runtimes/linux-x64/native/plugins').mkdir(parents=True)
            (runtime / 'debug').mkdir()
            (runtime / 'debug/private.log').write_text('private')
            write(runtime / 'hif-build-info.json', {'platform': 'linux-x64'})
            with patch.object(hif_build, 'ROOT', root):
                package = hif_build.package(runtime, 'linux-x64')
            with tarfile.open(package) as archive:
                self.assertFalse(any(p.issym() or p.islnk() for p in archive))
                self.assertNotIn('MaaGakumasu-HIF/debug', archive.getnames())
            extract_package(package, root / 'relocated')
            installed = root / 'relocated/MaaGakumasu-HIF'
            self.assertTrue(python_executable(installed).stat().st_mode & 0o111)
            self.assertEqual(python_executable(installed).read_bytes(), binary.read_bytes())
            self.assertTrue((installed / 'runtimes/linux-x64/native/plugins').is_dir())
            hif_update.verify_checksum(package, Path(str(package) + '.sha256'))

    def test_updater_rejects_another_platform_before_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'installed'
            candidate = Path(directory) / 'candidate'
            for folder, platform in ((root, 'linux-x64'), (candidate, 'win-x64')):
                write(folder / 'hif-release.json', {'version': 'v261008.2'})
                write(folder / 'hif-build-info.json', {'platform': platform})
            with self.assertRaisesRegex(ValueError, '平台不匹配'):
                hif_update.validate_candidate(root, candidate)
