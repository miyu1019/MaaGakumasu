"""Audit the pending source set and optional release ZIP before publication."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import digest_tree, read, write


def verify_defaults(priority, drinks, custom):
    for field in ('priority_profiles', 'preferred_acquisition_profiles', 'swap_out_priority_profiles',
                  'recognition_profiles', 'use_condition_profiles', 'unknown_priority_profiles'):
        for profession, value in priority[field].items():
            assert profession == '集中' or not value, (field, profession)
    assert priority['conditional_priority_profiles'] == {'全力': {}}
    for profession, entries in drinks['profiles'].items():
        assert profession == '集中' or not entries
    assert not custom.get('cards')
    for profession, entries in custom.get('profiles', {}).items():
        assert profession == '集中' or not entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zip')
    args = parser.parse_args()
    # This includes new untracked files but respects ignore rules; staged audit is also
    # run after the explicit source selection. Old upstream files are never re-uploaded.
    changed = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '-z'], cwd=ROOT)
    new = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    new_names = {name.decode('utf-8') for name in new.split(b'\0') if name}
    names = sorted({name.decode('utf-8') for name in (changed + new).split(b'\0') if name})
    denied = {'config', 'debug', 'logs', 'backup', 'temp', 'build', 'dist', 'python', 'libs', 'runtimes', 'frontend-source'}
    private_patterns = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    for name in names:
        path = ROOT / name
        parts = PurePosixPath(name).parts
        public_template = name in ('extensions/hif/first-run/config/config.json',
                                   'extensions/hif/first-run/config/instances/hif.json')
        assert public_template or not denied.intersection(parts), name
        assert path.suffix.lower() not in {'.dll', '.exe', '.pyd', '.log', '.bak', '.pdb'}, name
        if path.is_file() and path.suffix.lower() in {'.py', '.cs', '.json', '.md', '.txt', '.patch', '.yml', '.csproj'}:
            if name in new_names:
                text = path.read_text(encoding='utf-8-sig')
            else:
                diff = subprocess.check_output(['git', 'diff', '--unified=0', 'HEAD', '--', name], cwd=ROOT).decode('utf-8')
                text = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
            assert not private_patterns.search(text), 'Private/local value in ' + name
    defaults = ROOT / 'extensions/hif/defaults'
    first_run = ROOT / 'extensions/hif/first-run'
    instance = read(first_run / 'config/instances/hif.json')
    assert set(instance) == {'InstanceName', 'CurrentControllerName', 'Resource', 'CurrentTasks', 'TaskItems', 'ResourceOptionItems'}
    assert instance['InstanceName'] == '一键培育'
    assert len(instance['TaskItems']) == 1 and instance['TaskItems'][0]['entry'] == 'ProduceHIF'
    assert instance['CurrentTasks'] == ['HIF培育<|||>ProduceHIF']
    assert not any('Position' in key or 'Instance.' in key for key in read(first_run / 'config/config.json'))
    assert re.fullmatch(r'v\d{6}\.\d+', read(ROOT / 'hif-release.json')['version'])
    verify_defaults(*(read(defaults / name) for name in
                      ('cards_priority.json', 'hif_drink_profiles.json', 'hif_custom_card_selection.json')))
    subprocess.run(['git', 'diff', '--exit-code', read(ROOT / 'hif-release.json')['upstream_commit'], '--', 'agent', 'assets'],
                   cwd=ROOT, check=True)
    result = {'source_files': len(names), 'source_clean': True, 'public_defaults_concentration_only': True,
              'upstream_source_unchanged': True, 'remote_uploaded': False}
    if args.zip:
        package = Path(args.zip)
        with zipfile.ZipFile(package) as archive:
            entries = archive.namelist()
            for entry in entries:
                relative = PurePosixPath(entry).parts[1:]
                assert not {'debug', 'logs', 'backup', 'temp', '__pycache__', 'frontend-source'}.intersection(relative), entry
                assert 'appsettings.json' not in relative, entry
                assert not any(part.endswith(('.log', '.bak', '.pdb', '.pyc')) for part in relative), entry
            expected_config = {'MaaGakumasu-HIF/config/config.json', 'MaaGakumasu-HIF/config/instances/hif.json',
                               *('MaaGakumasu-HIF/config/hif/' + p.name for p in defaults.glob('*.json'))}
            actual_config = {name for name in entries if name.startswith('MaaGakumasu-HIF/config/') and not name.endswith('/')}
            assert actual_config == expected_config
            for source in (first_run / 'config').rglob('*.json'):
                assert archive.read('MaaGakumasu-HIF/config/' + source.relative_to(first_run / 'config').as_posix()) == source.read_bytes()
            assert archive.read('MaaGakumasu-HIF/resource/mfa_layout.json') == (first_run / 'resource/mfa_layout.json').read_bytes()
            for source in defaults.glob('*.json'):
                assert archive.read('MaaGakumasu-HIF/config/hif/' + source.name) == source.read_bytes()
            assert 'MaaGakumasu-HIF/runtimes/win-x64/native/plugins/' in entries
            assert not any('验证记录' in name for name in entries)
            expected_version = read(ROOT / 'hif-release.json')['version']
            assert package.name == 'MaaGakumasu-HIF-win-x64-' + expected_version + '.zip'
            assert json.loads(archive.read('MaaGakumasu-HIF/hif-build-info.json'))['version'] == expected_version
            assert json.loads(archive.read('MaaGakumasu-HIF/hif-release.json'))['version'] == expected_version
            assert archive.read('MaaGakumasu-HIF/runtimes/win-x64/native/MaaFramework.dll') == (
                ROOT / 'build/framework/bin/MaaFramework.dll').read_bytes()
            assert len(archive.read('MaaGakumasu-HIF/libs/MFAAvalonia.Core.dll')) > 1000
        result.update(zip_files=len(entries), zip_bytes=package.stat().st_size,
                      zip_sha256=hashlib.sha256(package.read_bytes()).hexdigest(), zip_clean=True)
    write(ROOT / 'build/audit-result.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
