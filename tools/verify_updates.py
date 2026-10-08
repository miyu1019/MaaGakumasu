"""Explicit offline update QA using real scripts/models and native validation."""
import copy
import json
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hif_app import ROOT, digest_tree, install_composition, read, update, write
from hif_update import INSTALL, PROTECTED, validate_candidate, install_candidate


def main():
    root = ROOT / 'temp/update-qa'
    if root.exists():
        raise SystemExit('Update QA fixture already exists; use its evidence or choose a fresh fixture.')
    root.mkdir(parents=True)
    for name in ('upstream', 'extensions', 'config', 'resource', 'python', 'libs', 'runtimes'):
        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'temp').mkdir()
    (root / 'tools').mkdir()
    for name in ('hif_app.py', 'validate.py'):
        shutil.copy2(ROOT / 'tools' / name, root / 'tools' / name)
    install_composition(root)
    protected = {name: digest_tree(root / name) for name in ('extensions', 'config', 'resource', 'python', 'libs', 'runtimes')}

    def unchanged():
        assert protected == {name: digest_tree(root / name) for name in protected}

    full = root / 'full.zip'
    spec = read(root / 'upstream/tasks/produce.json')
    case = copy.deepcopy(spec['option']['培育难度']['cases'][-1])
    case['name'] = 'QA新增剧本'
    spec['option']['培育难度']['cases'].append(case)
    with zipfile.ZipFile(full, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (root / 'upstream').rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                archive.write(path, path.relative_to(root / 'upstream').as_posix())
        # Append the changed task only once, avoiding duplicate zip members.
        archive.writestr('resource/base/pipeline/QAAdded.json', json.dumps({'QAAddedScenario': {'recognition': 'DirectHit'}}))
        archive.writestr('config/hif/cards_priority.json', '{"must_not_install":true}')
        archive.writestr('libs/MFAAvalonia.Core.dll', b'must not install')
    # Keep the full archive well formed when replacing a task definition.
    rewritten = root / 'full-new-scenario.zip'
    with zipfile.ZipFile(full) as source, zipfile.ZipFile(rewritten, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, json.dumps(spec, ensure_ascii=False) if name == 'tasks/produce.json' else source.read(name))
    update(root, rewritten)
    assert read(root / 'upstream/tasks/produce.json')['option']['培育难度']['cases'][-1]['name'] == 'QA新增剧本'
    unchanged()

    delta = root / 'delta.zip'
    with zipfile.ZipFile(delta, 'w') as archive:
        archive.writestr('changes.json', json.dumps({'deleted_files': ['resource/base/pipeline/QAAdded.json']}))
        archive.writestr('interface.json', (root / 'upstream/interface.json').read_bytes())
        archive.writestr('resource/base/pipeline/QADelta.json', json.dumps({'QADeltaScenario': {'recognition': 'DirectHit'}}))
    update(root, delta)
    assert not (root / 'upstream/resource/base/pipeline/QAAdded.json').exists()
    assert (root / 'upstream/resource/base/pipeline/QADelta.json').exists()
    unchanged()

    before = digest_tree(root / 'upstream')
    generated = {p: p.read_bytes() for p in [root / 'interface.json', root / 'upstream-provenance.json', *(root / 'lang').glob('*.json')]}
    try:
        update(root, delta, fail_after_swap=True)
    except RuntimeError as error:
        assert 'Injected failure' in str(error)
    else:
        raise AssertionError('Rollback failure was not injected')
    assert before == digest_tree(root / 'upstream')
    assert all(p.read_bytes() == contents for p, contents in generated.items())
    unchanged()

    incompatible = root / 'incompatible.zip'
    interface = read(root / 'upstream/interface.json')
    interface['interface_version'] = 3
    with zipfile.ZipFile(incompatible, 'w') as archive:
        archive.writestr('changes.json', '{}')
        archive.writestr('interface.json', json.dumps(interface))
    try:
        update(root, incompatible)
    except ValueError:
        pass
    else:
        raise AssertionError('Incompatible package was accepted')
    assert before == digest_tree(root / 'upstream')
    unchanged()
    result = {'full_native': True, 'incremental_native': True, 'new_scenario': True,
              'rollback_native': True, 'incompatible_rejected': True, 'protected_unchanged': True}
    # Exercise the real Fork update validation/transaction without any network or game actions.
    fork_root = ROOT / 'temp/fork-update-qa'
    candidate = ROOT / 'temp/fork-update-candidate'
    for destination in (fork_root, candidate):
        destination.mkdir()
        for name in (*INSTALL, *PROTECTED):
            source = ROOT / name
            target = destination / name
            if not source.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    write(fork_root / 'config/hif/personal-update-fixture.json', {'preserve': 'own settings'})
    # Deliberately differ from the incoming first-run template, including task choices and layout.
    write(fork_root / 'config/instances/hif.json', {'InstanceName': '用户实例', 'TaskItems': [
        {'entry': 'ProduceHIF', 'option': [{'name': 'HIF.培育次数', 'index': 4}]}]})
    write(fork_root / 'config/config.json', {'BaseTheme': 'Dark', 'UI.MainWindow.Width': '1440'})
    write(fork_root / 'resource/mfa_layout.json', {'rows': 9, 'columns': 10, 'spacing': 12})
    current_version = read(candidate / 'hif-release.json')['version']
    date, sequence = current_version.rsplit('.', 1)
    fixture_version = date + '.' + str(int(sequence) + 1)
    for name in ('hif-release.json', 'hif-build-info.json'):
        incoming = read(candidate / name)
        incoming['version'] = fixture_version
        write(candidate / name, incoming)
    write(candidate / 'extensions/hif/catalog/update-fixture.json', {'hif_updated': True})
    new_scenario = read(root / 'upstream/tasks/produce.json')
    write(candidate / 'upstream/tasks/produce.json', new_scenario)
    provenance = read(candidate / 'upstream-provenance.json')
    provenance['files'] = digest_tree(candidate / 'upstream')
    write(candidate / 'upstream-provenance.json', provenance)
    validate_candidate(fork_root, candidate)
    fork_protected = {name: digest_tree(fork_root / name) for name in PROTECTED}
    original_scripts = {name: digest_tree(fork_root / name) for name in ('upstream', 'extensions')}
    try:
        install_candidate(fork_root, candidate, fail_after_swap=True)
    except RuntimeError as error:
        assert 'Injected failure' in str(error)
    else:
        raise AssertionError('Fork rollback failure was not injected')
    assert original_scripts == {name: digest_tree(fork_root / name) for name in original_scripts}
    # Rollback restores the installed version; revalidate a fresh candidate for a successful install.
    for name in ('upstream', 'extensions/hif'):
        shutil.copytree(ROOT / name, candidate / name)
    write(candidate / 'upstream/tasks/produce.json', new_scenario)
    write(candidate / 'extensions/hif/catalog/update-fixture.json', {'hif_updated': True})
    install_composition(candidate)
    installed = install_candidate(fork_root, candidate)
    assert installed['installed'] == fixture_version
    assert (fork_root / 'extensions/hif/catalog/update-fixture.json').exists()
    assert read(fork_root / 'upstream/tasks/produce.json')['option']['培育难度']['cases'][-1]['name'] == 'QA新增剧本'
    assert fork_protected == {name: digest_tree(fork_root / name) for name in PROTECTED}
    result.update(fork_native_validation=True, fork_hif_and_synced_upstream=True,
                  fork_rollback=True, fork_personal_and_runtime_preserved=True)
    write(ROOT / 'debug/update-qa-result.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
