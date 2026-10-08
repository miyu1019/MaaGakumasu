"""Build the independent Windows or Linux x64 release from pinned public inputs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import digest_tree, extract_package, initialize_defaults, install_composition, python_executable, read, write

LOCK = read(ROOT / 'hif-release.json')
BUILD = ROOT / 'build'
FRONTEND = BUILD / 'frontend-source'


def run(command, cwd=ROOT):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True)


def download(name, platform='win-x64'):
    spec = LOCK['downloads'][name] if name == 'frontend' or platform == 'win-x64' else LOCK['linux_downloads'][name]
    extension = '.tar.gz' if name == 'frontend' or spec['url'].endswith('.tar.gz') else '.zip'
    filename = name if name == 'frontend' or platform == 'win-x64' else name + '-' + platform
    path = BUILD / 'downloads' / (filename + extension)
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
    patch = ROOT / 'patches/mfaavalonia-v2.14.0-hif.patch'
    stamp = hashlib.sha256(patch.read_bytes()).hexdigest()
    marker = FRONTEND / '.hif-patch-sha256'
    if FRONTEND.exists():
        if marker.exists() and marker.read_text().strip() == stamp:
            return
        raise ValueError('Frontend output already exists with a different patch; choose a fresh build directory.')
    extracted = BUILD / 'frontend-extracted'
    if extracted.exists():
        raise ValueError('An incomplete frontend extraction exists; inspect it before retrying.')
    extracted.mkdir(parents=True)
    extract_package(download('frontend'), extracted)
    source = next(p for p in extracted.iterdir() if p.is_dir())
    if LOCK['frontend_commit'] not in source.name:
        raise ValueError('Unexpected frontend archive root.')
    shutil.move(str(source), FRONTEND)
    run(['git', 'init', '--quiet'], FRONTEND)
    run(['git', 'apply', '--check', patch], FRONTEND)
    run(['git', 'apply', patch], FRONTEND)
    marker.write_text(stamp + '\n', encoding='utf-8')


def frontend_build(platform='win-x64'):
    prepare_frontend()
    output = BUILD / ('frontend-publish' if platform == 'win-x64' else 'frontend-publish-' + platform)
    run(['dotnet', 'publish', FRONTEND / 'MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
         '-c', 'Release', '-r', platform, '-o', output, '--self-contained', str(platform == 'linux-x64').lower(),
         '--disable-build-servers', '-m:1', '-p:UseSharedCompilation=false',
         '-p:NuGetAudit=false', '-p:RestoreIgnoreFailedSources=true', '--nologo', '-v:quiet'])
    return output


def stage(output, frontend, platform='win-x64'):
    destination = Path(output).resolve()
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing runtime directory: ' + str(destination))
    output = BUILD / ('stage-' + str(time.time_ns()))
    output.mkdir(parents=True)
    shutil.copytree(frontend, output, dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.pdb', '*.log'))
    executable = '.exe' if platform == 'win-x64' else ''
    os.replace(output / ('MFAAvalonia' + executable), output / ('MaaGakumasu' + executable))
    # The binding package ships older natives. Replace them with the pinned engine.
    framework = BUILD / ('framework' if platform == 'win-x64' else 'framework-' + platform)
    if not framework.exists():
        framework.mkdir()
        extract_package(download('framework', platform), framework)
    library = 'MaaFramework.dll' if platform == 'win-x64' else 'libMaaFramework.so'
    bin_dir = next(p.parent for p in framework.rglob(library))
    native = output / 'runtimes' / platform / 'native'
    native.mkdir(parents=True, exist_ok=True)
    (native / 'plugins').mkdir(exist_ok=True)
    for path in bin_dir.glob('*.dll' if platform == 'win-x64' else '*.so*'):
        shutil.copy2(path, native / path.name)
    agent_binary = next(p for p in framework.rglob('MaaAgentBinary') if p.is_dir())
    shutil.copytree(agent_binary, output / 'libs/MaaAgentBinary', dirs_exist_ok=True)
    (output / 'licenses').mkdir(exist_ok=True)
    shutil.copy2(framework / 'LICENSE.md', output / 'licenses/MaaFramework.LICENSE.md')
    shutil.copy2(FRONTEND / 'LICENSE', output / 'licenses/MFAAvalonia.LICENSE')
    # Do not ship a second older engine that a loader might find first.
    for path in (output / 'libs').glob('Maa*.dll' if platform == 'win-x64' else '*.so*'):
        replacement = bin_dir / path.name
        if replacement.exists():
            shutil.copy2(replacement, path)
    python = output / 'python'
    if platform == 'linux-x64':
        # This checksum-pinned runtime contains safe relative links, unlike update inputs.
        with tarfile.open(download('python', platform)) as archive:
            archive.extractall(output, filter='data')
        run([python / 'bin/python3', '-m', 'pip', 'install', '--only-binary=:all:', '--no-compile',
             '-r', ROOT / 'requirements-hif.txt'])
        # Run from the runtime root even when launched by a desktop or another cwd.
        launcher = output / 'start.sh'
        launcher.write_text('#!/bin/sh\nset -eu\ncd "$(dirname "$(readlink -f "$0")")"\nexec ./MaaGakumasu "$@"\n', encoding='utf-8')
        launcher.chmod(0o755)
    else:
        python.mkdir()
        extract_package(download('python'), python)
        (python / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\nimport site\n', encoding='utf-8')
        run([sys.executable, '-m', 'pip', 'install', '--only-binary=:all:', '--no-compile',
             '--python-version', LOCK['python_version'], '--platform', 'win_amd64',
             '--implementation', 'cp', '--abi', 'cp' + ''.join(LOCK['python_version'].split('.')[:2]),
             '--target', python / 'Lib/site-packages', '-r', ROOT / 'requirements-hif.txt'])

    upstream = output / 'upstream'
    upstream.mkdir()
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
    for name in ('assets', 'agent'):
        original_name = upstream if name == 'assets' else upstream / 'agent'
        expected = digest_tree(ROOT / name)
        actual = {relative: digest for relative, digest in digest_tree(original_name).items() if relative in expected}
        if actual != expected:
            raise ValueError('Original ' + name + ' changed; frozen upstream build refused.')
    write(output / 'upstream-provenance.json', {'version': read(upstream / 'interface.json')['version'],
          'commit': LOCK['upstream_commit'], 'files': digest_tree(upstream)})
    shutil.copytree(ROOT / 'extensions', output / 'extensions', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'patches', output / 'patches')
    for name in ('agent_entry.py', 'hif_app.py', 'hif_update.py', 'validate.py', 'run_hif_smoke.py', 'verify_updates.py'):
        (output / 'tools').mkdir(exist_ok=True)
        shutil.copy2(ROOT / 'tools' / name, output / 'tools' / name)
    for name in ('README.md', 'LICENSE', 'logo.ico', 'logo.png', 'hif-release.json', 'requirements-hif.txt'):
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
    write(output / 'hif-build-info.json', {**{key: LOCK[key] for key in
          ('version', 'upstream_commit', 'frontend_commit', 'frontend_version', 'framework_version', 'python_version')},
          'platform': platform})
    install_composition(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(output, destination)
    return destination


def package(output, platform='win-x64'):
    output = Path(output).resolve()
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    extension = '.tar.gz' if platform == 'linux-x64' else '.zip'
    name = 'MaaGakumasu-HIF-' + platform + '-' + LOCK['version'] + extension
    path = dist / name
    def included(item):
        relative = item.relative_to(output)
        return (relative.parts[0] not in ('debug', 'logs', 'temp', 'backup', 'tests')
                and '__pycache__' not in relative.parts and item.suffix.lower() not in ('.pdb', '.log', '.pyc', '.bak')
                and relative.as_posix() != 'config/maa_option.json')

    if platform == 'linux-x64':
        # Dereference the pinned Python links; update archives deliberately reject links.
        with tarfile.open(path, 'w:gz', dereference=True) as archive:
            for item in sorted(output.rglob('*')):
                if included(item):
                    archive.add(item, 'MaaGakumasu-HIF/' + item.relative_to(output).as_posix(), recursive=False)
    else:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            # Native plugin discovery requires an existing directory, even when unused.
            archive.writestr('MaaGakumasu-HIF/runtimes/win-x64/native/plugins/', '')
            for item in sorted(output.rglob('*')):
                if item.is_file() and included(item):
                    archive.write(item, 'MaaGakumasu-HIF/' + item.relative_to(output).as_posix())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + '.sha256').write_text(digest + '  ' + name + '\n', encoding='utf-8')
    print('PACKAGED ' + str(path), flush=True)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['frontend', 'build', 'package'])
    parser.add_argument('--platform', choices=['win-x64', 'linux-x64'], default='win-x64')
    parser.add_argument('--output')
    args = parser.parse_args()
    args.output = args.output or str(BUILD / ('MaaGakumasu-HIF' if args.platform == 'win-x64' else 'MaaGakumasu-HIF-linux-x64'))
    BUILD.mkdir(exist_ok=True)
    (BUILD / 'temp').mkdir(exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(BUILD / 'temp')
    os.environ['DOTNET_CLI_HOME'] = str(BUILD / 'dotnet')
    os.environ['DOTNET_CLI_TELEMETRY_OPTOUT'] = '1'
    if args.command == 'frontend':
        frontend_build(args.platform)
    elif args.command == 'build':
        output = stage(args.output, frontend_build(args.platform), args.platform)
        run([python_executable(output), output / 'tools/validate.py', '--native'], output)
    else:
        package(args.output, args.platform)


if __name__ == '__main__':
    main()
