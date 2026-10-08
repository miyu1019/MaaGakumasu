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

from hif_app import ROOT, digest_tree, download, extract_package, install_composition, python_executable, read, runtime_platform

REPOSITORY = 'miyu1019/MaaGakumasu'
COMPONENTS = ('frontend_commit', 'frontend_version', 'framework_version', 'python_version')
INSTALL = ('upstream', 'extensions/hif', 'tools', 'lang', 'data', 'interface.json',
           'hif-release.json', 'hif-build-info.json', 'upstream-provenance.json',
           'README.md', 'docs', 'patches', 'requirements-hif.txt')
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
    if runtime_platform(root) != runtime_platform(candidate):
        raise ValueError('运行包平台不匹配；请使用对应平台的完整运行包。')
    if incoming.get('repository') != REPOSITORY:
        raise ValueError('运行包不属于本 HIF Fork。')
    for key in ('version', 'upstream_commit', *COMPONENTS):
        if info.get(key) != incoming.get(key):
            raise ValueError('构建信息不一致：' + key)
    for key in COMPONENTS:
        if current.get(key) != incoming.get(key):
            raise ValueError('新版需要升级 ' + key + '；请解压完整运行包到新目录并迁移 config/。')
    for name in ('patches/mfaavalonia-v2.14.0-hif.patch', 'requirements-hif.txt'):
        if (root / name).read_bytes() != (candidate / name).read_bytes():
            raise ValueError('新版需要更换前台或 Python 依赖；请使用完整运行包并迁移 config/。')
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
    subprocess.run([str(python_executable(root)), str(candidate / 'tools/validate.py'),
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
    platform = runtime_platform(root)
    extension = '.tar.gz' if platform == 'linux-x64' else '.zip'
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
            name = 'MaaGakumasu-HIF-' + platform + '-' + release['tag_name'] + extension
            assets = {asset['name']: asset['browser_download_url'] for asset in release.get('assets', [])}
            if name not in assets or name + '.sha256' not in assets:
                raise ValueError('本 Fork 发布缺少 ' + platform + ' HIF 运行包或 SHA-256，未安装更新。')
            package = stage / name
            download(assets[name], package)
            download(assets[name + '.sha256'], Path(str(package) + '.sha256'))
        package = Path(package).resolve()
        verify_checksum(package, Path(str(package) + '.sha256'))
        extracted = stage / 'extracted'
        extract_package(package, extracted)
        candidate = extracted / 'MaaGakumasu-HIF'
        if set(p.name for p in extracted.iterdir()) != {'MaaGakumasu-HIF'}:
            raise ValueError('不是完整 HIF 运行包。')
        incoming = read(candidate / 'hif-release.json')['version']
        if package.name != 'MaaGakumasu-HIF-' + platform + '-' + incoming + extension:
            raise ValueError('运行包名称与 HIF 版本不一致。')
        if release and incoming != release['tag_name']:
            raise ValueError('发布标签与运行包版本不一致。')
        if version(incoming) <= version(installed):
            return {'installed': installed, 'skipped': True, 'repository': REPOSITORY}
        validate_candidate(root, candidate)
        return install_candidate(root, candidate, fail_after_swap)
