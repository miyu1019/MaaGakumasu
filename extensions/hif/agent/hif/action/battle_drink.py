"""HIF 本战用饮 mixin。"""
import os
import json
import time
from difflib import SequenceMatcher

import numpy as np

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
    """HIF 本战用饮：面板时机、出牌前配对、组合检索黒酢与饮料详情弹窗。"""

    # 旧普通饮料回合入口暂留作回归对照；运行时改读 HIF 饮料面板。
    DRINK_TURN = 4
    DRINK_TURN_NODE = "ProduceHIF__ProduceHIFRemainingDrinkTurn"
    MAX_DRINK_TURN = 12
    # 开始饮料扫描前等待技能特效/回合切换动画收尾，再截图并点击饮料栏。
    DRINK_ACTION_PRE_DELAY = 3.0
    # 棕色第3瓶饮料（组合技道具）：打出国民(脚光收尾)后检测饮料栏这瓶,有则点它使用,用后进移动技能卡界面找脚光
    # 组合技饮料「初星黒酢」: 打断国民后依次点开饮料栏每瓶, OCR弹窗名字找「初星黒酢」, 找到才点使う,
    # 不是则点キャンセル返回继续下一瓶。饮料瓶位置不固定, 用 ProduceHIF__ProduceCheckDrinkButton 识别瓶子box依次点。
    BROWN_NAME_ROI = [150, 788, 410, 67]    # Pドリンク詳細弹窗饮料名区(182257/182305实测, 名字高度可能不同,取较宽)
    BROWN_TARGET_NAME = "初星黒酢"           # 游戏内目标饮料名
    BATTLE_DRINK_DETAIL_ROI = [20, 560, 680, 430]
    BATTLE_DRINK_NAME_DY = (50, 110)
    # 目标名的常见OCR形式(参考技能卡定制 expected 用|连接). 容忍繁体/部分字/带后缀
    BROWN_TARGET_CARDS = ["初星黒酢", "初星黑醋", "初星黒酔", "初星黑酔", "初星黒", "初星酢", "黒酢"]
    BROWN_TARGET_RATIO = 0.5                # 相似度≥0.5视为目标(容忍OCR误差)
    # 全力职业在两场本战第一回合寻找的P饮料。详情标题OCR优先，茶杯模板作为日文OCR
    # 失误时的兜底；只用目标饮料，不会把其它饮料当作普通buff提前消耗。
    FULL_POWER_CHAI_NAME = "厳選初星チャイ"
    FULL_POWER_CHAI_NAMES = ["厳選初星チャイ", "厳選初星チヤイ", "初星チャイ"]
    FULL_POWER_CHAI_NAME_ROI = [190, 910, 400, 70]
    FULL_POWER_CHAI_TEMPLATE = "hif/produce/hif_drink_gensen_hatsuboshi_chai.png"
    FULL_POWER_CHAI_TEMPLATE_ROI = [40, 900, 170, 200]
    FULL_POWER_CHAI_TEMPLATE_THRESHOLD = 0.85
    FULL_POWER_CHAI_NAME_RATIO = 0.55
    BROWN_USE_POS = (502, 1169)              # 弹窗「✓使う」按钮中心(182305实测)
    BROWN_CANCEL_POS = (206, 1188)           # 弹窗「✕キャンセル」按钮中心(182305实测)
    # 「センブリソーダ」Pドリンク指定buff(用户): ①打国民后无初星黒酢且手牌无脚光; ②剩余回合=6 时用
    SEMBRI_NAME = "センブリソーダ"
    SEMBRI_CARDS = ["センブリソーダ", "センブリ", "ソーダ"]   # 纯片假名, MAA OCR配expected可读(裸rapidocr读"夕")
    SEMBRI_RATIO = 0.5
    # 注意名字坐标与初星黒酢不同(用户点出): センブリソーダ弹窗名 实测 x194-395,y845-874; 初星黒酢用 BROWN_NAME_ROI=[150,788,410,67]
    SEMBRI_NAME_ROI = [170, 828, 300, 70]   # 弹窗饮料名区(x170-470, y828-898), 避开下方效果文(y898+)
    # 纯片假名名 OCR 读成 "LAZ"/"ZAEL"(rapidocr误读), 名字匹配不可靠 → 用瓶子模板识别
    SEMBRI_BOTTLE_TPL = "hif/produce/sembri_soda_bottle.png"   # 弹窗瓶子卡(95x190, 取自105445截图 x55-150,y888-1078)
    SEMBRI_BOTTLE_ROI = [40, 870, 200, 245]  # 弹窗瓶子区(x40-240, y870-1115, 大于模板供滑动)
    SEMBRI_BOTTLE_THRESH = 0.8
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
    BROWN_DRINK_ROI = [10, 1150, 350, 100]   # 饮料栏检测区(参考ProduceCheckDrinkButton)
    BROWN_DRINK_THRESHOLD = 0.8
    BROWN_DRINK_MAX = 4                     # 饮料栏最多4瓶,依次点完为止

    def _prepare_paired_drink(self, context, decision) -> bool:
        if (self._paired_card is not None or getattr(self, '_paired_drink_disabled', False)
                or not decision.get('key') or decision['key'] not in self._drink_before_targets
                or self._followups is not None and self._followups.active):
            return False
        name = '特製ハツボシエキス'
        if name in self._drink_disabled_names:
            return False
        if self._consume_battle_drinks(context, [name], 'HIF出牌前配对', max_uses=1):
            self._paired_card = dict(decision)
            self._paired_unusable_count = 0
            self._paired_deadline = _hif_recovery.now() + self.TIME_OUT
            logger.info(f"HIF饮料配对: 已用一瓶，锁定目标={decision['key']}，等待重新定位出牌")
            return True
        return False

    def _recover_paired_drink(self, context) -> None:
        """Consumption was verified before pairing; abandon only the unmet card goal."""
        def safe(image):
            if self._is_drink_detail_open(context, image):
                return False
            if self._is_battle_end(context, image):
                return True
            skip = context.run_recognition('ProduceHIF__ProduceRecognitionSkipRound', image)
            return bool(skip and skip.hit and self._read_turn_count(context, image) is not None)
        if context is None or not common._hif_observe_result(context, safe):
            self._abort_drink_flow(context, 'HIF饮料配对: 未确认安全战斗/结算页，保留现场停止')
        logger.warning('HIF安全降级: 用饮已确认但目标出牌未完成，解除配对并关闭本战配对用饮；不补喝')
        self._paired_card = self._paired_deadline = None
        self._paired_unusable_count = 0
        self._paired_drink_disabled = True
        self._drink_disabled_names.add('特製ハツボシエキス')

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
            logger.info(f"HIF饮料配对: 连续两帧确认{self._paired_card['key']}游戏内不可用，解除锁定并重新决策")
            self._paired_card = None
            self._paired_deadline = None
            self._paired_unusable_count = 0

    def _load_hif_remaining_drink_turn(self, context: Context) -> int:
        """读取普通剩余饮料的使用回合；无效或缺失时保持倒数第4回合。"""
        fallback = self.DRINK_TURN
        try:
            node = context.get_node_data(self.DRINK_TURN_NODE)
            value = int(node.get("max_hit")) if node and node.get("max_hit") is not None else fallback
            if 1 <= value <= self.MAX_DRINK_TURN:
                return value
            logger.warning(
                f"HIF剩余饮料使用回合超出1-{self.MAX_DRINK_TURN}: {value}，沿用{fallback}"
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF剩余饮料使用回合读取异常: {e!r}，沿用{fallback}")
        return fallback

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
        self._drink_before_targets = []
        self._drink_first_battle_policy = {"enabled": True, "max_uses": -1, "unconfigured_max_uses": -1}
        self._drink_first_battle_limits = {}
        self._drink_first_battle_enabled = {}
        self._drink_second_battle_disabled = set()
        try:
            with open(common.HIF_DRINK_CATALOG_PATH, encoding="utf-8") as file:
                catalog = json.load(file)["drinks"]
            with open(common.HIF_DRINK_PROFILES_PATH, encoding="utf-8") as file:
                config = json.load(file)
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
                first = entry.get("first_battle_enabled", entry.get("disabled") is not True)
                second = entry.get("second_battle_enabled", entry.get("disabled") is not True)
                if not isinstance(first, bool) or not isinstance(second, bool):
                    raise ValueError(f"{name}一二战使用开关无效")
                self._drink_first_battle_enabled[name] = first
                if not second:
                    self._drink_second_battle_disabled.add(name)
                if not first and not second:
                    self._drink_disabled_names.add(name)
                    continue
                timing = entry.get("use_timing")
                targets = entry.get('before_card_targets')
                if targets is not None:
                    with open(os.path.join(common.EXT_DIR, 'catalog', 'hif_target_card_catalog.json'), encoding='utf-8') as file:
                        active = {card['name'] for card in json.load(file) if card.get('type') == 'active'}
                    if (entry['id'] != 25 or not isinstance(targets, list)
                            or any(not isinstance(key, str) or key not in active for key in targets)
                            or len(targets) != len(set(targets))):
                        self._drink_disabled_names.add(name)
                        logger.warning(f'HIF饮料配对: {name}候选配置无效，停止使用该饮料')
                    elif targets:
                        self._drink_before_targets = targets
                if timing is not None:
                    if self._valid_battle_drink_timing(
                        timing, allow_combo_only=name == self.BROWN_TARGET_NAME,
                    ):
                        self._drink_specific_timings[name] = timing
                    else:
                        self._drink_disabled_names.add(name)
                        logger.warning(f"HIF本战饮料: 「{name}」使用时机无效，停止自动使用该饮料")
            logger.info(
                f"HIF本战饮料: 职业={self.profession}，已导入={len(self._drink_imported_names)}，"
                f"不使用={len(self._drink_disabled_names)}，未指定饮料时机={self._drink_default_timing}"
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._drink_catalog_by_key = {
                common._hif_drink_name_key(self.BROWN_TARGET_NAME): self.BROWN_TARGET_NAME
            }
            self._drink_imported_names = []
            self._drink_specific_timings = {}
            self._drink_disabled_names = {self.BROWN_TARGET_NAME}
            self._drink_default_timing = {"mode": "never"}
            self._drink_before_targets = []
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

    def _battle_drink_boxes(self, context: Context, image=None) -> list:
        try:
            if image is None:
                image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
            boxes = [result.box for result in (reco.filtered_results if reco and reco.hit else [])]
            return sorted(boxes, key=lambda box: box[0])[:self.BROWN_DRINK_MAX]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"HIF本战饮料: 饮料栏定位异常 {exc!r}")
            return []

    def _consume_battle_drinks(
        self, context: Context, wanted_names: list[str], label: str,
        max_uses: int = 4, verify_post_use: bool = True,
    ) -> bool:
        """从左到右直接用饮；同批次补位后跳过已确认不使用的前缀。"""
        wanted = {name for name in wanted_names
                  if name not in self._drink_disabled_names and not self._battle_drink_limit_reached(name)}
        if not wanted:
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
            before_count = len(boxes)
            # 只复用本批次已读名并关闭详情的连续前缀；补位后仍核对其位置。
            if self._drink_list_signature(boxes[:len(skipped_boxes)]) != self._drink_list_signature(skipped_boxes):
                skipped_boxes = []
            if skipped_boxes:
                logger.info(f"{label}: 前{len(skipped_boxes)}瓶已确认本批次不使用且位置未变，跳过重复读名")
            open_failed = False
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
                category = (
                    "不使用" if name in self._drink_disabled_names else
                    "已导入" if name in self._drink_imported_names else
                    "未导入" if name else "无法识别"
                )
                logger.info(f"{label}: 第{index + 1}瓶 名称=[{name or raw or '未读到'}]，分类={category}")
                if context.tasker.stopping:
                    return used
                if name not in wanted or self._battle_drink_limit_reached(name):
                    if name in wanted:
                        logger.info(f"{label}: 「{name}」已达到一战单种或总使用上限，保留")
                    if not self._close_drink_detail(context):
                        self._abort_drink_flow(context, f"{label}: 详情未能关闭，停止当前HIF任务")
                    if name and index == len(skipped_boxes):
                        skipped_boxes.append(list(box))
                    elif not name:
                        skipped_boxes = []
                    continue
                logger.info(f"{label}: 「{name}」符合当前使用条件，直接使用")
                _hif_recovery.pending = f'用饮:{name}'
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
                stable = returned and self._wait_for_drink_list_stable(context, after_use=False, expected_count=before_count - 1)
                after_count = len(self._battle_drink_boxes(context))
                if not stable or after_count != before_count - 1:
                    def used_safely(image):
                        battle = context.run_recognition('ProduceHIF__ProduceRecognitionSkipRound', image)
                        return bool(battle and battle.hit and not self._is_drink_detail_open(context, image)
                                    and len(self._battle_drink_boxes(context, image)) == before_count - 1)
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
            if name == '特製ハツボシエキス' and (
                self._drink_before_targets or self._followups is not None and self._followups.active
            ):
                continue
            timing = self._drink_specific_timings.get(name, self._drink_default_timing)
            matched = (
                timing["mode"] == "first_turn" if first_turn else
                timing["mode"] == "remaining_turn" and timing["turn"] == turn
            )
            if not matched:
                continue
            if (name == self.BROWN_TARGET_NAME and (
                self._followups is not None and any(rule['use_black_vinegar'] and (
                    source == self._followups.active or source not in self._followups.successful or rule['wait_after_success']
                ) for source, rule in self._followups.rules.items())
            )):
                logger.info("HIF本战饮料: 初星黒酢留给国民/脚光组合流程")
                continue
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
        logger.info(f"HIF本战饮料: 时机={trigger}，准备按配置扫描{len(due)}种饮料")
        return self._consume_battle_drinks(context, due, "HIF本战饮料")

    # 旧的普通饮料、全力茶和センブリソーダ路径暂留作回归对照；本战入口不再调用。
    def _drink_buff(self, context: Context) -> bool:
        """喝饮料 buff：逐瓶点开弹窗 OCR 名字, 跳过初星黒酢(组合技道具非buff, 喝它会弹出移动界面),
        其余 buff 点「使う」使用。饮料喝掉后下一瓶会补位到前面(用户实测), 故每喝一瓶重新检测。
        返回是否真的用了 buff(仅初星黒酢则返回 False 交给正常出牌)。

        Returns:
            bool: True=有用掉 buff；False=无瓶可喝/只有初星黒酢/识别异常。
        """
        logger.info(f"喝buff: 等待 {self.DRINK_ACTION_PRE_DELAY:.1f} 秒，确保战斗动画结束")
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        if not self._wait_for_drink_list_stable(context, after_use=False):
            return False
        used = False
        detail_rescans = 0
        for _round in range(6):   # 防死循环: 最多4瓶 + 1轮空检测
            if context.tasker.stopping:
                break
            try:
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
                boxes = [r.box for r in (reco_detail.filtered_results if reco_detail and reco_detail.hit else [])]
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF喝饮料buff异常: {e}")
                break
            boxes.sort(key=lambda b: b[0])   # 按x排序(左→右)
            if not boxes:
                break
            logger.info(f"识别到{len(boxes)}瓶饮料")
            drank_one = False
            detail_open_failed = False
            for i, box in enumerate(boxes):
                if context.tasker.stopping:
                    break
                cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
                logger.info(f"喝buff: 点第{i + 1}瓶 @({cx},{cy}) 看名字")
                if not self._open_drink_detail(context, (cx, cy), f"喝buff: 第{i + 1}瓶"):
                    detail_open_failed = True
                    continue
                name = self._read_drink_name(context)
                if self._is_target_drink(name):
                    logger.info(f"喝buff: 第{i + 1}瓶是初星黒酢(组合技道具),跳过 → 点キャンセル")
                    if not self._close_drink_detail(context):
                        return used
                    continue   # 初星黒酢不消耗不补位, 看下一瓶
                logger.info(f"喝buff: 第{i + 1}瓶[buff] → 点使う使用")
                if not self._use_drink(context):
                    return used
                used = True
                drank_one = True
                if not self._wait_for_drink_list_stable(context):
                    # 饮料效果已经生效；此时继续点击可能落在残影或战斗界面上，
                    # 保守结束本轮，交还给正常出牌流程。
                    return used
                break   # 消耗一瓶后补位, 下一轮重新检测再喝下一瓶
            if not drank_one:
                if (
                    detail_open_failed
                    and detail_rescans < self.DRINK_DETAIL_RESCAN_ATTEMPTS
                    and self._wait_for_drink_list_stable(context, after_use=False)
                ):
                    detail_rescans += 1
                    logger.info("喝buff: 有饮料详情未打开，饮料栏稳定后从第一瓶重扫")
                    continue
                break   # 本轮没有可喝的buff(全是初星黒酢/没瓶) → 结束
        return used

    def _drink_brown_bottle(self, context: Context) -> bool:
        """组合满等待时使用一瓶初星黒酢检索后续卡；每次衔接只尝试一次，身份由统一详情 OCR 确认。"""
        state = self._followups
        if state is None or not state.active or not state.full_wait or state.vinegar_attempted:
            return False
        state.vinegar_attempted = True
        if self.BROWN_TARGET_NAME in self._drink_disabled_names:
            return False
        self._followup_move_targets = state.rules[state.active]['targets']
        used = self._consume_battle_drinks(context, [self.BROWN_TARGET_NAME], 'HIF组合检索', max_uses=1)
        self._followup_move_targets = None
        return used

    def _drink_full_power_chai(self, context: Context) -> bool:
        """全力职业每场本战开场查找并用完所有「厳選初星チャイ」。"""
        logger.info(
            f"HIF全力饮料: 第一回合等待 {self.DRINK_ACTION_PRE_DELAY:.1f} 秒后扫描"
            f"「{self.FULL_POWER_CHAI_NAME}」"
        )
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        if not self._wait_for_drink_list_stable(context, after_use=False):
            return False
        used = False
        detail_rescans = 0
        for _round in range(self.BROWN_DRINK_MAX + 1):
            if context.tasker.stopping:
                return used
            try:
                image = context.tasker.controller.post_screencap().wait().get()
                reco = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF全力饮料: 饮料栏识别异常 {e!r}")
                return used
            boxes = [result.box for result in (reco.filtered_results if reco and reco.hit else [])]
            boxes.sort(key=lambda box: box[0])
            if not boxes:
                return used
            logger.info(f"HIF全力饮料: 识别到{len(boxes)}瓶，按顺序检查")
            drank_one = False
            detail_open_failed = False
            for index, box in enumerate(boxes[:self.BROWN_DRINK_MAX]):
                if context.tasker.stopping:
                    return used
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                if not self._open_drink_detail(context, pos, f"HIF全力饮料: 第{index + 1}瓶"):
                    detail_open_failed = True
                    continue
                name = self._read_full_power_chai_name(context)
                if self._is_full_power_chai(name) or self._match_full_power_chai_template(context):
                    logger.info(f"HIF全力饮料: 第{index + 1}瓶命中「{self.FULL_POWER_CHAI_NAME}」→ 点使う")
                    if not self._use_drink(context):
                        return used
                    used = True
                    drank_one = True
                    if not self._wait_for_drink_list_stable(context):
                        return used
                    break
                logger.info(f"HIF全力饮料: 第{index + 1}瓶非目标[{name}] → 点キャンセル")
                if not self._close_drink_detail(context):
                    logger.warning("HIF全力饮料: 当前详情无法关闭，结束本场扫描")
                    return used
            if not drank_one:
                if (
                    detail_open_failed
                    and detail_rescans < self.DRINK_DETAIL_RESCAN_ATTEMPTS
                    and self._wait_for_drink_list_stable(context, after_use=False)
                ):
                    detail_rescans += 1
                    logger.info("HIF全力饮料: 有饮料详情未打开，饮料栏稳定后从第一瓶重扫")
                    continue
                logger.info(f"HIF全力饮料: 未找到更多「{self.FULL_POWER_CHAI_NAME}」，结束扫描")
                return used
        return used

    def _read_full_power_chai_name(self, context: Context) -> str:
        """读取当前饮料详情的全力茶名称区域。"""
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.FULL_POWER_CHAI_NAME_ROI,
                        "expected": "|".join(self.FULL_POWER_CHAI_NAMES) + "|" + r"[^\n]+",
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF全力饮料: 读名称异常 {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(result.text for result in reco.filtered_results)

    def _is_full_power_chai(self, text: str) -> bool:
        """以标题OCR确认是否为全力茶，容忍チ/チヤ等常见OCR变体。"""
        name = "".join((text or "").split())
        if not name:
            return False
        if any(target in name for target in self.FULL_POWER_CHAI_NAMES):
            return True
        score = max(
            (SequenceMatcher(None, name, target).ratio() for target in self.FULL_POWER_CHAI_NAMES),
            default=0.0,
        )
        return score >= self.FULL_POWER_CHAI_NAME_RATIO

    def _match_full_power_chai_template(self, context: Context) -> bool:
        """OCR 读不到名称时，以详情窗茶杯图案确认目标饮料。"""
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition(
                "ProduceHIF__ProduceIdentityMatch",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceIdentityMatch": {
                        "recognition": "TemplateMatch",
                        "template": self.FULL_POWER_CHAI_TEMPLATE,
                        "roi": self.FULL_POWER_CHAI_TEMPLATE_ROI,
                        "threshold": self.FULL_POWER_CHAI_TEMPLATE_THRESHOLD,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF全力饮料: 茶杯模板识别异常 {e!r}")
            return False
        return bool(reco and reco.hit)

    def _read_drink_name(self, context: Context) -> str:
        """OCR读Pドリンク詳細弹窗的饮料名。返回文本(拼接所有结果), 读不到返回空串。"""
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR",
                                                               "roi": self.BROWN_NAME_ROI,
                                                               "expected": "|".join(self.BROWN_TARGET_CARDS) + "|" + r"[^\n]+"}},
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝饮料: 读饮料名异常 {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(r.text for r in reco.filtered_results)

    def _is_target_drink(self, text: str) -> bool:
        """判断OCR读出的饮料名是否为「初星黒酢」。容忍OCR误差:
        ① 子串精确匹配(含目标词, 如初星黒酢/初星黒酢青汁等)
        ② SequenceMatcher 相似度 ≥0.5(容忍初星黒/初星酔等变体)"""
        t = (text or "").replace(" ", "").replace("　", "")
        if not t:
            return False
        for target in self.BROWN_TARGET_CARDS:
            if target in t:
                return True
        best_s = max((SequenceMatcher(None, t, target).ratio() for target in self.BROWN_TARGET_CARDS), default=0.0)
        if best_s >= self.BROWN_TARGET_RATIO:
            logger.info(f"喝初星黒酢: 名字相似度[{t}]={best_s:.2f} → 判定为「{self.BROWN_TARGET_NAME}」")
            return True
        return False

    def _is_sembri(self, text: str) -> bool:
        """判断OCR读出的饮料名是否为「センブリソーダ」。子串或相识度≥SEMBRI_RATIO。"""
        t = (text or "").replace(" ", "").replace("　", "")
        if not t:
            return False
        for c in self.SEMBRI_CARDS:
            if c in t:
                return True
        best_s = max((SequenceMatcher(None, t, c).ratio() for c in self.SEMBRI_CARDS), default=0.0)
        if best_s >= self.SEMBRI_RATIO:
            logger.info(f"喝センブリソーダ: 名字相似度[{t}]={best_s:.2f} → 判定为「{self.SEMBRI_NAME}」")
            return True
        return False

    def _read_sembri_name(self, context: Context) -> str:
        """OCR Pドリンク詳細弹窗饮料名(用 SEMBRI_NAME_ROI, 与初星黒酢 BROWN_NAME_ROI 坐标不同)。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", context.tasker.controller.post_screencap().wait().get(),
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR",
                                                               "roi": self.SEMBRI_NAME_ROI,
                                                               "expected": "|".join(self.SEMBRI_CARDS) + "|" + r"[^\n]+"}},
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝センブリソーダ: 读名异常 {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(r.text for r in reco.filtered_results)

    def _match_sembri_bottle(self, context: Context) -> bool:
        """模板匹配当前Pドリンク詳細弹窗的瓶子是否为「センブリソーダ」。

        纯片假名名 OCR 读成"LAZ/ZAEL"(rapidocr误读), 名字匹配不可靠 → 用弹窗瓶子卡(cv2多尺度)识别。
        """
        try:
            image = context.tasker.controller.post_screencap().wait().get()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝センブリソーダ: 截图失败 {e!r}")
            return False
        try:
            tpl = cv2.imread(os.path.join(common.EXT_DIR, "resource/base/image", self.SEMBRI_BOTTLE_TPL))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝センブリソーダ: 读瓶模板异常 {e!r}")
            return False
        if tpl is None or tpl.size == 0:
            logger.warning(f"喝センブリソーダ: 瓶模板不存在 {self.SEMBRI_BOTTLE_TPL}")
            return False
        x0, y0, w, h = self.SEMBRI_BOTTLE_ROI
        region = image[y0:y0 + h, x0:x0 + w]
        if region.size == 0 or region.shape[0] == 0:
            return False
        best = 0.0
        for s in np.arange(0.78, 1.21, 0.05):
            ts = cv2.resize(tpl, None, fx=s, fy=s)
            th, tw = ts.shape[:2]
            if th >= region.shape[0] or tw >= region.shape[1]:
                continue
            _, mx, _, _ = cv2.minMaxLoc(cv2.matchTemplate(region, ts, cv2.TM_CCOEFF_NORMED))
            best = max(best, float(mx))
        if best >= self.SEMBRI_BOTTLE_THRESH:
            logger.info(f"喝センブリソーダ: 瓶卡模板命中 score={best:.2f}")
            return True
        return False

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
            logger.warning(f"喝センブリソーダ: 检查详情弹窗异常 {e!r}")
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
                logger.warning(f"喝buff: 使用后饮料栏识别异常 {e!r}")
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
                    logger.info(
                        f"喝buff: 使用后饮料栏稳定，剩余{len(boxes)}瓶，"
                        "重新从当前第一瓶检查"
                    )
                    return True
            else:
                previous = signature
                stable = 1
            time.sleep(self.DRINK_LIST_STABLE_POLL_INTERVAL)

        logger.warning(
            f"喝饮料: 饮料栏未在 {self.DRINK_LIST_STABLE_TIMEOUT:.1f} 秒内稳定，"
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
            logger.warning("Pドリンク詳細: 点击前操作按钮已消失，重新确认页面，不继续点击")
            return False
        if not compatibility:
            x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
            logger.info(f"Pドリンク詳細: 动态点击{'使う' if action == 'use' else 'キャンセル'} @ ({x}, {y})")
        else:
            x, y = self.BROWN_USE_POS if action == "use" else self.BROWN_CANCEL_POS
            logger.warning(
                f"Pドリンク詳細: 第二次点击{'使う' if action == 'use' else 'キャンセル'}，"
                f"使用已有兼容坐标 @ ({x}, {y})"
            )
        context.tasker.controller.post_click(x, y).wait()
        return True

    def _close_drink_detail(self, context: Context) -> bool:
        """两次取消未生效后观察十秒，再复核同一详情并补点一次。"""
        identity = None
        for attempt in range(1, self.DRINK_DETAIL_ACTION_ATTEMPTS + 2):
            if getattr(context.tasker, 'stopping', False):
                return False
            if attempt > self.DRINK_DETAIL_ACTION_ATTEMPTS:
                logger.warning('Pドリンク詳細: 两次取消未关闭，等待10秒后复核并最后重试')
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
                f"Pドリンク詳細: 第{attempt}/{self.DRINK_DETAIL_ACTION_ATTEMPTS + 1}次"
                "点キャンセル后未确认关闭"
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
                f"Pドリンク詳細: 第{attempt}/{self.DRINK_DETAIL_ACTION_ATTEMPTS}次"
                "点使う后未确认关闭"
            )
        if self._wait_for_drink_detail(context, should_be_open=False, timeout=10.0, confirmation_count=3):
            logger.info('HIF结果复核: 饮料详情延迟关闭，不再补点使う')
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

    def _drink_sembri(self, context: Context, use_all: bool = False) -> bool:
        """依次检查饮料栏；use_all 时用完所有「センブリソーダ」后才返回。"""
        logger.info(
            f"喝センブリソーダ: 等待 {self.DRINK_ACTION_PRE_DELAY:.1f} 秒，确保战斗动画结束"
        )
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        if not self._wait_for_drink_list_stable(context, after_use=False):
            return False
        used = False
        detail_rescans = 0
        for _round in range(self.BROWN_DRINK_MAX + 1):
            if context.tasker.stopping:
                return used
            try:
                image = context.tasker.controller.post_screencap().wait().get()
                reco = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"喝センブリソーダ: 饮料栏识别异常 {e!r}")
                return used
            boxes = [r.box for r in (reco.filtered_results if reco and reco.hit else [])]
            boxes.sort(key=lambda box: box[0])
            if not boxes:
                return used
            logger.info(f"喝センブリソーダ: 识别到{len(boxes)}瓶，按顺序检查")
            drank_one = False
            detail_open_failed = False
            for i, box in enumerate(boxes[:self.BROWN_DRINK_MAX]):
                if context.tasker.stopping:
                    return used
                cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
                logger.info(f"喝センブリソーダ: 点第{i + 1}瓶 @({cx},{cy})，等待详情弹窗")
                if not self._open_drink_detail(context, (cx, cy), f"喝センブリソーダ: 第{i + 1}瓶"):
                    detail_open_failed = True
                    continue
                if self._match_sembri_bottle(context):
                    logger.info(f"喝センブリソーダ: 第{i + 1}瓶命中「{self.SEMBRI_NAME}」→ 点使う")
                    if not self._use_drink(context):
                        return used
                    used = True
                    drank_one = True
                    if not use_all:
                        return True
                    if not self._wait_for_drink_list_stable(context):
                        return used
                    break
                logger.info(f"喝センブリソーダ: 第{i + 1}瓶非センブリソーダ → 点キャンセル")
                if not self._close_drink_detail(context):
                    return used
            if not drank_one:
                if (
                    detail_open_failed
                    and detail_rescans < self.DRINK_DETAIL_RESCAN_ATTEMPTS
                    and self._wait_for_drink_list_stable(context, after_use=False)
                ):
                    detail_rescans += 1
                    logger.info("喝センブリソーダ: 有饮料详情未打开，饮料栏稳定后从第一瓶重扫")
                    continue
                return used
        return used
