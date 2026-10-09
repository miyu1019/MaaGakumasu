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


if __name__ == '__main__':
    unittest.main()
