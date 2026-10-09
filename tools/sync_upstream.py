"""Explicit maintainer-only upstream sync; never called by the runtime updater."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from hif_app import ROOT, check_source_commit, digest_tree, extract_package, initialize_defaults, read, write

PROTECTED = ('extensions/hif', 'config', 'resource', 'frontend', 'requirements-hif.txt',
             'python', 'libs', 'runtimes')
OFFICIAL = ('https://github.com/SuperWaterGod/MaaGakumasu.git',
            'git@github.com:SuperWaterGod/MaaGakumasu.git')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def fingerprints(root):
    result = {}
    for name in PROTECTED:
        path = root / name
        result[name] = (digest_tree(path) if path.is_dir() else
                        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


def unchanged_sources(root, commit):
    for name in ('agent', 'assets'):
        if not (root / name).resolve().is_relative_to(root.resolve()):
            raise ValueError('上游源码目录不能位于仓库之外。')
    check_source_commit(root, commit)


def check_dependencies(root, commit, runtime):
    # The maintainer build environment already requires pip; install no new dependencies.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    declarations = git(root, 'show', commit + ':requirements.txt').decode('utf-8-sig')
    environment = default_environment()
    lock = read(root / 'hif-release.json')
    environment.update(python_version='.'.join(lock['python_version'].split('.')[:2]),
                       python_full_version=lock['python_version'], extra='')
    requirements = []
    for line in declarations.splitlines():
        line = line.split(' #', 1)[0].strip()
        if not line or line.startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.url or requirement.extras:
            raise ValueError('上游含额外来源或 extras 依赖，需要人工审查；未安装任何依赖。')
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirements.append(requirement)
    names = [requirement.name for requirement in requirements]
    code = ('import importlib.metadata as m,json,platform; names=' + repr(names) +
            '; print(json.dumps({"python":platform.python_version(),"maafw":m.version("maafw"),'
            '"packages":{n:m.version(n) for n in names}}))')
    installed = json.loads(subprocess.check_output([str(runtime / 'python/python.exe'), '-c', code], cwd=runtime))
    if installed['python'] != lock['python_version'] or installed['maafw'] != lock['framework_version']:
        raise ValueError('校验运行环境的实际 Python/MaaFramework 版本与固定版本不一致。')
    versions = installed['packages']
    for requirement in requirements:
        if versions[requirement.name] not in requirement.specifier:
            raise ValueError('固定运行环境不满足上游依赖 ' + str(requirement) + '；请单独处理运行时升级。')


def check_candidate(root, commit, source, stage, runtime):
    check_dependencies(root, commit, runtime)
    upstream = stage / 'upstream'
    shutil.copytree(source / 'assets', upstream)
    shutil.copytree(source / 'agent', upstream / 'agent')
    shutil.copytree(root / 'extensions/hif', stage / 'extensions/hif',
                    ignore=shutil.ignore_patterns('__pycache__'))
    initialize_defaults(stage)
    (stage / 'tools').mkdir()
    shutil.copy2(root / 'tools/hif_app.py', stage / 'tools/hif_app.py')
    shutil.copy2(root / 'hif-release.json', stage / 'hif-release.json')
    subprocess.run([str(runtime / 'python/python.exe'), str(root / 'tools/validate.py'),
                    '--root', str(stage), '--upstream', str(upstream), '--native'], cwd=stage, check=True)


def sync(root, ref, runtime, *, apply=False, fetch=True):
    root, runtime = Path(root).resolve(), Path(runtime).resolve()
    if not ref or ref.startswith('-'):
        raise ValueError('请指定上游版本标签、分支或提交号。')
    lock = read(root / 'hif-release.json')
    manifest_before = (root / 'hif-release.json').read_bytes()
    unchanged_sources(root, lock['upstream_commit'])
    protected = fingerprints(root)
    if fetch:
        if git(root, 'remote', 'get-url', 'upstream').decode().strip() not in OFFICIAL:
            raise ValueError('upstream remote 必须指向原作者 SuperWaterGod/MaaGakumasu。')
        subprocess.run(['git', '-C', str(root), 'fetch', '--no-tags', 'upstream', ref], check=True)
        ref = 'FETCH_HEAD'
    commit = git(root, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    if not (runtime / 'python/python.exe').is_file():
        raise ValueError('找不到已构建的固定运行环境；先构建运行包，或用 --runtime 指定其目录。')
    runtime_lock = read(runtime / 'hif-release.json')
    for key in ('framework_version', 'python_version'):
        if runtime_lock[key] != lock[key]:
            raise ValueError('校验运行环境与源码的固定版本不同：' + key)
    changes = git(root, 'diff', '--name-status', lock['upstream_commit'], commit,
                  '--', 'agent', 'assets').decode('utf-8')
    build = root / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sync-upstream-', dir=build) as directory:
        stage = Path(directory)
        archive = stage / 'source.tar'
        archive.write_bytes(git(root, 'archive', '--format=tar', commit, 'agent', 'assets'))
        source = stage / 'source'
        source.mkdir()
        extract_package(archive, source)
        check_candidate(root, commit, source, stage, runtime)
        unchanged_sources(root, lock['upstream_commit'])
        if protected != fingerprints(root) or (root / 'hif-release.json').read_bytes() != manifest_before:
            raise RuntimeError('校验期间 HIF 或个人配置被修改，请重新检查；未替换上游。')
        result = {'upstream_commit': commit, 'upstream_version': read(source / 'assets/interface.json')['version'],
                  'compatible': True, 'applied': False, 'changes': changes.splitlines()}
        if not apply or commit == lock['upstream_commit']:
            return result
        backup = build / 'upstream-backups' / str(time.time_ns())
        source_hashes = {name: digest_tree(source / name) for name in ('agent', 'assets')}
        backup.mkdir(parents=True)
        manifest = root / 'hif-release.json'
        shutil.copy2(manifest, backup / manifest.name)
        moved, installed = [], []
        try:
            for name in ('agent', 'assets'):
                os.replace(root / name, backup / name)
                moved.append(name)
                os.replace(source / name, root / name)
                installed.append(name)
            lock['upstream_commit'] = commit
            write(manifest, lock)
            if source_hashes != {name: digest_tree(root / name) for name in source_hashes}:
                raise RuntimeError('替换后的上游文件与固定提交不一致，恢复旧上游。')
            if protected != fingerprints(root):
                raise RuntimeError('受保护的 HIF 或个人文件内容变化，恢复旧上游。')
        except BaseException:
            for name in reversed(installed):
                os.replace(root / name, stage / ('failed-' + name))
            for name in reversed(moved):
                os.replace(backup / name, root / name)
            if manifest.read_bytes() != (backup / manifest.name).read_bytes():
                os.replace(backup / manifest.name, manifest)
            raise
        result.update(applied=True, backup=str(backup))
        return result


def main():
    parser = argparse.ArgumentParser(description='维护者手动同步上游；不会提交、推送或发布。')
    parser.add_argument('command', choices=['check', 'apply'])
    parser.add_argument('--ref', required=True, help='原作者的版本标签、分支或提交号')
    parser.add_argument('--runtime', default=str(ROOT / 'build/MaaGakumasu-HIF'))
    parser.add_argument('--no-fetch', action='store_true', help='仅使用已经获取的本地 Git 提交')
    args = parser.parse_args()
    try:
        result = sync(ROOT, args.ref, args.runtime, apply=args.command == 'apply', fetch=not args.no_fetch)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, '同步未完成，旧上游和 HIF 保留：' + str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
