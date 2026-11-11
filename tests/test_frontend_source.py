import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import hif_build as builder


class FrontendSourceTests(unittest.TestCase):
    def test_source_check_uses_repo_and_needs_no_download(self):
        self.assertEqual(builder.FRONTEND, ROOT / 'frontend')
        with patch.object(builder, 'download', side_effect=AssertionError('unexpected download')):
            builder.prepare_frontend()

    def test_source_identity_tracks_changes_but_ignores_output_and_line_endings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            source = Path(directory)
            project = source / 'MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj'
            project.parent.mkdir()
            project.write_bytes(b'<Project/>\n')
            (source / 'LICENSE').write_text('license')
            code = source / 'Window.cs'
            code.write_bytes(b'original\n')
            with patch.object(builder, 'FRONTEND', source):
                original = builder.frontend_fingerprint()
                for folder in ('bin', 'obj', '.avalonia-build-tasks'):
                    output = source / folder
                    output.mkdir()
                    (output / 'generated.cs').write_text('build output')
                code.write_bytes(b'original\r\n')
                self.assertEqual(builder.frontend_fingerprint(), original)
                code.write_bytes(b'changed\n')
                self.assertNotEqual(builder.frontend_fingerprint(), original)

    def test_agent_binaries_are_published_only_under_libs(self):
        # Maa.AgentBinary 默认复制到输出根目录 MaaAgentBinary/，核心库的这份会随项目引用进入桌面发布目录。
        for project in ('MFAAvalonia/MFAAvalonia.csproj', 'MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj'):
            text = (ROOT / 'frontend' / project).read_text(encoding='utf-8-sig')
            self.assertTrue('<MaaAgentBinaryPath>libs/MaaAgentBinary</MaaAgentBinaryPath>' in text, project)

    def test_frontend_publish_starts_empty_and_rejects_root_agent_binaries(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            build = Path(directory)
            stale = build / 'frontend-publish/MaaAgentBinary/minicap/x86/minicap'
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b'left by an earlier publish')
            published = []

            def publish(command, *args, **kwargs):
                output = Path(command[command.index('-o') + 1])
                published.append(sorted(p.name for p in output.iterdir()) if output.exists() else [])
                (output / 'libs/MaaAgentBinary').mkdir(parents=True)
            with patch.object(builder, 'BUILD', build), patch.object(builder, 'run', side_effect=publish):
                self.assertEqual(builder.frontend_build(), build / 'frontend-publish')
            self.assertEqual(published, [[]])

            def publish_with_root_copy(command, *args, **kwargs):
                (Path(command[command.index('-o') + 1]) / 'MaaAgentBinary').mkdir(parents=True)
            with patch.object(builder, 'BUILD', build), patch.object(builder, 'run', side_effect=publish_with_root_copy):
                with self.assertRaisesRegex(ValueError, 'MaaAgentBinaryPath'):
                    builder.frontend_build()


if __name__ == '__main__':
    unittest.main()
