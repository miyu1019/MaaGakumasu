"""Back up and migrate task-level combo waits before loading HIF rules."""
import copy
import json
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from extensions.hif.followups import legacy_rule

PROFESSIONS = ('好调', '集中', '好印象', '元気', '全力', '強気')
OLD_WAIT = 'HIF.国民脚光等待回合'


def legacy_waits(instances):
    values = {}
    for data in instances:
        for task in data.get('TaskItems', []):
            if task.get('entry') != 'ProduceHIF':
                continue
            options = task.get('option', [])
            profession = next((o for o in options if o.get('name') == 'HIF.角色职业'), {})
            index = profession.get('index', 1)
            name = PROFESSIONS[index] if type(index) is int and 0 <= index < len(PROFESSIONS) else '集中'
            for option in options:
                if option.get('name') != OLD_WAIT:
                    continue
                value = option.get('data', {}).get('combo_wait_turns')
                if isinstance(value, bool):
                    continue
                if str(value) in tuple(str(i) for i in range(7)):
                    values.setdefault(name, set()).add(int(value))
    return values


def migrate_followups(root, choices=None):
    root = Path(root)
    path = root / 'config/hif/cards_priority.json'
    if not path.is_file():
        return {'changed': False, 'conflicts': {}}
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    paths = sorted((root / 'config/instances').glob('*.json'))
    instances = [json.loads(p.read_text(encoding='utf-8-sig')) for p in paths]
    waits = legacy_waits(instances)
    profiles = config.get('followup_profiles', {})
    if not isinstance(profiles, dict):
        raise ValueError('followup_profiles 无效')
    conflicts = {name: sorted(values) for name, values in waits.items()
                 if name not in profiles and len(values) > 1 and legacy_rule(config, name)}
    choices = choices or {}
    for name, values in list(conflicts.items()):
        if type(choices.get(name)) is int and choices[name] in values:
            del conflicts[name]
    if conflicts:
        return {'changed': False, 'conflicts': conflicts}
    after = copy.deepcopy(config)
    after.setdefault('followup_profiles', {})
    for name in PROFESSIONS:
        if name not in profiles:
            values = waits.get(name, set())
            turn = choices.get(name, next(iter(values)) if len(values) == 1 else None)
            after['followup_profiles'][name] = legacy_rule(config, name, turn)
    after.pop('combo', None)
    after.pop('skip_limit', None)
    changed_instances = []
    for instance_path, data in zip(paths, instances):
        updated = copy.deepcopy(data)
        for task in updated.get('TaskItems', []):
            if task.get('entry') == 'ProduceHIF':
                task['option'] = [o for o in task.get('option', []) if o.get('name') != OLD_WAIT]
        if updated != data:
            changed_instances.append((instance_path, updated))
    changed = after != config or bool(changed_instances)
    if changed:
        backup = root / 'backup/followups-before-migration'
        originals = [(path, after)] if after != config else []
        originals += changed_instances
        # Prepare every backup before the first replacement.
        for source, _ in originals:
            destination = backup / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
        for source, updated in originals:
            temp = source.with_name(source.name + '.' + uuid.uuid4().hex + '.tmp')
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp.replace(source)
    return {'changed': changed, 'conflicts': {}}
