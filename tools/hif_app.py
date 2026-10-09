"""Compose independent tasks, migrate settings and import offline upstream fixtures."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {'interface.json', 'interface.jsonc', 'tasks', 'lang', 'resource', 'data', 'agent',
           'requirements.txt', 'README.md', 'LICENSE', 'AGENTS.md', 'changes.json', 'logo.ico'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def digest_tree(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def check_source_commit(root, commit):
    """Compare complete source trees using Git's text filters without changing the real index."""
    root = Path(root).resolve()
    build = root / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='upstream-index-', dir=build) as directory:
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = str(Path(directory) / 'index')
        prefix = ['git', '-C', str(root)]
        subprocess.run([*prefix, 'read-tree', commit], check=True, env=env, capture_output=True)
        subprocess.run([*prefix, 'add', '--all', '--', 'agent', 'assets'], check=True, env=env, capture_output=True)
        changes = subprocess.check_output([*prefix, 'diff', '--cached', '--name-only', commit,
                                           '--', 'agent', 'assets'], env=env)
        if changes.strip():
            raise ValueError('agent/ 或 assets/ 有本地修改或缺少上游文件；不会覆盖这些修改。')
def runtime_platform(root=ROOT):
    info = Path(root) / 'hif-build-info.json'
    return read(info).get('platform', 'win-x64') if info.exists() else (
        'linux-x64' if (Path(root) / 'python/bin/python3').exists() else 'win-x64')


def python_executable(root=ROOT):
    return Path(root) / ('python/bin/python3' if runtime_platform(root) == 'linux-x64' else 'python/python.exe')


def compose(root=ROOT, upstream=None):
    root = Path(root)
    upstream = Path(upstream) if upstream else root / 'upstream'
    interface = read(upstream / 'interface.json')
    if interface.get('interface_version') != 2:
        raise ValueError('Only interface_version 2 is supported; the installed version was retained.')
    if not (upstream / 'agent/main.py').is_file():
        raise ValueError('Missing upstream Agent entry.')
    imports = []
    for name in interface.get('import', []):
        path = upstream / name
        # The frozen Windows interface spells Shutdown.json differently from the file.
        if not path.exists() and path.parent.is_dir():
            matches = [p for p in path.parent.iterdir() if p.name.casefold() == path.name.casefold()]
            if len(matches) == 1:
                path = matches[0]
        if not path.resolve().is_relative_to(upstream.resolve()) or not path.is_file():
            raise ValueError(f'Invalid/missing upstream import: {name}')
        read(path)
        imports.append('./' + path.relative_to(upstream).as_posix())
    result = copy.deepcopy(interface)
    result['name'] = 'MaaGakumasu-HIF'
    result['contact'] = 'https://github.com/miyu1019/MaaGakumasu'
    result['description'] = '独立 HIF 培育；仅手动更新本 Fork 发布版本'
    release_file = root / 'hif-release.json'
    if release_file.exists():
        result['version'] = read(release_file)['version']
    if str(result.get('welcome', '')).startswith('./resource/'):
        result['welcome'] = './upstream/' + result['welcome'].removeprefix('./')
    result['import'] = ['./upstream/' + name.removeprefix('./') for name in imports]
    result['import'].append('./extensions/hif/tasks/produce_hif.json')
    result['agent'] = {'child_exec': './' + python_executable(root).relative_to(root).as_posix(),
                       'child_args': ['-u', './tools/agent_entry.py']}
    for resource in result['resource']:
        originals = resource['path']
        resource['path'] = ['./upstream/' + p.removeprefix('./') for p in originals]
    result['languages'] = {key: './lang/' + Path(value).name for key, value in interface.get('languages', {}).items()}
    # Never expose the stock updater or a mutable upstream URL through the composed interface.
    for key in ('github', 'mirrorchyan_rid', 'mirrorchyan_multiplatform', 'update', 'url'):
        result.pop(key, None)
    hif = read(root / 'extensions/hif/tasks/produce_hif.json')
    required_resources = {name for task in hif['task'] for name in task.get('resource', [])}
    if not required_resources.issubset({resource['name'] for resource in interface['resource']}):
        raise ValueError('Upstream resource identifiers changed; update rejected to preserve HIF availability.')
    upstream_names = {task['name'] for task in interface.get('task', [])}
    for name in imports:
        upstream_names.update(task['name'] for task in read(upstream / name).get('task', []))
    if upstream_names & {task['name'] for task in hif['task']}:
        raise ValueError('Upstream task names conflict with HIF; update rejected.')
    languages = {}
    for key, path in interface.get('languages', {}).items():
        content = read(upstream / path)
        # Frontend markdown resolves images from the application root.
        content = {name: value.replace('](resource/', '](upstream/resource/').replace('](./resource/', '](./upstream/resource/')
                   if isinstance(value, str) else value for name, value in content.items()}
        extension_path = root / 'extensions/hif/lang' / Path(path).name
        if extension_path.exists():
            for name, value in read(extension_path).items():
                if name.startswith('HIF培育') or name not in content:
                    if isinstance(value, str):
                        value = value.replace('](resource/base/image/', '](extensions/hif/resource/base/image/hif/')
                    content[name] = value
        languages[Path(path).name] = content
    return result, languages


def install_composition(root=ROOT, upstream=None):
    interface, languages = compose(root, upstream)
    source = Path(upstream) if upstream else Path(root) / 'upstream'
    # Unchanged upstream callbacks use data/ relative to the application working directory.
    if (source / 'data').is_dir():
        shutil.copytree(source / 'data', Path(root) / 'data', dirs_exist_ok=True)
    for name, language in languages.items():
        write(Path(root) / 'lang' / name, language)
    write(Path(root) / 'interface.json', interface)


def initialize_defaults(root=ROOT):
    """Install public templates only when the user's file does not exist."""
    root = Path(root)
    for source in (root / 'extensions/hif/defaults').glob('*.json'):
        target = root / 'config/hif' / source.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def migrate_personal_files(root=ROOT):
    """Retain legacy user values and move only known HIF image references."""
    root = Path(root)
    changed = 0
    for name in ('cards_priority.json', 'hif_drink_profiles.json', 'hif_custom_card_selection.json'):
        target = root / 'config/hif' / name
        legacy = root / 'config' / name
        template = root / 'extensions/hif/defaults' / name
        backup = root / 'backup/personal-before-hif-migration' / name
        if legacy.is_file() and (not target.exists() or template.is_file() and read(target) == read(template)):
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(legacy, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            changed += 1
        if not target.exists():
            continue
        before = read(target)
        def image_name(value):
            if isinstance(value, list):
                return [image_name(item) for item in value]
            if isinstance(value, str) and not value.startswith('hif/'):
                path = root / 'extensions/hif/resource/base/image/hif' / value
                if path.is_file():
                    return 'hif/' + value.replace('\\', '/')
            return value
        def rename(value):
            if isinstance(value, dict):
                return {key: image_name(item) if key == 'template' else rename(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rename(item) for item in value]
            return value
        after = rename(before)
        if after != before:
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
            write(target, after)
            changed += 1
    return changed


def migrate_config(root=ROOT):
    root = Path(root)
    mapping = read(root / 'extensions/hif/namespace.json')['options']
    definitions = read(root / 'extensions/hif/tasks/produce_hif.json')
    tasks_by_name = {task['name']: task for task in definitions['task']}
    changed = 0
    for path in (root / 'config/instances').glob('*.json'):
        config = read(path)
        before = copy.deepcopy(config)
        for task in config.get('TaskItems', []):
            if task.get('name') not in ('开始培育', '开始培育-zh_CN') or task.get('entry') != 'Produce':
                continue
            difficulty = next((o for o in task.get('option', []) if o.get('name') == '培育难度'), {})
            # Legacy difficulty indexes were 初=0, NIA=1, HIF=2. Stored child options
            # are not proof of the active difficulty (inactive old children may remain).
            if difficulty.get('index') != 2 and difficulty.get('selected_cases') != ['HIF']:
                continue
            name = 'HIF培育-zh_CN' if task['name'].endswith('-zh_CN') else 'HIF培育'
            old_options = task.get('option', [])
            retained = {key: task[key] for key in ('default_check', 'display_name_override', 'remark') if key in task}
            task.clear()
            task.update(copy.deepcopy(tasks_by_name[name]), **retained)
            def rename_option(option):
                option = copy.deepcopy(option)
                option['name'] = mapping[option['name']]
                option.pop('pipeline_override', None)
                children = option.get('sub_options')
                if children is not None:
                    option['sub_options'] = [rename_option(o) for o in children if o.get('name') in mapping]
                return option
            task['option'] = [rename_option(o) for o in old_options if o.get('name') in mapping]
            key = name + '<|||>ProduceHIF'
            if key not in config.setdefault('CurrentTasks', []):
                config['CurrentTasks'].append(key)
        if config != before:
            backup = root / 'backup/config-before-hif-migration' / path.name
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
            write(path, config)
            changed += 1
    return changed


def download(url, target):
    request = urllib.request.Request(url, headers={'User-Agent': 'MaaGakumasu-HIF-manual-update'})
    with urllib.request.urlopen(request, timeout=40) as response:
        with open(target, 'wb') as output:
            shutil.copyfileobj(response, output)


def extract_package(package, target):
    # Validate members before extracting. Do not allow archives to reach protected files.
    def safe(name):
        name = name.replace('\\', '/')
        parts = PurePosixPath(name)
        if parts.is_absolute() or '..' in parts.parts or ':' in name:
            raise ValueError('Unsafe archive path: ' + name)
    if zipfile.is_zipfile(package):
        with zipfile.ZipFile(package) as archive:
            for item in archive.infolist():
                safe(item.filename)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Archive symlinks are not allowed.')
            archive.extractall(target)
    else:
        with tarfile.open(package) as archive:
            for item in archive.getmembers():
                safe(item.name)
                if item.issym() or item.islnk() or not (item.isfile() or item.isdir()):
                    raise ValueError('Archive links and special files are not allowed.')
            archive.extractall(target, filter='data')


def update(root=ROOT, package=None, fail_after_swap=False):
    # Offline import used by upstream isolation QA, never by the frontend update button.
    root = Path(root).resolve()
    if package is None:
        raise ValueError('Offline upstream import requires an explicit package.')
    (root / 'temp').mkdir(parents=True, exist_ok=True)
    # The native front end stops Agents before calling this manual updater.
    with tempfile.TemporaryDirectory(prefix='upstream-update-', dir=root / 'temp') as directory:
        stage = Path(directory)
        extracted = stage / 'extracted'
        extracted.mkdir()
        extract_package(package, extracted)
        candidates = [p.parent for p in extracted.rglob('interface.json') if (p.parent / 'resource').is_dir()]
        if len(candidates) != 1:
            raise ValueError('Package must contain exactly one upstream interface/resource root.')
        origin = candidates[0]
        candidate = stage / 'upstream'
        incremental = (origin / 'changes.json').is_file()
        if incremental:
            shutil.copytree(root / 'upstream', candidate)
            changes = read(origin / 'changes.json')
            deleted = changes.get('deleted', changes.get('deleted_files', []))
            if not isinstance(deleted, list):
                raise ValueError('Invalid incremental deletion list.')
            for relative in deleted:
                relative = str(relative).replace('\\', '/').removeprefix('./')
                target = candidate / relative
                if not target.resolve().is_relative_to(candidate.resolve()) or relative.split('/')[0] not in ALLOWED:
                    raise ValueError('Unsafe incremental deletion: ' + relative)
                if target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            for relative in sorted(changes.get('deleted_dir', []), key=lambda x: str(x).count('/'), reverse=True):
                relative = str(relative).replace('\\', '/').removeprefix('./')
                target = candidate / relative
                if not target.resolve().is_relative_to(candidate.resolve()) or relative.split('/')[0] not in ALLOWED:
                    raise ValueError('Unsafe incremental directory deletion: ' + relative)
                if target.is_dir():
                    target.rmdir()  # Unexpected remaining files reject the package instead of deleting them.
        else:
            candidate.mkdir()
        for source in origin.iterdir():
            if source.name not in ALLOWED:
                continue
            target = candidate / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, target)
        # Source releases store Agent code next to assets, deployment packages store it inside.
        if origin.name == 'assets' and (origin.parent / 'agent').is_dir():
            shutil.copytree(origin.parent / 'agent', candidate / 'agent', dirs_exist_ok=True)
        compose(root, candidate)
        subprocess.run([str(python_executable(root)), str(root / 'tools/validate.py'),
                        '--root', str(root), '--upstream', str(candidate), '--native'], check=True, cwd=root)
        protected_before = {name: digest_tree(root / name) for name in ('extensions/hif', 'config/hif', 'libs', 'runtimes', 'python')}
        stamp = str(time.time_ns())
        backup = root / 'backup' / ('upstream-' + stamp)
        backup.parent.mkdir(exist_ok=True)
        generated = {p: p.read_bytes() for p in [root / 'interface.json', *(root / 'lang').glob('*.json'),
                                              *(p for p in (root / 'data').rglob('*') if p.is_file())]}
        provenance_path = root / 'upstream-provenance.json'
        if provenance_path.exists():
            generated[provenance_path] = provenance_path.read_bytes()
        os.replace(root / 'upstream', backup)
        swapped = False
        try:
            os.replace(candidate, root / 'upstream')
            swapped = True
            if fail_after_swap:
                raise RuntimeError('Injected failure for rollback test')
            install_composition(root)
            for name, hashes in protected_before.items():
                if digest_tree(root / name) != hashes:
                    raise RuntimeError('Protected files changed: ' + name)
            write(provenance_path, {'version': read(root / 'upstream/interface.json')['version'],
                  'source': str(package), 'files': digest_tree(root / 'upstream')})
        except BaseException:
            if swapped:
                os.replace(root / 'upstream', stage / 'failed-upstream')
            os.replace(backup, root / 'upstream')
            for directory in (root / 'data', root / 'lang'):
                for path in directory.rglob('*'):
                    if path.is_file() and path not in generated:
                        path.unlink()
            if provenance_path.exists() and provenance_path not in generated:
                provenance_path.unlink()
            for path, data in generated.items():
                path.write_bytes(data)
            raise
        return {'installed': read(root / 'upstream/interface.json')['version'], 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['compose', 'migrate', 'check', 'update', 'prepare-update'])
    parser.add_argument('--package')
    args = parser.parse_args()
    if args.command == 'compose':
        initialize_defaults()
        install_composition()
    elif args.command == 'migrate':
        print(json.dumps({'settings': migrate_personal_files(), 'tasks': migrate_config()}, ensure_ascii=False))
    else:
        # Embedded Python's _pth excludes the script directory.
        sys.path.insert(0, str(ROOT / 'tools'))
        from hif_update import update as update_fork, prepare_update
        result = prepare_update(package=args.package) if args.command == 'prepare-update' else update_fork(package=args.package, check_only=args.command == 'check')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
