"""HIF 本战识别与操作 mixin。"""
import os
import re
import time
import unicodedata

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


class BattleRecognitionMixin:
    """HIF 本战识别与操作：手牌检测、卡名识别、选卡出牌、回合数、移动/保留页与结算判定。"""

    CLICK_DELAY = 0.3
    TIME_OUT = 15.0
    # 出牌后技能特效动画期会遮盖手牌，用于漏框。判定"特效已结束、可稳定识别手牌"的
    # 标志是"跳过(SKIP)按钮"连续稳定命中 N 次，而非盲等固定时长。
    SKIP_STABLE_COUNT = 3
    # 判定"战斗结束（结算界面）"需连续命中的次数：防单帧误匹配（动画闪光/过渡帧
    # 偶然匹配到类似次へ的橘色元素）导致"还在战斗却误退出"。
    BATTLE_END_STABLE = 2
    # 最后一回合过场跳过点击上限：间隔约2秒，覆盖实测约35秒的过场；重新出现可操作 HUD 时清零。
    BATTLE_TRANSITION_MAX_TAPS = 15
    # 只截「残りターン」环中心的白色数字，避开彩色刻度和右下角指针干扰 OCR。
    # 数字本体实测位于 x38-87/y74-107，可完整覆盖一战9回合、二战12回合及加回合后的两位数。
    TURN_COUNT_ROI = [34, 68, 56, 44]
    TURN_COUNT_EXPECTED = r"\d{1,2}"
    # 每次回到主循环最多只经过一个回合；技能或饮料加回合也只会使计数增加1。
    # 更大的跳变视为 OCR 异常；不限制总回合数，兼容一战9、二战12及额外加回合。
    TURN_COUNT_MAX_STEP = 1
    EXTRA_TURN_ROI = [103, 126, 46, 28]  # 白色椭圆中的蓝色 +N，独立于倒计时环
    # 每场刚进入可操作状态时的初始回合数，用于区分两场本战；加回合仅会发生在后续回合，
    # 因此判定结果在本场缓存，不会受饮料或卡牌加回合影响。
    BATTLE_START_TURNS = {9: 1, 12: 2}
    # 部分机型会在本战2开场演出/可操作状态稳定前进入第2/3回合，此时首次可读值为11/10。
    # 本战1不使用类似回退，避免将读到8的其它状态误判为本战1。
    BATTLE_EARLY_TURNS = {11: 2, 10: 2}
    # 卡牌Y轴位置边界（超出范围视为识别异常，不点击）
    CARD_Y_MIN = 840
    CARD_Y_MAX = 1000
    # 所有 HIF 职业共用：整手可出牌需连续两帧数量、身份及横向位置一致才允许点击。
    CARD_HAND_STABLE_COUNT = 2
    CARD_HAND_X_TOLERANCE = 30
    CARD_HAND_POLL_INTERVAL = 0.2
    HIF_CARD_STATE_TIMEOUT = 3.0
    HIF_CARD_CLICK_ATTEMPTS = 2
    HIF_PREVIEW_X_TOLERANCE = 45
    # 抬起牌遮挡邻牌时其余卡位几乎不动（实测≤3像素）；手牌数量变化时相邻卡位会偏移约40像素。
    HIF_OCCLUDED_X_TOLERANCE = 15
    # 选中牌抬起约28像素；邻牌比目标卡位高出此值即视为抬起。
    HIF_RAISED_Y = 15
    # 取消选中只点画面最左上角；再点已选中的卡会直接出牌。
    HIF_DESELECT_POS = (5, 5)
    PROFESSION_POINTER_TEMPLATES = {
        "全力": "hif/produce/hif_full_power_pointer.png",
        "強気": "hif/produce/hif_strong_pointer.png",
        "温存": "hif/produce/hif_conserve_pointer.png",
    }
    # 左侧状态区从左到右依次是：唯一的当前状态指针、当前全力值、下回合全力提示。
    # 此 ROI 只覆盖最左侧指针，不包含右侧的数值和“下回合可进入全力”图标。
    PROFESSION_POINTER_ROI = [0, 220, 95, 85]
    PROFESSION_POINTER_THRESHOLD = 0.8
    # 当前指针右侧灰色数值框中的全力值；更右侧的紫色图标只表示下回合状态。
    FULL_POWER_VALUE_ROI = [110, 238, 50, 46]
    # 移动/保留页（720x1280）。未选卡时上方是浅灰空详情板，卡组从板下开始，可上下滚动
    # （ほっと緑茶 2026-11-03 实测：首行中心 y≈569，行距 147，卡高≈118，按钮区从 y≈1120 起）。
    # 板下探测带若不是浅灰空板，则为首行在 y≈457 的旧版式（保留手牌页等），只扫可见三行。
    MOVE_PANEL_PROBE = (60, 440, 660, 476)
    MOVE_COLUMN_X = (139, 286, 433, 580)
    MOVE_ROW0_Y = 569
    MOVE_ROW_PITCH = 147
    MOVE_LIST_VIEW = (75, 500, 645, 1100)   # x0, y0, x1, y1；不含右侧滚动条
    MOVE_ROW_CLICK_RANGE = (560, 1040)      # 卡中心在此范围内整张卡可见，才点击
    MOVE_MAX_ROWS = 40
    MOVE_SWIPE_X = 360
    MOVE_SWIPE_SPAN = (1000, 860)           # 慢速上滑约一行，惯性也小于可测位移
    MOVE_SWIPE_DURATION = 1000
    MOVE_SCROLL_SETTLE_TIMEOUT = 2.0
    MOVE_MAX_SCROLLS = 14
    # 点中的卡格四角有橙色选中框（2026-11-04 实机截图：选中格 4/4 角，其余 0）；点末行空位时框和详情都停在上一张。
    MOVE_FRAME_CORNER = 66
    MOVE_FRAME_PATCH = 9
    MOVE_FRAME_MIN_CORNERS = 3
    MOVE_GRID = [(139, 457), (286, 457), (433, 457), (580, 457),
                 (139, 604), (286, 604), (433, 604), (580, 604),
                 (139, 741), (286, 741), (433, 741), (580, 741)]
    # 移动页详情标题位于分隔线上方，避开左侧卡图、右侧消耗和下方效果。
    HIF_MOVE_NAME_ROI = [190, 105, 410, 40]
    MOVE_TARGET_CARD = "脚光"
    MOVE_OCR_ALL = r"[^\n]+"                 # 宽匹配: 读详情区全部卡名文字(含非目标卡)
    MOVE_NAME_STABLE_COUNT = 2
    MOVE_NAME_TIMEOUT = 2.5
    HOLD_GREEN_TEA = "ほっと緑茶"
    HOLD_MULTI_SOURCE = "本領発揮+"          # 可保留两张：点一次看详情，再点一次才选中，可取消
    HOLD_CANCEL_POS = (220, 1160)
    MOVE_ENABLED_TIMEOUT = 3.0
    MOVE_ENABLED_STABLE_COUNT = 2
    MOVE_ENABLED_POLL = 0.2
    MOVE_SUBMIT_ATTEMPTS = 3                 # 点「移動」后仍停在移动页时最多补点到这个次数
    MOVE_SUBMIT_CONFIRM_TIMEOUT = 2.0        # 每次点击后等待离开移动页的时间
    MOVE_SUBMIT_POLL = 0.25

    @staticmethod
    def _load_hif_battle_skip(context: Context, battle_number: Optional[int]) -> bool:
        """读取前台「SKIP本战1/2」开关；场次未识别时默认不跳过。"""
        if battle_number not in (1, 2):
            return False
        try:
            node = context.get_node_data(f"ProduceHIF__ProduceHIFBattle{battle_number}Skip")
            return bool(node and node.get("enabled", False))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF战斗: 读取本战{battle_number} SKIP开关异常 {e!r}")
            return False

    def _detect_battle_number(self, context: Context, image) -> Optional[int]:
        """以战斗开始时的 9/12 回合数判定本战1/2，并兼容本战2入口识别延迟。"""
        current_image = image
        for attempt in range(3):
            turn = self._read_turn_count(context, current_image)
            battle_number = self.BATTLE_START_TURNS.get(turn)
            if battle_number is not None:
                logger.info(f"HIF战斗: 初始剩余回合数={turn} → 判定本战{battle_number}")
                return battle_number
            battle_number = self.BATTLE_EARLY_TURNS.get(turn)
            if battle_number is not None:
                logger.info(
                    f"HIF战斗: 首次可读剩余回合数={turn}（开场识别延迟）"
                    f"→ 回退判定本战{battle_number}"
                )
                return battle_number
            if attempt < 2:
                time.sleep(0.4)
                current_image = context.tasker.controller.post_screencap().wait().get()
        logger.warning(
            f"HIF战斗: 初始剩余回合数非9/12（含本战2开场回退11/10）或未读到"
            f"（最后值={self._last_turn_count}）"
        )
        return None

    def _in_range(self, box: list) -> bool:
        """校验卡牌Y轴坐标，超出范围视为识别异常，不点击。"""
        return self.CARD_Y_MIN <= box[1] <= self.CARD_Y_MAX

    def _reset_hif_recognition(self) -> None:
        self._hif_hand_geometry = ()
        self._hif_hand_boxes = ()
        self._hif_missing_cards = []
        self._hif_detail_keys = {}
        self._hif_detail_upgraded = {}
        self._hif_detail_attempted = set()
        self._hif_probe_started_at = None
        self._hif_probe_expired = False
        self._hif_empty_started_at = None
        self._hif_empty_count = 0
        self._hif_empty_timeout_reported = False
        self._hif_skip_reason = None
        self._hif_recognition_turn = None
        self._hif_unusable_signature = None
        self._hif_unusable_count = 0
        self._hif_selection_index = None
        self._hif_unselected_count = 0
        self._hif_diagnostic_saved = False
        self._hif_selection_pair_saved = False
        self._reset_card_hand_stability()

    def _hif_card_boxes(self, results: list) -> list:
        """建议是手牌属性；牌框在 cards/suggestions 间切换时不丢掉该牌。"""
        candidates = [(r.box, r.score) for r in results
                      if r.label in ("cards", "suggestions") and r.score >= 0.5 and self._in_range(r.box)
                      and 0 <= r.box[0] < 720 and r.box[2] >= 90
                      and 180 <= r.box[3] <= 330 and r.box[1] + r.box[3] <= 1170]
        kept = []
        for box, score in sorted(candidates, key=lambda item: item[0][2] * item[0][3], reverse=True):
            if not any(self._hif_duplicate_box(box, other) for other, _ in kept):
                kept.append((box, score))
        return sorted(kept, key=lambda item: item[0][0] + item[0][2] // 2)

    def _hif_duplicate_box(self, box: list, other: list) -> bool:
        # 选中牌抬起后会遮住邻牌；只去掉至少半数落在另一框内的重复框/窄条。
        area, other_area = box[2] * box[3], other[2] * other[3]
        iou = self._iou(box, other)
        intersection = iou * (area + other_area) / (1 + iou)
        return intersection >= min(area, other_area) * 0.50

    @staticmethod
    def _hif_geometry(boxes: list) -> tuple:
        return tuple(box[0] + box[2] // 2 for box, _ in boxes)

    def _hif_same_geometry(self, boxes: list) -> bool:
        current = self._hif_geometry(boxes)
        return len(current) == len(self._hif_hand_geometry) and all(
            abs(old - new) <= self.CARD_HAND_X_TOLERANCE
            for old, new in zip(self._hif_hand_geometry, current))

    def _hif_fill_occluded(self, kept: list, results: list) -> list:
        """抬起的选中牌遮住已识别邻牌、使其多数帧漏检时，按其原卡位补回本帧手牌。

        仅在其余卡位原地不动、相邻牌明显抬起、该卡位没有高置信度不可用框时补回；
        换手或卡牌变为不可用时不补。补回的牌点选时仍须 SELECT 与详情连续两帧确认。
        """
        previous = self._hif_hand_boxes
        if len(previous) < 3 or len(kept) != len(previous) - 1:
            return kept
        mapping = {}
        for box, _ in kept:
            cx = box[0] + box[2] // 2
            slots = [old for old, other in enumerate(previous)
                     if abs(other[0] + other[2] // 2 - cx) <= self.HIF_OCCLUDED_X_TOLERANCE and old not in mapping.values()]
            if len(slots) != 1:
                return kept
            mapping[tuple(box)] = slots[0]
        missing = next(old for old in range(len(previous)) if old not in mapping.values())
        key = self._hif_detail_keys.get(missing)
        slot = list(previous[missing])
        raised = any(old in (missing - 1, missing + 1) and slot[1] - box[1] >= self.HIF_RAISED_Y
                     for box, old in mapping.items())
        unusable = any(r.label == "useless" and r.score >= 0.8 and self._hif_duplicate_box(r.box, slot) for r in results)
        if not key or not raised or unusable:
            return kept
        self._hif_log_changed("hand_occluded", f"HIF手牌: 「{key}」被抬起的邻牌遮挡漏检，按原卡位补回 box={slot}",
                              signature=(self._last_turn_count, key, missing))
        return sorted(kept + [(slot, 0.5)], key=lambda item: item[0][0] + item[0][2] // 2)

    def _hif_bind_hand(self, boxes: list) -> None:
        current = self._hif_geometry(boxes)
        previous_boxes = list(self._hif_hand_boxes)
        previous_geometry = list(self._hif_hand_geometry)
        previous_keys = dict(self._hif_detail_keys)
        previous_upgraded = dict(self._hif_detail_upgraded)
        previous_attempted = set(self._hif_detail_attempted)
        # 同一手牌中的漏框只暂存身份；恢复到可见卡位后才能参与决策或点击。
        for box, key, attempted in self._hif_missing_cards:
            old = len(previous_boxes)
            previous_boxes.append(box)
            previous_geometry.append(box[0] + box[2] // 2)
            if key:
                previous_keys[old] = key
            if attempted:
                previous_attempted.add(old)
        mapping = {}
        pairs = sorted((abs(x - cx), index, old)
                       for index, cx in enumerate(current)
                       for old, x in enumerate(previous_geometry)
                       if abs(x - cx) <= self.HIF_PREVIEW_X_TOLERANCE)
        for _, index, old in pairs:
            if index not in mapping and old not in mapping.values():
                mapping[index] = old
        # 末张牌可能只剩右侧窄条，中心会偏移超过45像素；仅接受一对一的半框重叠。
        overlap = {index: [old for old, previous in enumerate(previous_boxes)
                           if old not in mapping.values() and self._hif_duplicate_box(box, previous)]
                   for index, (box, _) in enumerate(boxes) if index not in mapping}
        for index, matches in overlap.items():
            if len(matches) == 1 and sum(matches[0] in others for others in overlap.values()) == 1:
                mapping[index] = matches[0]
        self._hif_missing_cards = [(list(box), previous_keys.get(old), old in previous_attempted)
                                   for old, box in enumerate(previous_boxes) if old not in mapping.values()
                                   and (old in previous_keys or old in previous_attempted)]
        self._hif_detail_keys = {new: previous_keys[old] for new, old in mapping.items() if old in previous_keys}
        self._hif_detail_upgraded = {new: previous_upgraded[old] for new, old in mapping.items() if old in previous_upgraded}
        self._hif_detail_attempted = {new for new, old in mapping.items() if old in previous_attempted}
        self._hif_selection_index = next((new for new, old in mapping.items()
                                         if old == self._hif_selection_index), None)
        self._hif_hand_geometry = current
        self._hif_hand_boxes = tuple(list(box) for box, _ in boxes)

    @staticmethod
    def _load_hif_debug(context: Context) -> bool:
        try:
            node = context.get_node_data("ProduceHIF__ProduceHIFDebug")
            return isinstance(node, dict) and node.get("enabled") is True
        except Exception as exc:
            logger.warning(f"HIF Debug开关读取失败，关闭诊断截图保存: {exc!r}")
            return False

    def _hif_log_detection(self, image, results: list, reason: str) -> None:
        if self._hif_diagnostic_saved:
            return
        self._hif_diagnostic_saved = True
        detected = ", ".join(f"{r.label}{list(r.box)}:{r.score:.2f}" for r in results)
        shot = ""
        if self._hif_debug_enabled and cv2 is not None and image is not None:
            try:
                folder = os.path.join(common.BASE_DIR, "debug", "custom", "hif_cards")
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"{time.time_ns()}.png")
                if cv2.imwrite(path, image):
                    shot = f"，截图={path}"
            except Exception as exc:  # 诊断失败不能阻止战斗。
                logger.warning(f"HIF手牌诊断截图失败: {exc!r}")
        logger.info(f"HIF手牌诊断: {reason}，检测结果=[{detected}]{shot}")

    def _hif_no_playable_hand(self, context: Context, image, results: list) -> bool:
        """仅确认无牌或整手游戏内不可用牌时允许空过；识别不明继续观察。"""
        self._hif_skip_reason = None
        if image is not None and self._is_drink_detail_open(context, image):
            self._hif_empty_started_at = None
            self._hif_empty_count = 0
            self._hif_unusable_signature = None
            self._hif_unusable_count = 0
            return False
        self._reset_card_hand_stability()
        now = time.time()
        if self._hif_empty_started_at is None:
            self._hif_empty_started_at = now
        # 任何有效手牌框（含游戏内不可用牌）都否决“无手牌”判断。
        hand = [r for r in results if r.label in ("cards", "suggestions", "useless")
                and r.score >= 0.5 and self._in_range(r.box)
                and 0 <= r.box[0] < 720 and r.box[2] >= 90
                and 180 <= r.box[3] <= 330 and r.box[1] + r.box[3] <= 1170]
        unusable = bool(hand) and all(r.label == "useless" for r in hand)
        self._hif_log_detection(
            image, results, "检测到游戏内不可用牌，等待复核" if unusable else "尚未确认手牌状态，等待复核")
        empty = context.run_recognition("ProduceHIF__ProduceRecognitionNoCards", image) if not hand else None
        self._hif_empty_count = self._hif_empty_count + 1 if empty and empty.hit else 0
        if self._hif_empty_count >= 2:
            self._hif_skip_reason = "确认无手牌"
            logger.info("HIF手牌: 连续两次确认无手牌")
            return True
        playable = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image) if unusable else None
        turn = self._read_turn_count(context, image) if unusable and playable and playable.hit else None
        if unusable and playable and playable.hit and turn is not None and not self._is_drink_detail_open(context, image):
            signature = (turn, tuple((round((r.box[0] + r.box[2] / 2) / 20), r.label) for r in sorted(hand, key=lambda r: r.box[0])))
            self._hif_unusable_count = self._hif_unusable_count + 1 if signature == self._hif_unusable_signature else 1
            self._hif_unusable_signature = signature
            if self._hif_unusable_count >= 2:
                self._hif_skip_reason = "游戏内不可用"
                logger.info("HIF手牌: 连续两帧确认整手牌游戏内不可用")
                return True
        else:
            self._hif_unusable_signature = None
            self._hif_unusable_count = 0
        if now - self._hif_empty_started_at >= self.TIME_OUT and not self._hif_empty_timeout_reported:
            self._hif_empty_timeout_reported = True
            logger.warning("HIF手牌识别不明: 未确认无手牌或游戏内不可用，继续观察；不点击SKIP")
        return False

    def _hif_log_selection_pair(self, before, after, index: int) -> None:
        if not self._hif_debug_enabled or self._hif_selection_pair_saved or cv2 is None:
            return
        self._hif_selection_pair_saved = True
        try:
            folder = os.path.join(common.BASE_DIR, "debug", "custom", "hif_cards")
            os.makedirs(folder, exist_ok=True)
            prefix = f"{time.time_ns()}-select-{index + 1}"
            saved = [phase for phase, image in (("before", before), ("after", after))
                     if image is not None and cv2.imwrite(os.path.join(folder, f"{prefix}-{phase}.png"), image)]
            if saved:
                logger.info(f"HIF选中点击对照截图: 第{index + 1}张 {os.path.join(folder, prefix)}-{{{','.join(saved)}}}.png")
        except Exception as exc:
            logger.warning(f"HIF选中点击对照截图保存失败: {exc!r}")

    @staticmethod
    def _hif_panel_top(image) -> Optional[int]:
        """详情框向上扩展；以浅色面板定位顶部，避开效果正文。"""
        if cv2 is None or image is None or image.shape[:2] != (1280, 720):
            return None
        region = image[240:850, 80:640]
        white = ((region.min(axis=2) > 218)
                 & (region.max(axis=2) - region.min(axis=2) < 30)).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(white, 8)
        panels = [(int(area), int(y) + 240) for x, y, w, h, area in stats[1:]
                  if 480 <= w <= 560 and h >= 160 and area >= 50000
                  and 0 <= x <= 35 and 810 <= y + h + 240 <= 845]
        return max(panels)[1] if panels else None

    @staticmethod
    def _hif_has_select(context: Context, image, box: list) -> bool:
        cx = box[0] + box[2] // 2
        # 两个局部裁剪覆盖九张实测样本，避免整条手牌 OCR 与邻卡名字连读。
        for left, top, width, height in ((cx - 70, 1106, 140, 40), (cx - 60, 1110, 120, 35)):
            reco = context.run_recognition("ProduceHIF__ProduceIdentitySelected", image, pipeline_override={
                "ProduceHIF__ProduceIdentitySelected": {"recognition": "OCR", "expected": "SELECT",
                                            "roi": [max(0, left), top, width, height]}})
            if reco and reco.hit:
                return True
        return False

    def _hif_selected_index(self, context: Context, image, boxes: list, *, pending: bool = True) -> int:
        selected = [index for index, (box, _) in enumerate(boxes)
                    if self._hif_has_select(context, image, box)]
        if len(selected) == 1:
            # A temporarily missing neighbor must not shift the selected card's identity/index.
            if self._hif_hand_geometry:
                box = boxes[selected[0]][0]
                center = box[0] + box[2] // 2
                positions = [i for i, old in enumerate(self._hif_hand_geometry)
                             if abs(old - center) <= self.HIF_PREVIEW_X_TOLERANCE]
                if len(positions) != 1:
                    return -2
                selected = positions
            self._hif_unselected_count = 0
            self._hif_selection_index = selected[0]
            return selected[0]
        panel = self._hif_panel_top(image)
        raised = any(box[1] <= 875 and box[1] + box[3] <= 1130 for box, _ in boxes)
        if selected or panel is not None or raised:
            self._hif_unselected_count = 0
            return -2
        # 延迟选中失败后可能留下旧提示。两帧完整原手牌、无详情/SELECT/抬起牌才清理。
        if cv2 is not None and self._hif_same_geometry(boxes):
            self._hif_unselected_count += 1
            if pending and self._hif_unselected_count >= 2 and self._hif_selection_index is not None:
                logger.info("HIF点击前复核: 连续两帧未选中，清理上次选中记录")
                self._hif_selection_index = None
        else:
            self._hif_unselected_count = 0
        return -2 if pending and self._hif_selection_index is not None else -1

    def _hif_frame_boxes(self, context: Context, image) -> list:
        reco = context.run_recognition("ProduceHIF__ProduceRecognitionCards", image)
        return self._hif_card_boxes(reco.all_results if reco else [])

    def _hif_preview_box(self, boxes: list, index: int, *, context=None, image=None):
        """选中期间允许邻牌暂时漏框，但不接受新卡位或跨卡位关联。"""
        if index >= len(self._hif_hand_geometry):
            return None
        matches = {}
        extra = False
        for box, _ in boxes:
            cx = box[0] + box[2] // 2
            nearby = [i for i, x in enumerate(self._hif_hand_geometry)
                      if abs(x - cx) <= self.HIF_PREVIEW_X_TOLERANCE]
            if not nearby:
                nearby = [i for i, previous in enumerate(self._hif_hand_boxes)
                          if self._hif_duplicate_box(box, previous)]
            # 抬起的选中牌会遮住带建议光效的邻牌，使其在两侧卡位之间时有时无；
            # 这种中间漏框不改变已绑定卡位，仅在其余卡位全部可见时忽略；换手时卡位整体移动，仍会被拒绝。
            if not nearby and min(self._hif_hand_geometry) < cx < max(self._hif_hand_geometry):
                extra = True
                continue
            if len(nearby) != 1 or nearby[0] in matches:
                return None
            matches[nearby[0]] = box
        geometry = self._hif_hand_geometry
        steady = all(abs(box[0] + box[2] // 2 - geometry[i]) <= self.HIF_OCCLUDED_X_TOLERANCE for i, box in matches.items())
        if extra and (len(matches) != len(geometry) or not steady):
            return None
        target = matches.get(index)
        # 目标牌本身被抬起的邻牌遮挡漏框：其余卡位原地可见且相邻牌明显抬起时，按记录的卡位点选；
        # 选中后由 SELECT 与详情连续两帧确认，不会盲目出牌。
        if (target is None and not extra and steady and len(geometry) >= 3 and len(matches) == len(geometry) - 1
                and index < len(self._hif_hand_boxes)):
            previous = self._hif_hand_boxes[index]
            if any(i in matches and previous[1] - matches[i][1] >= self.HIF_RAISED_Y
                   for i in (index - 1, index + 1)):
                return list(previous)
        # 选中后模型可能只检测到详情面板；必须用当前帧 SELECT 和面板确认原卡位。
        if (target is None and context is not None and self._hif_selection_index == index
                and index < len(self._hif_hand_boxes) and self._hif_panel_top(image) is not None):
            previous = self._hif_hand_boxes[index]
            if self._hif_has_select(context, image, previous):
                return previous
        return target

    @staticmethod
    def _hif_click_card(context: Context, box: list, index: int, phase: str, attempt: int) -> bool:
        point = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        job_id = None
        try:
            job = context.tasker.controller.post_click(*point)
            job_id = job.job_id
            job.wait()
            if not job.succeeded:
                logger.warning(f"HIF{phase}点击执行未成功: 第{index + 1}张 @ {point} job_id={job_id}，重新识别")
                return False
            # 首次点击不单独记录，结果由选中/出牌确认行汇总；重试时记下坐标便于对照截图。
            if attempt:
                logger.info(f"HIF{phase}点击: 第{index + 1}张 @ {point}（第{attempt + 1}次），等待画面确认")
            return True
        except Exception as exc:
            logger.warning(f"HIF{phase}点击执行异常: 第{index + 1}张 @ {point} job_id={job_id} error={exc!r}，重新识别")
            return False

    def _hif_select_card(self, context: Context, image, index: int):
        if context.tasker.stopping:
            return None
        image = context.tasker.controller.post_screencap().wait().get()
        boxes = self._hif_frame_boxes(context, image)
        if self._hif_preview_box(boxes, index) is None:
            self._hif_log_changed("select_skip", f"HIF选中: 第{index + 1}张卡位与当前帧不一致，本次不点击; "
                                  f"卡位={self._hif_hand_geometry}，当前={self._hif_geometry(boxes)}", signature=("mismatch", index))
            return None
        skip = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
        if not (skip and skip.hit):
            self._hif_log_changed("select_skip", f"HIF选中: 第{index + 1}张选中前未见SKIP，本次不点击")
            return None
        selected = self._hif_selected_index(context, image, boxes)
        if selected == -2:
            logger.warning("HIF选中: 已选中卡的位置未确认，本次不点击")
            return None
        for attempt in range(self.HIF_CARD_CLICK_ATTEMPTS):
            clicked = False
            before_image = image.copy() if self._hif_debug_enabled and hasattr(image, "copy") else image
            if selected != index:
                box = self._hif_preview_box(boxes, index)
                if box is None:
                    return None
                if not self._hif_click_card(context, box, index, "选中", attempt):
                    return None
                clicked = True
                self._hif_selection_index = index
                self._hif_unselected_count = 0
                time.sleep(0.4)
            else:
                logger.info(f"HIF选中: 第{index + 1}张已处于选中状态，不再点击")
            deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
            stable = missed = 0
            while time.time() < deadline and not context.tasker.stopping:
                image = context.tasker.controller.post_screencap().wait().get()
                boxes = self._hif_frame_boxes(context, image)
                target = self._hif_preview_box(boxes, index, context=context, image=image)
                if target is None:
                    stable = missed = 0
                    time.sleep(self.CARD_HAND_POLL_INTERVAL)
                    continue
                target_selected = self._hif_has_select(context, image, target)
                stable = stable + 1 if target_selected and (cv2 is None or self._hif_panel_top(image) is not None) else 0
                if stable >= 2:
                    self._hif_selection_index = index
                    if attempt:
                        logger.info(f"HIF选中: 第{index + 1}张第{attempt + 1}次点击后已确认选中")
                    return image
                # 保留满3秒展开时间；末尾两帧明确仍未选中才允许重试。
                if target_selected:
                    missed = 0
                else:
                    observed = self._hif_selected_index(context, image, boxes, pending=False)
                    playable = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                    missed = missed + 1 if observed != -2 and playable and playable.hit else 0
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
            if missed < 2 or context.tasker.stopping:
                break
            self._hif_selection_index = None
            if not clicked:
                logger.info(f"HIF选中: 第{index + 1}张原选中状态已消失（本次未点击），重新识别")
                return None
            selected = -1
            if attempt == 0:
                logger.warning(f"HIF选中: 第{index + 1}张点击已执行但画面仍未选中，重试一次")
                self._hif_log_selection_pair(before_image, image, index)
        logger.warning(f"HIF选中: 第{index + 1}张选中未确认，重新识别；状态不明确时不连续点击")
        return None

    def _hif_read_detail(self, context: Context, index: int) -> tuple:
        deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
        previous = ""
        count = 0
        while time.time() < deadline and not context.tasker.stopping:
            image = context.tasker.controller.post_screencap().wait().get()
            boxes = self._hif_frame_boxes(context, image)
            top = self._hif_panel_top(image)
            target = self._hif_preview_box(boxes, index, context=context, image=image)
            if target is None:
                count = 0
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                continue
            text = ""
            if top is not None and self._hif_has_select(context, image, target):
                reco = context.run_recognition("ProduceHIF__ProduceIdentityDetail", image, pipeline_override={
                    "ProduceHIF__ProduceIdentityDetail": {"recognition": "OCR", "expected": r"[^\n]+",
                                              "roi": [150, top + 8, 410, 40]}})
                if reco and reco.hit:
                    text = "".join(r.text or "" for r in sorted(reco.filtered_results, key=lambda r: r.box[0]))
            normalized = self._hif_card_name(text)
            count = count + 1 if normalized and normalized == previous else 1 if normalized else 0
            previous = normalized
            if count >= 2:
                return self._hif_name_key(text), text
            time.sleep(self.CARD_HAND_POLL_INTERVAL)
        return None, ""

    def _hif_probe_unknowns(self, context: Context, image, identified: list, *, detection_results: list) -> bool:
        if self._hif_probe_expired:
            return False
        pending = [index for index, card in enumerate(identified)
                   if not card["key"] and index not in self._hif_detail_attempted]
        if not pending:
            return False
        if self._hif_probe_started_at is None:
            self._hif_probe_started_at = time.time()
        if time.time() - self._hif_probe_started_at >= self.TIME_OUT:
            self._hif_probe_expired = True
            logger.warning("HIF整手详情补认已达15秒，保留已有识别结果进入出牌决策")
            return False
        if cv2 is None:
            self._hif_detail_attempted.update(pending)
            logger.warning("HIF卡牌详情补认需要OpenCV，本次按兜底优先级处理")
            return False
        # 当前帧沿用主循环的检测结果；选中与详情复核仍重新截图、检测。
        self._hif_log_detection(image, detection_results, "暂未识别或存在歧义的卡进入详情补认")
        for index in pending:
            if context.tasker.stopping:
                break
            if time.time() - self._hif_probe_started_at >= self.TIME_OUT:
                self._hif_probe_expired = True
                logger.warning("HIF整手详情补认已达15秒，结束补认后重新截图决策")
                break
            self._hif_detail_attempted.add(index)
            selected = self._hif_select_card(context, image, index)
            key, text = self._hif_read_detail(context, index) if selected is not None else (None, "")
            logger.info(f"HIF手牌详情补认: 第{index + 1}张 原文=[{text}] -> {key or '暂未识别'}")
            if key:
                self._hif_detail_keys[index] = key
                self._hif_detail_upgraded[index] = self._hif_title_upgraded(text, complete=True)
            # 选中牌抬起会遮住邻牌、使其被判为不可用；先取消选中，再按原手牌复核卡位。
            if not self._hif_clear_selection(context):
                break
            image = context.tasker.controller.post_screencap().wait().get()
            fresh = self._hif_frame_boxes(context, image)
            if not self._hif_same_geometry(fresh):
                self._hif_bind_hand(fresh)
                break
        return True

    def _hif_selection_visible(self, context: Context, image, boxes: list) -> bool:
        raised = any(box[1] <= 875 and box[1] + box[3] <= 1130 for box, _ in boxes)
        return (raised or self._hif_panel_top(image) is not None
                or any(self._hif_has_select(context, image, box) for box, _ in boxes))

    def _hif_clear_selection(self, context: Context) -> bool:
        """取消选中并确认连续两帧无 SELECT、详情和抬起牌；画面本就未选中时不点击。"""
        # 第0轮只看画面：首帧仍选中才点左上角；之后每次点击等满状态超时再判断。
        for attempt in range(self.HIF_CARD_CLICK_ATTEMPTS + 1):
            if attempt:
                context.tasker.controller.post_click(*self.HIF_DESELECT_POS).wait()
                retry = f" (第{attempt}次)" if attempt > 1 else ""
                logger.info(f"HIF取消选中: 点左上角 @ {self.HIF_DESELECT_POS}{retry}")
                time.sleep(0.4)
            deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
            clear = 0
            while time.time() < deadline and not context.tasker.stopping:
                image = context.tasker.controller.post_screencap().wait().get()
                boxes = self._hif_frame_boxes(context, image)
                clear = clear + 1 if boxes and not self._hif_selection_visible(context, image, boxes) else 0
                if clear >= 2:
                    self._hif_selection_index = None
                    return True
                if not attempt and not clear:
                    break
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
            if context.tasker.stopping:
                return False
        logger.warning("HIF取消选中未确认，暂不按当前手牌决策，重新识别")
        return False

    def _play_hif_card(self, context: Context, box: list) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        boxes = self._hif_frame_boxes(context, image)
        cx = box[0] + box[2] // 2
        if not self._hif_hand_geometry:
            self._hif_log_changed("play_skip", "HIF出牌: 尚无已绑定卡位，重新识别")
            return False
        index = min(range(len(self._hif_hand_geometry)), key=lambda i: abs(self._hif_hand_geometry[i] - cx))
        if abs(self._hif_hand_geometry[index] - cx) > self.CARD_HAND_X_TOLERANCE:
            self._hif_log_changed("play_skip", f"HIF出牌: 目标卡位已偏移 cx={cx}，卡位={self._hif_hand_geometry}，重新识别",
                                  signature="offset")
            return False
        if not boxes or self._hif_preview_box(boxes, index) is None:
            self._hif_log_changed("play_skip", f"HIF出牌: 当前帧手牌与已绑定卡位不一致，重新识别; "
                                  f"卡位={self._hif_hand_geometry}，当前={self._hif_geometry(boxes)}", signature="mismatch")
            self._reset_card_hand_stability()
            if boxes:
                self._hif_bind_hand(boxes)
            return False
        image = self._hif_select_card(context, image, index)
        if image is None:
            return False
        boxes = self._hif_frame_boxes(context, image)
        target = self._hif_preview_box(boxes, index, context=context, image=image)
        if target is None:
            return False
        for attempt in range(self.HIF_CARD_CLICK_ATTEMPTS):
            if not self._hif_click_card(context, target, index, "确认出牌", attempt):
                return False
            deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
            transitioned = still_selected = 0
            while time.time() < deadline and not context.tasker.stopping:
                image = context.tasker.controller.post_screencap().wait().get()
                skip = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                selected = self._hif_has_select(context, image, target)
                panel = self._hif_panel_top(image)
                changed = not (skip and skip.hit) or (panel is None and not selected)
                transitioned = transitioned + 1 if changed else 0
                still_selected = still_selected + 1 if selected and (cv2 is None or panel is not None) else 0
                if transitioned >= 2:
                    retry = f"（确认点击第{attempt + 1}次）" if attempt else ""
                    logger.info(f"HIF出牌: 第{index + 1}张已打出{retry}")
                    self._hif_log_reset("play_skip", "select_skip")
                    self._reset_hif_recognition()
                    self._wait_until_playable(context, confirmation_count=self.SKIP_STABLE_COUNT)
                    return True
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
            target = self._hif_preview_box(self._hif_frame_boxes(context, image), index, context=context, image=image)
            if still_selected < 2 or target is None or context.tasker.stopping:
                break
            if attempt == 0:
                logger.warning(f"HIF出牌: 第{index + 1}张确认点击未命中，原卡仍选中，重试一次")
        logger.warning(f"HIF出牌: 第{index + 1}张确认点击后未见画面转换，重新识别，不盲连点")
        return False

    def _reset_card_hand_stability(self) -> None:
        self._pending_card_hand = None
        self._pending_card_hand_count = 0
        self._pending_card_hand_started_at = None

    @staticmethod
    def _card_hand_snapshot(identified: list) -> tuple:
        """按横向顺序保存整手可出牌的身份与中心位置。"""
        return tuple(
            (card.get("key"), card["box"][0] + card["box"][2] // 2)
            for card in sorted(
                identified,
                key=lambda card: card["box"][0] + card["box"][2] // 2,
            )
        )

    def _same_card_hand(self, previous: tuple, current: tuple) -> bool:
        return len(previous) == len(current) and all(
            old_key == new_key
            and abs(old_x - new_x) <= self.CARD_HAND_X_TOLERANCE
            for (old_key, old_x), (new_key, new_x) in zip(previous, current)
        )

    def _confirm_card_hand(self, identified: list) -> str:
        """所有职业共用：整手牌连续稳定后才进入出牌决策。"""
        hand = self._card_hand_snapshot(identified)
        now = time.time()
        if self._pending_card_hand_started_at is None:
            self._pending_card_hand_started_at = now

        if self._pending_card_hand is not None and self._same_card_hand(
            self._pending_card_hand, hand
        ):
            self._pending_card_hand_count += 1
        else:
            self._pending_card_hand_count = 1
        self._pending_card_hand = hand

        if self._pending_card_hand_count >= self.CARD_HAND_STABLE_COUNT:
            # 两帧稳定的名字身份与详情身份共用本手缓存，邻牌被遮挡时不重复补认。
            self._hif_detail_keys.update({index: card["key"] for index, card in enumerate(identified)
                                          if card.get("key")})
            self._hif_detail_upgraded.update({index: card["upgraded"] for index, card in enumerate(identified)
                                              if card.get("upgraded") is not None})
            names = [self._card_label(key) for key, _ in hand]
            waited = now - self._pending_card_hand_started_at
            # 不逐帧记录待稳定手牌；等待明显偏长时说明期间识别在变化（漏框、遮挡等）。
            note = f"（等待{waited:.1f}秒才稳定）" if waited >= 2 else ""
            self._hif_log_changed("hand", f"HIF手牌: 整手牌识别已稳定 {names}{note}", signature=(self._last_turn_count, names))
            self._reset_card_hand_stability()
            return "stable"
        if now - self._pending_card_hand_started_at >= self.TIME_OUT:
            logger.warning(
                f"HIF手牌: 整手牌连续{self.TIME_OUT:.0f}秒未稳定，"
                f"采用最后一次识别: {[self._card_label(key) for key, _ in hand]}"
            )
            self._reset_card_hand_stability()
            return "stable"
        return "wait"

    @staticmethod
    def _iou(a: list, b: list) -> float:
        """两框交集/并集(IOU)。"""
        ax0, ay0, aw, ah = a
        bx0, by0, bw, bh = b
        ax1, ay1 = ax0 + aw, ay0 + ah
        bx1, by1 = bx0 + bw, by0 + bh
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

    def _identify_card(self, context: Context, image, box: list, label_right: Optional[int] = None):
        """
        识别单张手牌；HIF 只读卡名，未识别时交给详情补认。

        HIF 禁用卡面模板兜底，避免匹配到重叠的邻牌。label_right 为右侧邻牌左缘，卡名只读到此处。

        Returns:
            (key, score): 命中返回其key与0.0；未命中返回 (None, 0.0)。
        """
        if not self._hif_name_index:
            return None, 0.0
        # 逐张结果由整手牌稳定日志汇总；卡名未识别的卡由详情补认记录原文。
        name_key = self._identify_by_name(context, image, box, label_right=label_right)
        return (name_key, 0.0) if name_key else (None, 0.0)

    @staticmethod
    def _hif_label_right(box: list, results: list) -> Optional[int]:
        """右侧最近邻牌（含不可用牌）的左缘；手牌重叠时它盖住本牌卡名的后半段。"""
        cx = box[0] + box[2] // 2
        edges = [r.box[0] for r in results or []
                 if getattr(r, "label", None) in ("cards", "suggestions", "useless") and getattr(r, "score", 0) >= 0.5
                 and r.box[1] >= 800 and 180 <= r.box[3] <= 330 and r.box[0] + r.box[2] // 2 > cx + 40
                 and box[0] < r.box[0] < box[0] + box[2]]
        return min(edges) if edges else None

    @staticmethod
    def _hif_full_name_visible(image, box: list, roi: list, result) -> bool:
        """无强化符号的短名须居中且末尾留白，避免把截断的长名当成完整短名。"""
        text_box = getattr(result, "box", None)
        if cv2 is None or image is None or getattr(image, "shape", ())[:2] != (1280, 720) or not text_box:
            return False
        x, y, w, h = text_box
        right = x + w
        # 720×1280 下卡面宽190；检测框常只覆盖其左侧可见部分。
        if (x < roi[0] + 4 or right > roi[0] + roi[2] - 12
                or abs(x + w / 2 - (box[0] + 95)) > 12
                or not roi[1] <= y + h / 2 <= roi[1] + roi[3] or w <= 0 or h <= 4):
            return False
        blank = image[y + 2:y + h - 2, right + 2:right + 12]
        return blank.size > 0 and min(cv2.mean(blank)[:3]) > 225

    def _identify_by_name(self, context: Context, image, box: list, label_right: Optional[int] = None):
        """OCR 卡名条识别卡牌；临时强化后卡名带 "+"，按前缀唯一匹配。"""
        try:
            # 名字居中；优先读可见整宽、避开上一行属性。建议框的光晕可延伸到牌底之外。
            # 手牌重叠时首个区域只读到右侧邻牌左缘，避免把邻牌卡名开头连进本牌卡名。
            bottom = min(box[1] + box[3], 1134)
            width = box[2]
            if label_right is not None and label_right - box[0] - 2 < width:
                width = max(label_right - box[0] - 2, 40)
            clipped = width < box[2]
            rois = [[max(0, box[0]), bottom - 32, width, 32],
                    [max(0, box[0]), box[1] + box[3] - 52, max(int(box[2] * 0.75), 78), 58]]
            for roi in rois:
                reco = context.run_recognition("ProduceHIF__ProduceIdentityName", image, pipeline_override={
                    "ProduceHIF__ProduceIdentityName": {"recognition": "OCR", "roi": roi, "expected": r"[^\n]+"}})
                texts = [r.text or "" for r in (reco.filtered_results if reco and reco.hit else [])]
                keys = {self._hif_name_key(text, partial=True) for text in texts}
                keys.discard(None)
                if roi is rois[0] and reco and reco.hit:
                    for result in reco.filtered_results:
                        exact = self._hif_name_key(result.text or "")
                        if exact and exact not in keys and self._hif_full_name_visible(image, box, roi, result):
                            keys.add(exact)
                if roi is rois[0] and clipped and not keys:
                    # 邻牌左缘常切掉末字的一部分（如「コ」读成「ニ」）；截断名去掉末字再按前缀唯一匹配。
                    trimmed = {self._hif_name_key(text[:-1], partial=True) for text in texts
                               if len(self._hif_card_name(text)) >= 4 and not text.rstrip().endswith("+")}
                    trimmed.discard(None)
                    if len(trimmed) == 1:
                        self._hif_last_upgraded = None
                        return next(iter(trimmed))
                if len(keys) == 1:
                    key = next(iter(keys))
                    titles = [r for r in reco.filtered_results if self._hif_name_key(r.text or "") == key]
                    if titles:
                        result = titles[0]
                        self._hif_last_upgraded = self._hif_title_upgraded(result.text or "", complete=(
                            roi is rois[0] and self._hif_full_name_visible(image, box, roi, result)))
                    return key
                if len(keys) > 1:
                    return None
            return None
        except Exception as exc:
            logger.warning(f"HIF手牌名字OCR异常: {exc!r}")
            return None

    def _read_active_pointer_state(self, context: Context, image) -> Optional[str]:
        """识别唯一的当前状态指针；右侧下回合提示图标不参与判断。"""
        for state, template in self.PROFESSION_POINTER_TEMPLATES.items():
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceIdentityMatch",
                    image,
                    pipeline_override={
                        "ProduceHIF__ProduceIdentityMatch": {
                            "recognition": "TemplateMatch",
                            "template": template,
                            "roi": self.PROFESSION_POINTER_ROI,
                            "threshold": self.PROFESSION_POINTER_THRESHOLD,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF{state}指针识别异常: {e!r}")
                continue
            if reco and reco.hit:
                return state
        return None

    def _read_full_power_value(self, context: Context, image) -> Optional[int]:
        """读取当前指针右侧灰色框中的全力值；失败时不触发动态提权。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.FULL_POWER_VALUE_ROI,
                        "expected": r"\d+",
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF全力值识别异常: {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        values = [
            int(value)
            for result in reco.filtered_results
            for value in re.findall(r"\d+", result.text or "")
        ]
        return max(values) if values else None

    def _play_fallback(self, context: Context, image, results) -> None:
        """兜底出牌：按 fallback 配置跟系统建议或打最高分牌；受组合留卡或使用限制时跳过回合。"""
        suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)
        if self.fallback == "suggestion" and suggestions > 0:
            box = suggestions_box
        else:
            box = best_box

        if box and box[2] > 1 and self._in_range(box):
            key, _ = self._identify_card(context, image, box)
            if self._followups is not None and (self._followups.active or not self._followups.ordinary_allowed(key)):
                logger.info('HIF兜底出牌: 候选受组合留卡限制，不单独出牌，跳过回合')
                self._skip_round(context)
                return
            if not self._allowed_by_use_conditions(key):
                logger.info(f"HIF兜底出牌: 候选「{key}」未满足卡牌使用限制，跳过回合")
                self._skip_round(context)
                return
            kind = "系统建议牌" if box is suggestions_box else "最高分牌"
            logger.info(f"HIF兜底出牌: 打出{kind}「{key or '暂未识别'}」")
            self._play_a_card(context, box)
            return

        logger.warning("HIF兜底出牌: 无可用牌，跳过回合")
        self._skip_round(context)

    def _skip_round(self, context: Context) -> None:
        """空过：跳过本回合；组合等待中记录空过前回合，由回合变化确认空过计数。"""
        reason = self._hif_skip_reason or "策略等待"
        self._reset_hif_recognition()
        self._reset_card_hand_stability()
        logger.info(f"HIF跳过回合: {reason}")
        if self._followups is not None and self._followups.active and self._last_turn_count is not None:
            self._followup_skip_turn = self._last_turn_count
            self._followup_skip_deadline = _hif_recovery.now() + self.TIME_OUT
        context.run_task("ProduceHIF__ProduceRecognitionSkipRound")
        self._wait_until_playable(context)

    def _play_a_card(self, context: Context, box: list) -> bool:
        """
        选中并确认打出一张手牌；只有观察到画面转换才计为已出牌。

        Args:
            context: maa的Context类
            box: 卡牌范围，格式为[x, y, w, h]（x、y为左上角的坐标）

        Returns:
            bool: 已确认打出返回True；未确认时返回False，由主循环重新识别，不盲连点。
        """
        played = self._play_hif_card(context, box)
        if played:
            self._battle_confirmed_plays = getattr(self, '_battle_confirmed_plays', 0) + 1
        return played

    def _handle_move_cards(self, context: Context, image=None) -> bool:
        """处理移动/保留技能卡界面；按来源决定选卡名单，卡组超过一屏时滚动扫描。

        - 出牌编排的起始卡前置/后置饮料与等待用饮：按后续候选顺序选，找不到选首格。
        - 全力/強気出牌或「ほっと緑茶」：按面板「保留」名单选；名单为空或未找到时选首格。
          「本領発揮+」可选两张，名单非空但都未找到时点取消。
        - 其他（其他职业、初星黒酢的普通时机等）：沿用找「脚光」，找不到选首格。

        Returns:
            bool: 已处理移动界面返回True；无移动界面或无法确认选卡时返回False。
        """
        if image is None:
            image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionChooseMoveCards", image)
        if not (reco_detail and reco_detail.hit):
            # 出牌后会先经过一两帧非保留页；不能在这里清空来源。
            return False
        drink, self._move_drink_source = self._move_drink_source, None
        try:
            if self._followup_move_targets is not None:
                return self._select_move_cards(context, image, list(self._followup_move_targets), True, "HIF组合检索")
            if drink == self.HOLD_GREEN_TEA or (drink is None and self.profession in ("全力", "強気")):
                source, self._hold_source = drink or self._hold_source or "未知来源", None
                ordered = self.hold_priority_ordered
                label = f"HIF保留({source}，{'按优先级' if ordered else '先找到先保留'})"
                if source == self.HOLD_MULTI_SOURCE:
                    return self._select_move_cards(context, image, self.hold_priority_keys, ordered, label,
                                                   picks=2, multi=True)
                return self._select_move_cards(context, image, self.hold_priority_keys, ordered, label)
            return self._select_move_cards(context, image, [self.MOVE_TARGET_CARD], True, "移动界面")
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF移动界面异常: {e!r}")
            return False

    def _move_name(self, text: Optional[str]) -> str:
        return self._hif_card_name(text).replace("腳", "脚")

    def _select_move_cards(self, context: Context, image, targets: list, ordered: bool, label: str,
                           picks: int = 1, multi: bool = False) -> bool:
        """扫描移动页卡组并选卡后移动。

        ordered=True 时按名单顺序取最靠前的；False 时扫描中先找到的即选，找够即停。
        multi=True（本領発揮）点一次只看详情，再点一次才选中；名单非空但都未找到时点取消。
        """
        keys = []
        for target in targets:
            key = self._move_name(target)
            if key and key not in keys:
                keys.append(key)
        self._move_low_layout = self._move_has_panel(image)
        self._move_offset = 0
        self._move_last_tap = None
        found = {}  # 名单顺位 -> (位置, 标题)；同名只记首次出现

        def on_card(title, location) -> bool:
            rank = keys.index(self._move_name(title)) if self._move_name(title) in keys else None
            if rank is not None and rank not in found:
                found[rank] = (location, title)
                logger.info(f"{label}: 找到名单第{rank + 1}位「{title}」")
            if not ordered:
                return len(found) >= picks
            return all(rank in found for rank in range(min(picks, len(keys))))

        if keys:
            logger.info(f"{label}: 名单={keys}，扫描卡组")
            if not self._scan_move_cards(context, on_card, label):
                return False
        chosen = [found[rank] for rank in (sorted(found) if ordered else found)][:picks]
        if not chosen:
            if multi and keys:
                logger.info(f"{label}: 名单卡都未找到 → 点取消 @ {self.HOLD_CANCEL_POS}")
                context.tasker.controller.post_click(*self.HOLD_CANCEL_POS).wait()
                time.sleep(0.8)
                return True
            first = (0, self.MOVE_GRID[0] if not self._move_low_layout else (self.MOVE_COLUMN_X[0], self.MOVE_ROW0_Y))
            logger.info(f"{label}: {'名单为空' if not keys else '名单卡都未找到'} → 选首格")
            pos = self._move_ensure_visible(context, first)
            if pos is None:
                return False
            self._move_tap(context, pos, first)
            if multi:
                self._move_tap(context, pos, first)
            time.sleep(0.8)
            return self._submit_move(context, label)
        for location, title in chosen:
            pos = self._move_ensure_visible(context, location)
            if pos is None:
                logger.warning(f"{label}: 无法把「{title}」滚回可点范围，不移动")
                return False
            if not multi or self._move_last_tap != location:
                self._move_tap(context, pos, location)
            read = self._read_move_title_stable(context)
            if self._move_name(read) != self._move_name(title):
                logger.warning(f"{label}: 再次点选后卡名不符=[{read}]，期望「{title}」，不移动")
                return False
            if multi:
                self._move_tap(context, pos, None)
                time.sleep(0.8)
            logger.info(f"{label}: 选择「{title}」")
        if not multi and not self._wait_for_move_enabled(context):
            return False
        return self._submit_move(context, label)

    def _submit_move(self, context: Context, label: str) -> bool:
        """点「移動」并确认已离开移动页；仍停在移动页且按钮可用时补点。

        2026-11-12 日志：初星黒酢选中「脚光+」后点击「移動」被游戏忽略，页面一直停留，等待用饮返回 90 秒后安全停止。
        只在移动页标题与「移動」按钮同时可见时补点，避免点到战斗页。
        """
        for attempt in range(1, self.MOVE_SUBMIT_ATTEMPTS + 1):
            if attempt > 1:
                logger.warning(f"{label}: 点击「移動」后仍停留在移动页，第{attempt}次点击")
            context.run_task("ProduceHIF__ProduceMoveCards")
            deadline = time.time() + self.MOVE_SUBMIT_CONFIRM_TIMEOUT
            image = None
            while time.time() < deadline:
                if context.tasker.stopping:
                    return False
                time.sleep(self.MOVE_SUBMIT_POLL)
                image = context.tasker.controller.post_screencap().wait().get()
                page = context.run_recognition("ProduceHIF__ProduceRecognitionChooseMoveCards", image)
                if not (page and page.hit):
                    return True
            button = context.run_recognition("ProduceHIF__ProduceMoveCards", image) if image is not None else None
            if not (button and button.hit):
                logger.warning(f"{label}: 仍停留在移动页但「移動」按钮不可用，不再点击")
                return False
        logger.warning(f"{label}: 点击「移動」{self.MOVE_SUBMIT_ATTEMPTS}次后仍停留在移动页")
        return False

    def _move_tap(self, context: Context, pos, location) -> None:
        context.tasker.controller.post_click(*pos).wait()
        self._move_last_tap = location

    def _move_has_panel(self, image) -> bool:
        """首行上方探测带为浅灰空详情板时，卡组首行在 y≈569 且可滚动；无法判断时按当前版式处理。"""
        if getattr(image, "shape", None) != (1280, 720, 3):
            return True
        x0, y0, x1, y1 = self.MOVE_PANEL_PROBE
        band = image[y0:y1, x0:x1]
        return float(band.mean()) >= 200 and float(band.std()) <= 6

    def _read_move_title_stable(self, context: Context) -> Optional[str]:
        """点卡后等标题连续读到相同卡名，避免读到上一张的残留详情；超时返回最后读到的值。"""
        deadline = time.time() + self.MOVE_NAME_TIMEOUT
        last, count = None, 0
        while time.time() < deadline and not context.tasker.stopping:
            time.sleep(self.CARD_HAND_POLL_INTERVAL)
            image = context.tasker.controller.post_screencap().wait().get()
            title = self._read_hif_move_card_title(context, image)
            if title and title == last:
                count += 1
            else:
                last, count = title, 1 if title else 0
            if count >= self.MOVE_NAME_STABLE_COUNT:
                break
        return last

    @classmethod
    def _move_cell_selected(cls, image, pos) -> Optional[bool]:
        """卡格四角是否有橙色选中框；截图不可用时返回 None。"""
        if cv2 is None or getattr(image, "shape", None) != (1280, 720, 3):
            return None
        x, y = pos
        c, h = cls.MOVE_FRAME_CORNER, cls.MOVE_FRAME_PATCH
        corners = 0
        for dx in (-c, c):
            for dy in (-c, c):
                patch = cv2.cvtColor(image[y + dy - h:y + dy + h, x + dx - h:x + dx + h], cv2.COLOR_BGR2HSV)
                orange = (patch[..., 0] >= 5) & (patch[..., 0] <= 22) & (patch[..., 1] >= 150) & (patch[..., 2] >= 200)
                corners += int(orange.sum()) > 15
        return corners >= cls.MOVE_FRAME_MIN_CORNERS

    def _move_cell_empty(self, context: Context, pos) -> bool:
        """点格后选中框不在该格即为空位；截图不可用时不判空。"""
        return self._move_cell_selected(self._move_list_image(context), pos) is False

    def _scan_move_cards(self, context: Context, on_card, label: str) -> bool:
        """逐格点卡读标题，交给 on_card(标题, 位置)；返回 True 时停止。被停止任务时返回 False。"""
        previous = []

        def scan(location, pos) -> Optional[bool]:
            if context.tasker.stopping:
                return None
            self._move_tap(context, pos, location)
            title = self._read_move_title_stable(context)
            # 卡组按行排满，末行空位之后不会再有卡；此时不再下滑，免得背景动画让位移无法确认。
            if self._move_low_layout and title and previous and title == previous[-1] and self._move_cell_empty(context, pos):
                logger.info(f"{label}: 偏移{location[0]} {pos} 为空位，卡组已到底")
                return True
            previous.append(title)
            logger.info(f"{label}: 偏移{location[0]} {pos} 卡名=[{title or '未读到'}]")
            return bool(title) and on_card(title, location)

        if not self._move_low_layout:
            for pos in self.MOVE_GRID:
                result = scan((0, pos), pos)
                if result is None:
                    return False
                if result:
                    return True
            return True
        scanned = set()
        for _ in range(self.MOVE_MAX_SCROLLS + 1):
            for row, y in self._move_visible_rows():
                if row in scanned:
                    continue
                scanned.add(row)
                for x in self.MOVE_COLUMN_X:
                    result = scan((self._move_offset, (x, y)), (x, y))
                    if result is None:
                        return False
                    if result:
                        return True
            delta = self._move_scroll(context, 1)
            if not delta:
                break
        return True

    def _move_visible_rows(self) -> list:
        low, high = self.MOVE_ROW_CLICK_RANGE
        rows = []
        for row in range(self.MOVE_MAX_ROWS):
            y = self.MOVE_ROW0_Y + row * self.MOVE_ROW_PITCH - self._move_offset
            if low <= y <= high:
                rows.append((row, y))
        return rows

    def _move_list_image(self, context: Context):
        image = context.tasker.controller.post_screencap().wait().get()
        return image if getattr(image, "shape", None) == (1280, 720, 3) else None

    def _move_settled_image(self, context: Context):
        deadline = time.time() + self.MOVE_SCROLL_SETTLE_TIMEOUT
        last = None
        while time.time() < deadline:
            time.sleep(self.CARD_HAND_POLL_INTERVAL)
            image = self._move_list_image(context)
            if image is None:
                return None
            if last is not None and common._hif_list_still(last, image, self.MOVE_LIST_VIEW):
                return image
            last = image
        return last

    def _move_swipe(self, context: Context, direction: int) -> bool:
        start, end = self.MOVE_SWIPE_SPAN if direction > 0 else self.MOVE_SWIPE_SPAN[::-1]
        job = context.tasker.controller.post_swipe(self.MOVE_SWIPE_X, start, self.MOVE_SWIPE_X, end,
                                                   self.MOVE_SWIPE_DURATION).wait()
        return getattr(job, "succeeded", True) is not False

    def _move_scroll(self, context: Context, direction: int) -> Optional[int]:
        """direction=1 看下方、-1 回上方；返回实测位移，0 表示到头，None 表示无法确认（已尝试回到顶部）。"""
        if self._move_offset is None or context.tasker.stopping:
            return None
        before = self._move_list_image(context)
        if before is None or not self._move_swipe(context, direction):
            return None
        after = self._move_settled_image(context)
        delta = common._hif_list_scroll_shift(before, after, direction, self.MOVE_LIST_VIEW)
        if delta is None:
            logger.warning("HIF移动界面: 无法确认卡组滚动位移，回到顶部重新定位")
            self._move_recover_top(context)
            return None
        self._move_offset = 0 if delta == 0 and direction < 0 else self._move_offset + delta
        logger.info(f"HIF移动界面: 卡组{'下' if direction > 0 else '上'}滑，实测位移={delta}，当前偏移={self._move_offset}")
        return delta

    def _move_recover_top(self, context: Context) -> None:
        """位移无法确认时上滑到列表不再移动，以顶部为基准；仍无法确认时不再按坐标点卡。"""
        for _ in range(self.MOVE_MAX_SCROLLS + 2):
            before = self._move_list_image(context)
            if before is None or context.tasker.stopping or not self._move_swipe(context, -1):
                break
            after = self._move_settled_image(context)
            if after is not None and common._hif_list_still(before, after, self.MOVE_LIST_VIEW):
                self._move_offset = 0
                return
        self._move_offset = None

    def _move_ensure_visible(self, context: Context, location) -> Optional[tuple]:
        """把扫描时记下的卡格滚回可点范围，返回当前坐标；无法确认时返回 None。"""
        offset, (x, y) = location
        low, high = self.MOVE_ROW_CLICK_RANGE
        for _ in range(self.MOVE_MAX_SCROLLS + 1):
            if self._move_offset is None:
                return None
            current = y + offset - self._move_offset
            if offset == self._move_offset or (self._move_low_layout and low <= current <= high):
                return x, current
            if not self._move_scroll(context, 1 if current > high else -1):
                if self._move_offset is not None and offset == self._move_offset:
                    return x, y
                return None
        return None

    def _wait_for_move_enabled(self, context: Context) -> bool:
        """等待「移动」按钮连续可用，确认点选已生效。"""
        deadline = time.time() + self.MOVE_ENABLED_TIMEOUT
        stable_count = 0
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition("ProduceHIF__ProduceMoveCards", image)
            if reco and reco.hit:
                stable_count += 1
                if stable_count >= self.MOVE_ENABLED_STABLE_COUNT:
                    return True
            else:
                stable_count = 0
            time.sleep(self.MOVE_ENABLED_POLL)
        logger.warning("HIF移动界面: 点选后移动按钮未稳定可用，不执行移动")
        return False

    def _read_hif_move_card_title(self, context: Context, image) -> Optional[str]:
        """只读取 HIF 移动页标题；非目标卡也返回卡名，None 仅表示未读到。"""
        try:
            left, top, width, height = self.HIF_MOVE_NAME_ROI
            # 只把标题裁图交给 OCR；不回退到宽详情区识别效果文字。
            title_image = image[top:top + height, left:left + width].copy()
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", title_image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": [0, 0, width, height],
                        "expected": self.MOVE_OCR_ALL,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF移动界面: 标题OCR异常 {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        # 逐帧读名不写日志；调用方在读稳后记录卡名。
        text = "".join(r.text or "" for r in sorted(reco.filtered_results, key=lambda r: r.box[0]))
        return text if text.strip() else None

    def _match_move_card_name(self, context: Context, image) -> Optional[str]:
        """识别移动页是否为脚光；完整匹配标题，不使用效果或相似度兜底。"""
        text = self._read_hif_move_card_title(context, image)
        return self.MOVE_TARGET_CARD if self._is_move_target(text) else None

    def _is_move_target(self, title: Optional[str]) -> bool:
        return self._hif_card_name(title).replace("腳", "脚") == self.MOVE_TARGET_CARD

    @staticmethod
    def _is_battle_end(context: Context, image=None, consecutive: list = None) -> bool:
        """
        反向判定：是否已到战斗结束（结算界面）。

        战斗中的信号（数字环随回合数变、体力条随血量变、右上绿心被卡牌特性挡、
        SKIP 会消失）都不稳定，不宜用"检测到即战斗继续"。改用反向——
        检测到"结算界面"（橘色「次へ」按钮 / 排名榜）才视为战斗结束。
        次へ按钮只在结算界面出现（战斗画面实测 0.22~0.26，恒 miss），方向反转最稳定。

        为防单帧误命中（战斗动画闪光/过渡帧偶然匹配到类似橘色元素），
        要求"次へ模板"连续命中 N 次才判战斗结束；中途 miss 立即清零重计。

        Args:
            consecutive: 传入一个长度1的 list 用作累计计数（list 可变对象可跨帧保存）。

        Returns:
            bool: True=战斗已结束（进入结算），False=仍在出牌场景。
        """
        if image is None:
            image = context.tasker.controller.post_screencap().wait().get()

        # 结算界面专属：橘色「次へ」按钮（produce/hif_result_next.png，结算图实测 hit=1.0）。
        reco = context.run_recognition(
            "ProduceHIF__ProduceRecognitionBattleEnd",
            image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionBattleEnd": {
                    "recognition": "TemplateMatch",
                    "template": "hif/produce/hif_result_next.png",
                    "roi": [220, 1080, 360, 120],
                    "threshold": 0.7,
                }
            },
        )
        _score = 0.0
        if reco and reco.best_result:
            _score = getattr(reco.best_result, "scoring", None)
            if _score is None:
                _score = getattr(reco.best_result, "score", 0.0) or 0.0
            _score = _score or 0.0
        hit = bool(reco and reco.hit)

        # 连续计数：consecutive 为 [n] 形式传入；用尾部计数避免误退出
        need = BattleRecognitionMixin.BATTLE_END_STABLE
        if consecutive is None:
            consecutive = [0]
        if hit:
            consecutive[0] += 1
        else:
            consecutive[0] = 0
        # 只在连续命中达标时记一行；战斗期间每帧 miss 与单帧命中都不记录。
        if hit and consecutive[0] == need:
            logger.info(f"HIF战斗: 结算次へ连续{need}帧命中(score={_score:.3f})，判定战斗结束")
        return consecutive[0] >= need

    def _skip_battle_end_animation(self, context: Context, image) -> None:
        """最后一回合的战斗 HUD 稳定消失后，点击可跳过的结算过场。"""
        if self._last_turn_count != 1:
            return
        # 退出战斗的联网阶段可出现通信错误；不重启出牌动作或清空战斗状态。
        if common._hif_retry_network(context, image):
            self._battle_transition_misses = 0
            return
        # 出牌效果与分数结算期间点击无效，可跳过的过场稍后才开始；配额需覆盖整段过场。
        if self._battle_transition_taps >= self.BATTLE_TRANSITION_MAX_TAPS:
            return
        title = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore",
            image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR",
                    "expected": "残りターン",
                    "roi": [0, 0, 150, 45],
                }
            },
        )
        self._battle_transition_misses = 0 if title and title.hit else self._battle_transition_misses + 1
        now = time.monotonic()
        if self._battle_transition_misses < 3 or now - self._last_battle_transition_tap < 2:
            return
        self._battle_transition_misses = 0
        self._battle_transition_taps += 1
        self._last_battle_transition_tap = now
        logger.info(f"HIF最后一回合过场: 战斗HUD已消失，点击跳过 ({self._battle_transition_taps}/{self.BATTLE_TRANSITION_MAX_TAPS})")
        context.tasker.controller.post_click(360, 640).wait()

    @classmethod
    def _looks_like_turn_seven(cls, image) -> bool:
        """OCR 返回1时，以白色笔画形状复核是否实际为7。

        同款字体中，1的底部横笔最宽，而7的顶部横笔宽、下部仅剩窄斜笔。
        此判定仅用于1/7纠错，不改变9/12或额外加回合的合法范围。
        """
        if cv2 is None or image is None:
            return False
        x, y, w, h = cls.TURN_COUNT_ROI
        region = image[y:y + h, x:x + w]
        if region.size == 0:
            return False
        white = cv2.inRange(region, np.array([205, 205, 205]), np.array([255, 255, 255]))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(white)
        components = []
        for index in range(1, count):
            left, top, width, height, area = (int(value) for value in stats[index])
            if area >= 40 and height >= 18:
                components.append((area, index, left, top, width, height))
        if not components:
            return False
        _, index, left, top, width, height = max(components)
        component = labels == index
        band = max(1, height // 4)
        top_pixels = int(component[top:top + band, left:left + width].sum())
        bottom_pixels = int(component[top + height - band:top + height, left:left + width].sum())
        return top_pixels > bottom_pixels * 1.35

    @classmethod
    def _read_extra_turn_count(cls, context: Context, image) -> Optional[int]:
        """读取尚未转入倒计时的 +N；0 表示标记不存在，None 表示无法确认。"""
        if not isinstance(image, getattr(np, 'ndarray', ())) or image.ndim != 3:
            return None
        x, y, w, h = cls.EXTRA_TURN_ROI
        crop = image[y:y + h, x:x + w, :3]
        if crop.shape != (h, w, 3):
            return None
        white = np.all(crop > 225, axis=2)
        blue = (crop[:, :, 0] > 170) & (crop[:, :, 1] > 130) & (crop[:, :, 2] < 110)
        # 同时要求白底和蓝色字，避免识别环、分数或天空背景中的数字。
        if white.mean() < 0.35 or blue.sum() < 12:
            return 0
        try:
            reco = context.run_recognition('ProduceHIF__ProduceExtraTurnNumber', image,
                pipeline_override={'ProduceHIF__ProduceExtraTurnNumber': {
                    'recognition': 'OCR', 'roi': cls.EXTRA_TURN_ROI,
                    'expected': r'^\s*[+＋]\s*[1-9]\d?\s*$', 'only_rec': True, 'threshold': 0.75}})
            if not (reco and reco.hit and reco.filtered_results):
                return None
            text = ''.join(r.text or '' for r in sorted(reco.filtered_results, key=lambda r: r.box[0]))
            match = re.fullmatch(r'\s*\+\s*([1-9]\d?)\s*', unicodedata.normalize('NFKC', text))
            return int(match.group(1)) if match else None
        except Exception as error:
            logger.debug(f'HIF额外回合标记未确认: {error!r}')
            return None

    def _read_turn_count(self, context: Context, image) -> Optional[int]:
        """读左上角「残りターン」环中心的剩余回合数，并修正1/7及异常跳变。

        环从不显示 0。普通计数允许变化1；1回合结束后，+N消失且环变为N时，
        单独确认额外回合转入，允许该跳变；额外回合不与新倒计时重复相加。
        """
        self._extra_turn_transition = False
        rollover_seen = self._extra_rollover_seen
        self._extra_rollover_seen = None
        extra = self._read_extra_turn_count(context, image)
        previous_extra = self._extra_rollover_reserve if self._extra_rollover_reserve is not None else self._pending_extra_turns
        self._pending_extra_turns = extra  # 储备标记独立更新，倒计时OCR失败也不能放行受限卡
        self._extra_turn_read_confirmed = extra is not None
        if self._last_turn_count == 1 and previous_extra and extra in (0, None):
            self._extra_rollover_reserve = previous_extra
            self._extra_turn_read_confirmed = False  # 待连续确认储备已转入倒计时
        elif extra is not None:
            self._extra_rollover_reserve = None
        try:
            # 固定数字区已经定位；省去文字检测，避免4被裁成窄框后识别成X。
            primary = {"recognition": "OCR", "expected": r"^[1-9]\d?$", "roi": self.TURN_COUNT_ROI,
                       "only_rec": True, "threshold": 0.9}
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": primary
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF读残りターン环异常: {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            # 主读法失败后，用另一种 OCR 裁剪方式补读；不沿用旧回合数猜测。
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore", image,
                    pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.TURN_COUNT_ROI,
                        "expected": r"^[1-9]\d?$", "only_rec": False, "threshold": 0.3,
                    }},
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF剩余回合OCR复核异常: {e!r}")
                return None
            if not (reco and reco.hit and reco.filtered_results):
                return None
            logger.info(f"HIF剩余回合OCR复核: 直接读取为空，文字检测补读={[result.text for result in reco.filtered_results]}")
        candidates = []
        digit_parts = []
        results = sorted(
            reco.filtered_results,
            key=lambda result: result.box[0] if getattr(result, "box", None) else 0,
        )
        for result in results:
            text = result.text or ""
            candidates.extend(int(m.group(0)) for m in re.finditer(r"\d{1,2}", text))
            digit_parts.extend(re.findall(r"\d", text))
        # OCR 偶尔把12/11拆成两个结果；按水平顺序拼回完整两位数。
        if len(digit_parts) == 2:
            candidates.append(int("".join(digit_parts)))
        candidates = [turn for turn in candidates if turn >= 1]
        if not candidates:
            return None

        if set(candidates) == {1} and self._looks_like_turn_seven(image):
            logger.info("HIF剩余回合OCR纠错: OCR=1，白色笔画顶部宽、底部窄 → 7")
            candidates = [7]

        previous = self._last_turn_count
        rollover = (previous == 1 and extra == 0 and previous_extra is not None
                    and previous_extra > 0 and previous_extra in candidates)
        if previous is None:
            # 若 OCR 将两位数拆成多个候选，首次读取优先采用较大的完整数值。
            turn = max(candidates)
        elif rollover:
            if rollover_seen != previous_extra:
                self._extra_rollover_seen = previous_extra
                self._extra_turn_read_confirmed = False
                return None  # 两帧确认标记消失及新倒计时，避免动画中间帧误判
            turn = previous_extra
            self._extra_turn_transition = True
            self._extra_turn_read_confirmed = True
            self._extra_rollover_reserve = None
            logger.info(f'HIF额外回合转入: 剩余1回合结束，+{turn}转为倒计时{turn}')
        else:
            plausible = [turn for turn in candidates if abs(turn - previous) <= self.TURN_COUNT_MAX_STEP]
            if not plausible:
                logger.info(
                    f"HIF剩余回合OCR跳变: 上次={previous}, 本次={candidates}，"
                    "超出单回合允许变化范围，忽略本次结果"
                )
                return None
            turn = min(plausible, key=lambda value: abs(value - previous))

        self._last_turn_count = turn
        self._extra_rollover_seen = None
        if extra is not None:
            self._pending_extra_turns = extra
        return turn

    def _wait_until_playable(self, context: Context, confirmation_count=1):
        """
        等待直到处于可出牌状态

        Args:
            context: maa的Context类
            confirmation_count：重复核对的次数，用来应对识别对象一闪而过的假True情况（主要存在于喝饮料的时候）

        Returns:
            bool: 处于出牌场景时，返回True；不处于出牌场景时，返回False
        """
        count_playable = 0
        count_exit = 0
        deadline = time.monotonic() + self.TIME_OUT
        while True:
            if context.tasker.stopping:
                return False
            if time.monotonic() >= deadline:
                logger.warning("HIF等待可出牌状态超过15秒，结束本次等待，交回状态识别")
                return False
            image = context.tasker.controller.post_screencap().wait().get()

            # 通过跳过回合按钮检测是否处于可出牌状态。
            # 特效动画期 skip 按钮会被特效遮盖/未稳定出现，故"连续"命中 N 次（非累计）
            # 才认为是真正的可出牌状态，从而滤掉出牌后特效期的动画帧（避免手牌漏框）。
            reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
            if reco_detail and reco_detail.hit:
                count_playable += 1
                if count_playable >= confirmation_count:
                    return True
            else:
                count_playable = 0  # 未连续命中：一 miss 即清零，确保是连续稳定

            # 检测血条是否存在，如连续n次检查不到血条，则认为已退出出牌场景
            # 如果识别时间太长，2次就够了，主要避免CLEAR效果遮住血条的情况
            # 如果识别时间太短，导致提前出函数，CLEAR转PERFECT的那一回合出牌计时会差很远。如果出现这种情况，就设置为3次
            reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionHealthFlag", image)
            if not (reco_detail and reco_detail.hit):
                count_exit += 1
                if count_exit >= 2:
                    return False
            else:
                count_exit = 0

            if context.tasker.stopping:
                return False

            # 解决莫名其妙的误触问题
            reco_detail = context.run_recognition("ProduceHIF__ProduceButton", image)
            if reco_detail and reco_detail.hit:
                context.run_task("ProduceHIF__ProduceButton")

            # 处理移动卡片界面
            if self._handle_move_cards(context, image):
                count_playable = 0
                count_exit = 0

            # 检测任务中止的情况，防止卡死，检测成功时返回False
            if context.tasker.stopping:
                return False

            # 睡一会
            time.sleep(1)
