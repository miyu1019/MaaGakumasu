import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import hif_app  # noqa: E402


@unittest.skipUnless(sys.platform == 'win32', 'Windows 证书库')
class HttpsContextTest(unittest.TestCase):
    def setUp(self):
        hif_app._HTTPS_CONTEXT = None
        self.addCleanup(setattr, hif_app, '_HTTPS_CONTEXT', None)

    def test_only_trusted_root_store_is_loaded(self):
        stores = []
        real = ssl.enum_certificates

        def enum(store):
            stores.append(store)
            return real(store)

        with patch.object(hif_app.ssl, 'enum_certificates', side_effect=enum):
            context = hif_app.https_context()
        self.assertEqual(stores, ['ROOT'])
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertIs(hif_app.https_context(), context)

    def test_empty_root_store_falls_back_to_default_context(self):
        with patch.object(hif_app.ssl, 'enum_certificates', return_value=[]):
            context = hif_app.https_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == '__main__':
    unittest.main()
