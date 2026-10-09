"""HIF follow-up rules and battle-local state, independent of recognition/UI."""
import copy


POLICIES = ('normal', 'reserve_first', 'combo_only')


def validate_rules(rules, known):
    if not isinstance(rules, dict):
        raise ValueError('组合配置不是对象')
    for source, rule in rules.items():
        if source not in known or not isinstance(rule, dict):
            raise ValueError(f'无效起始卡: {source}')
        targets = rule.get('targets')
        if (not isinstance(targets, list) or not targets
                or any(not isinstance(key, str) or key not in known or key == source for key in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError(f'无效后续卡: {source}')
        turns = rule.get('wait_turns')
        if type(turns) is not int or not 0 <= turns <= 6:
            raise ValueError(f'无效等待回合: {source}')
        if any(type(rule.get(key)) is not bool for key in ('use_black_vinegar', 'wait_after_success')):
            raise ValueError(f'无效组合开关: {source}')
        if rule.get('target_policy') not in POLICIES:
            raise ValueError(f'无效后续卡使用方式: {source}')
    visited, visiting = set(), set()
    def visit(source):
        if source in visiting:
            raise ValueError('组合不能形成循环')
        if source in visited:
            return
        visiting.add(source)
        for target in rules.get(source, {}).get('targets', []):
            visit(target)
        visiting.remove(source)
        visited.add(source)
    for source in rules:
        visit(source)


def legacy_rule(config, profession, wait_turns=None):
    combo = config.get('combo') or {}
    source, target = combo.get('first'), combo.get('second')
    keys = {entry.get('key') for entry in config.get('priority_profiles', {}).get(profession, [])}
    if not source or not target or source == target or not {source, target} <= keys:
        return {}
    turns = config.get('skip_limit', 3) if wait_turns is None else wait_turns
    if type(turns) is not int or not 0 <= turns <= 6:
        turns = 3
    return {source: {'targets': [target], 'wait_turns': turns, 'use_black_vinegar': True,
                     'wait_after_success': False, 'target_policy': 'reserve_first'}}


class Followups:
    def __init__(self, rules):
        self.rules = copy.deepcopy(rules)
        self.active = None
        self.successful = set()
        self.released = set()
        self.skips = 0
        self.vinegar_attempted = False
        self.full_wait = False

    def ordinary_allowed(self, key):
        # Unknown cards cannot bypass an exclusive/reserved target restriction.
        if key is None:
            return not any(self._reserved(source, rule) for source, rule in self.rules.items())
        return not any(key in rule['targets'] and self._reserved(source, rule)
                       for source, rule in self.rules.items())

    def _reserved(self, source, rule):
        return rule['target_policy'] == 'combo_only' or (
            rule['target_policy'] == 'reserve_first'
            and source not in self.successful and source not in self.released)

    def committed(self, key, role):
        if role == 'target':
            self.successful.add(self.active)
            self.released.add(self.active)
            self.active = None
        elif role in ('start', 'repeat'):
            if role == 'start':
                self.active = key
                self.vinegar_attempted = False
            self.skips = 0
            rule = self.rules[key]
            self.full_wait = key not in self.successful or rule['wait_after_success']

    def release(self):
        if self.active:
            self.released.add(self.active)
        self.active = None

    def skipped(self):
        if self.active:
            self.skips += 1
            if self.skips >= self.rules[self.active]['wait_turns']:
                self.release()
