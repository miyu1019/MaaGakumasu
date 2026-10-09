"""Build the independent Windows release from pinned public inputs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import check_source_commit, digest_tree, extract_package, initialize_defaults, install_composition, read, write

LOCK = read(ROOT / 'hif-release.json')
BUILD = ROOT / 'build'
FRONTEND = ROOT / 'frontend'


def run(command, cwd=ROOT):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True)


def download(name):
    spec = LOCK['downloads'][name]
    extension = '.tar.gz' if name == 'frontend' else '.zip'
    path = BUILD / 'downloads' / (name + extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        print('Downloading pinned ' + name, flush=True)
        request = urllib.request.Request(spec['url'], headers={'User-Agent': 'MaaGakumasu-HIF-build'})
        temporary = path.with_suffix(path.suffix + '.tmp')
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as target:
            shutil.copyfileobj(response, target)
        os.replace(temporary, path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != spec['sha256']:
        raise ValueError('Pinned ' + name + ' hash mismatch: ' + actual)
    return path


def prepare_frontend():
    for name in ('MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj', 'LICENSE'):
        if not (FRONTEND / name).is_file():
            raise ValueError('Missing tracked frontend source: ' + name)


def frontend_fingerprint():
    """Identify source changes without including generated build/IDE output."""
    prepare_frontend()
    files = {}
    ignored = {'.git', '.vs', '.idea', '.dotnet', '.dotnet-cli', '.avalonia-build-tasks',
               'bin', 'obj', 'artifacts', 'packages', 'logs', 'config', '__pycache__'}
    for directory, folders, names in os.walk(FRONTEND):
        folders[:] = [name for name in folders if name not in ignored]
        for name in names:
            if name.endswith(('.user', '.log', '.bak', '.pyc')) or name.startswith('.tmp_'):
                continue
            path = Path(directory) / name
            data = path.read_bytes()
            if b'\0' not in data:
                data = data.replace(b'\r\n', b'\n')
            files[path.relative_to(FRONTEND).as_posix()] = hashlib.sha256(data).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def frontend_build():
    prepare_frontend()
    output = BUILD / 'frontend-publish'
    run(['dotnet', 'publish', FRONTEND / 'MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
         '-c', 'Release', '-r', 'win-x64', '-o', output, '--self-contained', 'false',
         '--disable-build-servers', '-m:1', '-p:UseSharedCompilation=false',
         '-p:NuGetAudit=false', '-p:RestoreIgnoreFailedSources=true', '--nologo', '-v:quiet'])
    return output


def ensure_upstream_commit(root, commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('upstream_commit must be an immutable full commit SHA.')
    available = subprocess.run(['git', '-C', str(root), 'cat-file', '-e', commit + '^{commit}'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if available.returncode:
        run(['git', '-C', str(root), 'fetch', '--no-tags',
             'https://github.com/SuperWaterGod/MaaGakumasu.git', commit])


def stage(output, frontend):
    destination = Path(output).resolve()
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing runtime directory: ' + str(destination))
    output = BUILD / ('stage-' + str(time.time_ns()))
    output.mkdir(parents=True)
    shutil.copytree(frontend, output, dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.pdb', '*.log'))
    os.replace(output / 'MFAAvalonia.exe', output / 'MaaGakumasu.exe')
    # The binding package ships older natives. Replace them with the pinned engine.
    framework = BUILD / 'framework'
    if not framework.exists():
        framework.mkdir()
        extract_package(download('framework'), framework)
    bin_dir = next(p.parent for p in framework.rglob('MaaFramework.dll'))
    native = output / 'runtimes/win-x64/native'
    native.mkdir(parents=True, exist_ok=True)
    (native / 'plugins').mkdir(exist_ok=True)
    for path in bin_dir.glob('*.dll'):
        shutil.copy2(path, native / path.name)
    agent_binary = next(p for p in framework.rglob('MaaAgentBinary') if p.is_dir())
    shutil.copytree(agent_binary, output / 'libs/MaaAgentBinary', dirs_exist_ok=True)
    (output / 'licenses').mkdir(exist_ok=True)
    shutil.copy2(framework / 'LICENSE.md', output / 'licenses/MaaFramework.LICENSE.md')
    shutil.copy2(FRONTEND / 'LICENSE', output / 'licenses/MFAAvalonia.LICENSE')
    # Do not ship a second older engine that a loader might find first.
    for path in (output / 'libs').glob('Maa*.dll'):
        replacement = bin_dir / path.name
        if replacement.exists():
            shutil.copy2(replacement, path)
    python = output / 'python'
    python.mkdir()
    extract_package(download('python'), python)
    (python / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\nimport site\n', encoding='utf-8')
    run([sys.executable, '-m', 'pip', 'install', '--only-binary=:all:', '--no-compile',
         '--python-version', LOCK['python_version'], '--platform', 'win_amd64',
         '--implementation', 'cp', '--abi', 'cp' + ''.join(LOCK['python_version'].split('.')[:2]),
         '--target', python / 'Lib/site-packages', '-r', ROOT / 'requirements-hif.txt'])

    upstream = output / 'upstream'
    upstream.mkdir()
    ensure_upstream_commit(ROOT, LOCK['upstream_commit'])
    check_source_commit(ROOT, LOCK['upstream_commit'])
    # Export the original source; README of this fork is intentionally different.
    archive = subprocess.check_output(['git', '-C', str(ROOT), 'archive', LOCK['upstream_commit']])
    with tarfile.open(fileobj=io.BytesIO(archive)) as original:
        for member in original:
            if not member.isfile():
                continue
            if member.name.startswith('assets/'):
                name = member.name[7:]
            elif member.name.startswith('agent/') or member.name in ('requirements.txt', 'README.md', 'LICENSE', 'AGENTS.md'):
                name = member.name
            else:
                continue
            target = upstream / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original.extractfile(member).read())
    write(output / 'upstream-provenance.json', {'version': read(upstream / 'interface.json')['version'],
          'commit': LOCK['upstream_commit'], 'files': digest_tree(upstream)})
    shutil.copytree(ROOT / 'extensions', output / 'extensions', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('agent_entry.py', 'hif_app.py', 'hif_update.py', 'validate.py', 'run_hif_smoke.py', 'verify_updates.py', 'clear_logs.ps1',
                 'export_settings.ps1', 'import_settings.ps1', 'hif_update_runner.ps1'):
        (output / 'tools').mkdir(exist_ok=True)
        shutil.copy2(ROOT / 'tools' / name, output / 'tools' / name)
    for name in ('README.md', 'LICENSE', 'logo.ico', 'logo.png', 'hif-release.json', 'requirements-hif.txt', 'clear_logs_清除日志.bat',
                 'export_settings_导出设置.bat', 'import_settings_导入设置.bat', 'recover_update_恢复更新.bat'):
        shutil.copy2(ROOT / name, output / name)
    # Frontend publish may contain its own readme/docs, but never local user config.
    shutil.copytree(ROOT / 'docs', output / 'docs', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('node_modules', '.temp', '__pycache__'))
    (output / 'debug').mkdir(exist_ok=True)
    (output / 'temp').mkdir(exist_ok=True)
    # Only a fresh runtime receives the public UI/instance template. Updates keep config/ intact.
    for directory in ('config', 'resource'):
        shutil.copytree(ROOT / 'extensions/hif/first-run' / directory, output / directory, dirs_exist_ok=True)
    initialize_defaults(output)
    install_composition(output)
    write(output / 'hif-build-info.json', {key: LOCK[key] for key in
          ('version', 'upstream_commit', 'frontend_commit', 'frontend_version', 'framework_version', 'python_version')}
          | {'frontend_source_sha256': frontend_fingerprint()})
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(output, destination)
    return destination


def package(output):
    output = Path(output).resolve()
    from hif_update import package_manifest
    package_manifest(output, LOCK['version'])
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    name = 'MaaGakumasu-HIF-win-x64-' + LOCK['version'] + '.zip'
    path = dist / name
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        # Native plugin discovery requires an existing directory, even when unused.
        archive.writestr('MaaGakumasu-HIF/runtimes/win-x64/native/plugins/', '')
        manifest = json.loads((output / 'hif-package-manifest.json').read_text(encoding='utf-8'))
        included = set(manifest['files']) | set(manifest['defaults']) | {'hif-package-manifest.json'}
        for item in sorted(output.rglob('*')):
            relative = item.relative_to(output)
            if relative.as_posix() not in included:
                continue
            if not item.is_file() or relative.parts[0] in ('debug', 'logs', 'temp', 'backup', 'tests') or '__pycache__' in relative.parts:
                continue
            if item.suffix.lower() in ('.pdb', '.log', '.pyc', '.bak'):
                continue
            if relative.as_posix() == 'config/maa_option.json':
                continue
            archive.write(item, (Path('MaaGakumasu-HIF') / relative).as_posix())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(digest + '  ' + name + '\n', encoding='utf-8')
    print('PACKAGED ' + str(path), flush=True)
    return path


def main():
    global BUILD
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['frontend', 'build', 'package'])
    parser.add_argument('--output', default=str(BUILD / 'MaaGakumasu-HIF'))
    parser.add_argument('--build-dir', type=Path, default=BUILD)
    args = parser.parse_args()
    BUILD = args.build_dir.resolve()
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / 'temp').mkdir(exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(BUILD / 'temp')
    os.environ['DOTNET_CLI_HOME'] = str(BUILD / 'dotnet')
    os.environ['DOTNET_CLI_TELEMETRY_OPTOUT'] = '1'
    if args.command == 'frontend':
        frontend_build()
    elif args.command == 'build':
        output = stage(args.output, frontend_build())
        run([output / 'python/python.exe', output / 'tools/validate.py', '--native'], output)
    else:
        package(args.output)


if __name__ == '__main__':
    main()
