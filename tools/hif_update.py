"""Manual updates from this Fork's HIF Releases; never synchronize upstream."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from hif_app import ROOT, digest_tree, download, extract_package, install_composition, read, write

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


def prepare_update(root=ROOT, package=None):
    """Persistent preparation; never stop an Agent or write installed configuration."""
    root = Path(root).resolve()
    installed = read(root / 'hif-release.json')['version']
    release = latest_release() if package is None else None
    if release is None and package is None:
        raise ValueError('本 Fork 尚未发布 HIF 运行包。')
    if release and version(release['tag_name']) <= version(installed):
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
        with urllib.request.urlopen(request, timeout=40) as response, destination.open('wb') as output:
            total = int(response.headers.get('Content-Length', 0))
            size = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                size += len(chunk)
                report('downloading', downloaded=size, total=total)

    write(root / 'temp/hif-update-pending.json', {'operation': str(stage / 'operation.json'), 'state': 'preparing'})
    try:
        if package is None:
            name = 'MaaGakumasu-HIF-win-x64-' + release['tag_name'] + '.zip'
            assets = {a['name']: a['browser_download_url'] for a in release.get('assets', [])}
            if name not in assets or name + '.sha256' not in assets:
                raise ValueError('发布缺少完整 Windows x64 包或 SHA-256。')
            package = stage / name
            fetch(assets[name], package)
            fetch(assets[name + '.sha256'], package.with_suffix('.zip.sha256'))
        package = Path(package).resolve()
        report('verifying')
        verify_checksum(package, package.with_suffix('.zip.sha256'))
        # Validate all Windows paths and case-insensitive duplicates before extracting.
        import zipfile
        with zipfile.ZipFile(package) as archive:
            seen = set()
            for entry in archive.infolist():
                name = entry.filename.rstrip('/')
                safe_relative(name)
                if not name.startswith('MaaGakumasu-HIF/') and name != 'MaaGakumasu-HIF':
                    raise ValueError('不是完整 HIF 运行包。')
                if name.lower() in seen:
                    raise ValueError('安装包存在重复路径。')
                seen.add(name.lower())
            needed = sum(e.file_size for e in archive.infolist()) * 2 + package.stat().st_size
            if shutil.disk_usage(root).free < needed + 64 * 1024 * 1024:
                raise ValueError('磁盘空间不足，当前程序未退出。')
        report('extracting')
        extract_package(package, stage / 'extracted')
        candidate = stage / 'extracted/MaaGakumasu-HIF'
        incoming = read(candidate / 'hif-release.json')
        info = read(candidate / 'hif-build-info.json')
        target_version = incoming['version']
        if incoming.get('repository') != REPOSITORY or any(info.get(k) != incoming.get(k) for k in ('version', 'upstream_commit', *COMPONENTS)):
            raise ValueError('安装包来源或构建信息不一致。')
        if package.name != 'MaaGakumasu-HIF-win-x64-' + target_version + '.zip' or (release and target_version != release['tag_name']):
            raise ValueError('安装包名称、发布标签与版本不一致。')
        if version(target_version) <= version(installed):
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
        operation = {'format': 1, 'id': operation_id, 'root': str(root), 'stage': str(stage), 'candidate': str(candidate),
                     'backup': str(backup), 'from_version': installed, 'version': target_version,
                     'state': 'prepared', 'files': names, 'defaults': list(manifest['defaults']), 'changes': []}
        for name in ('hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
            shutil.copy2(root / 'tools' / name, stage / name)
        write(stage / 'operation.json', operation)
        report('prepared', version=target_version)
        write(root / 'temp/hif-update-pending.json', {'operation': str(stage / 'operation.json'), 'state': 'prepared'})
        return {'operation': str(stage / 'operation.json'), 'version': target_version, 'state': 'prepared'}
    except BaseException as error:
        write(progress, {'state': 'cancelled' if isinstance(error, InterruptedError) else 'failed', 'error': str(error)})
        raise

REPOSITORY = 'miyu1019/MaaGakumasu'
COMPONENTS = ('frontend_commit', 'frontend_version', 'framework_version', 'python_version')
INSTALL = ('upstream', 'extensions/hif', 'tools', 'lang', 'data', 'interface.json',
           'hif-release.json', 'hif-build-info.json', 'upstream-provenance.json',
           'README.md', 'docs', 'requirements-hif.txt', 'clear_logs_清除日志.bat',
           'export_settings_导出设置.bat', 'import_settings_导入设置.bat')
PROTECTED = ('config', 'resource', 'python', 'libs', 'runtimes')


def version(value):
    match = re.fullmatch(r'v(\d{6})\.(\d+)', str(value))
    if not match:
        raise ValueError('不是 HIF 日期版本：' + str(value))
    return tuple(map(int, match.groups()))


def latest_release():
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/releases/latest',
                                     headers={'User-Agent': 'MaaGakumasu-HIF-manual-update'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    version(release['tag_name'])
    return release


def verify_checksum(package, checksum):
    fields = Path(checksum).read_text(encoding='utf-8-sig').strip().split()
    if len(fields) != 2 or fields[1].lstrip('*') != Path(package).name:
        raise ValueError('SHA-256 文件名不匹配。')
    actual = hashlib.sha256(Path(package).read_bytes()).hexdigest()
    if fields[0].lower() != actual:
        raise ValueError('运行包 SHA-256 校验失败，未安装更新。')


def validate_candidate(root, candidate):
    current = read(root / 'hif-release.json')
    incoming = read(candidate / 'hif-release.json')
    info = read(candidate / 'hif-build-info.json')
    if incoming.get('repository') != REPOSITORY:
        raise ValueError('运行包不属于本 HIF Fork。')
    for key in ('version', 'upstream_commit', *COMPONENTS):
        if info.get(key) != incoming.get(key):
            raise ValueError('构建信息不一致：' + key)
    for key in COMPONENTS:
        if current.get(key) != incoming.get(key):
            raise ValueError('新版需要升级 ' + key + '；请解压完整运行包到新目录并迁移 config/。')
    current_info = read(root / 'hif-build-info.json')
    source_hash = info.get('frontend_source_sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', source_hash) or current_info.get('frontend_source_sha256') != source_hash:
        raise ValueError('新版前台源码或构建格式不同；请使用完整运行包并迁移 config/。')
    if (root / 'requirements-hif.txt').read_bytes() != (candidate / 'requirements-hif.txt').read_bytes():
        raise ValueError('新版需要更换 Python 依赖；请使用完整运行包并迁移 config/。')
    for name in INSTALL:
        if not (candidate / name).exists():
            raise ValueError('运行包缺少：' + name)
    provenance = read(candidate / 'upstream-provenance.json')
    if provenance.get('commit') != incoming['upstream_commit'] or provenance.get('files') != digest_tree(candidate / 'upstream'):
        raise ValueError('包内上游脚本与固定提交清单不一致，未安装更新。')
    # Validate with the user's settings, but write all validation output into staging.
    shutil.rmtree(candidate / 'config')
    shutil.copytree(root / 'config', candidate / 'config', ignore=shutil.ignore_patterns('__pycache__'))
    install_composition(candidate)
    subprocess.run([str(root / 'python/python.exe'), str(candidate / 'tools/validate.py'),
                    '--root', str(candidate), '--native'], cwd=candidate, check=True)


def install_candidate(root, candidate, fail_after_swap=False):
    protected = {name: digest_tree(root / name) for name in PROTECTED}
    backup = root / 'backup' / ('hif-' + read(root / 'hif-release.json')['version'] + '-' + str(time.time_ns()))
    backup.mkdir(parents=True)
    moved, installed = [], []
    try:
        for name in INSTALL:
            target, saved = root / name, backup / name
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, saved)
                moved.append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate / name, target)
            installed.append(name)
            if fail_after_swap and len(installed) == 2:
                raise RuntimeError('Injected failure for HIF rollback test')
        if protected != {name: digest_tree(root / name) for name in PROTECTED}:
            raise RuntimeError('个人配置或运行时在更新期间发生变化。')
    except BaseException:
        for name in reversed(installed):
            target = root / name
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        for name in reversed(moved):
            os.replace(backup / name, root / name)
        raise
    return {'installed': read(root / 'hif-release.json')['version'], 'backup': str(backup),
            'repository': REPOSITORY}


def update(root=ROOT, package=None, check_only=False, fail_after_swap=False):
    root = Path(root).resolve()
    installed = read(root / 'hif-release.json')['version']
    version(installed)
    release = latest_release() if package is None else None
    if check_only:
        latest = release['tag_name'] if release else None
        return {'installed': installed, 'latest': latest, 'repository': REPOSITORY,
                'update_available': bool(latest and version(latest) > version(installed))}
    if package is None and not release:
        raise ValueError('本 Fork 尚未发布 HIF 运行包，当前版本已保留。')
    if release and version(release['tag_name']) <= version(installed):
        return {'installed': installed, 'skipped': True, 'repository': REPOSITORY}
    (root / 'temp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hif-update-', dir=root / 'temp') as directory:
        stage = Path(directory)
        if package is None:
            name = 'MaaGakumasu-HIF-win-x64-' + release['tag_name'] + '.zip'
            assets = {asset['name']: asset['browser_download_url'] for asset in release.get('assets', [])}
            if name not in assets or name + '.sha256' not in assets:
                raise ValueError('本 Fork 发布缺少 Windows x64 HIF ZIP 或 SHA-256，未安装更新。')
            package = stage / name
            download(assets[name], package)
            download(assets[name + '.sha256'], package.with_suffix('.zip.sha256'))
        package = Path(package).resolve()
        verify_checksum(package, package.with_suffix('.zip.sha256'))
        extracted = stage / 'extracted'
        extract_package(package, extracted)
        candidate = extracted / 'MaaGakumasu-HIF'
        if set(p.name for p in extracted.iterdir()) != {'MaaGakumasu-HIF'}:
            raise ValueError('不是完整 HIF 运行包。')
        incoming = read(candidate / 'hif-release.json')['version']
        if package.name != 'MaaGakumasu-HIF-win-x64-' + incoming + '.zip':
            raise ValueError('运行包名称与 HIF 版本不一致。')
        if release and incoming != release['tag_name']:
            raise ValueError('发布标签与运行包版本不一致。')
        if version(incoming) <= version(installed):
            return {'installed': installed, 'skipped': True, 'repository': REPOSITORY}
        validate_candidate(root, candidate)
        return install_candidate(root, candidate, fail_after_swap)
