"""HIF 出牌编排：规则校验与本战组合状态，不依赖识别与界面。"""
import copy


POLICIES = ('normal', 'reserve_first', 'combo_only')
DRINK_SLOTS = ('before', 'after', 'waiting')


def _drink_list(value, known_drinks, label):
    if (not isinstance(value, list)
            or any(not isinstance(name, str) or name not in known_drinks for name in value)
            or len(set(value)) != len(value)):
        raise ValueError(f'无效饮料: {label}')
    return list(value)


def validate_sequences(rules, known_cards, known_drinks):
    """校验并规范化一个职业的出牌编排，结构无效时抛 ValueError。

    以起始卡为键：targets 是按顺序衔接的后续候选，可为空（只给这张卡前后用饮）；
    drinks 含起始卡的前置、后置饮料、组合等待用饮，以及各候选的前置、后置饮料。
    """
    if not isinstance(rules, dict):
        raise ValueError('出牌编排配置不是对象')
    result = {}
    for source, rule in rules.items():
        if source not in known_cards or not isinstance(rule, dict):
            raise ValueError(f'无效起始卡: {source}')
        targets = rule.get('targets')
        if (not isinstance(targets, list)
                or any(not isinstance(key, str) or key not in known_cards or key == source for key in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError(f'无效后续卡: {source}')
        turns = rule.get('wait_turns')
        if type(turns) is not int or not 0 <= turns <= 6:
            raise ValueError(f'无效等待回合: {source}')
        if type(rule.get('wait_after_success')) is not bool:
            raise ValueError(f'无效组合开关: {source}')
        if rule.get('target_policy') not in POLICIES:
            raise ValueError(f'无效后续卡使用方式: {source}')
        drinks = rule.get('drinks', {})
        if not isinstance(drinks, dict) or set(drinks) - {*DRINK_SLOTS, 'targets'}:
            raise ValueError(f'无效编排饮料: {source}')
        slots = {slot: _drink_list(drinks.get(slot, []), known_drinks, f'{source}/{slot}') for slot in DRINK_SLOTS}
        per_target = drinks.get('targets', {})
        if not isinstance(per_target, dict) or set(per_target) - set(targets):
            raise ValueError(f'无效后续卡饮料: {source}')
        target_drinks = {}
        for target in targets:
            entry = per_target.get(target, {})
            if not isinstance(entry, dict) or set(entry) - {'before', 'after'}:
                raise ValueError(f'无效后续卡饮料: {source}/{target}')
            target_drinks[target] = {slot: _drink_list(entry.get(slot, []), known_drinks, f'{source}/{target}/{slot}')
                                     for slot in ('before', 'after')}
        if not targets and slots['waiting']:
            raise ValueError(f'没有后续卡的编排不能设置等待用饮: {source}')
        if not targets and not slots['before'] and not slots['after']:
            raise ValueError(f'空的出牌编排: {source}')
        result[source] = {
            'targets': list(targets), 'wait_turns': turns, 'wait_after_success': rule['wait_after_success'],
            'target_policy': rule['target_policy'], 'drinks': {**slots, 'targets': target_drinks},
        }
    visited, visiting = set(), set()

    def visit(source):
        if source in visiting:
            raise ValueError('组合不能形成循环')
        if source in visited:
            return
        visiting.add(source)
        for target in result.get(source, {}).get('targets', []):
            visit(target)
        visiting.remove(source)
        visited.add(source)
    for source in result:
        visit(source)
    return result


class Followups:
    """本战组合衔接状态；只接收有后续候选的编排。"""

    def __init__(self, rules):
        self.rules = copy.deepcopy(rules)
        self.active = None
        self.successful = set()
        self.released = set()
        self.skips = 0
        self.waiting_attempted = False
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
                self.waiting_attempted = False
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
