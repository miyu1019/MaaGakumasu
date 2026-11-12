"""Explicit offline update QA using real scripts/models and native validation."""
import copy
import json
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hif_app import ROOT, digest_tree, install_composition, read, update, write


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
    write(ROOT / 'debug/update-qa-result.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
