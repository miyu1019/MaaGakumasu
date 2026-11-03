"""为本地同步准备 HIF 完整包安装操作（同版本覆盖；--allow-upgrade 时可升到源码的新版本），供 hif_update_runner.ps1 执行。

由 tools/sync_runtime.ps1 调用；改编自此前本地安装用的 prepare 脚本。
只准备暂存目录和 operation.json，不替换任何运行包文件。
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
from hif_app import read, write  # noqa: E402
from hif_build import frontend_fingerprint  # noqa: E402
from hif_update import package_manifest, safe_target, verify_manifest  # noqa: E402


def version_key(version):
    match = re.fullmatch(r'v(\d{6})\.(\d+)', version)
    assert match, f'无法识别的版本号：{version}'
    return int(match[1]), int(match[2])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--built', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--operation-file', required=True, type=Path)
    parser.add_argument('--notes', default='本地同步：移植上游演出跳过与生成按钮识别修复。')
    parser.add_argument('--allow-upgrade', action='store_true', help='允许把较旧的运行包升到源码版本（其他发行字段须一致）')
    args = parser.parse_args()

    built = args.built.resolve()
    root = args.root.resolve()
    assert (root / 'hif-release.json').is_file(), f'找不到运行包: {root}'
    assert read(built / 'hif-build-info.json')['frontend_source_sha256'] == frontend_fingerprint(), \
        '新构建的前台源码指纹与仓库不一致'
    new_release, old_release = read(built / 'hif-release.json'), read(root / 'hif-release.json')
    version, from_version = new_release['version'], old_release['version']
    if args.allow_upgrade and from_version != version:
        same = {k: v for k, v in new_release.items() if k != 'version'} == {k: v for k, v in old_release.items() if k != 'version'}
        assert same, '新构建与运行包的 hif-release.json 除版本外还有不同，不做本地升级'
        assert version_key(version) > version_key(from_version), f'只允许升级：运行包 {from_version} → 源码 {version}'
    else:
        assert new_release == old_release, \
            '新构建与运行包的 hif-release.json 不同；本脚本只做同版本同步（升级请加 -AllowUpgrade）'

    manifest = package_manifest(built, version)
    verify_manifest(built, version)

    operation_id = uuid.uuid4().hex
    stage = root / 'temp/hif-updates' / operation_id
    candidate = stage / 'extracted/MaaGakumasu-HIF'
    shutil.copytree(built, candidate, ignore=shutil.ignore_patterns('__pycache__'))

    old = verify_manifest(root, from_version, verify_files=False)['files']
    names = sorted(set(old) | set(manifest['files']) | {'hif-package-manifest.json'})
    for name in names:
        assert not safe_target(root, name).is_dir(), name

    personal = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / 'config').rglob('*') if p.is_file()}
    layout = root / 'resource/mfa_layout.json'
    if layout.exists():
        personal['resource/mfa_layout.json'] = hashlib.sha256(layout.read_bytes()).hexdigest()
    write(stage / 'personal-before.json', personal)

    op = {
        'format': 1, 'id': operation_id, 'root': str(root), 'stage': str(stage),
        'candidate': str(candidate), 'backup': str(root / 'backup' / ('hif-update-' + operation_id)),
        'from_version': from_version, 'version': version, 'state': 'prepared', 'files': names,
        'defaults': list(manifest['defaults']), 'changes': [], 'release_notes': args.notes,
    }
    for name in ('hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
        shutil.copy2(candidate / 'tools' / name, stage / name)
    write(stage / 'operation.json', op)
    write(root / 'temp/hif-update-pending.json', {'operation': str(stage / 'operation.json'), 'state': 'prepared'})
    args.operation_file.write_text(str(stage / 'operation.json'), encoding='utf-8')
    print(json.dumps({'operation': str(stage / 'operation.json'), 'files': len(names),
                      'personal_files': len(personal)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
