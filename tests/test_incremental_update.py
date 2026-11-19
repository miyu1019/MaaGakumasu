from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hif_update as updater
from hif_app import digest_tree, write
import test_full_update as full  # 只借用夹具；不导入测试类，避免重复收集


class AssetServer:
    """模拟 GitHub 发布资源：下载地址 302 到带签名的地址；只支持单段明确范围，末尾范围和多段返回 501；
    ETag 配 If-Match，不一致返回 412；If-Range 被忽略。"""

    def __init__(self):
        self.files, self.etags, self.requests = {}, {}, []
        self.served = 0
        self.signature = 0
        self.expired = set()
        self.ignore_range = False
        self.drop_next = 0
        self.on_signed = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body=b'', headers=()):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    server.served += len(body)
                except (ConnectionError, OSError):
                    pass  # 客户端发现不是 206 后会直接断开

            def do_GET(self):
                download = re.fullmatch(r'/download/([^/?]+)', self.path)
                if download:
                    server.signature += 1
                    server.requests.append(('download', download[1], None, None))
                    return self.send(302, headers=[('Location', '/signed/%s?sig=%d' % (download[1], server.signature))])
                signed = re.fullmatch(r'/signed/([^/?]+)\?sig=(\d+)', self.path)
                if not signed or signed[1] not in server.files:
                    return self.send(404)
                name, signature = signed[1], int(signed[2])
                wanted, condition = self.headers.get('Range'), self.headers.get('If-Match')
                server.requests.append(('signed', name, wanted, condition))
                if server.drop_next:
                    server.drop_next -= 1
                    self.close_connection = True
                    return  # 不回应，模拟握手或连接中断
                if signature in server.expired:
                    return self.send(403)
                if server.on_signed:
                    server.on_signed(server, name)
                data, etag = server.files[name], server.etags[name]
                if condition and condition != etag:
                    return self.send(412)
                if not wanted or server.ignore_range:
                    return self.send(200, data, [('ETag', etag)])
                match = re.fullmatch(r'bytes=(\d+)-(\d+)', wanted)
                if not match:
                    return self.send(501)
                start, end = int(match[1]), min(int(match[2]), len(data) - 1)
                return self.send(206, data[start:end + 1], [('ETag', etag), ('Accept-Ranges', 'bytes'),
                                                             ('Content-Range', 'bytes %d-%d/%d' % (start, end, len(data)))])

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name):
        return 'http://127.0.0.1:%d/download/%s' % (self.httpd.server_address[1], name)

    def publish(self, path, etag='"v1"'):
        self.files[path.name] = path.read_bytes()
        self.etags[path.name] = etag

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def signed(self, name):
        return [r for r in self.requests if r[0] == 'signed' and r[1] == name]


@unittest.skipUnless(os.name == 'nt', 'Full-package installation is Windows x64 only')
class IncrementalUpdateTests(unittest.TestCase):
    setUp_fixture = full.FullUpdateTests.setUp
    package = full.FullUpdateTests.package

    def setUp(self):
        self.setUp_fixture()
        # 不可压缩的大文件让完整包跨越多个块；已安装目录与新版大多相同，只有少数文件不同。
        for name, size in (('libs/unchanged-a.dll', 96 * 1024), ('libs/unchanged-b.dll', 96 * 1024), ('libs/changed.dll', 24 * 1024)):
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(size))
        updater.package_manifest(self.candidate, self.incoming['version'])
        for path in self.candidate.rglob('*'):
            relative = path.relative_to(self.candidate)
            if path.is_file() and relative.parts[0] not in ('config', 'hif-release.json') and relative.as_posix() != updater.MANIFEST:
                (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
                if not (self.root / relative).exists():
                    shutil.copy2(path, self.root / relative)
        (self.root / 'libs/changed.dll').write_bytes(b'old library')
        (self.root / 'MaaGakumasu.exe').write_bytes(b'old program')
        self.server = AssetServer()
        self.addCleanup(self.server.close)
        self.zip = self.package()
        self.server.publish(self.zip)
        self.server.publish(self.zip.with_suffix('.zip.sha256'))
        self.release = {'tag_name': self.incoming['version'], 'assets': [
            {'name': n, 'browser_download_url': self.server.url(n)} for n in (self.zip.name, self.zip.name + '.sha256')]}
        # 缩小分段尺寸，让小包也会产生多次请求和合并。
        for target, value in ((updater, ('INCREMENTAL_CHUNK', 16 * 1024)), (updater, ('INCREMENTAL_GAP', 1024)),
                              (updater, ('INCREMENTAL_READ', 4096)), (updater.time, ('sleep', lambda seconds: None))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        proxy = patch.dict(os.environ, {'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'})
        proxy.start()
        self.addCleanup(proxy.stop)

    def prepare(self, **options):
        with patch.object(updater, 'latest_release', return_value=self.release), patch.object(updater, 'runtime_prerequisites'), \
                patch.object(updater.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            return updater.prepare_update(self.root, **options)

    def candidate_of(self, result):
        return Path(json.loads(Path(result['operation']).read_text(encoding='utf-8'))['candidate'])

    def test_downloads_only_changed_files_and_matches_full_extraction(self):
        result = self.prepare()
        download = result['download']
        self.assertEqual(download['mode'], 'incremental', download)
        self.assertEqual(digest_tree(self.candidate_of(result)), digest_tree(self.candidate))
        changed = {'MaaGakumasu.exe', 'libs/changed.dll', 'hif-release.json', 'config/config.json', 'config/new-feature.json'}
        self.assertEqual(download['downloaded_files'], len(changed))
        self.assertLess(self.server.served, len(self.server.files[self.zip.name]) / 2)
        ranges = self.server.signed(self.zip.name)
        self.assertTrue(all(r[2] and re.fullmatch(r'bytes=\d+-\d+', r[2]) for r in ranges), ranges)
        self.assertIsNone(ranges[0][3])
        self.assertTrue(all(r[3] == '"v1"' for r in ranges[1:]))
        self.assertFalse(self.server.signed(self.zip.name + '.sha256'))
        # 只请求一次 github.com 跳转，之后直接用签名地址。
        self.assertEqual(len([r for r in self.server.requests if r[0] == 'download']), 1)
        operation = json.loads(Path(result['operation']).read_text(encoding='utf-8'))
        self.assertEqual(operation['download']['mode'], 'incremental')

    def test_reinstall_downloads_nothing_but_the_catalog_when_files_match(self):
        self.incoming = {**self.incoming, 'version': self.current['version']}
        for directory in (self.candidate, self.root):
            write(directory / 'hif-release.json', self.incoming)
        write(self.candidate / 'hif-build-info.json', self.incoming)
        for name in ('MaaGakumasu.exe', 'libs/changed.dll', 'hif-build-info.json'):
            shutil.copy2(self.candidate / name, self.root / name)
        updater.package_manifest(self.candidate, self.incoming['version'])
        package = self.package()
        self.server.publish(package)
        self.server.publish(package.with_suffix('.zip.sha256'))
        self.release['tag_name'] = self.incoming['version']
        self.release['assets'] = [{'name': n, 'browser_download_url': self.server.url(n)} for n in (package.name, package.name + '.sha256')]
        result = self.prepare(reinstall=True)
        self.assertEqual(result['download']['mode'], 'incremental')
        # 个人配置与公开默认模板不同，只有它们需要下载。
        self.assertEqual(result['download']['downloaded_files'], 2)
        self.assertEqual(digest_tree(self.candidate_of(result)), digest_tree(self.candidate))

    def test_server_without_range_support_falls_back_to_full_package(self):
        self.server.ignore_range = True
        result = self.prepare()
        self.assertEqual(result['download']['mode'], 'full')
        self.assertIn('不支持分段', result['download']['incremental_error'])
        self.assertEqual(digest_tree(self.candidate_of(result)), digest_tree(self.candidate))
        self.assertTrue(self.server.signed(self.zip.name + '.sha256'))

    def test_package_replaced_during_download_falls_back_to_full_package(self):
        def replace(server, name):
            if name == self.zip.name and len(server.signed(name)) == 3:
                server.etags[name] = '"v2"'
        self.server.on_signed = replace
        result = self.prepare()
        self.assertEqual(result['download']['mode'], 'full')
        self.assertIn('已更新', result['download']['incremental_error'])
        self.assertEqual(digest_tree(self.candidate_of(result)), digest_tree(self.candidate))

    def test_expired_signed_address_is_resolved_again(self):
        def expire(server, name):
            if name == self.zip.name and len(server.signed(name)) == 2:
                server.expired.add(server.signature)
        self.server.on_signed = expire
        result = self.prepare()
        self.assertEqual(result['download']['mode'], 'incremental', result['download'])
        self.assertEqual(len([r for r in self.server.requests if r[0] == 'download']), 2)

    def test_dropped_connections_are_retried(self):
        self.server.drop_next = 2
        result = self.prepare()
        self.assertEqual(result['download']['mode'], 'incremental', result['download'])
        self.assertEqual(digest_tree(self.candidate_of(result)), digest_tree(self.candidate))

    def test_many_changed_files_download_the_full_package(self):
        for name in ('libs/unchanged-a.dll', 'libs/unchanged-b.dll'):
            (self.root / name).unlink()
        result = self.prepare()
        self.assertEqual(result['download']['mode'], 'full')
        self.assertIn('较多', result['download']['incremental_error'])

    def test_cancellation_stops_without_falling_back(self):
        def cancel(server, name):
            if len(server.signed(name)) == 3:
                pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
                (Path(pointer['operation']).parent / 'cancel').write_bytes(b'cancel')
        self.server.on_signed = cancel
        with self.assertRaises(InterruptedError):
            self.prepare()
        self.assertFalse(self.server.signed(self.zip.name + '.sha256'))
        self.assertTrue(all(r[2] for r in self.server.signed(self.zip.name)))
        pointer = json.loads((self.root / 'temp/hif-update-pending.json').read_text(encoding='utf-8'))
        self.assertFalse((Path(pointer['operation']).parent / 'extracted').exists())
        self.assertFalse((self.root / 'backup').exists())


if __name__ == '__main__':
    unittest.main()
