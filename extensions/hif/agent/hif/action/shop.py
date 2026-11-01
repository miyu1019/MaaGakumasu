"""HIF 商店与定制：相谈商店（购饮/删卡）与中场技能卡定制。"""
import os
import re
import json
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
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from extensions.hif.recovery import state as _hif_recovery

from . import common, drinks
from .deck import ProduceHIF__ProduceHIFCardSwapAuto, ProduceHIF__ProduceHIFCardDeleteAuto
from .battle import ProduceHIF__ProduceCardsAuto
from .common import _hif_date_interrupt
from .drinks import ProduceHIF__ProduceHIFDrinkAuto


@AgentServer.custom_action("ProduceHIF__ProduceHIFConsultAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFConsultAuto(CustomAction):
    """HIF 相谈：按当前职业饮料名单购买，随后结束相谈。"""

    CLICK_DELAY = 0.8
    # 从行动页进入商店时，必须先同时看到左上「相谈」和中央兑换说明，才说明真正
    # 进入了商店；随后饮料格仍会播放展开动画。不能从点击相谈起直接计时。
    SHOP_READY_DELAY = 10.0
    # 入店前每秒点左上角跳过卡/饮料获得演出；选择页交给对应节点。
    SHOP_READY_TAP_POS = (10, 20)
    SHOP_READY_TAP_INTERVAL = 1.0
    SHOP_READY_TIMEOUT = 30.0
    SHOP_READY_POLL_INTERVAL = 0.2
    SHOP_TITLE_ROI = [0, 0, 220, 170]
    SHOP_TITLE_EXPECTED = r"(?:相[谈談]|相談)"
    SHOP_DETAIL_ROI = [40, 240, 650, 125]
    SHOP_DETAIL_EXPECTED = r"(?:Pポイント.*)?交換するものを選んでください"
    # 点击终了后，背景仍会短暂显示相谈商店。该窗口内禁止商店识别再次触发购饮。
    SHOP_FINISHED_GUARD_SECONDS = 6.0
    _shop_finished_until = 0.0
    CONSULT_ROI = [0, 1000, 720, 150]
    # 按钮文字只有「相談」；整行匹配，避免剧情台词「……相談させてほしい。」误命中。
    CONSULT_EXPECTED = r"^相[谈談]$"
    CONSULT_FALLBACK_POS = (360, 1000)
    # 商店只扫描下排 4 个 P 饮料；上排是技能卡，不能购买。
    DRINK_SLOTS = [(128, 730), (282, 730), (437, 730), (592, 730)]
    # 饮料点击后详情面板的刷新可能慢于按钮响应；先确认当前格出现选中边框，
    # 再读名称，避免已兑换格点不动时沿用上一格的详情。
    DRINK_DETAIL_WAIT = 1.5
    DRINK_DETAIL_MAX_ATTEMPTS = 3
    NAME_ROI = [190, 170, 470, 50]  # 只覆盖商店详情标题行，避开 y≈224 起的效果文字
    OCR_ALL = r"[^\n]+"
    EXCHANGE_ROI = [220, 1020, 300, 130]
    EXCHANGE_EXPECTED = "交換する"
    EXCHANGE_FALLBACK_POS = (360, 1085)
    # 点击「交換する」后出现的二次确认弹窗（截图 20260904-223305）。
    # ROI 仅覆盖右下确认按钮，避免把弹窗标题「交換確認」误当成按钮。
    EXCHANGE_CONFIRM_ROI = [350, 1080, 320, 150]
    EXCHANGE_CONFIRM_EXPECTED = "交換"
    EXCHANGE_CONFIRM_FALLBACK_POS = (500, 1160)
    END_ROI = [550, 1020, 170, 160]
    END_EXPECTED = "終了"
    END_FALLBACK_POS = (642, 1085)
    # 饮料栏最多 4 格；上限用于应对余额不足、点击未生效等异常，避免留在商店循环。
    MAX_PURCHASES = 4
    BUY_DRINK_NODE = "ProduceHIF__ProduceHIFConsultBuyDrink"
    CUSTOM_END_BUY_DRINK_NODE = "ProduceHIF__ProduceHIFCustomEndBuyDrink"
    CUSTOM_END_DRINK_SLOTS = DRINK_SLOTS[-2:]
    CUSTOM_END_MIN_POINTS = 50
    CUSTOM_END_REFRESH_POS = (118, 1058)
    CUSTOM_END_MAX_REFRESHES = 10
    # P 点在下排；排除 y=88 附近的上排数字，不能将两排 OCR 拼接成余额。
    SHOP_POINTS_ROI = [375, 100, 80, 40]
    CUSTOM_END_POST_EXCHANGE_DELAY = 3.0
    CUSTOM_END_CONFIRM_TIMEOUT = 3.0
    CUSTOM_END_POST_EXCHANGE_TIMEOUT = 5.0
    CUSTOM_END_POST_EXCHANGE_POLL = 0.25
    CUSTOM_END_CONTINUE_POS = (360, 640)
    CUSTOM_END_SHOP_TITLE_EXPECTED = "インターバル"
    CUSTOM_END_SHOP_DETAIL_EXPECTED = r"(?:Pポイント.*)?利用するものを選んでください"
    # 相谈商店中的「削除」入口（截图 20260911-225746）：位于强化按钮右侧。
    DELETE_ENTRY_ROI = [350, 900, 350, 130]
    DELETE_ENTRY_EXPECTED = "削除"
    DELETE_ENTRY_FALLBACK_POS = (520, 970)

    @staticmethod
    def _click(context: Context, pos, delay=None) -> None:
        if common._hif_stopping(context):
            return
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else ProduceHIF__ProduceHIFConsultAuto.CLICK_DELAY)

    @classmethod
    def _shop_finish_guard_active(cls) -> bool:
        """终了退出动画期间，避免将残留的相谈画面重新识别为商店。"""
        return time.time() < cls._shop_finished_until

    def _read_ocr(self, context: Context, image, roi: list, expected: str) -> str:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": expected,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF相谈: OCR异常 roi={roi} {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(result.text for result in reco.filtered_results)

    def _click_ocr_or_fallback(self, context: Context, image, roi: list, expected: str, fallback, label: str) -> None:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": expected,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                logger.info(f"HIF相谈: 点击{label} @ {pos}")
                self._click(context, pos)
                return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF相谈: {label} OCR异常 {e!r}")
        logger.info(f"HIF相谈: 未识别到{label}，回退点击 @ {fallback}")
        self._click(context, fallback)

    def _open_consult(self, context: Context) -> None:
        image = common._hif_nonbattle_screencap(context)
        self._click_ocr_or_fallback(
            context, image, self.CONSULT_ROI, self.CONSULT_EXPECTED,
            self.CONSULT_FALLBACK_POS, "相谈",
        )

    def _buy_drinks_enabled(self, context: Context) -> bool:
        """读取前台“相谈购买饮料”开关；节点未配置时默认不购买。"""
        try:
            node = context.get_node_data(self.BUY_DRINK_NODE)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF相谈: 读取购买饮料开关异常 {e!r}")
            return False

    def _buy_custom_end_drinks_enabled(self, context: Context) -> bool:
        """读取“技能卡定制后购买饮料”开关；未配置时不购买。"""
        try:
            node = context.get_node_data(self.CUSTOM_END_BUY_DRINK_NODE)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF相谈: 读取技能卡定制后购买饮料开关异常 {e!r}")
            return False

    def _read_shop_points(self, context: Context, image) -> Optional[int]:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR", "roi": self.SHOP_POINTS_ROI, "expected": r"^\d{1,3}$",
                }},
            )
        except Exception as error:
            logger.warning(f"HIF相谈: P 点识别异常 {error!r}")
            return None
        candidates = []
        x, y, width, height = self.SHOP_POINTS_ROI
        for result in reco.filtered_results if reco and reco.hit else []:
            box = getattr(result, "box", None)
            if box is None or not (x <= box[0] + box[2] / 2 < x + width
                                   and y <= box[1] + box[3] / 2 < y + height):
                continue
            text = unicodedata.normalize("NFKC", result.text or "").strip()
            if re.fullmatch(r"[0-9]{1,3}", text):
                candidates.append((box, text))
        candidates.sort(key=lambda item: item[0][0])
        # 同一余额可能拆成多个框；只合并同一行且紧邻的数字，拒绝重叠候选和其他行。
        for (left, _), (right, _) in zip(candidates, candidates[1:]):
            if not (-4 <= right[0] - (left[0] + left[2]) <= 8
                    and abs(left[1] + left[3] / 2 - right[1] - right[3] / 2) <= 6
                    and min(left[3], right[3]) >= max(left[3], right[3]) * 0.6):
                return None
        text = ''.join(text for _, text in candidates)
        return int(text) if re.fullmatch(r"[0-9]{1,3}", text) else None

    def _wait_shop_points(self, context: Context, image) -> Optional[int]:
        previous = None
        # 刷新后可能处于动画中；最多读取8帧，连续两帧同值后才用于购买判断。
        for attempt in range(8):
            if common._hif_stopping(context):
                return None
            if attempt:
                time.sleep(0.25)
                image = common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return None
            points = self._read_shop_points(context, image)
            if points is not None and points == previous:
                return points
            previous = points
        return None

    def _shop_main_visible(self, context: Context, image) -> bool:
        return bool(
            self._read_ocr(context, image, self.SHOP_TITLE_ROI, self.CUSTOM_END_SHOP_TITLE_EXPECTED)
            and self._read_ocr(context, image, self.SHOP_DETAIL_ROI, self.CUSTOM_END_SHOP_DETAIL_EXPECTED)
        )

    def _wait_after_custom_end_exchange(self, context: Context) -> bool:
        """购买后固定等待动画，再点屏幕并等待インターバル商店恢复。"""
        if common._hif_stopping(context):
            return False
        time.sleep(self.CUSTOM_END_POST_EXCHANGE_DELAY)
        if common._hif_stopping(context):
            return False
        logger.info("HIF技能卡定制后购饮: 购买动画等待完成 → 点屏幕继续")
        self._click(context, self.CUSTOM_END_CONTINUE_POS, delay=1.0)
        deadline = time.time() + self.CUSTOM_END_POST_EXCHANGE_TIMEOUT
        while not common._hif_stopping(context) and time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return False
            if self._shop_main_visible(context, image):
                return True
            time.sleep(self.CUSTOM_END_POST_EXCHANGE_POLL)
        logger.warning("HIF技能卡定制后购饮: 交易后未确认返回商店，停止后续购买")
        return False

    def _confirm_custom_end_exchange(self, context: Context) -> bool:
        """只在确认弹窗确实出现时点击；未出现时停止购买。"""
        deadline = time.time() + self.CUSTOM_END_CONFIRM_TIMEOUT
        while not common._hif_stopping(context) and time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return False
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore",
                    image,
                    pipeline_override={
                        "ProduceHIF__ProduceRecognitionScore": {
                            "recognition": "OCR",
                            "roi": self.EXCHANGE_CONFIRM_ROI,
                            "expected": self.EXCHANGE_CONFIRM_EXPECTED,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF商店饮料: 确认交换 OCR 异常 {e!r}")
                reco = None
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                if common._hif_stopping(context):
                    return False
                logger.info(f"HIF商店饮料: 点击确认交换 @ {pos}")
                self._click(context, pos, delay=1.0)
                return True
            time.sleep(self.CUSTOM_END_POST_EXCHANGE_POLL)
        logger.info("HIF商店饮料: 交换确认弹窗未出现，停止购买")
        return False

    def buy_custom_end_drinks(self, context: Context) -> None:
        """技能卡定制后按名单比较右下两格，买不到时限次刷新。"""
        if common._hif_stopping(context) or not self._buy_custom_end_drinks_enabled(context):
            return
        priority_names = drinks._hif_drink_priority_names(context, purchase_only=True)
        if not priority_names:
            logger.info("HIF技能卡定制后购饮: 当前职业饮料名单为空，不购买")
            return
        refresh_count = 0
        while not context.tasker.stopping:
            image = common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return
            if ProduceHIF__ProduceHIFDrinkAuto._bar_full(image):
                logger.info("HIF技能卡定制后购饮: 饮料栏已满，停止")
                return
            points_before = self._wait_shop_points(context, image)
            if points_before is None:
                if not common._hif_stopping(context):
                    logger.warning("HIF技能卡定制后购饮: 重读后仍无法确认稳定 P 点，停止购买和刷新")
                return
            if points_before < self.CUSTOM_END_MIN_POINTS:
                logger.info(f"HIF技能卡定制后购饮: P 点={points_before}，不足{self.CUSTOM_END_MIN_POINTS} P，停止")
                return
            chosen = self._select_priority_shop_drink(
                context, self.CUSTOM_END_DRINK_SLOTS, priority_names
            )
            if common._hif_stopping(context):
                return
            if chosen is False:
                logger.info("HIF技能卡定制后购饮: 名单饮料当前无法确认购买，停止刷新")
                return
            if chosen:
                logger.info(f"HIF技能卡定制后购饮: 选择「{chosen}」，准备交换")
                image = common._hif_nonbattle_screencap(context)
                self._click_ocr_or_fallback(
                    context, image, self.EXCHANGE_ROI, self.EXCHANGE_EXPECTED,
                    self.EXCHANGE_FALLBACK_POS, "交换する",
                )
                if not self._confirm_custom_end_exchange(context):
                    return
                if not self._wait_after_custom_end_exchange(context):
                    return
                continue
            if refresh_count >= self.CUSTOM_END_MAX_REFRESHES:
                logger.warning(
                    f"HIF技能卡定制后购饮: 已达到刷新上限{self.CUSTOM_END_MAX_REFRESHES}次，停止"
                )
                return
            logger.info("HIF技能卡定制后购饮: 右下两格无可购买的名单饮料 → 刷新")
            self._click(context, self.CUSTOM_END_REFRESH_POS, delay=2.0)
            refresh_count += 1

    @staticmethod
    def _delete_card_enabled(context: Context) -> bool:
        """读取前台「删卡」开关，与换卡状态机使用同一占位节点。"""
        return ProduceHIF__ProduceHIFCardSwapAuto._delete_card_enabled(context)

    def _delete_remembered_card(self, context: Context) -> bool:
        """进入削除页并删除 b；仅在确认回到相谈后返回 True。"""
        if ProduceHIF__ProduceHIFCardSwapAuto._delete_card_delete_attempted:
            logger.info("HIF相谈删卡: 本局已尝试过删除，跳过重复入口")
            return True
        target = ProduceHIF__ProduceHIFCardSwapAuto._delete_card_b
        if not target:
            logger.warning("HIF相谈删卡: 未记录第二次换卡 b，跳过删卡")
            # b 缺失时没有进入删卡页，当前仍安全停留在相谈；不要让 action
            # 返回失败而被调度器反复重试、重复点击商店。
            return True
        # 在进入删除页前锁定本局尝试。若确认后的页面状态不明确，也不再次
        # 删除同名卡，避免页面残留识别导致不可逆的重复操作。
        ProduceHIF__ProduceHIFCardSwapAuto._delete_card_delete_attempted = True
        image = common._hif_nonbattle_screencap(context)
        logger.info(f"HIF相谈删卡: 进入削除页，目标 b=[{target}]")
        self._click_ocr_or_fallback(
            context, image, self.DELETE_ENTRY_ROI, self.DELETE_ENTRY_EXPECTED,
            self.DELETE_ENTRY_FALLBACK_POS, "削除入口",
        )
        delete_action = ProduceHIF__ProduceHIFCardDeleteAuto()
        delete_action.delete_remembered_card(context, target)
        if not delete_action.wait_for_consult(context):
            if not common._hif_observe_result(context, lambda frame: delete_action._detect_state(context, frame) == 'consult'):
                logger.warning("HIF相谈删卡: 未确认返回相谈，不重复删除或购买")
                return False
            logger.warning('HIF安全降级: 已确认返回相谈，跳过未完成的可选删卡')
        return True

    @staticmethod
    def _shop_slot_selected(image, pos) -> bool:
        """用卡格两侧的橙色选中边框确认详情属于刚点击的饮料。"""
        try:
            y0, y1 = pos[1] - 50, pos[1] + 95
            for x0, x1 in ((pos[0] - 78, pos[0] - 62), (pos[0] + 62, pos[0] + 79)):
                border = image[y0:y1, x0:x1]
                orange = (border[:, :, 2] > 210) & (border[:, :, 1] > 75) & (border[:, :, 1] < 205) & (border[:, :, 0] < 110)
                if int(orange.sum()) < 150:
                    return False
            return True
        except (AttributeError, IndexError, TypeError):
            return False

    def _read_shop_drink(self, context: Context, pos, index: int):
        """跳过已兑换格；选中后只读名称，未选中时不沿用旧详情。"""
        for attempt in range(1, self.DRINK_DETAIL_MAX_ATTEMPTS + 1):
            image = common._hif_nonbattle_screencap(context)
            if self._shop_slot_redeemed(context, image, pos):
                logger.info(f"HIF商店饮料: 第{index + 1}格已兑换，跳过")
                return "", None
            self._click(context, pos, delay=self.DRINK_DETAIL_WAIT)
            image = common._hif_nonbattle_screencap(context)
            if not self._shop_slot_selected(image, pos):
                if self._shop_slot_redeemed(context, image, pos):
                    logger.info(f"HIF商店饮料: 第{index + 1}格已兑换，停止重试")
                    return "", None
                logger.info(f"HIF商店饮料: 第{index + 1}格 第{attempt}/{self.DRINK_DETAIL_MAX_ATTEMPTS}次 未见选中边框")
                continue
            name = self._read_ocr(context, image, self.NAME_ROI, self.OCR_ALL)
            logger.info(f"HIF商店饮料: 第{index + 1}格 第{attempt}/{self.DRINK_DETAIL_MAX_ATTEMPTS}次 名称=[{name}]")
            if name:
                return name, image
        return "", None

    def _shop_slot_redeemed(self, context: Context, image, pos) -> bool:
        """只读当前格的兑换标记；空白名称或未选中边框不代表已兑换。"""
        shape = getattr(image, "shape", ())
        if len(shape) != 3 or shape[0] < 1000 or shape[1] < 700:
            return False
        # 720×1280 实机截图：白勾下的「交換済み」，不包含下方价格。
        roi = [pos[0] - 65, pos[1] + 20, 130, 55]
        return bool(self._read_ocr(context, image, roi, r"交換済"))

    def _select_priority_shop_drink(self, context: Context, slots, priority_names: list[str]):
        """读完本页饮料格并择优；None=无名单命中，False=复选未确认。"""
        priority = {common._hif_drink_name_key(name): rank for rank, name in enumerate(priority_names)}
        candidates = []
        blocked = False
        for index, pos in enumerate(slots):
            name, image = self._read_shop_drink(context, pos, index)
            rank = priority.get(common._hif_drink_name_key(name))
            if rank is None or image is None:
                continue
            candidates.append((rank, index, pos, name))
        for rank, index, pos, name in sorted(candidates):
            selected_name, image = self._read_shop_drink(context, pos, index)
            if image is not None and common._hif_drink_name_key(selected_name) == common._hif_drink_name_key(name):
                logger.info(f"HIF商店饮料: 比较后选择第{index + 1}格「{name}」(名单第{rank + 1}位，待交换确认)")
                return name
            logger.warning(f"HIF商店饮料: 第{index + 1}格复选未确认「{name}」，尝试下一个候选")
            blocked = True
        return False if blocked else None

    def _finish_consult(self, context: Context) -> None:
        # 先打开门控再点击，覆盖点击到实际退场之间仍可见商店标题的动画帧。
        self.__class__._shop_finished_until = time.time() + self.SHOP_FINISHED_GUARD_SECONDS
        image = common._hif_nonbattle_screencap(context)
        self._click_ocr_or_fallback(
            context, image, self.END_ROI, self.END_EXPECTED,
            self.END_FALLBACK_POS, "终了",
        )
    def _shop_visible(self, context: Context, image) -> bool:
        """左上标题和中央兑换说明必须同时出现，前置事件不算商店。"""
        return bool(self._read_ocr(context, image, self.SHOP_TITLE_ROI, self.SHOP_TITLE_EXPECTED)
                    and self._read_ocr(context, image, self.SHOP_DETAIL_ROI, self.SHOP_DETAIL_EXPECTED))

    def _end_visible(self, context: Context, image) -> bool:
        return bool(self._read_ocr(context, image, self.END_ROI, self.END_EXPECTED))

    def _wait_for_shop_ready(self, context: Context, finish_only: bool = False) -> bool:
        """先推进入店事件/饮料选择，确认商店后只观察商品展开；仅终了时只确认终了按钮。"""
        ready_delay = 0.0 if finish_only else self.SHOP_READY_DELAY
        deadline = time.monotonic() + self.SHOP_READY_TIMEOUT
        next_tap, shop_since, stable = 0.0, None, 0
        handled_receive = None
        receive_nodes = (
            "ProduceHIF__ProduceHIFDrinkFullKeepFlag",
            "ProduceHIF__ProduceHIFDrinkFullConfirmFlag",
            "ProduceHIF__ProduceHIFDrinkFullFlag",
            "ProduceHIF__ProduceHIFDrinkFlag",
        )
        while time.monotonic() < deadline:
            if common._hif_stopping(context):
                return False
            image = common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return False
            if self._shop_visible(context, image):
                if shop_since is None:
                    shop_since = time.monotonic()
                    logger.info("HIF相谈: 标题及兑换说明已确认，" + ("不购买也不删卡，确认终了按钮后退出" if finish_only
                                else "等待商店商品展开") + "，不再点击跳过")
                stable = stable + 1 if not finish_only or self._end_visible(context, image) else 0
                if stable >= 3 and time.monotonic() - shop_since >= ready_delay:
                    return True
            else:
                shop_since, stable = None, 0
                for node in receive_nodes:
                    result = context.run_recognition(node, image)
                    if result and result.hit:
                        if node == handled_receive:
                            break  # 已执行的领取页只观察退出，不重新提交。
                        if common._hif_stopping(context):
                            return False
                        logger.info(f"HIF相谈: 入店前饮料事件，复用处理节点 {node}")
                        # 子任务只能处理当前页，不能递归进入育成主入口。
                        detail = context.run_task(node, pipeline_override={node: {
                            "next": [], "on_error": [], "timeout": 3000,
                        }})
                        if common._hif_stopping(context):
                            return False
                        if detail is None or not detail.status.succeeded:
                            logger.warning(f"HIF相谈: 入店前饮料事件未完成 {node}，保留现场")
                            return False
                        handled_receive = node
                        # 饮料选择页可能耗时数十秒；处理完成后重新计时，留给商店展开确认。
                        deadline = time.monotonic() + self.SHOP_READY_TIMEOUT
                        break
                else:
                    handled_receive = None
                    if time.monotonic() >= next_tap:
                        if common._hif_stopping(context):
                            return False
                        self._click(context, self.SHOP_READY_TAP_POS, delay=0)
                        next_tap = time.monotonic() + self.SHOP_READY_TAP_INTERVAL
            time.sleep(self.SHOP_READY_POLL_INTERVAL)
        logger.warning(f"HIF相谈: {self.SHOP_READY_TIMEOUT:.1f}秒内未完成前置事件及商店稳定确认，保留现场")
        return False

    def _confirm_exchange(self, context: Context) -> bool:
        """只有确认弹窗确实出现才提交购买。"""
        return self._confirm_custom_end_exchange(context)

    def run_shop(self, context: Context, wait_for_ready: bool = False) -> bool:
        """处理已经进入的相谈商店；供行动页入口和商店页恢复节点共同调用。"""
        if self._shop_finish_guard_active():
            logger.info("HIF相谈: 已完成商店处理，忽略退出动画中的商店残留识别")
            return True
        buy_drinks = self._buy_drinks_enabled(context)
        delete_card = self._delete_card_enabled(context)
        # 不购买也不删卡时无需等待商品展开，终了按钮稳定可见即可退出。
        finish_only = not buy_drinks and not delete_card
        if wait_for_ready and not self._wait_for_shop_ready(context, finish_only=finish_only):
            if common._hif_stopping(context):
                return True
            if not common._hif_observe_result(context, lambda frame: self._shop_visible(context, frame)):
                return common._hif_timeout(context, 'ProduceHIFConsultFlag', '进入商店', '相谈商店未安全恢复')
            logger.warning('HIF安全降级: 相谈商店已恢复，跳过本次购买及删卡')
            self._finish_consult(context)
            return True
        if finish_only:
            logger.info("HIF相谈: 前台未开启购买饮料或删卡 → 直接终了")
            self._finish_consult(context)
            return True
        # 删卡优先于饮料兑换，确保 200 P 点不会被先行购买消耗；失败时会取消并回到商店。
        if delete_card:
            if not self._delete_remembered_card(context):
                return False
        if not buy_drinks:
            logger.info("HIF相谈: 删卡流程结束，前台未开启购买饮料 → 终了")
            self._finish_consult(context)
            return True
        priority_names = drinks._hif_drink_priority_names(context, purchase_only=True)
        if not priority_names:
            logger.info("HIF相谈: 当前职业饮料名单为空，不购买")
            self._finish_consult(context)
            return True
        for purchase_index in range(self.MAX_PURCHASES):
            image = common._hif_nonbattle_screencap(context)
            if ProduceHIF__ProduceHIFDrinkAuto._bar_full(image):
                logger.info("HIF相谈: 左下饮料栏已满 → 终了")
                break
            logger.info(f"HIF相谈: 饮料未满，检查商店饮料（第{purchase_index + 1}次购买）")
            if not self._select_priority_shop_drink(context, self.DRINK_SLOTS, priority_names):
                logger.info("HIF相谈: 商店无可购买的名单饮料 → 终了")
                break
            image = common._hif_nonbattle_screencap(context)
            self._click_ocr_or_fallback(
                context, image, self.EXCHANGE_ROI, self.EXCHANGE_EXPECTED,
                self.EXCHANGE_FALLBACK_POS, "交换する",
            )
            if not self._confirm_exchange(context):
                logger.warning("HIF相谈: 未确认交换弹窗，停止购买")
                break
        else:
            logger.warning("HIF相谈: 已达到单次相谈购买上限，终了以避免循环")
        self._finish_consult(context)
        return True

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        self._open_consult(context)
        return self.run_shop(context, wait_for_ready=True)


@AgentServer.custom_action("ProduceHIF__ProduceHIFConsultShopAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFConsultShopAuto(CustomAction):
    """任务从相谈商店页恢复时，抢在通用「終了」前处理商店。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.info("HIF相谈: 已在相谈商店，进入商店处理流程")
        return ProduceHIF__ProduceHIFConsultAuto().run_shop(context, wait_for_ready=True)


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardCustomAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFCardCustomAuto(CustomAction):
    """HIF 技能卡定制（スキルカードカスタマイズ）状态机。

    触发：`ProduceHIF__ProduceHIFCardCustomFlag` 识别到 P点商店界面（「スキルカードカスタマイズ」绿钮）。
    内部循环完成「主界面→选卡→卡详情→待执行→完成提示→返回→…→退出」整个定制流程，
    直到所有需定制的卡均完成（选卡界面「あとN枚」=0）。

    界面判定与决策（依 定制功能开发.md；坐标/锚点来自 094632/094642/095236/095241/095246/095304）：
      A 商店主界面(094632)   名额为0时确认刷新前后余额；低于40 P结束，不读取刷新价格
      B 选卡界面(094642)     再查「あとN枚」→ N=0 点<<もどる退出,N≠0 模板匹配目标卡
      C 卡详情(095407/095236) 选中卡后点 →カスタマイズ
      D 待执行(095241)       选可用菜单项 → 点実行する
      E 完成提示(095246)     「XXをカスタマイズしました」→ 点屏幕中间
      F 上限态(095304)       菜单项「カスタマイズ上限」|| 実行する变灰 → 点一覧に戻る

    定制项目选项由面板保存的卡牌 ID 和定制项目 ID 决定；同一列数的菜单使用固定格位。
    防死循环：STEP_LIMIT 步 + MASTER_LIMIT 单卡 + stopping。
    """

    STEP_LIMIT = 200     # 主循环防死步数
    MAX_CARDS = 6        # 一次育成最多定制6张卡(再多元点/P点不够)
    MASTER_LIMIT = 6     # 单张卡最多定制次数（防单卡死循环）
    CLICK_DELAY = 0.5
    BETWEEN_LIMIT = 10   # 连续识别不到当前阶段的最大容忍次数（防卡界面）
    MAIN_CYCLE_LIMIT = 5 # 连续N次回主界面但队列没推进(目标卡不在批次/钱不够) → 强制结束,防 back/刷新/重进死循环
    MIN_CUSTOM_POINTS = 40
    HEAL_NODE = "ProduceHIF__ProduceHIFIntervalHeal"
    # 超时宽限交回入口后，该时限内重新进入商店跳过一次定制前回血（离店前回血照常）。
    REENTRY_HEAL_SKIP_SECONDS = 180.0
    _skip_heal_until = 0.0
    CARD_CUSTOM_ENABLED_NODE = "ProduceHIF__ProduceHIFCardCustomEnabled"
    HEAL_OPEN_POS = (622, 930)
    HEAL_PLUS_POS = (594, 944)
    HEAL_CONFIRM_POS = (512, 1162)
    HEAL_CANCEL_POS = (220, 1162)

    # —— A 商店主界面(094632) ——
    CUSTOMIZE_TPL  = "hif/produce/hif_customize_btn.png"   # 「スキルカードカスタマイズ」绿钮(185223实测重裁)
    CUSTOMIZE_ROI  = [290, 860, 300, 130]              # 绿钮搜索区(实测覆盖 x317-589,y889-959)
    CUSTOMIZE_POS  = (445, 924)                        # 点绿钮(185223实测)
    REFRESH_POS    = (118, 1058)                       # 「リフレッシュ」(目测)
    MAIN_LEFT_ROI  = [330, 958, 240, 32]               # 「カスタマイズ可能枚数あとN枚」白字(185223实测 center 452,980)
    LEFT_PAT       = r"あと(\d+)"              # あとN

    # —— B 选卡界面(185300实测) ——
    SELECT_LEFT_ROI = [180, 1078, 360, 18]             # 「カスタマイズ可能スキルカード あとN枚」白字(center 361,1087)
    BACK_POS        = (165, 1150)                      # 「<< もどる」(目测)
    # 选卡：逐格点取 + OCR 顶部卡名确认（目标卡位置会变,故不用固定坐标/模板匹配,改为逐个点格、文字确认）
    # 点击、灰态检测和选中角框共用实测卡格中心，避免第三排偏移。
    CARD_GRID = [(x, y) for y in (624, 771, 917) for x in (140, 286, 433, 580)]
    SELECT_CELL_CENTERS = CARD_GRID
    DISABLED_CARD_BRIGHT_RATIO = 0.05
    SELECT_CONFIRM_TIMEOUT = 5.0
    SELECT_CONFIRM_POLL = 0.25
    TARGET_CARD_NAME = "精神統一"   # 被点卡顶部详情卡名含它即命中
    # 选中卡详情的标题行位于 x≈194, y≈114（MuMu 720×1280）。此前 ROI
    # 从 y=140 开始，实际读到的是标题下方的效果文字，导致卡名匹配失效。
    NAME_ROI   = [185, 105, 450, 45]
    OCR_ALL    = r"[^\n]+"             # 宽匹配: 读该区全部卡名文字(含非配置卡名, 供子串判断). 勿只列配置卡名否则纯假名卡被过滤读不到(卡面兜底不触发)

    # —— C/D 卡详情与待执行(185641实测) ——
    DO_CUSTOMIZE_POS = (500, 1155)                     # 「→ カスタマイズ」(目测,TODO)
    MENU_ITEMS = [(140, 929), (360, 929), (580, 929)]  # 720×1280 画面的三列按钮中心；两项菜单使用前两列
    MENU_LEFT_ROIS = [[40, 875, 200, 30], [260, 875, 200, 30], [480, 875, 200, 30]]  # 2026-10-11 五张 MuMu 样本：逐项「あとN回」，避开上方菜单标题
    MENU_LIMIT_ROIS = [[40, 980, 200, 35], [260, 980, 200, 35], [480, 980, 200, 35]]  # 已达上限文字位于菜单按钮下方，不在次数徽章处
    EXEC_POS   = (501, 1160)                           # 「実行する」(192151实测)
    # 「合計あとN回」绿标签(192151实测 center 501,1104)—— 剩余可加总次数,驱动连续加
    TOTAL_ROI  = [425, 1090, 155, 40]
    TOTAL_PAT  = r"あと(\d+)"

    # —— F 返回与上限 ——
    BACK_TO_LIST_POS = (230, 1160)                     # 「一覧に戻る」(与実行对称,目测)
    TAP_CENTER_POS   = (360, 640)                      # 完成提示「点屏幕中间」
    TERMINATE_POS    = (635, 1072)                     # 技能卡定制主界面「終了」(全部定制完→进下一场景)
    MAX_CUSTOM_CLICK = 3                               # 单张卡最多点「カスタマイズ」次数,超限=该卡无法定制(上限/P不足)→跳过
    EXEC_STALL_LIMIT = 3                               # 待执行时「合計」连续N次不减少(点実行する不生效=P点不足/上限) → 跳过该卡

    def __init__(self):
        super().__init__()
        self.done_cards = set()   # 已确认完成的卡（同名只做一张）
        self.skipped_cards = {}   # 未完成的卡及原因
        self._unconfirmed_slots = []  # 本轮选卡超时的格位；不猜测其卡名
        self._card_selected = False  # 选卡界面是否已选中某卡(等OCR确认)
        self._try_idx = 0            # 逐卡点取当前尝试的格位索引
        self._cur_card = None        # 当前要定制的卡名(OCR卡名匹配配置)
        self._menu_attempt = 0       # 待执行时在勾选项间轮换的索引(读合計自动点满)
        self._total_none = 0         # 合計连续读不到的计数(防到上限合計标签消失不停止)
        self._exec_stall = 0         # 待执行时「合計」连续不减少的计数(点実行する不生效=P点不足/上限)
        self._last_total = None      # 上一次读到的「合計」值(比较是否减少)
        self._option_counts = {}     # 当前卡逐项累计次数；仅由稳定 OCR 或已确认提交推进
        self._pending_option = None  # 一次提交只确认一次，不把点击当作成功
        self._queue = []             # 需要定制的卡队列(勾选了项的卡)
        self._queue_idx = 0          # 当前队列下标
        self._done_all = False       # 全部卡已处理完(可点終了退出)
        self._ocr_none = 0           # 卡名OCR连续读不出计数(点卡后详情未弹,等稳定再判,防点太快跳过)
        self._unmatched_card_name = ""  # 当前格读到的非目标卡名；与真正无有效OCR分开记录
        self._card_custom_click = 0  # 当前卡已点「カスタマイズ」次数(防死循环)
        self._last_back_qidx = -1    # 上次回主界面时的队列下标(判队列是否推进)
        self._no_progress_cycles = 0 # 连续回主界面但队列没推进的次数(防 back/刷新/重进死循环)
        self._load_custom_cards()

    def _load_custom_cards(self, profession=ProduceHIF__ProduceCardsAuto.DEFAULT_PROFESSION):
        """读取当前职业的最新选择；旧全局文件在首次面板保存前仍兼容。"""
        self.custom_cards = {}
        self._catalog_card_names = {}
        legacy_cards, legacy_names = {}, {}
        try:
            with open(os.path.join(common.EXT_DIR, "catalog", "cards_customize.json"), encoding="utf-8") as f:
                legacy_cards = json.load(f)
            with open(os.path.join(common.EXT_DIR, "catalog", "custom_card_images.json"), encoding="utf-8") as f:
                legacy_names = {item["gk_img_id"]: item["card_name"] for item in json.load(f)}
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"HIF技能卡定制: 旧卡识别模板不可用，继续使用卡名 OCR {e!r}")
        try:
            with open(os.path.join(common.EXT_DIR, "catalog", "hif_customization_catalog.json"), encoding="utf-8") as f:
                catalog = json.load(f)["cards"]
            self._catalog_card_names = {
                re.sub(r"\s+", "", unicodedata.normalize("NFKC", card["name"])): card["name"]
                for card in catalog
            }
            selection_path = os.path.join(common.BASE_DIR, "config", "hif", "hif_custom_card_selection.json")
            selected = {}
            if os.path.exists(selection_path):
                with open(selection_path, encoding="utf-8") as f:
                    selection = json.load(f)
                selected = (
                    selection["profiles"].get(profession, {})
                    if "profiles" in selection else selection.get("cards", {})
                )
                if not isinstance(selected, dict):
                    raise ValueError(f"{profession}职业定制配置不是对象")
            for card in catalog:
                card_id = card["id"]
                options = card["options"]
                targets = self._custom_targets(selected.get(str(card_id), {}), options)
                if not any(targets.values()) or len(options) not in (2, 3):
                    continue
                menu = [
                    {"id": option["id"], "name": option["name"], "pos": self.MENU_ITEMS[index],
                     "index": index, "max": option["max"], "target": targets[option["id"]]}
                    for index, option in enumerate(options) if targets.get(option["id"], 0) > 0
                ]
                if not menu:
                    logger.warning(f"HIF技能卡定制: 卡牌 {card['name']} 的已选定制项目 ID 不在目录中，跳过")
                    continue
                legacy = legacy_cards.get(legacy_names.get(card_id, ""), {})
                self.custom_cards[card["name"]] = {
                    "card_id": card_id,
                    "menu": menu,
                    "template": legacy.get("template", ""),
                    "ocr_markers": legacy.get("ocr_markers", []),
                }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as e:
            self.custom_cards = {}
            logger.warning(f"HIF技能卡定制: 读取面板选择或卡牌目录失败 {e!r}")
    @staticmethod
    def _custom_targets(selected, options) -> dict:
        """兼容旧勾选数组；非法目标拒绝执行，不截断用户配置。"""
        maxima = {option["id"]: option["max"] for option in options}
        if any(type(value) is not int or value < 1 for value in maxima.values()):
            raise ValueError("定制目录缺少有效上限")
        if isinstance(selected, list):
            if any(type(value) is not int or value not in maxima for value in selected):
                raise ValueError("旧定制配置包含无效项目")
            return {option_id: maxima[option_id] for option_id in selected}
        if not isinstance(selected, dict):
            raise ValueError("定制目标必须是对象或旧数组")
        targets = {}
        for key, value in selected.items():
            option_id = int(key)
            if option_id not in maxima or type(value) is not int or not 0 <= value <= maxima[option_id]:
                raise ValueError(f"定制项目 {key} 的目标次数无效: {value!r}")
            targets[option_id] = value
        return targets

    @staticmethod
    def _click(context: Context, pos, delay=None):
        if common._hif_stopping(context):
            return
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else ProduceHIF__ProduceHIFCardCustomAuto.CLICK_DELAY)

    @staticmethod
    def _heal_dialog_visible(context: Context, image) -> bool:
        dialog = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": [20, 580, 450, 185],
                "expected": "体力回復確認",
            }},
        )
        return bool(dialog and dialog.hit)

    @staticmethod
    def _heal_plus_pos(context: Context, image):
        result = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "TemplateMatch", "roi": [500, 820, 180, 250],
                "template": "hif/shopping_plus.png", "threshold": 0.85,
            }},
        )
        if result and result.hit and result.box:
            x, y, width, height = result.box
            return (x + width // 2, y + height // 2)
        return None

    @staticmethod
    def _heal_full_health_visible(context: Context, image) -> bool:
        notice = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": [20, 580, 450, 185],
                "expected": "^体力は最大です$",
            }},
        )
        return bool(notice and notice.hit)

    @staticmethod
    def _heal_button_pos(context: Context, image, cancel=False):
        result = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR",
                "roi": [50, 1100, 310, 120] if cancel else [360, 1100, 310, 120],
                "expected": "キャンセル" if cancel else "回復する",
            }},
        )
        if result and result.hit and result.box:
            x, y, width, height = result.box
            return (x + width // 2, y + height // 2)
        return None

    def _cancel_heal_dialog(self, context: Context) -> bool:
        if common._hif_stopping(context):
            return False
        image = common._hif_nonbattle_screencap(context)
        if not self._heal_dialog_visible(context, image):
            return False
        pos = self._heal_button_pos(context, image, cancel=True)
        if pos is None or common._hif_stopping(context):
            return False
        self._click(context, pos, delay=0.5)
        return self._wait_heal_shop(context)

    @staticmethod
    def _wait_heal_shop(context: Context) -> bool:
        stable = 0
        for _ in range(30):
            if context.tasker.stopping:
                return False
            image = common._hif_nonbattle_screencap(context)
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            stable = stable + 1 if shop and shop.hit else 0
            # 商店按钮会先于回体退出动画出现；连续四帧（至少1.5秒）后再点击。
            if stable >= 4:
                return True
            time.sleep(0.5)
        return False

    def _heal_before_customization(self, context: Context) -> bool:
        """按中间回血开关执行回血；定制前与离店前各执行一次。"""
        if common._hif_stopping(context):
            return False
        try:
            node = context.get_node_data(self.HEAL_NODE)
            enabled = bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF中场回血: 读取开关失败，跳过回血 {e!r}")
            return True
        if not enabled:
            return True

        for attempt in range(2):
            logger.info(f"HIF中场回血: 点回復打开弹窗（第{attempt + 1}次）")
            self._click(context, self.HEAL_OPEN_POS, delay=1.5)
            for _ in range(6):
                if context.tasker.stopping:
                    return False
                image = common._hif_nonbattle_screencap(context)
                if self._heal_dialog_visible(context, image):
                    time.sleep(0.5)  # 弹窗标题可能先于按钮动画出现
                    break
                if self._heal_full_health_visible(context, image):
                    shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
                    if shop and shop.hit:
                        logger.info("HIF中场回血: 体力已满，跳过回血并继续商店流程")
                        return True
                time.sleep(0.5)
            else:
                shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
                if not (shop and shop.hit):
                    logger.warning("HIF中场回血: 未确认回復弹窗或商店页，停止后续固定点击")
                    return False
                if attempt == 0:
                    logger.info("HIF中场回血: 仍在商店页，重试一次回復入口")
                    continue
                logger.info("HIF中场回血: 两次未打开回復弹窗，跳过回血并继续商店流程")
                return True
            break

        plus_clicks = 0
        for _ in range(10):
            if common._hif_stopping(context):
                return False
            image = common._hif_nonbattle_screencap(context)
            pos = self._heal_plus_pos(context, image)
            if pos is None:
                if plus_clicks == 0:
                    logger.info("HIF中场回血: 未见加量按钮，安全取消本次回血")
                    return self._cancel_heal_dialog(context)
                break
            self._click(context, pos, delay=0.3)
            plus_clicks += 1
        if common._hif_stopping(context):
            return False
        image = common._hif_nonbattle_screencap(context)
        pos = self._heal_button_pos(context, image)
        if pos is None:
            logger.warning("HIF中场回血: 未确认回復する按钮，取消本次回血")
            return self._cancel_heal_dialog(context)
        logger.info(f"HIF中场回血: 已点＋{plus_clicks}次，确认回復")
        # 回血消耗P点：结果未确认前标记待确认，超时宽限不会交回入口重放回血。
        _hif_recovery.pending = '中场回血'
        self._click(context, pos, delay=1.0)
        if self._wait_heal_shop(context):
            _hif_recovery.pending = None
            logger.info("HIF中场回血: 商店页已恢复，继续商店流程")
            return True
        if context.tasker.stopping:
            return False
        image = common._hif_nonbattle_screencap(context)
        if self._heal_dialog_visible(context, image):
            # 弹窗仍在说明回復未执行，取消即可。
            _hif_recovery.pending = None
            logger.warning("HIF中场回血: 回復未确认，取消弹窗并继续商店流程")
            return self._cancel_heal_dialog(context)
        logger.warning("HIF中场回血: 等待商店页恢复超时，停止后续点击")
        return False

    @classmethod
    def _customization_enabled(cls, context: Context) -> bool:
        try:
            node = context.get_node_data(cls.CARD_CUSTOM_ENABLED_NODE)
            return bool(node.get("enabled", True)) if node else True
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 读取开关失败，保持开启 {e!r}")
            return True

    @staticmethod
    def _read_left(context: Context, image, roi) -> Optional[int]:
        """OCR 读「あとN」并返回 N；识别失败/读不到返回 None。"""
        pat = ProduceHIF__ProduceHIFCardCustomAuto.LEFT_PAT
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": pat,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 读「あとN」异常 {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        for r in reco.filtered_results:
            m = re.search(r"あと(\d+)|(\d+)枚", r.text or "")
            if m:
                return int(m.group(1) or m.group(2))
        return None

    def _match_card(self, context: Context, image) -> Optional[list]:
        """选卡网格内模板匹配目标卡，命中返回 box，否则 None。"""
        for tpl in self.CARD_TEMPLATES:
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceIdentityMatch", image,
                    pipeline_override={
                        "ProduceHIF__ProduceIdentityMatch": {
                            "recognition": "TemplateMatch", "template": tpl,
                            "roi": self.CARD_SEARCH_ROI, "threshold": self.CARD_THRESH,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF技能卡定制: 卡模板匹配异常 {e!r}")
                continue
            if reco and reco.hit and reco.filtered_results:
                return reco.filtered_results[0].box
        return None

    def _end_customization(self, reason: str) -> None:
        self.skipped_cards.update({name: reason for name in self._queue[self._queue_idx:]})
        self._done_all = True
        self._shop_consumption_stopped = True
        logger.info(f"HIF技能卡定制: {reason}，结束剩余定制")

    def _refresh_custom_shop(self, context: Context, image) -> str:
        """只在刷新前后读余额；确认扣款后等名额稳定，不读取价格。"""
        consult = ProduceHIF__ProduceHIFConsultAuto()
        before = consult._wait_shop_points(context, image)
        if common._hif_stopping(context):
            return "stopped"
        if before is None or before < self.MIN_CUSTOM_POINTS:
            self._end_customization("刷新前P点无法确认" if before is None else f"刷新前P点={before}，低于40")
            return "refresh"
        logger.info(f"HIF技能卡定制: 可选0张，刷新前P点={before} → 尝试一次刷新")
        self._click(context, self.REFRESH_POS, delay=1.0)
        if common._hif_stopping(context):
            return "stopped"
        time.sleep(2.0)
        if common._hif_stopping(context):
            return "stopped"
        image = common._hif_nonbattle_screencap(context)
        after = consult._wait_shop_points(context, image)
        if common._hif_stopping(context):
            return "stopped"
        if after is None or after >= before:
            self._end_customization("刷新后P点无法确认" if after is None else f"刷新前后P点未减少：{before}→{after}，停止刷新")
            return "refresh"
        logger.info(f"HIF技能卡定制: 刷新成功，P点={before}→{after}")
        if after < self.MIN_CUSTOM_POINTS:
            self._end_customization(f"刷新后P点={after}，低于40")
            return "refresh"
        # 余额减少已证明刷新成功；名额显示过渡期间不再点击刷新。
        stable = 0
        previous = None
        for _ in range(8):
            if common._hif_stopping(context):
                return "stopped"
            image = common._hif_nonbattle_screencap(context)
            left = self._read_left(context, image, self.MAIN_LEFT_ROI)
            stable = (stable + 1 if left == previous else 1) if left is not None and left > 0 else 0
            previous = left
            if stable >= 2:
                return "refresh"
            time.sleep(0.25)
        self._end_customization("刷新成功，但定制名额显示未确认，不再刷新")
        return "refresh"

    def _handle_main(self, context: Context, image) -> str:
        """商店主界面(A)：返回 'enter'(进选卡) / 'refresh'(点刷新) / 'exit'(全部完成点终了) / 'unknown'。"""
        if common._hif_stopping(context):
            return "stopped"
        if self._done_all:
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            if not (shop and shop.hit):
                return "unknown"
            consult = ProduceHIF__ProduceHIFConsultAuto()
            if not getattr(self, "_shop_consumption_stopped", False):
                consult.buy_custom_end_drinks(context)
            if common._hif_stopping(context):
                logger.info("HIF技能卡定制: 任务已停止，保留商店现场，不点击終了")
                return "stopped"
            has_shop_actions = self._customization_enabled(context) or consult._buy_custom_end_drinks_enabled(context)
            if has_shop_actions and not self._heal_before_customization(context):
                if common._hif_stopping(context):
                    return "stopped"
                if self._hand_back(context, "离店前回血", "未确认回血后返回商店，不点击終了"):
                    return "handed_back"
                return "failed"
            if common._hif_stopping(context):
                return "stopped"
            logger.info(f"HIF技能卡定制: 处理结束，已完成={sorted(self.done_cards)}，未完成={self.skipped_cards} → 点終了退出 @ {self.TERMINATE_POS}")
            self._click(context, self.TERMINATE_POS)
            return "exit"
        left = self._read_left(context, image, self.MAIN_LEFT_ROI)
        if left is None:
            # 入口点击后的过渡可能跨帧；只在选卡页枚数可读时推进。
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            if not (shop and shop.hit) and self._read_left(context, image, self.SELECT_LEFT_ROI) is not None:
                return "enter"
            return "unknown"
        if left >= 0:
            logger.info(f"HIF技能卡定制: 主界面 可选{left}张")
        if left > 0:
            logger.info(f"HIF技能卡定制: 主界面 点スキルカードカスタマイズ @ {self.CUSTOMIZE_POS}")
            self._click(context, self.CUSTOMIZE_POS, delay=1.0)   # 点绿钮后等选卡界面过渡稳定
            time.sleep(0.5)
            image = common._hif_nonbattle_screencap(context)
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            if shop and shop.hit:
                logger.info("HIF技能卡定制: 点击后仍在商店页，等待并重试自定义入口")
                return "unknown"
            if self._read_left(context, image, self.SELECT_LEFT_ROI) is not None:
                return "enter"
            logger.info("HIF技能卡定制: 尚未确认选卡页，等待页面稳定")
            return "unknown"
        return self._refresh_custom_shop(context, image)

    @staticmethod
    def _slot_label(index: int) -> str:
        return f"第{index + 1}格（第{index // 4 + 1}行第{index % 4 + 1}列）"

    def _save_unconfirmed_slot(self, context: Context) -> None:
        """跳过前保存当前现场，避免保存十秒等待前的旧截图。"""
        if common._hif_stopping(context):
            return
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            if common._hif_stopping(context):
                return
            folder = os.path.join(common.BASE_DIR, "debug", "custom", "hif_custom_unconfirmed")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{time.time_ns()}-slot-{self._try_idx + 1}.png")
            if cv2 is None or image is None or not cv2.imwrite(path, image):
                logger.warning(f"HIF技能卡定制: {self._slot_label(self._try_idx)}未确认现场截图保存失败")
                return
            logger.info(f"HIF技能卡定制未确认截图: 候选={self._slot_label(self._try_idx)}, OCR=[{self._last_card_name_ocr}], 路径={path}")
        except Exception as exc:  # 截图失败不能阻止继续扫描其他候选。
            logger.warning(f"HIF技能卡定制: {self._slot_label(self._try_idx)}未确认现场截图保存异常 {exc!r}")

    def _handle_select(self, context: Context, image) -> str:
        """选卡界面(B)：确认后选卡/进入定制/返回；未确认跳过当前格，取消时停止。"""
        # 已选中某卡 → OCR 顶部卡名：当前列表中先处理任意尚未完成的目标卡。
        if self._card_selected:
            self._last_card_name_ocr = ""
            status, card = self._wait_selected_card(context, image)
            if status == "stopped":
                return "stopped"
            if status != "ready" or card is None:
                reason = f"{self._slot_label(self._try_idx)}选中/卡名稳定未确认，有界复核后仍未确认，跳过当前格"
                self._unconfirmed_slots.append(self._try_idx)
                logger.warning(f"HIF技能卡定制: {reason}，继续扫描其他候选")
                self._save_unconfirmed_slot(context)
                if common._hif_stopping(context):
                    return "stopped"
                return self._advance_select_candidate(context, image, reason)
            if card in self._queue[self._queue_idx:]:
                if card != self._cur_card:
                    found_idx = self._queue.index(card, self._queue_idx)
                    self._queue[self._queue_idx], self._queue[found_idx] = (
                        self._queue[found_idx], self._queue[self._queue_idx]
                    )
                    self._cur_card = card
                    self._card_custom_click = 0
                    logger.info(f"HIF技能卡定制: 当前列表先处理「{card}」，其余目标保留待处理")
                # 反复点カスタマイズ无效(该卡已到上限/Pポイント不足) → 跳过该卡,换成队列下一张
                if self._card_custom_click >= self.MAX_CUSTOM_CLICK:
                    logger.info(f"HIF技能卡定制: 目标卡「{card}」多次点カスタマイズ无效 → 跳过该卡")
                    self._card_selected = False
                    self._try_idx = 0
                    self._advance_queue(reason="点カスタマイズ多次无效")
                    return "pick"
                self._card_selected = False
                self._try_idx = 0
                self._ocr_none = 0
                self._unmatched_card_name = ""
                self._menu_attempt = 0
                self._card_custom_click += 1
                logger.info(f"HIF技能卡定制: 找到待定制目标卡「{card}」(队列{self._queue_idx}) → 点→カスタマイズ进入详情(尝试{self._card_custom_click}/{self.MAX_CUSTOM_CLICK}) @ {self.DO_CUSTOMIZE_POS}")
                self._click(context, self.DO_CUSTOMIZE_POS)
                return "detail"
            self._ocr_none = 0
            if card in self.done_cards:
                logger.info(f"HIF技能卡定制: {self._slot_label(self._try_idx)}「{card}」已完成，跳过，不重复定制")
                return self._advance_select_candidate(context, image, f"「{card}」已完成，跳过")
            reason = f"识别到非目标卡「{card}」"
            return self._advance_select_candidate(context, image, reason)
        # 未选中：队列空了全部完成，或读枚数
        if self._done_all:
            logger.info("HIF技能卡定制: 目标卡处理结束 → 点<<もどる回主界面")
            self._click(context, self.BACK_POS)
            return "back"
        left = self._read_left(context, image, self.SELECT_LEFT_ROI)
        if left is not None and left <= 0:
            logger.info("HIF技能卡定制: 选卡界面 可选0张 → 点<<もどる")
            self._click(context, self.BACK_POS)
            return "back"
        self._try_idx = self._next_selectable_card(image, 0)
        if self._try_idx >= len(self.CARD_GRID):
            return self._finish_select_scan(context)
        self._card_selected = True
        self._unmatched_card_name = ""
        self._click(context, self.CARD_GRID[self._try_idx], delay=1.0)   # 点卡后留足详情弹出时间
        logger.info(f"HIF技能卡定制: 点{self._slot_label(self._try_idx)} @ {self.CARD_GRID[self._try_idx]}")
        return "pick"

    def _advance_select_candidate(self, context: Context, image, reason: str) -> str:
        self._unmatched_card_name = ""
        self._try_idx = self._next_selectable_card(image, self._try_idx + 1)
        if self._try_idx >= len(self.CARD_GRID):
            return self._finish_select_scan(context)
        time.sleep(0.4)
        if getattr(getattr(context, "tasker", None), "stopping", False):
            return "stopped"
        self._click(context, self.CARD_GRID[self._try_idx])
        logger.info(f"HIF技能卡定制: {reason}, 点下一候选{self._slot_label(self._try_idx)} @ {self.CARD_GRID[self._try_idx]}")
        return "pick"

    def _selected_card_slot(self, image, index: int) -> bool:
        """四个橙色角框必须都在当前格；不是卡面内的橙色图案。"""
        if getattr(image, "shape", ()) != (1280, 720, 3):
            return False
        x, y = self.SELECT_CELL_CENTERS[index]
        # 2026-10-11 八张选卡页：当前格四角橙色占比0.701～0.948，其他格为0。
        for bx, by in ((x - 66, y - 70), (x + 30, y - 70), (x - 66, y + 63), (x + 30, y + 63)):
            region = image[by:by + 8, bx:bx + 36].astype(int)
            b, g, r = region[..., 0], region[..., 1], region[..., 2]
            orange = (r > 230) & (g > 130) & (g < 225) & (b < 150) & (r - g > 25) & (g - b > 25)
            if float(orange.mean()) < 0.5:
                return False
        return True

    def _selection_state(self, context: Context, image) -> str:
        """选中状态独立于详情/卡名；没有明确证据时返回 unknown。"""
        if self._selected_card_slot(image, self._try_idx):
            return "selected"
        if any(self._selected_card_slot(image, index) for index in range(len(self.CARD_GRID)) if index != self._try_idx):
            return "unselected"
        result = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": [40, 275, 640, 85],
                "expected": r"カスタマイズするスキルカードを[1１]つ選んでください",
            }},
        )
        return "unselected" if result and result.hit else "unknown"

    def _wait_selected_card(self, context: Context, image) -> tuple:
        """两段各五秒观察；已选中不补点，仅连续确认未选中才重试一次。"""
        selected_seen = False
        for attempt in range(2):
            deadline = time.monotonic() + self.SELECT_CONFIRM_TIMEOUT
            last_card, stable = None, 0
            while not context.tasker.stopping:
                card = None
                selected = self._selected_card_slot(image, self._try_idx)
                selected_seen = selected_seen or selected
                if selected:
                    card = self._match_card_name(context, image)
                if context.tasker.stopping:
                    return "stopped", None
                stable = stable + 1 if card is not None and card == last_card else (1 if card is not None else 0)
                last_card = card
                if stable >= 2:
                    logger.info(f"HIF技能卡定制: {self._slot_label(self._try_idx)}选中角框已确认，卡名「{card}」连续两帧稳定")
                    return "ready", card
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self.SELECT_CONFIRM_POLL, remaining))
                if context.tasker.stopping:
                    return "stopped", None
                image = common._hif_nonbattle_screencap(context)
            if context.tasker.stopping:
                return "stopped", None
            if attempt == 0:
                self._save_unconfirmed_slot(context)
                if context.tasker.stopping:
                    return "stopped", None
                if selected_seen:
                    logger.info(f"HIF技能卡定制: {self._slot_label(self._try_idx)}已选中，继续复核卡名，不重复点击")
                    continue
                first_state = self._selection_state(context, image)
                if context.tasker.stopping:
                    return "stopped", None
                time.sleep(self.SELECT_CONFIRM_POLL)
                if context.tasker.stopping:
                    return "stopped", None
                image = common._hif_nonbattle_screencap(context)
                if context.tasker.stopping:
                    return "stopped", None
                second_state = self._selection_state(context, image)
                if first_state == second_state == "unselected":
                    logger.warning(f"HIF技能卡定制: {self._slot_label(self._try_idx)}连续两帧确认未选中，补点当前格一次")
                    self._click(context, self.CARD_GRID[self._try_idx], delay=self.SELECT_CONFIRM_POLL)
                    if context.tasker.stopping:
                        return "stopped", None
                    image = common._hif_nonbattle_screencap(context)
                else:
                    logger.info(f"HIF技能卡定制: {self._slot_label(self._try_idx)}选中状态={first_state}/{second_state}，继续观察，不补点")
        return "failed", None

    def _next_selectable_card(self, image, start: int) -> int:
        """跳过卡面明显变暗的不可定制卡；无法判断时保留该候选。"""
        shape = getattr(image, "shape", ())
        if len(shape) != 3 or shape[0] < 1000 or shape[1] < 700:
            return start
        skipped = []
        for index in range(start, len(self.CARD_GRID)):
            x, y = self.CARD_GRID[index]
            region = image[y - 32:y + 32, x - 32:x + 32]
            if region.size == 0:
                if skipped:
                    logger.info(f"HIF技能卡定制: 跳过灰态候选 {[self._slot_label(index) for index in skipped]}")
                return index
            bright_ratio = float((region.max(axis=2) > 180).mean())
            if bright_ratio >= self.DISABLED_CARD_BRIGHT_RATIO or float(region.mean()) >= 155:
                if skipped:
                    logger.info(f"HIF技能卡定制: 跳过灰态候选 {[self._slot_label(index) for index in skipped]}")
                return index
            skipped.append(index)
        if skipped:
            logger.info(f"HIF技能卡定制: 跳过灰态候选 {[self._slot_label(index) for index in skipped]}，后续无可选卡")
        return len(self.CARD_GRID)

    def _finish_select_scan(self, context: Context) -> str:
        """本批次已无可处理候选，返回商店主界面。"""
        self._card_selected = False
        self._try_idx = 0
        missing = self._queue[self._queue_idx:]
        reason = "当前列表未找到可定制目标"
        if self._unconfirmed_slots:
            reason += f"（候选{[self._slot_label(index) for index in self._unconfirmed_slots]}有界复核后仍未确认，已跳过）"
        self.skipped_cards.update({name: reason for name in missing})
        self._queue_idx = len(self._queue)
        self._cur_card = None
        self._done_all = True
        logger.info(f"HIF技能卡定制: 当前列表没有可定制的目标卡 {missing}，点<<もどる结束")
        self._click(context, self.BACK_POS)
        return "back"

    def _match_card_name(self, context: Context, image) -> Optional[str]:
        """识别已选目标或完整目录卡名；无法确定时返回 None，供上层重试。"""
        if not self.custom_cards:
            return None
        keys = self.custom_cards
        text = ""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.NAME_ROI,
                        "expected": self.OCR_ALL,  # 读取完整标题，不能用目标卡名过滤掉非目标文字。
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 卡名区域OCR异常 {e!r}")
            reco = None
        if reco and reco.hit and reco.filtered_results:
            text = "".join(r.text for r in reco.filtered_results)
            self._last_card_name_ocr = text
            logger.info(f"HIF技能卡定制: 卡名区域OCR读出=[{text}]")
            # 只忽略空白和全半角；保留强化标记+，完整相等才能认定卡名。
            normalized_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
            catalog_card = self._catalog_card_names.get(normalized_text)
            if catalog_card and catalog_card not in self.custom_cards:
                return catalog_card
            for card in keys:
                normalized_name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", card))
                if normalized_name == normalized_text:
                    return card
        # 完整卡名未确认时等待或跳过，不使用卡面猜测。
        return None

    @staticmethod
    def _success_text_key(text: str) -> str:
        """仅用于成功提示比对，将已证实的易混字符统一到同一形式。"""
        text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
        return text.translate(str.maketrans({"ヘ": "へ", "ー": "一"}))

    def _confirm_completed(self, context: Context, image) -> bool:
        """白条仅辅助定位；必须读到当前卡的定制成功提示。"""
        try:
            band = image[975:1050, 30:690]
            r, g, b = band[..., 2].astype(int), band[..., 1].astype(int), band[..., 0].astype(int)
            sat = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
            white = (r > 200) & (g > 200) & (b > 200) & (sat < 40)
            if white.mean() <= 0.6:
                return False
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR", "roi": [90, 960, 540, 130], "expected": r"[^\n]+",
                }},
            )
            # 长卡名会让成功提示换行；只合并气泡文字区，避开两侧闪光和背后的商店文字。
            results = []
            for result in reco.filtered_results if reco and reco.hit else []:
                box = getattr(result, "box", None)
                if box is not None and not (90 <= box[0] + box[2] / 2 < 630
                                            and 960 <= box[1] + box[3] / 2 < 1090):
                    continue
                results.append(result)
            results.sort(key=lambda result: (result.box[1], result.box[0]) if getattr(result, "box", None) is not None else (0, 0))
            texts = [self._success_text_key(result.text) for result in results]
            card = self._success_text_key(self._cur_card)
            expected = self._success_text_key(f"{self._cur_card or ''}をカスタマイズしました")
            return bool(card and (expected in texts or "".join(texts) == expected))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 完成提示检测异常 {e!r}")
            return False

    def _at_custom_limit(self, context: Context, image) -> bool:
        """只接受合計位置的明确上限文字，不能把菜单单项上限当作整卡完成。"""
        reco = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": self.TOTAL_ROI, "expected": "カスタマイズ上限",
            }},
        )
        return bool(reco and reco.hit and any(
            "カスタマイズ上限" in re.sub(r"\s+", "", result.text or "") for result in reco.filtered_results
        ))

    @staticmethod
    def _confirmation_network_busy(context: Context, image) -> bool:
        """只确认通信弹窗或右下角 CONNECTING，不将商店残影视为提交结果。"""
        if not hasattr(context, "run_recognition"):
            return False
        if common._hif_network_dialog_possible(image):
            retry = context.run_recognition("ProduceHIF__ProduceNetworkRetry", image)
            if retry and retry.hit:
                return True
        reco = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": [500, 1200, 220, 80], "expected": "CONNECTING",
            }},
        )
        return bool(reco and reco.hit and any(
            re.sub(r"\s+", "", result.text or "").upper() == "CONNECTING"
            for result in getattr(reco, "filtered_results", [])
        ))

    def _handle_execution_confirmation(self, context: Context, image) -> str:
        """正常确认10秒；通信等待最多30秒，恢复后重新确认，整体最多40秒。"""
        deadline = _hif_recovery.now() + 10.0
        wall_deadline = time.monotonic() + 40.0
        network_deadline = None
        paused = _hif_recovery.paused
        network_waiting = network_timeout = False
        success_frames = stable_frames = unchanged_frames = 0
        stable_total = None
        success_seen = False
        while not context.tasker.stopping:
            if time.monotonic() >= wall_deadline:
                network_timeout = True
                break
            if _hif_recovery.paused > paused:
                # 通信弹窗重试可能在原10秒预算即将耗尽时才出现。
                paused = _hif_recovery.paused
                network_deadline = network_deadline or time.monotonic() + 30.0
                network_waiting = True
            if self._confirmation_network_busy(context, image):
                if network_deadline is None:
                    network_deadline = time.monotonic() + 30.0
                if not network_waiting:
                    logger.info("HIF技能卡定制: 通信处理中，只等待原提交结果，不重复执行")
                network_waiting = True
                success_frames = stable_frames = unchanged_frames = 0
                stable_total = None
                remaining = min(network_deadline, wall_deadline) - time.monotonic()
                if remaining <= 0:
                    network_timeout = True
                    break
                time.sleep(min(0.5, remaining))
                if context.tasker.stopping:
                    return "stopped"
                image = common._hif_nonbattle_screencap(context, retry_limit=1)
                continue
            if network_waiting:
                deadline = _hif_recovery.now() + 10.0
                success_frames = stable_frames = unchanged_frames = 0
                stable_total = None
                network_waiting = False
                logger.info("HIF技能卡定制: 通信恢复，重新等待原提交结果确认")
            success_frames = success_frames + 1 if self._confirm_completed(context, image) else 0
            if context.tasker.stopping:
                return "stopped"
            if success_frames >= 3 and not success_seen:
                success_seen = True
                self._confirm_pending_option()
                logger.info(f"HIF技能卡定制: 「{self._cur_card}」本次操作成功，卡名及完成提示连续三帧")
                self._click(context, self.TAP_CENTER_POS)
                # 成功提示可能遮住合計；下一帧再判断剩余次数/上限。
                stable_frames = 0
            else:
                total = self._read_total(context, image)
                # 提交前只剩一次且当前卡成功已确认，就已耗尽；上限时合計标签会消失。
                exhausted = total == 0 or (success_seen and (
                    self._last_total == 1 or (total is None and self._at_custom_limit(context, image))))
                observed = 0 if exhausted else total
                stable_frames = stable_frames + 1 if observed is not None and observed == stable_total else 1
                stable_total = observed
                if stable_frames >= 3 and observed is not None:
                    if context.tasker.stopping:
                        return "stopped"
                    if observed == 0:
                        if self._last_total is not None and observed < self._last_total:
                            self._confirm_pending_option()
                        if not self._option_counts:
                            return "execute"  # 尚未读逐项次数，不能把总次数耗尽当作目标完成。
                        return self._finish_custom_targets(context, "游戏总次数已耗尽")
                    if self._last_total is None:
                        return "execute"   # 提交前动画已稳定，现在才允许首次提交。
                    if observed < self._last_total:
                        self._confirm_pending_option()
                        logger.info(f"HIF技能卡定制: 「{self._cur_card}」本次操作成功，剩余次数{self._last_total}→{observed}")
                        self._exec_stall = 0
                        return "execute"
                    unchanged_frames += 1
            if context.tasker.stopping:
                return "stopped"
            remaining = min(deadline - _hif_recovery.now(), wall_deadline - time.monotonic())
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if context.tasker.stopping:
                return "stopped"
            image = common._hif_nonbattle_screencap(context, retry_limit=1)
        if context.tasker.stopping:
            return "stopped"
        if network_timeout:
            common._hif_failure(context, 'ProduceHIFCardCustomFlag', '定制通信等待', '等待网络或提交结果超时，不重复提交，保留现场')
            return "failed"
        reason = "执行结果未确认"
        if unchanged_frames >= self.EXEC_STALL_LIMIT:
            reason += f"（合計{stable_total}连续不减少，P点不足/无法定制）"
        logger.warning(f"HIF技能卡定制: 「{self._cur_card}」{reason}，不重复提交，返回列表")
        if self._pending_option is not None:
            common._hif_failure(context, 'ProduceHIFCardCustomFlag', '定制提交后', reason + '；不返回列表重放')
            return 'failed'
        self._click(context, self.BACK_TO_LIST_POS)
        self._advance_queue(reason=reason)
        return "list"

    def _menu_available(self, context: Context, image, idx: int) -> Optional[int]:
        """逐项剩余次数；只有明确上限文字才返回0，未读到返回None。"""
        for roi, expected in ((self.MENU_LEFT_ROIS[idx], r"あと\s*\d+"),
                              (self.MENU_LIMIT_ROIS[idx], "カスタマイズ上限")):
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore", image,
                    pipeline_override={
                        "ProduceHIF__ProduceRecognitionScore": {
                            "recognition": "OCR", "roi": roi, "expected": expected,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF技能卡定制: 菜单可用性识别异常 {e!r}")
                return None
            if not (reco and reco.hit and reco.filtered_results):
                continue
            for r in reco.filtered_results:
                text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", r.text or ""))
                if "カスタマイズ上限" in text:
                    return 0
                m = re.search(r"あと(\d+)", text)
                if m:
                    return int(m.group(1))
        return None

    def _read_custom_counts(self, context: Context, image) -> Optional[dict]:
        """逐项次数连续两帧一致才使用；OCR未知或上游与实机矛盾时不提交。"""
        menu = self.custom_cards[self._cur_card]["menu"]
        deadline = time.monotonic() + 5.0
        previous = None
        while not context.tasker.stopping:
            counts = {}
            for option in menu:
                if context.tasker.stopping:
                    return None
                remaining = self._menu_available(context, image, option["index"])
                if remaining is None or not 0 <= remaining <= option["max"]:
                    counts = None
                    break
                counts[option["id"]] = option["max"] - remaining
            if context.tasker.stopping:
                return None
            total = self._read_total(context, image)
            observed = (counts, total)
            if counts is not None and total is not None and observed == previous:
                self._last_total = total
                return {key: max(value, self._option_counts.get(key, 0)) for key, value in counts.items()}
            previous = observed
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                return None
            time.sleep(min(0.5, remaining_time))
            if context.tasker.stopping:
                return None
            image = common._hif_nonbattle_screencap(context)
        return None

    def _confirm_pending_option(self):
        if self._pending_option is not None:
            option_id = self._pending_option
            self._option_counts[option_id] = self._option_counts.get(option_id, 0) + 1
            self._pending_option = None

            _hif_recovery.pending = None

    def _finish_custom_targets(self, context: Context, reason="") -> str:
        menu = self.custom_cards.get(self._cur_card, {}).get("menu", [])
        missing = [f"{item['name']}={self._option_counts.get(item['id'], '?')}/{item['target']}"
                   for item in menu if self._option_counts.get(item['id'], -1) < item["target"]]
        if context.tasker.stopping:
            return "stopped"
        self._click(context, self.BACK_TO_LIST_POS)
        detail = reason + "；未完成目标: " + ", ".join(missing) if missing else ""
        self._advance_queue(completed=not missing, reason=detail)
        return "list"

    def _current_menu_items(self, context: Context) -> list:
        """返回面板已选定制项目在两列或三列菜单中的格位。"""
        if not self._cur_card:
            return []
        cfg = self.custom_cards.get(self._cur_card)
        if not cfg or not isinstance(cfg.get("menu"), list):
            return []
        return [tuple(item["pos"]) for item in cfg["menu"]]

    def _read_total(self, context: Context, image) -> Optional[int]:
        """读「合計あとN回」绿标签的剩余可加总次数；读不到返回 None。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": self.TOTAL_ROI, "expected": self.TOTAL_PAT}
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 读合計次数异常 {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        for r in reco.filtered_results:
            m = re.search(r"あと(\d+)", r.text or "")
            if m:
                return int(m.group(1))
        return None

    def _advance_queue(self, *, completed: bool = False, reason: str = "") -> None:
        """记录当前卡结果后推进队列；前缀中的卡不再重复处理。"""
        if self._cur_card:
            if completed:
                self.done_cards.add(self._cur_card)
            else:
                self.skipped_cards[self._cur_card] = reason
            logger.info(f"HIF技能卡定制: 「{self._cur_card}」{'已完成' if completed else '未完成: ' + reason}")
        self._queue_idx += 1
        self._card_custom_click = 0   # 换新卡,重置点カスタマイズ计数
        self._total_none = 0
        if self._queue_idx >= len(self._queue):
            self._done_all = True
            self._cur_card = None
            logger.info("HIF技能卡定制: 队列已全部处理完")
        else:
            self._cur_card = self._queue[self._queue_idx]
            logger.info(f"HIF技能卡定制: 队列推进到下一张「{self._cur_card}」")
        self._menu_attempt = 0
        self._exec_stall = 0          # 换新卡,重置合計不减少计数
        self._last_total = None       # 换新卡,重置上次合計
        self._option_counts = {}
        self._pending_option = None

    def _handle_detail_execute(self, context: Context, image) -> str:
        """只补足逐项累计目标；轮换未达目标项，一次提交后等待确认。"""
        if context.tasker.stopping:
            return "stopped"
        menu_items = self._current_menu_items(context)
        if not menu_items:
            self._menu_attempt = 0
            logger.info("HIF技能卡定制: 当前卡无勾选的项 → 点一覧に戻る @ " + str(self.BACK_TO_LIST_POS))
            self._click(context, self.BACK_TO_LIST_POS)
            self._advance_queue(reason="当前卡无勾选的菜单项")
            return "list"
        total = self._read_total(context, image)
        if total is None:
            # 未提交前OCR缺失也只等待；零次数需连续两帧确认。
            self._last_total = None
            return "confirm"
        self._last_total = total
        counts = self._read_custom_counts(context, image)
        if context.tasker.stopping:
            return "stopped"
        if counts is None:
            self._click(context, self.BACK_TO_LIST_POS)
            self._advance_queue(reason="逐项定制次数未确认，不提交")
            return "list"
        self._option_counts = counts
        total = self._last_total
        menu = self.custom_cards[self._cur_card]["menu"]
        eligible = [index for index, item in enumerate(menu) if counts[item["id"]] < item["target"]]
        if not eligible or total == 0:
            return self._finish_custom_targets(context, "游戏总次数已耗尽" if total == 0 else "")
        chosen = next((index % len(menu) for index in range(self._menu_attempt, self._menu_attempt + len(menu))
                       if index % len(menu) in eligible))
        self._menu_attempt = chosen + 1
        pos = menu_items[chosen]
        option = self.custom_cards[self._cur_card]["menu"][chosen]
        logger.info(
            f"HIF技能卡定制: 「{self._cur_card}」合計剩余{total if total is not None else '?'}次 "
            f"→ 选择「{option['name']}」(ID={option['id']}, 累计{counts[option['id']]}/{option['target']}) "
            f"@ {pos} → 点実行する @ {self.EXEC_POS}"
        )
        self._click(context, pos)
        if context.tasker.stopping:
            return "stopped"
        self._pending_option = option["id"]
        _hif_recovery.pending = f'定制:{self._cur_card}:{option["id"]}'
        self._click(context, self.EXEC_POS)
        logger.info(f"HIF技能卡定制: 「{self._cur_card}」已提交，进入等待确认，提交前剩余{total}次")
        return "confirm"

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """显式状态机：主界面→选卡→待执行→提交后确认。

        不靠每帧 OCR 猜当前界面（选卡/待执行底部都有「あとN枚」，会互相误判），
        而是用 stage 明确当前阶段，只推进对应 handler，阶段流转清晰可靠。
        """
        _hif_recovery.bind(argv)
        if common._hif_stopping(context):
            return True
        logger.info("HIF技能卡定制: CardCustomAuto.run 已调用, 开始构建队列")
        try:
            return self._run_inner(context, argv)
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f"HIF技能卡定制: run 异常 {e!r}")
            return self._skip_customization(context, f'定制异常 {e!r}')

    def _skip_customization(self, context, reason) -> bool:
        """Skip only from a confirmed shop, without an unresolved submission."""
        if common._hif_stopping(context):
            return True
        if _hif_recovery.pending or getattr(self, '_pending_option', None) is not None:
            return common._hif_failure(context, 'ProduceHIFCardCustomFlag', '定制/回血恢复', reason + '；提交结果待确认')
        def shop(frame):
            flag = context.run_recognition('ProduceHIF__ProduceHIFCardCustomFlag', frame)
            return bool(flag and flag.hit and not self._heal_dialog_visible(context, frame))
        if not common._hif_observe_result(context, shop):
            return self._hand_back(context, '定制/回血恢复', reason + '；商店未确认')
        if common._hif_stopping(context):
            return True
        logger.warning(f'HIF安全降级: 已确认商店，跳过剩余定制及购饮；原因={reason}')
        self._click(context, self.TERMINATE_POS)
        if common._hif_stopping(context):
            return True
        if common._hif_observe_result(context, lambda frame: bool(common._hif_safe_scene(
                context, frame, exclude=('ProduceHIFCardCustomFlag', 'ProduceHIFConsultShopFlag')))):
            return True
        # 商店仍在时不交回入口，避免重新进入定制流程；离开后出现其他已知场景即继续。
        return self._hand_back(context, '商店退出', '未确认退出，不重复结束点击', still_there=shop)

    def _wait_custom_page(self, context: Context, stage: str) -> bool:
        """返回点击后只观察；连续确认目标页面，才允许下一阶段操作。"""
        def visible(image):
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            if stage == "main":
                return bool(shop and shop.hit and not self._heal_dialog_visible(context, image))
            if shop and shop.hit:
                return False
            # 选卡页正常保留当前卡的详情与定制菜单。以底部「もどる」和枚数确认列表，
            # 不能把顶部菜单当成弹窗，也不能用会读到同一枚数的合計 ROI 排除列表。
            back = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR", "roi": [90, 1120, 270, 90], "expected": "もどる",
                }},
            )
            return bool(back and back.hit and self._read_left(context, image, self.SELECT_LEFT_ROI) is not None)

        return common._hif_observe_result(context, visible)

    def _custom_page_lost(self, context: Context, stage: str) -> bool:
        """返回点击后未确认目标页：只读宽限后交回入口（结束本动作）；定制结果待确认时停止。"""
        if common._hif_stopping(context):
            return True
        return self._hand_back(context, '返回商店' if stage == 'main' else '返回列表', '未确认返回，不继续选卡、购饮或结束点击')

    def _hand_back(self, context: Context, stage: str, reason: str, still_there=None) -> bool:
        """定制商店内的超时统一走宽限；交回入口后重新进入商店时跳过一次定制前回血，避免重复消耗P点。"""
        if not common._hif_timeout(context, 'ProduceHIFCardCustomFlag', stage, reason, still_there=still_there):
            return False
        type(self)._skip_heal_until = time.monotonic() + self.REENTRY_HEAL_SKIP_SECONDS
        return True

    def _run_inner(self, context: Context, argv: CustomAction.RunArg) -> bool:
        self._shop_consumption_stopped = False
        if time.monotonic() < type(self)._skip_heal_until:
            type(self)._skip_heal_until = 0.0
            logger.info("HIF中场回血: 超时宽限后重新进入商店，本次跳过定制前回血，避免重复消耗P点")
        elif not self._heal_before_customization(context):
            return self._skip_customization(context, '中场回血后商店未恢复')
        stage, step, unknown = "main", 0, 0
        profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
        self._load_custom_cards(profession)
        # 面板文件只保留已导入卡；每张卡的 menu 只含已勾选的定制项目。
        enabled = self._customization_enabled(context)
        self._unconfirmed_slots = []
        self._queue = list(self.custom_cards) if enabled else []
        if not enabled:
            logger.info("HIF技能卡定制: 前台开关已关闭，跳过面板卡牌定制")
        self._queue = self._queue[: self.MAX_CARDS]   # 一次育成最多6张,再多钱不够
        self._queue_idx = 0
        self.done_cards.clear()
        self.skipped_cards.clear()
        self._option_counts = {}
        self._pending_option = None
        self._menu_attempt = 0
        self._last_total = None
        self._done_all = not bool(self._queue)
        self._cur_card = self._queue[0] if self._queue else None
        logger.info(f"HIF技能卡定制: 职业={profession}, 进入状态机, 需定制队列={self._queue}")
        while not context.tasker.stopping and step < self.STEP_LIMIT:
            step += 1
            # 提交后的通信弹窗每帧最多重试一次，由确认阶段统一限制等待时间。
            image = common._hif_nonbattle_screencap(context, retry_limit=1) if stage == "confirm" else common._hif_nonbattle_screencap(context)
            if common._hif_stopping(context):
                return True

            if stage == "main":
                act = self._handle_main(context, image)
                if act == "stopped":
                    return True
                if act == "failed":
                    return False
                if act in ("exit", "handed_back"):
                    return True   # 已在商店主界面完成购饮和終了，不再误点もどる
                if act == "enter":
                    stage = "select"; unknown = 0; continue
                if act == "refresh":
                    continue   # 保持main：等刷新动画完成，下帧重新读枚数(不直接进选卡)
                unknown += 1
                if unknown >= self.BETWEEN_LIMIT:
                    logger.warning("HIF技能卡定制: 自定义入口未能进入选卡页，停止后续点击")
                    return self._skip_customization(context, '自定义入口未能进入选卡页')
                time.sleep(0.5)
                continue
            if stage == "select":
                act = self._handle_select(context, image)
                if act == "stopped":
                    return True
                if act == "pick":
                    unknown = 0; continue
                if act == "detail":
                    stage = "execute"; unknown = 0; continue
                if act == "back":
                    if not self._wait_custom_page(context, "main"):
                        return self._custom_page_lost(context, "main")
                    # 回主界面：队列完成→点終了；没次数→_handle_main 会点刷新并重进。不 break。
                    # 无进展安全网: 连续多次回主界面但队列没推进(目标卡不在可刷新批次) → 强制结束点終了
                    if self._queue_idx != self._last_back_qidx:
                        self._no_progress_cycles = 0      # 队列有推进 → 正常
                    else:
                        self._no_progress_cycles += 1
                        if self._no_progress_cycles >= self.MAIN_CYCLE_LIMIT:
                            logger.info(f"HIF技能卡定制: 连续{self.MAIN_CYCLE_LIMIT}次回主界面无进展(目标卡不在批次/钱不够) → 强制结束")
                            self.skipped_cards.update({name: "多次返回主界面仍无进展" for name in self._queue[self._queue_idx:]})
                            self._done_all = True
                    self._last_back_qidx = self._queue_idx
                    stage = "main"; unknown = 0; continue
                # unknown：非待选卡界面，转待执行处理
                stage = "execute"; continue
            if stage == "confirm":
                act = self._handle_execution_confirmation(context, image)
                if act == 'failed':
                    return False
                if act == "stopped":
                    return True
                if act == "list" and not self._wait_custom_page(context, "select"):
                    return self._custom_page_lost(context, "select")
                stage = "select" if act == "list" else "execute"
                unknown = 0
                continue
            if stage == "execute":
                act = self._handle_detail_execute(context, image)
                if act == "stopped":
                    return True
                if act == "confirm":
                    stage = "confirm"; unknown = 0; continue
                if act == "execute":
                    # 保持 execute：连续加本卡的下一个菜单项(点完一个接着点下一个,不重识别卡名)
                    unknown = 0; continue
                if act == "list":
                    if not self._wait_custom_page(context, "select"):
                        return self._custom_page_lost(context, "select")
                    stage = "select"; unknown = 0; continue # 本卡菜单项点完→回选卡
                unknown += 1
                if unknown >= self.BETWEEN_LIMIT:
                    logger.warning(f"HIF技能卡定制: 连续{self.BETWEEN_LIMIT}步无进展，退出防卡死")
                    break

        if context.tasker.stopping:
            logger.info("HIF技能卡定制: 任务已停止，跳过退出收尾点击")
            return True
        return self._skip_customization(context, '定制状态机恢复耗尽')
