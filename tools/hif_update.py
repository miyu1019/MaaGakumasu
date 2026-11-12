"""Manual updates from this Fork's HIF Releases; never synchronize upstream."""
import hashlib
from html.parser import HTMLParser
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from hif_app import ROOT, digest_tree, extract_package, https_context, read, write

MANIFEST = 'hif-package-manifest.json'
PRIVATE = {'config', 'resource', 'appsettings.json', 'plugins', 'logs', 'debug', 'backup', 'temp', 'tests'}


def safe_relative(name):
    parts = str(name).split('/')
    if not parts or any(not p or p in ('.', '..') or re.search(r'[\\:*?"<>|\x00-\x1f]', p)
                        or p.endswith(('.', ' ')) or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', p)
                        for p in parts):
        raise ValueError('非法更新路径：' + str(name))
    return parts


def safe_target(root, name):
    target = Path(root)
    for part in safe_relative(name):
        target = target / part
        if target.exists() and getattr(target.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400:
            raise ValueError('更新路径不能经过链接或目录联接：' + name)
        if target.is_symlink():
            raise ValueError('更新路径不能经过链接：' + name)
    return target


def managed_file(name):
    return safe_relative(name)[0].lower() not in PRIVATE and name != MANIFEST


def package_manifest(root, release_version):
    """A fresh build's public inputs only; never infer ownership from installed files."""
    files, defaults = {}, {}
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix.lower() in ('.log', '.pyc', '.bak', '.pdb'):
            continue
        name = path.relative_to(root).as_posix()
        safe_target(root, name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if managed_file(name):
            files[name] = digest
        elif name.startswith('config/') or name == 'resource/mfa_layout.json':
            if name != 'config/maa_option.json':
                defaults[name] = digest
    manifest = {'format': 1, 'version': release_version, 'repository': REPOSITORY, 'files': files, 'defaults': defaults}
    write(Path(root) / MANIFEST, manifest)
    return manifest


def verify_manifest(root, expected_version=None, verify_files=True):
    manifest = read(Path(root) / MANIFEST)
    if manifest.get('format') != 1 or manifest.get('repository') != REPOSITORY:
        raise ValueError('不支持的 HIF 文件清单。')
    if expected_version and manifest.get('version') != expected_version:
        raise ValueError('文件清单版本不一致。')
    seen = set()
    for group in ('files', 'defaults'):
        if not isinstance(manifest.get(group), dict):
            raise ValueError('无效的 HIF 文件清单。')
        for name, digest in manifest[group].items():
            if name.lower() in seen:
                raise ValueError('文件清单存在重复路径：' + name)
            seen.add(name.lower())
            if group == 'files' and not managed_file(name):
                raise ValueError('程序清单包含个人文件：' + name)
            if group == 'defaults' and not (name.startswith('config/') or name == 'resource/mfa_layout.json'):
                raise ValueError('不允许的默认模板：' + name)
            path = safe_target(root, name)
            if not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError('文件清单哈希无效：' + name)
            if verify_files and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise ValueError('文件校验失败：' + name)
    return manifest


def runtime_prerequisites(candidate):
    options = read(candidate / 'MFAAvalonia.runtimeconfig.json')['runtimeOptions']
    frameworks = options.get('frameworks', [options['framework']] if 'framework' in options else [])
    dotnet = shutil.which('dotnet') or str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'dotnet/dotnet.exe')
    try:
        installed = subprocess.check_output([dotnet, '--list-runtimes'], text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法检查新版所需 .NET 运行时，请先安装运行时。') from error
    for framework in frameworks:
        required = tuple(int(p) for p in framework['version'].split('.')[:3])
        matches = re.findall(r'^' + re.escape(framework['name']) + r' (\d+)\.(\d+)\.(\d+) ', installed, re.M)
        if not any(tuple(map(int, match))[:2] == required[:2] and tuple(map(int, match)) >= required for match in matches):
            raise ValueError('请先安装 ' + framework['name'] + ' ' + framework['version'] + '，当前程序未退出。')


def prepare_update(root=ROOT, package=None, expected_version=None, channel='stable', reinstall=False):
    """Persistent preparation; never stop an Agent or write installed configuration.

    reinstall 重新安装渠道里不低于当前版本的最新包；安装器只替换内容不同的文件，用于修复缺失或被改动的程序文件。
    """
    root = Path(root).resolve()
    installed = read(root / 'hif-release.json')['version']
    installed_commit = installed_autobuild_commit(root)
    release = latest_release(channel) if package is None else None
    if expected_version and release and release_identity(release) != expected_version:
        raise ValueError('发布版本已变化，请重新检查版本和更新说明后确认。')
    if release is None and package is None:
        raise ValueError('AutoBuild 测试版尚未发布。' if channel == AUTOBUILD_TAG else '本 Fork 尚未发布 HIF 运行包。')
    if release and not is_newer(release_version(release), release.get('commit'), installed, installed_commit, reinstall):
        return {'skipped': True, 'installed': installed}
    operation_id = uuid.uuid4().hex
    stage = safe_target(root, 'temp/hif-updates/' + operation_id)
    stage.mkdir(parents=True)
    progress = stage / 'progress.json'

    def report(state, **values):
        if (stage / 'cancel').exists():
            raise InterruptedError('更新已取消。')
        write(progress, {'state': state, **values})

    def fetch(url, destination):
        request = urllib.request.Request(url, headers={'User-Agent': 'MaaGakumasu-HIF-update'})
        with open_url(request, 40) as response, destination.open('wb') as output:
            total = int(response.headers.get('Content-Length', 0))
            size = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                size += len(chunk)
                report('downloading', mode='full', downloaded=size, total=total)

    write(root / 'temp/hif-update-pending.json', {'operation': str(stage / 'operation.json'), 'state': 'preparing'})
    # 下载的包和解压目录只为已准备的操作保留；失败、取消或跳过时立即删除，只留进度记录。
    payload = [stage / 'extracted']
    prepared = False
    candidate = stage / 'extracted/MaaGakumasu-HIF'
    try:
        download = fallback = None
        if package is None:
            name = 'MaaGakumasu-HIF-win-x64-' + release['tag_name'] + '.zip'
            assets = {a['name']: a['browser_download_url'] for a in release.get('assets', [])}
            if name not in assets or name + '.sha256' not in assets:
                raise ValueError('发布缺少完整 Windows x64 包或 SHA-256。')
            package_name = name
            try:
                download = incremental_candidate(root, assets[name], candidate, report)
            except InterruptedError:
                raise
            except Exception as error:
                # zipfile 会把读取中的 OSError（取消也是）改写成 BadZipFile，所以按取消标记判断，而不是按异常类型。
                if (stage / 'cancel').exists():
                    raise InterruptedError('更新已取消。') from error
                # 增量下载只是省流量的捷径：任何问题都改为下载并校验完整包，原因留在操作记录里。
                discard([stage / 'extracted'])
                fallback = str(error) or type(error).__name__
                package = stage / name
                payload += [package, package.with_suffix('.zip.sha256')]
                fetch(assets[name], package)
                fetch(assets[name + '.sha256'], package.with_suffix('.zip.sha256'))
        if download is None:
            package = Path(package).resolve()
            package_name = package.name
            extract_full_package(root, package, stage / 'extracted', report)
            download = {'mode': 'full', 'package_size': package.stat().st_size}
            if fallback:
                download['incremental_error'] = fallback
        incoming = read(candidate / 'hif-release.json')
        info = read(candidate / 'hif-build-info.json')
        target_version = incoming['version']
        # A confirmed AutoBuild identity was matched against the release before downloading.
        if expected_version and target_version != (release['version'] if release and release.get('commit') else expected_version):
            raise ValueError('安装包与确认的版本不一致，请重新检查后确认。')
        if incoming.get('repository') != REPOSITORY or any(info.get(k) != incoming.get(k) for k in ('version', 'upstream_commit', *COMPONENTS)):
            raise ValueError('安装包来源或构建信息不一致。')
        # AutoBuild keeps its base version; the source commit identifies each build, downloaded or local.
        autobuild = info.get('build_channel') == AUTOBUILD_TAG
        commit = info.get('source_commit') if autobuild else None
        if autobuild and not re.fullmatch(r'[0-9a-f]{40}', str(commit)):
            raise ValueError('AutoBuild 安装包缺少源码提交，无法判断新旧。')
        if release and release.get('commit'):
            if not autobuild or target_version != release['version'] or commit != release['commit']:
                raise ValueError('AutoBuild 安装包与发布说明的版本或源码提交不一致。')
        elif release and (autobuild or target_version != release['tag_name']):
            raise ValueError('安装包与发布标签的版本不一致。')
        if package_name != 'MaaGakumasu-HIF-win-x64-' + (AUTOBUILD_TAG if autobuild else target_version) + '.zip':
            raise ValueError('安装包名称与版本不一致。')
        if not is_newer(target_version, commit, installed, installed_commit, reinstall):
            report('skipped')
            return {'skipped': True, 'installed': installed}
        manifest = verify_manifest(candidate, target_version)
        required = {'MaaGakumasu.exe', 'MFAAvalonia.runtimeconfig.json', 'python/python.exe', 'tools/validate.py',
                    'tools/hif_update_runner.ps1', 'tools/export_settings.ps1', 'tools/import_settings.ps1',
                    'extensions/hif/tasks/produce_hif.json'}
        if not required.issubset(manifest['files']):
            raise ValueError('安装包缺少必需程序文件。')
        actual = {p.relative_to(candidate).as_posix() for p in candidate.rglob('*') if p.is_file()}
        if actual != set(manifest['files']) | set(manifest['defaults']) | {MANIFEST}:
            raise ValueError('安装包存在清单外文件。')
        provenance = read(candidate / 'upstream-provenance.json')
        if provenance.get('commit') != incoming['upstream_commit'] or provenance.get('files') != digest_tree(candidate / 'upstream'):
            raise ValueError('包内上游脚本清单不一致。')
        report('validating')
        runtime_prerequisites(candidate)
        validated = subprocess.run([str(candidate / 'python/python.exe'), str(candidate / 'tools/validate.py'),
                                    '--root', str(candidate), '--native'], cwd=candidate, capture_output=True, text=True, errors='replace', timeout=120)
        if validated.returncode:
            raise ValueError('新版运行环境检查失败：' + validated.stderr[-1500:])
        old_files = verify_manifest(root, installed, verify_files=False)['files'] if (root / MANIFEST).is_file() else {}
        names = sorted(set(old_files) | set(manifest['files']) | {MANIFEST})
        for name in names:
            target = safe_target(root, name)
            if target.is_dir():
                raise ValueError('目录阻挡更新文件：' + name)
        backup = safe_target(root, 'backup/hif-update-' + operation_id)
        notes_path = candidate / 'docs/hif/发布说明.md'
        release_notes = release.get('body', '') if release else (
            notes_path.read_text(encoding='utf-8-sig') if notes_path.is_file() else '')
        display_version = release_identity(release) if release else build_identity(target_version, commit)
        operation = {'format': 1, 'id': operation_id, 'root': str(root), 'stage': str(stage), 'candidate': str(candidate),
                     'backup': str(backup), 'from_version': installed, 'version': target_version,
                     'display_version': display_version, 'state': 'prepared', 'files': names, 'defaults': list(manifest['defaults']), 'changes': [],
                     'release_notes': release_notes, 'download': download}
        for name in ('hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
            shutil.copy2(root / 'tools' / name, stage / name)
        write(stage / 'operation.json', operation)
        prepared = True
        report('prepared', version=target_version)
        write(root / 'temp/hif-update-pending.json', {'operation': str(stage / 'operation.json'), 'state': 'prepared'})
        return {'operation': str(stage / 'operation.json'), 'version': target_version, 'state': 'prepared', 'download': download}
    except BaseException as error:
        write(progress, {'state': 'cancelled' if isinstance(error, InterruptedError) else 'failed', 'error': str(error)})
        raise
    finally:
        if not prepared:
            discard(payload)


def discard(paths):
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError:
            pass  # 下次安装前安装器会删除整个过期的更新目录。


PACKAGE_ROOT = 'MaaGakumasu-HIF'


def check_entries(infos):
    """完整包条目的共同检查：Windows 合法路径、单一根目录、不区分大小写不重名、不含链接。"""
    seen = set()
    for entry in infos:
        name = entry.filename.rstrip('/')
        safe_relative(name)
        if not name.startswith(PACKAGE_ROOT + '/') and name != PACKAGE_ROOT:
            raise ValueError('不是完整 HIF 运行包。')
        if name.lower() in seen:
            raise ValueError('安装包存在重复路径。')
        seen.add(name.lower())
        if (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('安装包不能包含链接。')


def extract_full_package(root, package, destination, report):
    import zipfile
    if not package.with_suffix('.zip.sha256').is_file():
        raise ValueError('安装包旁缺少同名 SHA-256 文件：' + package.with_suffix('.zip.sha256').name)
    report('verifying')
    verify_checksum(package, package.with_suffix('.zip.sha256'))
    # Validate all Windows paths and case-insensitive duplicates before extracting.
    with zipfile.ZipFile(package) as archive:
        check_entries(archive.infolist())
        needed = sum(e.file_size for e in archive.infolist()) * 2 + package.stat().st_size
        if shutil.disk_usage(root).free < needed + 64 * 1024 * 1024:
            raise ValueError('磁盘空间不足，当前程序未退出。')
    report('extracting')
    extract_package(package, destination)


# 增量下载：从发布的完整包里按范围只读需要的文件。GitHub 资源不支持末尾范围和多段范围，If-Range 也会被忽略，
# 所以先取总大小再请求明确的单段范围，并用 If-Match 发现下载途中被替换的包（AutoBuild 原地覆盖）。
INCREMENTAL_GAP = 64 * 1024           # 相距不超过这么远的两段合并成一次请求
INCREMENTAL_CHUNK = 8 * 1024 * 1024   # 一次请求最多取这么多，连接中断时只重下这一段
INCREMENTAL_READ = 64 * 1024          # 计划外读取（文件目录、清单、文件头余量）每次至少取这么多
INCREMENTAL_RATIO = 0.7               # 需下载的数据超过完整包这一比例时直接下载完整包
INCREMENTAL_ATTEMPTS = 4


class IncrementalUnavailable(Exception):
    """增量下载走不通（不支持分段、包已更新、校验不符等），改为下载完整包。"""


class RemoteZip(io.RawIOBase):
    """只读、可定位的远程文件。只缓存已取回的分段；按计划取需要的文件所在范围，相邻的合并成一次请求。"""

    def __init__(self, url, report):
        super().__init__()
        self.url = self.location = url
        self.report = report
        self.etag = None
        self.segments = []
        self.planned = []
        self.planned_bytes = 0
        self.downloaded = 0
        self.position = 0
        # 先取 1 字节得到总大小、ETag 和签名地址。
        headers = self.fetch(0, 0)[1]
        self.size = self.total_size
        self.etag = headers.get('ETag')

    def plan(self, spans):
        """spans 为需要读取的 [起点, 终点) 范围；合并相近的，再按单次请求上限切开。"""
        merged = []
        for start, end in sorted(spans):
            if merged and start - merged[-1][1] <= INCREMENTAL_GAP:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self.planned = [(offset, min(offset + INCREMENTAL_CHUNK, end)) for start, end in merged
                        for offset in range(start, end, INCREMENTAL_CHUNK)]
        self.planned_bytes = sum(end - start for start, end in self.planned)

    def fetch(self, start, end):
        error = None
        refreshed = False
        attempt = 0
        while attempt < INCREMENTAL_ATTEMPTS:
            # 取消检查放在网络重试之外，不会被当作连接错误吞掉。
            self.progress()
            headers = {'User-Agent': 'MaaGakumasu-HIF-update', 'Range': 'bytes=%d-%d' % (start, end)}
            if self.etag:
                headers['If-Match'] = self.etag
            try:
                with open_url(urllib.request.Request(self.location, headers=headers), 40) as response:
                    if getattr(response, 'status', None) != 206:
                        raise IncrementalUnavailable('下载服务器不支持分段下载。')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', '').strip())
                    if not match or int(match[1]) != start or int(match[2]) != end:
                        raise IncrementalUnavailable('分段下载的响应范围不符。')
                    self.total_size = int(match[3])
                    body = bytearray()
                    while len(body) <= end - start and (chunk := response.read(min(1024 * 1024, end - start + 1 - len(body)))):
                        body += chunk
                        self.downloaded += len(chunk)
                        self.progress()
                    if len(body) == end - start + 1:
                        # 第一次请求经 github.com 跳转到带签名的下载地址，之后直接用该地址。
                        self.location = response.geturl()
                        return bytes(body), response.headers
                    error = IncrementalUnavailable('分段下载的数据不完整。')
            except InterruptedError:
                raise
            except urllib.error.HTTPError as failure:
                failure.close()
                if failure.code == 412:
                    raise IncrementalUnavailable('安装包在下载过程中已更新。') from failure
                if failure.code in (401, 403) and not refreshed and self.location != self.url:
                    # 签名地址约一小时后过期：回到发布下载地址重新跳转，不计入重试次数。
                    self.location, refreshed = self.url, True
                    continue
                raise IncrementalUnavailable('分段下载失败：HTTP %d。' % failure.code) from failure
            except (urllib.error.URLError, OSError, http.client.HTTPException) as failure:
                error = failure
            attempt += 1
            if attempt < INCREMENTAL_ATTEMPTS:
                time.sleep(attempt)
        raise IncrementalUnavailable('分段下载多次失败：' + str(error)) from error

    def progress(self):
        if self.planned_bytes:
            self.report('downloading', mode='incremental', downloaded=self.downloaded, total=self.planned_bytes)
        else:
            self.report('inspecting')

    def segment_at(self, position):
        for start, data in self.segments:
            if start <= position < start + len(data):
                return start, data
        return None

    def load(self, position, wanted):
        start, end = next(((s, e) for s, e in self.planned if s <= position < e),
                          (position, min(self.size, position + max(wanted, INCREMENTAL_READ))))
        self.segments.append((start, self.fetch(start, end - 1)[0]))

    def release_before(self, offset):
        self.segments = [(start, data) for start, data in self.segments if start + len(data) > offset]

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def readinto(self, buffer):
        size = max(0, min(len(buffer), self.size - self.position))
        view = memoryview(buffer)
        done = 0
        while done < size:
            position = self.position + done
            found = self.segment_at(position)
            if found is None:
                self.load(position, size - done)
                found = self.segment_at(position)
            if found is None:
                raise IncrementalUnavailable('分段下载的数据不完整。')
            start, data = found
            chunk = data[position - start:position - start + size - done]
            view[done:done + len(chunk)] = chunk
            done += len(chunk)
        self.position += size
        return size


def copy_if_same(source_root, name, digest, target):
    """已安装文件与清单一致时复制进候选目录；读不到或内容不同返回 False，由远程包补齐。"""
    try:
        source = safe_target(source_root, name)
        if not source.is_file():
            return False
        sha = hashlib.sha256()
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as reader, target.open('wb') as output:
            while chunk := reader.read(1024 * 1024):
                sha.update(chunk)
                output.write(chunk)
    except (OSError, ValueError):
        target.unlink(missing_ok=True)
        return False
    if sha.hexdigest() != digest:
        target.unlink()
        return False
    return True


def incremental_candidate(root, url, candidate, report):
    """拼出与解压完整包相同的候选目录：和已安装文件相同的直接复制，其余从远程完整包里按范围下载并逐个校验。"""
    import zipfile
    remote = RemoteZip(url, report)
    try:
        archive = zipfile.ZipFile(remote)
    except zipfile.BadZipFile as error:
        raise IncrementalUnavailable('无法读取安装包目录：' + str(error)) from error
    with archive:
        infos = archive.infolist()
        check_entries(infos)
        entries = {i.filename[len(PACKAGE_ROOT) + 1:]: i for i in infos if not i.is_dir()}
        if MANIFEST not in entries:
            raise ValueError('安装包缺少文件清单。')
        candidate.mkdir(parents=True)
        (candidate / MANIFEST).write_bytes(archive.read(entries[MANIFEST]))
        manifest = verify_manifest(candidate, verify_files=False)
        listed = {**manifest['files'], **manifest['defaults']}
        if set(entries) != set(listed) | {MANIFEST}:
            raise ValueError('安装包存在清单外文件。')
        if shutil.disk_usage(root).free < sum(entries[n].file_size for n in listed) + 64 * 1024 * 1024:
            raise ValueError('磁盘空间不足，当前程序未退出。')
        names = sorted(listed)
        needed = []
        for index, name in enumerate(names):
            if index % 100 == 0:
                report('comparing', compared=index, total=len(names))
            if not copy_if_same(root, name, listed[name], safe_target(candidate, name)):
                needed.append(name)
        needed.sort(key=lambda n: entries[n].header_offset)
        # 本地文件头的扩展字段可能比目录里的长，留 1 KiB 余量；仍不够时读取会再单独请求。
        remote.plan((entries[n].header_offset, min(remote.size, entries[n].header_offset + 30 + len(entries[n].orig_filename.encode())
                                                     + len(entries[n].extra) + entries[n].compress_size + 1024)) for n in needed)
        if remote.planned_bytes > INCREMENTAL_RATIO * remote.size:
            raise IncrementalUnavailable('变化的文件较多，直接下载完整包。')
        before = remote.downloaded
        for name in needed:
            info = entries[name]
            remote.release_before(info.header_offset)
            target = safe_target(candidate, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = hashlib.sha256()
            with archive.open(info) as source, target.open('wb') as output:
                while chunk := source.read(1024 * 1024):
                    sha.update(chunk)
                    output.write(chunk)
            if sha.hexdigest() != listed[name]:
                raise IncrementalUnavailable('下载的文件校验失败：' + name)
        remote.progress()
    return {'mode': 'incremental', 'package_size': remote.size, 'downloaded': remote.downloaded,
            'changed_bytes': remote.downloaded - before, 'downloaded_files': len(needed), 'reused_files': len(names) - len(needed)}


REPOSITORY = 'miyu1019/MaaGakumasu'
AUTOBUILD_TAG = 'autobuild'
AUTOBUILD_PACKAGE = 'MaaGakumasu-HIF-win-x64-' + AUTOBUILD_TAG + '.zip'
# hif-autobuild.yml 随测试包上传的身份文件：基础版本、源码提交与更新说明。
AUTOBUILD_INFO = 'autobuild.json'
CHANNELS = ('stable', AUTOBUILD_TAG)
COMPONENTS = ('frontend_commit', 'frontend_version', 'framework_version', 'python_version')


def version(value):
    match = re.fullmatch(r'v(\d{6})\.(\d+)', str(value))
    if not match:
        raise ValueError('不是 HIF 日期版本：' + str(value))
    return tuple(map(int, match.groups()))


def is_newer(target_version, target_commit, installed, installed_commit=None, reinstall=False):
    """AutoBuild 不递增版本号，按源码提交区分；装着 AutoBuild 时，同版本号的正式版也可切换安装。

    reinstall 时只要不低于当前版本即可。
    """
    if version(target_version) < version(installed):
        return False
    if reinstall:
        return True
    if target_commit:
        return target_commit != installed_commit
    return bool(installed_commit) or version(target_version) > version(installed)


def github_api(path):
    """GitHub 接口请求；设置里填写的 Token 由前台经 HIF_GITHUB_TOKEN 传入，只发给 api.github.com。"""
    headers = {'User-Agent': 'MaaGakumasu-HIF-manual-update', 'Accept': 'application/vnd.github+json'}
    token = os.environ.get('HIF_GITHUB_TOKEN', '').strip()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/releases/' + path, headers=headers)


def open_url(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout, context=https_context())


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'MaaGakumasu-HIF-update'})
    with open_url(request, 30) as response:
        return json.loads(response.read(1024 * 1024).decode('utf-8-sig'))


def latest_release(channel='stable'):
    """Stable follows this Fork's latest release; autobuild follows the fixed AutoBuild prerelease."""
    if channel not in CHANNELS:
        raise ValueError('未知更新渠道：' + str(channel))
    path = 'tags/' + AUTOBUILD_TAG if channel == AUTOBUILD_TAG else 'latest'
    try:
        with open_url(github_api(path), 30) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        if error.code == 401:
            raise ValueError('GitHub Token 无效或已过期，请在版本更新设置中更正或清空。') from error
        if error.code in (403, 429) and (
                error.code == 429 or (error.headers or {}).get('X-RateLimit-Remaining') == '0'
                or (error.fp and b'rate limit' in error.read(4096).lower())):
            return release_from_public_page(channel)
        raise
    if channel == AUTOBUILD_TAG:
        return describe_autobuild(release)
    version(release['tag_name'])
    return release


def describe_autobuild(release):
    """AutoBuild 的基础版本、源码提交和更新说明只取自工作流上传的 autobuild.json。"""
    if release.get('tag_name') != AUTOBUILD_TAG:
        raise ValueError('不是 AutoBuild 测试版发布。')
    asset = next((a for a in release.get('assets', []) if a.get('name') == AUTOBUILD_INFO), None)
    if not asset:
        raise ValueError('AutoBuild 发布缺少 ' + AUTOBUILD_INFO + '，无法判断是否需要更新。')
    info = fetch_json(asset['browser_download_url'])
    base, commit = info.get('version'), info.get('commit')
    if info.get('tag') != AUTOBUILD_TAG or info.get('package') != AUTOBUILD_PACKAGE or not re.fullmatch(r'[0-9a-f]{40}', str(commit)):
        raise ValueError('AutoBuild 身份文件无效，无法判断是否需要更新。')
    version(base)
    return {**release, 'version': base, 'commit': commit, 'body': info.get('notes') or ''}


def release_version(release):
    return release['version'] if release.get('commit') else release['tag_name']


def build_identity(version_name, commit=None):
    return version_name + ' · AutoBuild · ' + commit[:7] if commit else version_name


def release_identity(release):
    return build_identity(release_version(release), release.get('commit'))


def installed_autobuild_commit(root):
    path = Path(root) / 'hif-build-info.json'
    info = read(path) if path.is_file() else {}
    return info.get('source_commit') if info.get('build_channel') == AUTOBUILD_TAG else None


def installed_identity(root, installed):
    return build_identity(installed, installed_autobuild_commit(root))


def check(root=ROOT, channel='stable'):
    """Read the channel's latest release and its notes without downloading the package."""
    root = Path(root).resolve()
    installed = read(root / 'hif-release.json')['version']
    version(installed)
    release = latest_release(channel)
    available = bool(release and is_newer(release_version(release), release.get('commit'), installed, installed_autobuild_commit(root)))
    return {'installed': installed_identity(root, installed), 'latest': release_identity(release) if release else None,
            'repository': REPOSITORY, 'channel': channel, 'release_notes': release.get('body', '') if release else '',
            'update_available': available}


class ReleaseNotesParser(HTMLParser):
    """Extract only the rendered description of this release, excluding page navigation."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.done = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif not self.done and 'markdown-body' in dict(attrs).get('class', '').split():
                self.depth = 1
        if self.depth and tag in ('p', 'li', 'h1', 'h2', 'h3', 'pre', 'br', 'tr'):
            self.parts.append('\n- ' if tag == 'li' else '\n')

    def handle_endtag(self, tag):
        if self.depth and tag in ('p', 'li', 'h1', 'h2', 'h3', 'pre', 'tr'):
            self.parts.append('\n')
        if self.depth and tag == 'div':
            self.depth -= 1
            self.done = self.depth == 0

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def release_from_public_page(channel='stable'):
    """GitHub 接口限流时的兜底：AutoBuild 直接读固定下载地址；正式版从公开发布页取标签和说明。"""
    base = 'https://github.com/' + REPOSITORY + '/releases/'
    if channel == AUTOBUILD_TAG:
        assets = [{'name': name, 'browser_download_url': base + 'download/' + AUTOBUILD_TAG + '/' + name}
                  for name in (AUTOBUILD_PACKAGE, AUTOBUILD_PACKAGE + '.sha256', AUTOBUILD_INFO)]
        try:
            return describe_autobuild({'tag_name': AUTOBUILD_TAG, 'assets': assets})
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise ValueError('GitHub 接口限流且测试版下载地址暂时无法访问，请稍后重试。') from error
    request = urllib.request.Request(base + 'latest', headers={'User-Agent': 'MaaGakumasu-HIF-update'})
    try:
        with open_url(request, 30) as response:
            location = urllib.parse.urlsplit(response.geturl())
            prefix = '/' + REPOSITORY + '/releases/tag/'
            if location.scheme != 'https' or location.netloc.lower() != 'github.com' or not location.path.lower().startswith(prefix.lower()):
                raise ValueError('公开发布页未返回本 Fork 的稳定版本。')
            tag = urllib.parse.unquote(location.path[len(prefix):])
            version(tag)
            parser = ReleaseNotesParser()
            parser.feed(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ValueError('GitHub 接口限流且公开发布页暂时无法访问，请稍后重试。') from error
    name = 'MaaGakumasu-HIF-win-x64-' + tag + '.zip'
    notes = re.sub(r'\n\s*\n+', '\n\n', ''.join(parser.parts)).strip()
    return {'tag_name': tag, 'body': notes,
            'assets': [{'name': n, 'browser_download_url': base + 'download/' + tag + '/' + n} for n in (name, name + '.sha256')]}


def verify_checksum(package, checksum):
    fields = Path(checksum).read_text(encoding='utf-8-sig').strip().split()
    if len(fields) != 2 or fields[1].lstrip('*') != Path(package).name:
        raise ValueError('SHA-256 文件名不匹配。')
    actual = hashlib.sha256(Path(package).read_bytes()).hexdigest()
    if fields[0].lower() != actual:
        raise ValueError('运行包 SHA-256 校验失败，未安装更新。')
