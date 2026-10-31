"""HIF 出牌决策 mixin（纯逻辑，不依赖 maa，可单独测试）。"""
from __future__ import annotations

import os
import json
import unicodedata
from typing import TYPE_CHECKING, Optional
from collections import Counter

from utils import logger

if TYPE_CHECKING:
    from maa.context import Context

from extensions.hif.recovery import state as _hif_recovery
from extensions.hif.followups import Followups, validate_rules

from . import common
from .common import HifConfigError


class BattleDecisionMixin:
    """HIF 出牌决策：按职业加载策略，计算优先级、条件规则、使用限制与组合衔接。

    只依赖传入的手牌描述与本对象状态，不调用 maa；需要截图或点击的步骤（配对用饮、黒酢检索）
    通过 self 上其他 mixin 的方法完成。
    """

    # 阈值常量
    PROFESSION_NODE = "ProduceHIF__ProduceHIFProfessionFlag"
    PROFESSION_BY_MAX_HIT = {
        1: "好调", 2: "集中", 3: "好印象", 4: "元気", 5: "全力", 6: "強気",
    }
    DEFAULT_PROFESSION = "集中"

    def _load_hif_followups(self) -> None:
        try:
            with open(common.CARDS_PRIORITY_CONFIG_PATH, encoding='utf-8') as file:
                config = json.load(file)
            with open(common.HIF_PRIORITY_CATALOG_PATH, encoding='utf-8') as file:
                catalog = json.load(file)
        except (OSError, ValueError) as exc:
            raise HifConfigError(f'HIF组合配置读取失败: {exc}') from exc
        try:
            profiles = config.get('followup_profiles')
            if not isinstance(profiles, dict) or self.profession not in profiles:
                raise ValueError('组合尚未迁移，请打开卡牌面板保存组合配置')
            rules = profiles[self.profession]
            validate_rules(rules, catalog)
        except ValueError as exc:
            logger.warning(f'HIF安全降级: 本战关闭组合功能，保留出牌优先级与使用限制；原因={exc}')
            rules = {}
        self._followups = Followups(rules)

    def _observe_followup_turn(self, turn) -> None:
        if self._followups is not None and self._followup_skip_turn is not None and turn is not None:
            if turn < self._followup_skip_turn or self._extra_turn_transition:
                source = self._followups.active
                self._followups.skipped()
                logger.info(f'HIF组合: {source} 已确认空过，等待计数={self._followups.skips}')
                self._followup_skip_turn = None
                self._followup_skip_deadline = None

    @classmethod
    def _load_hif_profession(cls, context: Context) -> str:
        """读取前台角色职业；无效或缺失时使用现有的集中职业。"""
        try:
            node = context.get_node_data(cls.PROFESSION_NODE)
            if node and node.get("max_hit") is not None:
                profession = cls.PROFESSION_BY_MAX_HIT.get(int(node["max_hit"]))
                if profession:
                    return profession
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF角色职业读取异常: {e!r}，使用{cls.DEFAULT_PROFESSION}")
        return cls.DEFAULT_PROFESSION

    def _load_config(self, profession: str = DEFAULT_PROFESSION) -> None:
        """按当前职业加载卡名、模板和出牌优先级。"""
        try:
            with open(common.CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise HifConfigError(f'HIF基础出牌策略无法读取: {e}') from e

        if not isinstance(cfg, dict):
            raise HifConfigError('HIF基础出牌策略不是对象')
        # 组合等待只由 Followups 管理；旧 combo/skip_limit 字段由迁移工具处理，这里不再读取。
        self.fallback = cfg.get("fallback", "suggestion")

        profiles = cfg.get("priority_profiles", {}) or {}
        if not isinstance(profiles, dict):
            raise HifConfigError('HIF基础出牌优先级结构无效')
        priority = profiles.get(profession)
        using_default = priority is None
        if using_default:
            priority = profiles.get(self.DEFAULT_PROFESSION, []) or cfg.get("priority", []) or []
        if profession != self.DEFAULT_PROFESSION and using_default:
            logger.warning(f"HIF角色职业={profession}不存在优先级表，沿用{self.DEFAULT_PROFESSION}")
        if not isinstance(priority, list) or any(not isinstance(item, dict) for item in priority):
            raise HifConfigError('HIF基础出牌优先级不是卡牌列表')

        self.profession = profession
        self.unconfigured_card_preference = {}
        preference = (cfg.get("unconfigured_card_preference_profiles") or {}).get(profession, {})
        if isinstance(preference, dict) and (
            preference.get("mode", "none") in ("none", "mental", "active", "remaining_turns")
            and preference.get("early", "mental") in ("none", "mental", "active")
            and preference.get("late", "active") in ("none", "mental", "active")
            and type(preference.get("remaining_turns_lte", 3)) is int
            and 1 <= preference.get("remaining_turns_lte", 3) <= 99
            and type(preference.get("prefer_upgraded", False)) is bool
            and not set(preference) - {"mode", "early", "late", "remaining_turns_lte", "prefer_upgraded"}
        ):
            self.unconfigured_card_preference = preference
        else:
            logger.warning(f"HIF未配置卡偏好无效: {preference!r}，沿用无偏好")
        self._hif_card_types = {}
        try:
            with open(os.path.join(common.EXT_DIR, "catalog", "hif_target_card_catalog.json"), encoding="utf-8") as f:
                self._hif_card_types = {self._hif_card_name(card["name"]): card["type"] for card in json.load(f)}
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"HIF卡牌类型目录读取失败: {e}，本次不应用类型偏好")
        default_unknown = 8 if profession == "全力" else 11
        unknown = (cfg.get("unknown_priority_profiles") or {}).get(profession, default_unknown)
        if isinstance(unknown, (int, float)) and not isinstance(unknown, bool) and unknown >= 0:
            self.unknown_priority = unknown
        else:
            logger.warning(f"HIF兜底优先级无效: {unknown!r}，沿用{default_unknown}")
            self.unknown_priority = default_unknown
        rules = (cfg.get("conditional_priority_profiles") or {}).get(profession, {})
        self.conditional_priority_rules = rules if isinstance(rules, dict) else {}
        excluded = (cfg.get('no_extra_turn_profiles') or {}).get(profession, [])
        self.no_extra_turn_keys = {key for key in excluded if isinstance(key, str) and key} if isinstance(excluded, list) else set()
        conditions = {}
        condition_profiles = cfg.get("use_condition_profiles") or {}
        if not isinstance(condition_profiles, dict):
            raise HifConfigError('HIF职业使用限制不是对象')
        configured = condition_profiles.get(profession, {})
        if not isinstance(configured, dict):
            raise HifConfigError('HIF卡牌使用限制结构无效，不能忽略限制继续')
        for key, clauses in configured.items():
            if not isinstance(clauses, list) or len(clauses) > 3 or any(
                not isinstance(clause, dict)
                or not clause
                or set(clause) - {"state", "remaining_turns_lte"}
                or ("state" in clause and (
                    profession not in ("全力", "強気")
                    or clause["state"] not in ("全力", "強気", "温存")
                ))
                or ("remaining_turns_lte" in clause and (
                    not isinstance(clause["remaining_turns_lte"], int)
                    or isinstance(clause["remaining_turns_lte"], bool)
                    or not 1 <= clause["remaining_turns_lte"] <= 99
                ))
                for clause in clauses
            ):
                logger.warning(f"HIF卡牌使用限制无效: 职业={profession}, 卡牌={key}, 条件={clauses!r}")
                raise HifConfigError(f'HIF卡牌使用限制无效: {key}')
            conditions[key] = clauses
        self.use_conditions = conditions
        self.priority_index = {}
        for i, entry in enumerate(priority):
            key = entry.get("key")
            if not key:
                continue
            self.priority_index[key] = entry.get("priority", i)
        try:
            with open(common.HIF_PRIORITY_CATALOG_PATH, encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"未找到或解析失败 HIF 卡目录 ({e})，使用当前优先级表识别")
            catalog = {}
        self._hif_name_index = {}
        for key in dict.fromkeys([*(entry.get("key") for entry in priority), *catalog]):
            if key:
                self._hif_name_index.setdefault(self._hif_card_name(key), [key])

    @staticmethod
    def _hif_card_name(text: str) -> str:
        return "".join(unicodedata.normalize("NFKC", text or "").split()).rstrip("+")

    @staticmethod
    def _hif_title_upgraded(text: str, *, complete: bool = False) -> Optional[bool]:
        title = "".join(unicodedata.normalize("NFKC", text or "").split())
        return True if title.endswith("+") else False if title and complete else None

    def _prefer_unconfigured_card(self, best: dict, candidates: list, remaining_turns: Optional[int]) -> dict:
        """保留卡名优先级；未配置卡应用类型偏好，同名已配置卡可优先强化。"""
        rule = self.unconfigured_card_preference
        upgraded = rule.get("prefer_upgraded", False)
        if best["key"] in self.priority_index:
            if not upgraded:
                return best
            return min((card for card in candidates if card["key"] == best["key"]),
                       key=lambda card: (0 if card.get("upgraded") is True else 1, -card["conf"]))
        preferred = rule.get("mode", "none")
        if preferred == "remaining_turns":
            preferred = (rule.get("late", "active") if remaining_turns <= rule.get("remaining_turns_lte", 3)
                         else rule.get("early", "mental")) if remaining_turns is not None else "none"
        if preferred == "none" and not upgraded:
            return best
        rank = self._full_power_priority(best, remaining_turns)[1]
        pool = [card for card in candidates if card["key"] not in self.priority_index
                and self._full_power_priority(card, remaining_turns)[1] == rank]
        def preference(card):
            card_type = self._hif_card_types.get(self._hif_card_name(card["key"])) if card["key"] else None
            return (0 if card["key"] else 1,
                    0 if preferred == "none" or card_type == preferred else 1,
                    0 if upgraded and card.get("upgraded") is True else 1, -card["conf"])
        selected = min(pool, key=preference)
        logger.info(f"HIF未配置卡偏好: 类型={preferred}, 优先强化={upgraded}, 剩余回合={remaining_turns}, "
                    f"选择={self._card_label(selected['key'])}, 强化={selected.get('upgraded')}")
        return selected

    def _hif_name_key(self, text: str, *, partial: bool = False) -> Optional[str]:
        name = self._hif_card_name(text)
        keys = self._hif_name_index.get(name, [])
        # 强化符号在标题末尾；「アドリブ+」不是「アドリブの基本」的截断前缀。
        complete = "".join(unicodedata.normalize("NFKC", text or "").split()).endswith("+")
        if partial and not complete and (keys or len(name) >= 3):
            keys = [key for base, entries in self._hif_name_index.items()
                    if base.startswith(name) for key in entries]
        return keys[0] if len(keys) == 1 else None

    def _card_label(self, key: Optional[str]) -> str:
        return key or "暂未识别"

    def _hif_priority_source(self, key: Optional[str]) -> str:
        if not key:
            return "兜底（暂未识别）"
        return "卡牌配置" if key in self.priority_index else "兜底（未单独配置）"

    def _full_power_priority(self, card: dict, remaining_turns: Optional[int]) -> tuple:
        """返回基础优先级、生效优先级和条件优先级状态。"""
        key = card["key"]
        base = self.priority_index.get(key, self.unknown_priority) if key else self.unknown_priority
        rule = self.conditional_priority_rules.get(key) if key else None
        if not isinstance(rule, dict):
            return base, base, "未设置"
        value = rule.get("priority")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return base, base, "规则无效"
        checks = []
        descriptions = []
        bounds = []
        for field, symbol in (('remaining_turns_gte', '≥'), ('remaining_turns_lte', '≤')):
            if field not in rule:
                continue
            limit = rule[field]
            if type(limit) is not int or limit < 0:
                return base, base, '规则无效'
            bounds.append((symbol, limit))
        if bounds:
            if len(bounds) == 2 and bounds[0][1] > bounds[1][1]:
                return base, base, '规则无效'
            checks.append(remaining_turns is not None and all(
                remaining_turns >= limit if symbol == '≥' else remaining_turns <= limit
                for symbol, limit in bounds))
            descriptions.append('且'.join(f'剩余回合{symbol}{limit}' for symbol, limit in bounds))
        power_limit = rule.get("full_power_lt")
        if self.profession == '全力' and isinstance(power_limit, int) and not isinstance(power_limit, bool) and power_limit >= 0:
            checks.append(self._full_power_value is not None and self._full_power_value < power_limit)
            descriptions.append(f"全力值<{power_limit}")
        if "pointer_states" in rule and self.profession in ("全力", "強気"):
            states = rule["pointer_states"]
            if (not isinstance(states, list) or not states or len(set(states)) != len(states)
                    or any(state not in ("全力", "強気", "温存") for state in states)):
                return base, base, "规则无效"
            checks.append(self._active_pointer_state in states)
            descriptions.append(f"指针={'/'.join(states)}")
        mode = rule.get("mode", "any")
        if not checks or mode not in ("any", "all"):
            return base, base, "规则无效"
        matched = any(checks) if mode == "any" else all(checks)
        reason = (" 或 " if mode == "any" else " 且 ").join(descriptions)
        hits = "、".join(label for label, hit in zip(descriptions, checks) if hit) or "无"
        status = "已触发" if matched else "未触发"
        return base, value if matched else base, f"{status}(规则={reason}; 命中={hits})"

    def _decide_full_power(self, identified: list, hand_keys=None) -> dict:
        """全力职业的卡牌优先级及依赖同手牌、剩余回合的使用条件。"""
        keys_in_hand = hand_keys if hand_keys is not None else {card["key"] for card in identified if card["key"]}
        has_habatake = "羽ばたけ！+" in keys_in_hand
        has_wind = "わたしは、風！+" in keys_in_hand
        has_finisher = has_habatake or has_wind
        remaining_turns = self._last_turn_count

        def allowed(card: dict) -> bool:
            key = card["key"]
            if key == "本領発揮+":
                return not (has_habatake and has_wind)
            if key == "アイドルになります+":
                return has_finisher
            return True

        candidates = [card for card in identified if self._allowed_by_use_conditions(card["key"]) and allowed(card)]
        if not candidates:
            self._hif_skip_reason = "使用条件限制" if identified else "策略等待"
            logger.info("全力有效手牌为空：当前卡牌均未满足使用条件，空过")
            return {"type": "skip"}

        scored = [(card, *self._full_power_priority(card, remaining_turns)) for card in candidates]
        best, base, effective, reason = min(
            scored,
            key=lambda item: (item[2], 0 if item[0]["key"] else 1, -item[0]["conf"]),
        )
        best = self._prefer_unconfigured_card(best, candidates, remaining_turns)
        base, effective, reason = self._full_power_priority(best, remaining_turns)
        _, use_status = self._use_condition_status(best["key"])
        priority_source = f", 优先级来源={self._hif_priority_source(best['key'])}"
        logger.info(
            f"全力候选手牌={[(self._card_label(card['key']), actual) for card, _, actual, _ in scored]} "
            f"| 按优先级拟出牌={self._card_label(best['key'])}"
            f"(生效优先级={effective}, 基础优先级={base}, 条件优先级={reason}, "
            f"使用限制={use_status}{priority_source}, 当前状态={self._active_pointer_state or '未识别'}, "
            f"剩余回合={remaining_turns if remaining_turns is not None else '未读出'}, "
            f"全力值={self._full_power_value if self._full_power_value is not None else '未读出'})"
        )
        return {"type": "play", "box": best["box"], "combo": None, "key": best["key"]}

    def _decide(self, context: Context, identified: list) -> dict:
        decision = self._decide_card(context, identified)
        if self.unconfigured_card_preference.get("prefer_upgraded", False) and decision["type"] == "play":
            matches = [card for card in identified if card["key"] == decision.get("key") and card["key"]]
            if matches:
                best = min(matches, key=lambda card: (0 if card.get("upgraded") is True else 1, -card["conf"]))
                decision = {**decision, "box": best["box"]}
        return decision

    def _decide_card(self, context: Context, identified: list) -> dict:
        self._hif_skip_reason = None
        if self._paired_card is not None:
            key = self._paired_card['key']
            if _hif_recovery.now() >= self._paired_deadline:
                self._recover_paired_drink(context)
                return self._decide_card(context, identified)
            matches = [card for card in identified if card['key'] == key and self._allowed_by_use_conditions(key)]
            if matches:
                return {**self._paired_card, 'box': max(matches, key=lambda c: c['conf'])['box']}
            return {'type': 'retry'}
        if self._followups is not None:
            return self._decide_followup(context, identified)
        return self._decide_legacy(context, identified)

    def _decide_followup(self, context, identified):
        state = self._followups
        candidates = [card for card in identified if self._allowed_by_use_conditions(card['key'])]
        if self.profession == '全力':
            keys = {card['key'] for card in identified}
            candidates = [card for card in candidates if (
                card['key'] != '本領発揮+' or not {'羽ばたけ！+', 'わたしは、風！+'} <= keys
            ) and (card['key'] != 'アイドルになります+' or keys & {'羽ばたけ！+', 'わたしは、風！+'})]
        restricted_hand = bool(identified) and not candidates
        if restricted_hand:
            self._hif_skip_reason = "使用条件限制"
        if state.active:
            rule = state.rules[state.active]
            for key in rule['targets']:
                matches = [card for card in candidates if card['key'] == key]
                if matches:
                    card = max(matches, key=lambda c: c['conf'])
                    logger.info(f'HIF组合: {state.active} → {key}，按候选顺序衔接')
                    return {'type': 'play', 'key': key, 'box': card['box'], 'combo': None, 'followup_role': 'target'}
            if state.full_wait:
                if rule['use_black_vinegar'] and not state.vinegar_attempted:
                    if self._drink_brown_bottle(context):
                        return {'type': 'retry'}
                matches = [card for card in candidates if card['key'] == state.active]
                if matches:
                    card = max(matches, key=lambda c: c['conf'])
                    return {'type': 'play', 'key': state.active, 'box': card['box'], 'combo': None, 'followup_role': 'repeat'}
                if state.skips < rule['wait_turns']:
                    if self._followup_skip_turn is not None or self._last_turn_count is None:
                        if self._followup_skip_deadline is None:
                            self._followup_skip_deadline = _hif_recovery.now() + self.TIME_OUT
                        if _hif_recovery.now() >= self._followup_skip_deadline:
                            self._abort_drink_flow(context, 'HIF组合: 无法确认空过后的回合变化，停止任务')
                        return {'type': 'retry'}
                    return {'type': 'skip'}
            logger.info(f'HIF组合: {state.active} 本次无可用候选，解除衔接')
            state.release()
        candidates = [card for card in candidates if state.ordinary_allowed(card['key'])]
        if not candidates:
            self._hif_skip_reason = "使用条件限制" if restricted_hand else "策略等待"
            return {'type': 'skip'}
        decision = (self._decide_full_power(candidates, {card['key'] for card in identified if card['key']})
                    if self.profession == '全力' else self._decide_legacy(context, candidates))
        if decision['type'] == 'play' and decision.get('key') in state.rules:
            decision['followup_role'] = 'start'
        return decision

    def _decide_legacy(self, context: Context, identified: list) -> dict:
        """
        按职业优先级决定本回合要打哪张牌；组合等待由 _decide_followup 负责。

        identified: [{"box", "key", "score", "conf"}]，仅含 YOLO 判定为可出牌(cards) 的手牌。
                    key 为 None 表示暂未识别。

        Returns:
            决策字典：
                {"type": "play", "box": [...], "combo": None, "key": ...}
                {"type": "fallback"}
                {"type": "skip"}
        """
        if self.profession == "全力":
            return self._decide_full_power(identified)

        restricted = [card for card in identified if not self._allowed_by_use_conditions(card["key"])]
        identified = [card for card in identified if card not in restricted]
        if restricted and not identified:
            self._hif_skip_reason = "使用条件限制"
            logger.info("HIF手牌均未满足使用限制，本回合不使用受限卡")
            return {"type": "skip"}

        # 已配置卡按逐卡优先级，未配置或暂未识别的卡按职业兜底优先级参与排序。
        # 优先级升序；同级优先身份已确认的卡，再按检测置信度排序。
        candidates = identified

        def _pr(c) -> int:
            return self._full_power_priority(c, self._last_turn_count)[1]

        if candidates:
            best = min(candidates, key=lambda c: (_pr(c), 0 if c["key"] else 1, -c["conf"]))
            best = self._prefer_unconfigured_card(best, candidates, self._last_turn_count)
            pr = _pr(best)
            _, use_status = self._use_condition_status(best["key"])
            priority_source = f", 优先级来源={self._hif_priority_source(best['key'])}"
            _, _, reason = self._full_power_priority(best, self._last_turn_count)
            pointer = f", 当前状态={self._active_pointer_state or '未识别'}" if self.profession == "強気" else ""
            logger.info(
                f"候选手牌={[(self._card_label(c['key']), _pr(c)) for c in candidates]} "
                f"| 按优先级拟出牌={self._card_label(best['key'])}"
                f"(优先级={pr}, 条件优先级={reason}, 使用限制={use_status}{priority_source}{pointer})"
            )
            return {"type": "play", "box": best["box"], "combo": None, "key": best["key"]}

        # 无牌可打 → 交给兜底（系统建议 / 最高分）
        logger.info("无可打牌，走兜底")
        return {"type": "fallback"}

    def _use_condition_status(self, key: Optional[str]) -> tuple[bool, str]:
        """返回当前卡是否满足使用限制，以及便于排查的命中组说明。"""
        if key in self.no_extra_turn_keys and (not self._extra_turn_read_confirmed or self._pending_extra_turns != 0):
            status = f'尚有+{self._pending_extra_turns}额外回合' if self._extra_turn_read_confirmed and self._pending_extra_turns is not None else '额外回合标记未确认'
            return False, f'有额外回合时不使用({status})'
        clauses = self.use_conditions.get(key) if key else None
        if not clauses:
            return True, "未设置"
        groups = []
        for index, clause in enumerate(clauses, 1):
            checks = []
            labels = []
            if "state" in clause:
                labels.append(f"状态={clause['state']}")
                checks.append(clause["state"] == self._active_pointer_state)
            if "remaining_turns_lte" in clause:
                labels.append(f"剩余≤{clause['remaining_turns_lte']}回合")
                checks.append(
                    self._last_turn_count is not None
                    and self._last_turn_count <= clause["remaining_turns_lte"]
                )
            description = "且".join(labels)
            groups.append(description)
            if checks and all(checks):
                return True, f"第{index}组已满足({description})"
        return False, f"未满足({' 或 '.join(groups)})"

    def _allowed_by_use_conditions(self, key: Optional[str]) -> bool:
        """组内同时满足、组间满足任一；无法确认状态或回合时不猜测。"""
        allowed, status = self._use_condition_status(key)
        if allowed:
            return True
        logger.info(
            f"HIF卡牌使用限制: 「{key}」{status}，"
            f"当前状态={self._active_pointer_state or '未识别'}, "
            f"剩余回合={self._last_turn_count if self._last_turn_count is not None else '未读出'}，本次不选"
        )
        return False

    @staticmethod
    def _get_card_info(results: list):
        """
        从识别结果中获取卡牌信息

        Args:
            results (list): 识别结果列表

        Returns:
            suggestions (int): 建议牌数量
            useless (int): 无用牌数量
            cards (int): 可用牌数量
            suggestions_box (list): 建议牌区域，格式为[x, y, w, h]（x、y为区域左上角的坐标）
            best_box (list): 可用牌区域，格式为[x, y, w, h]（x、y为区域左上角的坐标）
        """
        label_counts = Counter()
        suggestions_box = [0, 0, 1, 1]
        best_box = [0, 0, 1, 1]
        best_score = 0
        for result in results:
            label_counts[result.label] += 1
            if result.label == "suggestions":
                suggestions_box = result.box
            if result.label == "cards":
                # 选出识别分数最高分的卡牌，避免错误
                if result.score > best_score:
                    best_score = result.score
                    best_box = result.box
        suggestions = label_counts["suggestions"]
        useless = label_counts["useless"]
        cards = label_counts["cards"]
        return suggestions, useless, cards, suggestions_box, best_box
