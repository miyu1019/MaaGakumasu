"""HIF 本战用饮 mixin。"""
import os
import json
import time

# opencv 为可选依赖。未安装时不让 agent 启动整体崩溃；
# 饮料识别降级，授业定制标记未确认时按非定制卡处理。
try:
    import cv2
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    cv2 = None

from typing import Optional

from utils import logger
from maa.context import Context

from extensions.hif.recovery import state as _hif_recovery

from . import common
from .common import HifDrinkFlowError


class BattleDrinkMixin:
    """HIF 本战用饮：面板时机、出牌编排的前置/后置/等待用饮与饮料详情弹窗。"""

    # 开始饮料扫描前等待技能特效/回合切换动画收尾，再截图并点击饮料栏。
    DRINK_ACTION_PRE_DELAY = 3.0
    BATTLE_DRINK_DETAIL_ROI = [20, 560, 680, 430]
    BATTLE_DRINK_NAME_DY = (50, 110)
    BROWN_USE_POS = (502, 1169)              # 弹窗「✓使う」按钮中心(182305实测)
    BROWN_CANCEL_POS = (206, 1188)           # 弹窗「✕キャンセル」按钮中心(182305实测)
    # 指定饮料逐瓶检查时，点击瓶子后不能只依赖固定延时。部分设备上详情弹窗
    # 展开超过 1.2 秒；若尚未展开便点取消，下一次“点下一瓶”会落在第一瓶的详情上。
    # 用详情窗口底部必有的「使う/キャンセル」作为状态门槛。
    DRINK_DETAIL_BUTTON_ROI = [80, 1120, 550, 140]
    DRINK_DETAIL_BUTTON_EXPECTED = "使う|キャンセル|キヤンセル"
    DRINK_DETAIL_OPEN_TIMEOUT = 4.0
    DRINK_DETAIL_CLOSE_TIMEOUT = 3.0
    DRINK_DETAIL_POLL_INTERVAL = 0.2
    # OCR 首次读到按钮时，弹窗仍可能处在动画帧；连续命中后才允许点击。
    DRINK_DETAIL_STABLE_COUNT = 2
    DRINK_DETAIL_SETTLE_DELAY = 0.5
    # 详情有时在可操作页稳定后才开始展开；同一瓶只重试一次，并在整轮没有
    # 成功打开详情时重新识别饮料栏后从第一瓶扫描一次，避免把该瓶直接漏掉。
    DRINK_DETAIL_OPEN_ATTEMPTS = 2
    DRINK_DETAIL_RESCAN_ATTEMPTS = 1
    DRINK_DETAIL_ACTION_ATTEMPTS = 2
    # 普通 buff 点「使う」后，弹窗先关闭、饮料栏随后才播放消失/补位动画。不能把
    # “弹窗已关”误当成“饮料栏已可再次识别”，否则会点击到过渡帧里的残影。
    DRINK_LIST_POST_USE_DELAY = 3.0
    DRINK_LIST_STABLE_TIMEOUT = 4.0
    DRINK_LIST_STABLE_POLL_INTERVAL = 0.2
    DRINK_LIST_STABLE_COUNT = 2
    DRINK_LIST_INITIAL_STABLE_COUNT = 3
    DRINK_LIST_POSITION_QUANTUM = 8
    BROWN_DRINK_THRESHOLD = 0.8
    # 饮料栏固定四槽（识别框左边缘）。瓶子模板实际匹配稀有度圆环，浅色圆环（如ジンジャーエール）
    # 逐帧在 0.76~0.89 间跳动，单帧计数会漏瓶；按物理槽汇总多帧，空杯轮廓模板作为空槽反证。
    DRINK_SLOT_XS = (27, 111, 197, 282)
    DRINK_SLOT_BOX_Y = 1165
    DRINK_SLOT_SIZE = 70
    DRINK_SLOT_TOLERANCE = 8
    DRINK_EMPTY_CUP_DX = 19                 # 空杯模板 drink_empty_slot.png 相对槽框左边缘的偏移
    DRINK_SLOT_WEAK_SCORE = 0.6             # 圆环分低于此值又无命中视为空槽；0.6~0.8 且无空杯为不确定
    DRINK_SLOT_MIN_FRAMES = 3
    DRINK_SLOT_MAX_FRAMES = 10

    def _sequence_play_plan(self, decision: dict) -> Optional[dict]:
        """这次出牌对应的编排用饮：衔接打出的后续卡用该候选位置的饮料，其余用这张卡自己的编排。"""
        key = decision.get('key')
        sequences = getattr(self, '_sequences', None) or {}
        if not key:
            return None
        if decision.get('followup_role') == 'target':
            source = decision.get('followup_source')
            rule = sequences.get(source)
            if rule is None:
                return None
            slot = rule['drinks']['targets'].get(key, {})
            return {'before': slot.get('before', []), 'after': slot.get('after', []),
                    'move_targets': None, 'label': f'HIF出牌编排「{source}」→「{key}」'}
        rule = sequences.get(key)
        if rule is None:
            return None
        return {'before': rule['drinks']['before'], 'after': rule['drinks']['after'],
                'move_targets': rule['targets'] or None, 'label': f'HIF出牌编排「{key}」'}

    def _consume_sequence_drinks(self, context, names: list, label: str, move_targets=None) -> bool:
        """按编排顺序各用一瓶；饮料栏没有、已达上限或该场关闭的饮料直接跳过，不补喝。"""
        used = False
        for name in names:
            if context is not None and context.tasker.stopping:
                break
            # 用饮后出现选卡页时，按这条编排的后续候选选卡。
            self._followup_move_targets = list(move_targets) if move_targets else None
            try:
                if self._consume_battle_drinks(context, [name], f'{label}「{name}」', max_uses=1, remember_absent=True):
                    used = True
            finally:
                self._followup_move_targets = None
        return used

    def _prepare_sequence_before(self, context, decision) -> bool:
        """出牌前用饮；用过饮料就锁定这张卡，重新识别后再打出，同一次出牌不重复用饮。"""
        if self._paired_card is not None:
            return False
        plan = self._sequence_play_plan(decision)
        if not plan or not plan['before'] or decision['key'] in self._sequence_before_failed:
            return False
        if not self._consume_sequence_drinks(context, plan['before'], f"{plan['label']}前置", plan['move_targets']):
            return False
        self._paired_card = dict(decision)
        self._paired_unusable_count = 0
        self._paired_deadline = _hif_recovery.now() + self.TIME_OUT
        logger.info(f"{plan['label']}: 前置饮料已用，锁定「{decision['key']}」，重新定位后出牌")
        return True

    def _schedule_sequence_after(self, decision: dict) -> None:
        """确认打出后登记后置饮料；只在回到同一回合的可操作页时使用。"""
        plan = self._sequence_play_plan(decision)
        if not plan or not plan['after']:
            return
        if self._last_turn_count is None:
            logger.info(f"{plan['label']}: 出牌时未读到剩余回合，无法确认同一回合，跳过后置饮料")
            return
        self._sequence_after = {**plan, 'turn': self._last_turn_count,
                                'deadline': _hif_recovery.now() + self.TIME_OUT}

    def _process_sequence_after(self, context, turn) -> Optional[bool]:
        """回到可操作页时处理后置饮料：仍是出牌那一回合才喝，回合已结束就跳过。

        返回 None 表示还在等剩余回合读出；True 表示用过饮料，需要重新识别手牌。
        """
        pending = self._sequence_after
        if pending is None:
            return False
        if turn is None and _hif_recovery.now() < pending['deadline']:
            return None
        self._sequence_after = None
        if turn != pending['turn'] or self._extra_turn_transition:
            logger.info(f"{pending['label']}: 打出后回合已结束，跳过后置饮料，不补喝")
            return False
        return self._consume_sequence_drinks(context, pending['after'], f"{pending['label']}后置", pending['move_targets'])

    def _sequence_waiting_drinks(self, context) -> bool:
        """组合满等待且后续候选都不在手牌时，每次衔接只用一轮等待用饮。"""
        state = self._followups
        if state is None or not state.active or not state.full_wait or state.waiting_attempted:
            return False
        state.waiting_attempted = True
        rule = (getattr(self, '_sequences', None) or {}).get(state.active)
        names = rule['drinks']['waiting'] if rule else []
        if not names:
            return False
        return self._consume_sequence_drinks(context, names, f'HIF出牌编排「{state.active}」等待', rule['targets'])

    def _recover_paired_drink(self, context) -> None:
        """前置饮料已确认用过；只放弃未完成的这次出牌。"""
        # 结算次へ需跨帧连续命中才判定结束；计数必须在各帧间共用，每帧新建计数永远达不到要求。
        battle_end_count = [0]

        def safe(image):
            if self._is_drink_detail_open(context, image):
                return False
            if self._is_battle_end(context, image, consecutive=battle_end_count):
                return True
            skip = context.run_recognition('ProduceHIF__ProduceRecognitionSkipRound', image)
            return bool(skip and skip.hit and self._read_turn_count(context, image) is not None)
        if context is None or not common._hif_observe_result(context, safe):
            self._abort_drink_flow(context, 'HIF出牌编排: 未确认安全战斗/结算页，保留现场停止')
        key = (self._paired_card or {}).get('key')
        logger.warning(f'HIF安全降级: 前置饮料已用但「{key}」未能打出，解除锁定；本场这张卡不再用前置饮料，不补喝')
        self._paired_card = self._paired_deadline = None
        self._paired_unusable_count = 0
        if key:
            self._sequence_before_failed.add(key)

    def _release_unusable_paired_card(self, results: list) -> None:
        if self._paired_card is None:
            self._paired_unusable_count = 0
            return
        box = self._paired_card['box']
        matches = [result for result in results
                   if result.label in ('cards', 'suggestions', 'useless') and result.score >= 0.5
                   and self._in_range(result.box) and result.box[2] >= 90
                   and 180 <= result.box[3] <= 330 and result.box[1] + result.box[3] <= 1170
                   and self._hif_duplicate_box(result.box, box)]
        unusable = bool(matches) and all(result.label == 'useless' for result in matches)
        self._paired_unusable_count = self._paired_unusable_count + 1 if unusable else 0
        if self._paired_unusable_count >= 2:
            logger.info(f"HIF出牌编排: 连续两帧确认{self._paired_card['key']}游戏内不可用，解除锁定并重新决策")
            self._paired_card = None
            self._paired_deadline = None
            self._paired_unusable_count = 0

    @staticmethod
    def _describe_drink_timing(mode: str, turn=None) -> str:
        """日志用的饮料时机文字。"""
        return {
            "first_turn": "开场第1回合", "never": "不使用", "combo_only": "只在出牌编排中使用",
        }.get(mode, f"剩余{turn}回合" if mode == "remaining_turn" else str(mode))

    @staticmethod
    def _valid_battle_drink_timing(
        value, allow_never: bool = False, allow_combo_only: bool = False,
    ) -> bool:
        if not isinstance(value, dict):
            return False
        mode = value.get("mode")
        if allow_never and mode == "never":
            return set(value) == {"mode"}
        if allow_combo_only and mode == "combo_only":
            return set(value) == {"mode"}
        if mode == "first_turn":
            return set(value) == {"mode"}
        turn = value.get("turn")
        return (
            mode == "remaining_turn" and set(value) == {"mode", "turn"}
            and isinstance(turn, int) and not isinstance(turn, bool) and 1 <= turn <= 99
        )

    @staticmethod
    def _valid_drink_use_limit(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and -1 <= value <= 4

    def _battle_drink_limit_reached(self, name: str) -> bool:
        # 场次无法确认时保留一战限制；只有确认二战才放开。
        if self.hif_battle_number == 2:
            return name in self._drink_second_battle_disabled
        policy = self._drink_first_battle_policy
        if not self._drink_first_battle_enabled.get(name, policy["enabled"]):
            return True
        total = policy["max_uses"]
        limit = self._drink_first_battle_limits.get(name, policy["unconfigured_max_uses"])
        return (total >= 0 and sum(self._drink_used_counts.values()) >= total
                or limit >= 0 and self._drink_used_counts.get(name, 0) >= limit)

    def _load_hif_battle_drink_policy(self) -> None:
        """加载当前职业的本战用饮规则；配置损坏时停止自动喝饮料。"""
        self._drink_catalog_by_key = {}
        self._drink_imported_names = []
        self._drink_specific_timings = {}
        self._drink_disabled_names = set()
        self._drink_default_timing = {"mode": "never"}
        self._drink_first_battle_policy = {"enabled": True, "max_uses": -1, "unconfigured_max_uses": -1}
        self._drink_first_battle_limits = {}
        self._drink_first_battle_enabled = {}
        self._drink_second_battle_disabled = set()
        self._drinks_unavailable = False
        try:
            with open(common.HIF_DRINK_CATALOG_PATH, encoding="utf-8") as file:
                catalog = json.load(file)["drinks"]
            config = common._hif_load_personal_config(common.HIF_DRINK_PROFILES_PATH)
            by_id = {item["id"]: item["name"] for item in catalog}
            self._drink_catalog_by_key = {
                common._hif_drink_name_key(name): name for name in by_id.values()
            }
            profiles = config.get("profiles", {})
            defaults = config.get("default_use_timing_profiles", {})
            if not isinstance(profiles, dict) or not isinstance(defaults, dict):
                raise ValueError("饮料职业配置结构无效")
            entries = profiles.get(self.profession, [])
            if not isinstance(entries, list):
                raise ValueError(f"{self.profession}饮料名单不是数组")
            fallback = defaults.get(self.profession, {"mode": "remaining_turn", "turn": 4})
            if not self._valid_battle_drink_timing(fallback, allow_never=True):
                raise ValueError(f"{self.profession}未指定饮料使用时机无效")
            self._drink_default_timing = fallback
            policies = config.get("first_battle_policy_profiles", {})
            if not isinstance(policies, dict):
                raise ValueError("一战饮料配置结构无效")
            policy = policies.get(self.profession, {})
            if (not isinstance(policy, dict) or not isinstance(policy.get("enabled", True), bool)
                    or any(not self._valid_drink_use_limit(policy.get(key, -1))
                           for key in ("max_uses", "unconfigured_max_uses"))):
                raise ValueError(f"{self.profession}一战饮料配置无效")
            self._drink_first_battle_policy.update(policy)
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") not in by_id:
                    continue
                name = by_id[entry["id"]]
                if name in self._drink_imported_names:
                    continue
                self._drink_imported_names.append(name)
                limit = entry.get("first_battle_max_uses", -1)
                if not self._valid_drink_use_limit(limit):
                    raise ValueError(f"{name}一战使用上限无效")
                self._drink_first_battle_limits[name] = limit
                if "disabled" in entry and ("first_battle_enabled" not in entry or "second_battle_enabled" not in entry):
                    common._hif_note_incompatible("饮料")
                first = entry.get("first_battle_enabled", True)
                second = entry.get("second_battle_enabled", True)
                if not isinstance(first, bool) or not isinstance(second, bool):
                    raise ValueError(f"{name}一二战使用开关无效")
                self._drink_first_battle_enabled[name] = first
                if not second:
                    self._drink_second_battle_disabled.add(name)
                if not first and not second:
                    self._drink_disabled_names.add(name)
                    continue
                timing = entry.get("use_timing")
                if 'before_card_targets' in entry:
                    # 旧版关联卡牌不迁移；出牌前用饮改在出牌编排中设置。
                    common._hif_note_incompatible("饮料")
                if timing is not None:
                    # 「只在出牌编排中使用」(combo_only) 对所有饮料开放。
                    if self._valid_battle_drink_timing(timing, allow_combo_only=True):
                        self._drink_specific_timings[name] = timing
                    else:
                        self._drink_disabled_names.add(name)
                        logger.warning(f"HIF本战饮料: 「{name}」使用时机无效，停止自动使用该饮料")
            logger.info(
                f"HIF本战饮料: 职业={self.profession}，已导入={len(self._drink_imported_names)}，"
                f"不使用={len(self._drink_disabled_names)}，未指定饮料时机="
                f"{self._describe_drink_timing(self._drink_default_timing['mode'], self._drink_default_timing.get('turn'))}"
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._drink_catalog_by_key = {}
            self._drink_imported_names = []
            self._drink_specific_timings = {}
            self._drink_disabled_names = set()
            self._drink_default_timing = {"mode": "never"}
            self._drinks_unavailable = True
            self._drink_first_battle_policy = {"enabled": False, "max_uses": 0, "unconfigured_max_uses": 0}
            self._drink_first_battle_limits = {}
            self._drink_first_battle_enabled = {}
            self._drink_second_battle_disabled = set()
            logger.warning(f"HIF本战饮料: 配置读取失败，停止自动使用饮料: {exc!r}")

    @classmethod
    def _battle_drink_identity_from_results(cls, results, catalog_by_key: dict) -> tuple:
        """详情已由按钮确认打开；以 Pドリンク 定位标题下方的名称行。"""
        titles = [
            result for result in results
            if result.box[0] < 130
            and "Pドリンク" in common._hif_drink_name_key(result.text)
        ]
        if not titles:
            return None, ""
        title_y = min(titles, key=lambda result: result.box[1]).box[1]
        names = [
            result for result in results
            if 170 <= result.box[0] <= 540
            and cls.BATTLE_DRINK_NAME_DY[0] <= result.box[1] - title_y <= cls.BATTLE_DRINK_NAME_DY[1]
            and result.box[3] >= 20
        ]
        if not names:
            return None, ""
        raw = min(names, key=lambda result: result.box[1]).text
        return catalog_by_key.get(common._hif_drink_name_key(raw)), raw

    def _read_battle_drink_identity(self, context: Context) -> tuple:
        """对稳定展开的本战详情做有限次读名；未知身份保持未知。"""
        raw = ""
        for attempt in range(2):
            try:
                image = context.tasker.controller.post_screencap().wait().get()
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore", image,
                    pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.BATTLE_DRINK_DETAIL_ROI,
                        "expected": r"[^\n]+",
                    }},
                )
                results = reco.filtered_results if reco and reco.hit else []
                name, raw = self._battle_drink_identity_from_results(results, self._drink_catalog_by_key)
                if name:
                    return name, raw
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(f"HIF本战饮料: 详情读名异常 {exc!r}")
            if attempt == 0:
                time.sleep(0.2)
        return None, raw

    def _drink_slot_states(self, context: Context, image) -> list[str]:
        """单帧四槽证据：drink 圆环命中；cup 空杯命中；low 圆环分很低；weak 介于两者且无空杯。"""
        rings = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
        cups = context.run_recognition("ProduceHIF__ProduceDrinkEmptySlot", image)
        rings = list(getattr(rings, 'all_results', None) or [])
        cups = list(cups.filtered_results or []) if cups and cups.hit else []
        states = []
        for x in self.DRINK_SLOT_XS:
            score = max((r.score for r in rings if abs(r.box[0] - x) <= self.DRINK_SLOT_TOLERANCE), default=0)
            if score >= self.BROWN_DRINK_THRESHOLD:
                states.append('drink')
            elif any(abs(r.box[0] - self.DRINK_EMPTY_CUP_DX - x) <= self.DRINK_SLOT_TOLERANCE for r in cups):
                states.append('cup')
            elif score < self.DRINK_SLOT_WEAK_SCORE:
                states.append('low')
            else:
                states.append('weak')
        return states

    @staticmethod
    def _merge_slot_evidence(seen: set) -> str:
        """任一帧命中且从未见空杯即有瓶；见空杯且从未命中即空槽；只见低分亦为空槽；其余不确定。"""
        if 'drink' in seen:
            return 'unknown' if 'cup' in seen else 'drink'
        if 'cup' in seen or seen == {'low'}:
            return 'empty'
        return 'unknown'

    def _drink_slot_boxes(self, states) -> Optional[list]:
        """四槽须为「有瓶…空槽…」从左连续；否则返回 None 表示证据不足。"""
        count = states.count('drink')
        if states != ['drink'] * count + ['empty'] * (len(states) - count):
            return None
        return [[x, self.DRINK_SLOT_BOX_Y, self.DRINK_SLOT_SIZE, self.DRINK_SLOT_SIZE] for x in self.DRINK_SLOT_XS[:count]]

    def _battle_drink_boxes(self, context: Context, image=None) -> Optional[list]:
        """指定 image 时只读单帧；否则按物理槽汇总多帧。证据不足返回 None，调用方不得据此计数或点击。"""
        try:
            if image is not None:
                return self._drink_slot_boxes([self._merge_slot_evidence({s}) for s in self._drink_slot_states(context, image)])
            seen = [set() for _ in self.DRINK_SLOT_XS]
            for frame in range(self.DRINK_SLOT_MAX_FRAMES):
                if context.tasker.stopping:
                    return None
                image = context.tasker.controller.post_screencap().wait().get()
                for slot, state in zip(seen, self._drink_slot_states(context, image)):
                    slot.add(state)
                states = [self._merge_slot_evidence(s) for s in seen]
                if frame + 1 >= self.DRINK_SLOT_MIN_FRAMES and 'unknown' not in states:
                    boxes = self._drink_slot_boxes(states)
                    if boxes is not None:
                        return boxes
                time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)
            logger.warning(f"HIF本战饮料: 饮料栏四槽证据不足 {[sorted(s) for s in seen]}，不据此计数")
            return None
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"HIF本战饮料: 饮料栏定位异常 {exc!r}")
            return None

    def _consume_battle_drinks(
        self, context: Context, wanted_names: list[str], label: str,
        max_uses: int = 4, verify_post_use: bool = True, remember_absent: bool = False,
    ) -> bool:
        """从左到右直接用饮；同批次补位后跳过已确认不使用的前缀。

        remember_absent（出牌编排用饮）：整栏都读出名称仍没有某种目标饮料时，记入本场缺少名单，
        之后编排不再为它扫描饮料栏。普通时机每批次重新确认，不使用这份名单。
        """
        absent = self._drink_absent_names if remember_absent else set()
        wanted = {name for name in wanted_names
                  if name not in self._drink_disabled_names and name not in absent
                  and not self._battle_drink_limit_reached(name)}
        if self._drinks_unavailable or not wanted:
            return False
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        if not self._wait_for_drink_list_stable(context, after_use=False):
            return False
        used = False
        used_count = 0
        rescans = 0
        skipped_boxes = []
        for _ in range(max_uses + self.DRINK_DETAIL_RESCAN_ATTEMPTS):
            if used_count >= max_uses:
                return used
            if context.tasker.stopping:
                return used
            boxes = self._battle_drink_boxes(context)
            if boxes is None:
                boxes = self._battle_drink_boxes(context)
            if boxes is None:
                # 瓶数不明时既不能选瓶也无法核对用后少一瓶，本轮不用饮料。
                logger.warning(f"{label}: 饮料栏复读后仍有不确定槽位，本轮不用饮料")
                return used
            before_count = len(boxes)
            # 只复用本批次已读名并关闭详情的连续前缀；补位后仍核对其位置。
            if self._drink_list_signature(boxes[:len(skipped_boxes)]) != self._drink_list_signature(skipped_boxes):
                skipped_boxes = []
            if skipped_boxes:
                logger.info(f"{label}: 前{len(skipped_boxes)}瓶已确认本批次不使用且位置未变，跳过重复读名")
            open_failed = False
            uncertain = False
            for index in range(len(skipped_boxes), len(boxes)):
                box = boxes[index]
                if context.tasker.stopping:
                    return used
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                if not self._open_drink_detail(context, pos, f"{label}第{index + 1}瓶"):
                    skipped_boxes = []
                    open_failed = True
                    logger.warning(f"{label}: 第{index + 1}瓶详情未打开")
                    image = context.tasker.controller.post_screencap().wait().get()
                    if self._is_drink_detail_open(context, image) and not self._close_drink_detail(context):
                        return used
                    continue
                name, raw = self._read_battle_drink_identity(context)
                uncertain = uncertain or not name
                category = (
                    "不使用" if name in self._drink_disabled_names else
                    "已导入" if name in self._drink_imported_names else
                    "未导入" if name else "无法识别"
                )
                skip = name not in wanted or self._battle_drink_limit_reached(name)
                verdict = (
                    "直接使用" if not skip else
                    "已达到一战单种或总使用上限，保留" if name in wanted else "本次不使用"
                )
                logger.info(f"{label}: 第{index + 1}瓶 名称=[{name or raw or '未读到'}]，分类={category} → {verdict}")
                if context.tasker.stopping:
                    return used
                if skip:
                    if not self._close_drink_detail(context):
                        self._abort_drink_flow(context, f"{label}: 详情未能关闭，停止当前HIF任务")
                    if name and index == len(skipped_boxes):
                        skipped_boxes.append(list(box))
                    elif not name:
                        skipped_boxes = []
                    continue
                _hif_recovery.pending = f'用饮:{name}'
                self._move_drink_source = name  # 用饮后的选卡页按饮料来源处理，处理时清除
                if not self._use_drink(context):
                    self._abort_drink_flow(context, f"{label}: 「{name}」使用后未恢复，停止当前HIF任务")
                # 已执行使う并关闭详情；即使后续校验失败，主循环也必须重新截图。
                used = True
                if not verify_post_use:
                    self._drink_used_counts[name] = self._drink_used_counts.get(name, 0) + 1
                    _hif_recovery.pending = None
                    logger.info(f"{label}: 已使用「{name}」")
                    return True
                returned = self._wait_for_battle_drink_return(context)
                if returned:
                    self._move_drink_source = None
                    # 只用于等补位动画收尾；结果以四槽汇总为准，单帧漏检不改判。
                    self._wait_for_drink_list_stable(context, after_use=False, expected_count=before_count - 1)
                # 用前已读名，用后只核对少一瓶且剩余从左连续；同名多瓶同样适用。
                after = self._battle_drink_boxes(context) if returned else None
                after_count = len(after) if after is not None else None
                if after_count != before_count - 1:
                    def used_safely(image):
                        battle = context.run_recognition('ProduceHIF__ProduceRecognitionSkipRound', image)
                        if not (battle and battle.hit and not self._is_drink_detail_open(context, image)):
                            return False
                        boxes = self._battle_drink_boxes(context, image)
                        return boxes is not None and len(boxes) == before_count - 1
                    if not common._hif_observe_result(context, used_safely):
                        self._abort_drink_flow(context, f"{label}: 「{name}」用饮后页面或少一瓶结果未确认，不重复使用")
                    after_count = before_count - 1
                _hif_recovery.pending = None
                logger.info(f"{label}: 已使用「{name}」，饮料栏{before_count}→{after_count}瓶")
                self._drink_used_counts[name] = self._drink_used_counts.get(name, 0) + 1
                used_count += 1
                break  # 重新定位补位后的目标，保留前面已确认不使用的瓶子。
            else:
                if (open_failed and rescans < self.DRINK_DETAIL_RESCAN_ATTEMPTS
                        and self._wait_for_drink_list_stable(context, after_use=False)):
                    rescans += 1
                    skipped_boxes = []
                    continue
                missing = {name for name in wanted if not self._battle_drink_limit_reached(name)}
                if remember_absent and missing and not open_failed and not uncertain:
                    self._drink_absent_names |= missing
                    logger.info(f"{label}: 饮料栏已确认没有「{'、'.join(sorted(missing))}」，本场不再为它扫描")
                return used
        return used

    def _wait_for_battle_drink_return(self, context: Context) -> bool:
        """保留当前用饮批次，处理选卡后确认战斗页恢复，再允许校验饮料栏。"""
        deadline = time.time() + self.DRINK_LIST_STABLE_TIMEOUT
        stable = 0
        handled_move = False
        while time.time() < deadline:
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            move = context.run_recognition("ProduceHIF__ProduceRecognitionChooseMoveCards", image)
            if move and move.hit:
                if handled_move:
                    stable = 0
                else:
                    logger.info("HIF本战饮料: 进入选卡页，暂停用饮批次并处理选卡")
                    if not self._handle_move_cards(context, image):
                        return False
                    handled_move = True
                    stable = 0
                    deadline = time.time() + self.DRINK_LIST_STABLE_TIMEOUT
                    continue
            else:
                playable = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                stable = stable + 1 if playable and playable.hit and not self._is_drink_detail_open(context, image) else 0
                if stable >= self.DRINK_DETAIL_STABLE_COUNT:
                    if handled_move:
                        logger.info("HIF本战饮料: 已返回战斗页，继续当前用饮批次")
                    return True
            time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)
        return False

    def _due_battle_drink_names(self, turn: Optional[int], first_turn: bool = False) -> list[str]:
        if turn is None:
            return []
        ordered = self._drink_imported_names + [
            name for name in self._drink_catalog_by_key.values()
            if name not in self._drink_imported_names
        ]
        due = []
        for name in ordered:
            if name in self._drink_disabled_names:
                continue
            # 出牌编排用到的饮料照常按时机使用；只有「只在出牌编排中使用」(combo_only) 不在这里喝。
            timing = self._drink_specific_timings.get(name, self._drink_default_timing)
            matched = (
                timing["mode"] == "first_turn" if first_turn else
                timing["mode"] == "remaining_turn" and timing["turn"] == turn
            )
            if matched:
                due.append(name)
        return due

    def _process_battle_drink_timing(
        self, context: Context, turn: Optional[int], first_turn: bool = False,
    ) -> bool:
        trigger = ("first_turn" if first_turn else "remaining_turn", turn)
        if turn is None or trigger in self._drink_attempted_triggers:
            return False
        due = self._due_battle_drink_names(turn, first_turn)
        if not due:
            return False
        self._drink_attempted_triggers.add(trigger)
        logger.info(f"HIF本战饮料: 时机={self._describe_drink_timing(*trigger)}，准备按配置扫描{len(due)}种饮料")
        return self._consume_battle_drinks(context, due, "HIF本战饮料")

    def _find_drink_detail_actions(self, context: Context, image) -> dict:
        """返回当前详情窗中可点击的操作按钮文本框。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.DRINK_DETAIL_BUTTON_ROI,
                        "expected": self.DRINK_DETAIL_BUTTON_EXPECTED,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF本战饮料: 详情按钮识别异常 {e!r}")
            return {}
        if not (reco and reco.hit and reco.filtered_results):
            return {}
        actions = {}
        for result in reco.filtered_results:
            text = (result.text or "").replace(" ", "").replace("　", "")
            if "使う" in text:
                actions["use"] = list(result.box)
            elif "キャンセル" in text or "キヤンセル" in text:
                actions["cancel"] = list(result.box)
        return actions

    def _is_drink_detail_open(self, context: Context, image) -> bool:
        """详情弹窗是否已完整展开（以底部操作按钮为准）。"""
        actions = self._find_drink_detail_actions(context, image)
        return "cancel" in actions and "use" in actions

    def _wait_for_drink_detail(self, context: Context, should_be_open: bool, timeout: float, confirmation_count=None) -> bool:
        """等待详情打开或关闭连续稳定，避免在动画帧操作按钮。"""
        clock = _hif_recovery.now if confirmation_count else time.time
        deadline = clock() + timeout
        required = confirmation_count or self.DRINK_DETAIL_STABLE_COUNT
        stable = 0
        while clock() < deadline:
            if context.tasker.stopping:
                return False
            image = common._hif_nonbattle_screencap(context) if confirmation_count else context.tasker.controller.post_screencap().wait().get()
            actions = self._find_drink_detail_actions(context, image)
            is_open = "cancel" in actions and "use" in actions
            returned = False
            if not should_be_open and not actions:
                battle = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                move = context.run_recognition("ProduceHIF__ProduceRecognitionChooseMoveCards", image)
                returned = bool(battle and battle.hit or move and move.hit)
            matches = is_open if should_be_open else not actions and returned
            if matches:
                stable += 1
                if stable >= required:
                    if should_be_open:
                        time.sleep(self.DRINK_DETAIL_SETTLE_DELAY)
                    return True
            else:
                stable = 0
            time.sleep(max(.25, self.DRINK_DETAIL_POLL_INTERVAL) if confirmation_count else self.DRINK_DETAIL_POLL_INTERVAL)
        return False

    def _drink_list_signature(self, boxes) -> tuple:
        """把饮料栏识别结果归一化为稳定性签名，仅比较数量和从左到右的横向位置。"""
        quantum = self.DRINK_LIST_POSITION_QUANTUM
        return tuple(
            round((box[0] + box[2] / 2) / quantum)
            for box in sorted(boxes, key=lambda box: box[0])
        )

    def _wait_for_drink_list_stable(self, context: Context, after_use: bool = True, expected_count: Optional[int] = None) -> bool:
        """等待饮料栏连续稳定；首次扫描比使用后补位动画多要求一帧稳定。"""
        if after_use:
            logger.info(
                f"喝buff: 使用后等待 {self.DRINK_LIST_POST_USE_DELAY:.1f} 秒，"
                "确保饮料消失和补位动画结束"
            )
            time.sleep(self.DRINK_LIST_POST_USE_DELAY)
        elif expected_count is not None:
            logger.info(f"喝饮料: 等待数量降至{expected_count}瓶且饮料栏连续稳定")
        else:
            logger.info("喝饮料: 等待回合切换后的饮料栏连续稳定，再从第一瓶扫描")

        deadline = time.time() + self.DRINK_LIST_STABLE_TIMEOUT
        stable_need = (
            self.DRINK_LIST_STABLE_COUNT if after_use
            else self.DRINK_LIST_INITIAL_STABLE_COUNT
        )
        previous = None
        stable = 0
        while time.time() < deadline:
            if context.tasker.stopping:
                return False
            try:
                image = context.tasker.controller.post_screencap().wait().get()
                reco = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
                boxes = [
                    result.box
                    for result in (reco.filtered_results if reco and reco.hit else [])
                ]
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF本战饮料: 饮料栏识别异常 {e!r}")
                stable = 0
                previous = None
                time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)
                continue

            if expected_count is not None and len(boxes) != expected_count:
                previous = None
                stable = 0
                time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)
                continue
            signature = self._drink_list_signature(boxes)
            if signature == previous:
                stable += 1
                if stable >= stable_need:
                    if expected_count is None:
                        logger.info(f"HIF本战饮料: 饮料栏已稳定，共{len(boxes)}瓶，从第一瓶开始检查")
                    return True
            else:
                previous = signature
                stable = 1
            time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)

        waited = f"降至{expected_count}瓶并" if expected_count is not None else ""
        logger.warning(
            f"HIF本战饮料: 饮料栏未在{self.DRINK_LIST_STABLE_TIMEOUT:.1f}秒内{waited}稳定，"
            "结束本轮饮料扫描以避免点击残影"
        )
        return False

    def _open_drink_detail(self, context: Context, pos, label: str) -> bool:
        """点同一饮料并等详情稳定；失败时有限重试，调用方再决定是否重扫。"""
        for attempt in range(1, self.DRINK_DETAIL_OPEN_ATTEMPTS + 1):
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            if self._wait_for_drink_detail(
                context, should_be_open=True, timeout=self.DRINK_DETAIL_OPEN_TIMEOUT
            ):
                if attempt > 1:
                    logger.info(f"{label}: 第{attempt}/{self.DRINK_DETAIL_OPEN_ATTEMPTS}次重试后详情已稳定打开")
                return True
            logger.warning(
                f"{label}: 第{attempt}/{self.DRINK_DETAIL_OPEN_ATTEMPTS}次详情未在 "
                f"{self.DRINK_DETAIL_OPEN_TIMEOUT:.1f} 秒内稳定打开"
            )
        return False

    def _click_drink_detail_action(self, context: Context, action: str, *, compatibility: bool = False) -> bool:
        """重新确认按钮仍存在；首次用动态位置，第二次可指定原兼容坐标。"""
        image = context.tasker.controller.post_screencap().wait().get()
        box = self._find_drink_detail_actions(context, image).get(action)
        if not box:
            logger.warning("HIF本战饮料: 详情操作按钮在点击前已消失，重新确认页面，不继续点击")
            return False
        if not compatibility:
            # 首次按识别位置点击不单独记录；未生效时由调用方的告警带上坐标。
            x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
        else:
            x, y = self.BROWN_USE_POS if action == "use" else self.BROWN_CANCEL_POS
            logger.warning(
                f"HIF本战饮料: 第二次点击{'使う' if action == 'use' else 'キャンセル'}，"
                f"使用已有兼容坐标 @ ({x}, {y})"
            )
        self._drink_detail_last_click = (x, y)
        context.tasker.controller.post_click(x, y).wait()
        return True

    def _drink_detail_click_note(self) -> str:
        point = self.__dict__.pop("_drink_detail_last_click", None)
        return f" @ {point}" if point else ""

    def _close_drink_detail(self, context: Context) -> bool:
        """两次取消未生效后观察十秒，再复核同一详情并补点一次。"""
        identity = None
        for attempt in range(1, self.DRINK_DETAIL_ACTION_ATTEMPTS + 2):
            if getattr(context.tasker, 'stopping', False):
                return False
            if attempt > self.DRINK_DETAIL_ACTION_ATTEMPTS:
                logger.warning('HIF本战饮料: 两次点キャンセル后详情仍未关闭，等待10秒后复核并最后重试')
                if self._wait_for_drink_detail(context, should_be_open=False, timeout=10.0):
                    return True
                if getattr(context.tasker, 'stopping', False):
                    return False
            image = context.tasker.controller.post_screencap().wait().get()
            if "cancel" not in self._find_drink_detail_actions(context, image):
                return self._wait_for_drink_detail(context, should_be_open=False, timeout=self.DRINK_DETAIL_CLOSE_TIMEOUT)
            current = self._read_battle_drink_identity(context)
            current = current[0] or current[1].strip()
            if attempt > 1 and (not identity or current != identity):
                self._abort_drink_flow(context, "取消重试前无法确认仍是同一饮料详情，停止当前HIF任务")
            identity = current
            self._click_drink_detail_action(context, "cancel", compatibility=attempt > 1)
            if self._wait_for_drink_detail(
                context, should_be_open=False, timeout=self.DRINK_DETAIL_CLOSE_TIMEOUT
            ):
                return True
            logger.warning(
                f"HIF本战饮料: 第{attempt}/{self.DRINK_DETAIL_ACTION_ATTEMPTS + 1}次"
                f"点キャンセル{self._drink_detail_click_note()}后未确认详情关闭"
            )
        if getattr(context.tasker, 'stopping', False):
            return False
        self._abort_drink_flow(context, "饮料详情等待10秒并最后重试后仍未关闭，停止当前HIF任务")

    def _use_drink(self, context: Context) -> bool:
        """使用当前详情饮料；首次点击未生效时只重试同一详情一次。"""
        identity = None
        for attempt in range(1, self.DRINK_DETAIL_ACTION_ATTEMPTS + 1):
            image = common._hif_nonbattle_screencap(context)
            if "use" not in self._find_drink_detail_actions(context, image):
                if attempt == 1:
                    return self._wait_for_drink_detail(context, should_be_open=False, timeout=self.DRINK_DETAIL_CLOSE_TIMEOUT)
                # 上次点击已生效但饮料演出未结束（实测ジンジャーエール约 6 秒），交给下方较长复核，不补点。
                break
            current = self._read_battle_drink_identity(context)
            current = current[0] or current[1].strip()
            if attempt > 1 and (not identity or current != identity):
                self._abort_drink_flow(context, "使用重试前无法确认仍是同一饮料详情，停止当前HIF任务")
            identity = current
            self._click_drink_detail_action(context, "use", compatibility=attempt > 1)
            if self._wait_for_drink_detail(
                context, should_be_open=False, timeout=self.DRINK_DETAIL_CLOSE_TIMEOUT
            ):
                return True
            logger.warning(
                f"HIF本战饮料: 第{attempt}/{self.DRINK_DETAIL_ACTION_ATTEMPTS}次"
                f"点使う{self._drink_detail_click_note()}后未确认详情关闭"
            )
        if self._wait_for_drink_detail(context, should_be_open=False, timeout=10.0, confirmation_count=3):
            logger.info('HIF本战饮料: 详情延迟关闭，确认已使用，不再补点使う')
            return True
        self._abort_drink_flow(context, "饮料连续两次点击使う后未恢复，停止当前HIF任务")

    def _abort_drink_flow(self, context: Context, message: str) -> None:
        if context is not None and getattr(context.tasker, 'stopping', False):
            raise HifDrinkFlowError('任务已停止，取消后续饮料操作')
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            folder = os.path.join(common.BASE_DIR, "debug", "custom", "hif_drink_failures")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{time.time_ns()}.png")
            if cv2 is not None and image is not None and cv2.imwrite(path, image):
                logger.error(f"HIF饮料异常截图: {path}")
        except Exception as exc:
            logger.warning(f"HIF饮料异常截图保存失败: {exc!r}")
        raise HifDrinkFlowError(message)
