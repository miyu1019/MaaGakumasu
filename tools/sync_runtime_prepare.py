"""用本次构建的 ZIP 准备同版本本地同步，安装和恢复由共用更新器处理。"""
import argparse
import hashlib
import json
from pathlib import Path

from hif_app import read, write
from hif_build import ROOT, frontend_fingerprint
from hif_update import AUTOBUILD_PACKAGE, AUTOBUILD_TAG, prepare_update, verify_manifest, version


def check_release(installed, incoming, allow_upgrade=False, allow_release_change=False):
    before, after = installed['version'], incoming['version']
    if version(after) < version(before):
        raise ValueError(f'不允许降级：运行包 {before} → 源码 {after}')
    missing = object()
    changed = sorted(key for key in installed.keys() | incoming.keys() if installed.get(key, missing) != incoming.get(key, missing))
    if allow_release_change:
        print('发行信息变化：' + ('、'.join(changed) if changed else '无'))
    elif changed and not (allow_upgrade and changed == ['version']):
        option = '-AllowUpgrade' if changed == ['version'] else '-AllowReleaseChange'
        raise ValueError(f'运行包与构建的发行信息不同（{"、".join(changed)}），允许此变化请加 {option}。')


def prepare_sync(built, root, package=None, *, allow_upgrade=False, allow_release_change=False, notes=None):
    incoming = read(built / 'hif-release.json')
    check_release(read(root / 'hif-release.json'), incoming, allow_upgrade, allow_release_change)
    info = read(built / 'hif-build-info.json')
    if info.get('frontend_source_sha256') != frontend_fingerprint():
        raise ValueError('本次构建的前台源码指纹与仓库不一致。')
    manifest = verify_manifest(built, incoming['version'])
    if package is None:
        name = AUTOBUILD_PACKAGE if info.get('build_channel') == AUTOBUILD_TAG else f'MaaGakumasu-HIF-win-x64-{incoming["version"]}.zip'
        package = built.parent / name
        if not package.is_file():
            package = ROOT / 'dist' / name
    if notes is None and info.get('source_commit'):
        notes = f'本地同步 {incoming["version"]}，源码提交 {info["source_commit"][:7]}。'
        if info.get('source_dirty'):
            notes += '本次构建包含未提交的工作区修改。'

    # 保留本地同步原有的个人文件哈希记录，实际导出、导回与回滚仍由安装器完成。
    personal = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / 'config').rglob('*') if p.is_file()}
    layout = root / 'resource/mfa_layout.json'
    if layout.is_file():
        personal['resource/mfa_layout.json'] = hashlib.sha256(layout.read_bytes()).hexdigest()
    result = prepare_update(root, package, expected_version=incoming['version'], reinstall=True,
                            expected_manifest=manifest, release_notes=notes)
    if result.get('state') != 'prepared':
        raise ValueError('本次构建未进入可安装状态。')
    write(Path(result['operation']).parent / 'personal-before.json', personal)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--built', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--operation-file', required=True, type=Path)
    parser.add_argument('--notes')
    parser.add_argument('--allow-upgrade', action='store_true', help='允许仅版本号变化的升级')
    parser.add_argument('--allow-release-change', action='store_true', help='允许版本号不降低时变更其他发行信息')
    args = parser.parse_args()
    result = prepare_sync(args.built.resolve(), args.root.resolve(), args.package, allow_upgrade=args.allow_upgrade,
                          allow_release_change=args.allow_release_change, notes=args.notes)
    args.operation_file.write_text(result['operation'], encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
