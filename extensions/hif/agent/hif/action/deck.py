"""HIF 技能卡换入与删除：授业换卡、交换、删卡事件与支给领卡。"""
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

from . import common
from .battle import ProduceHIF__ProduceCardsAuto
from .common import _hif_date_interrupt
from .drinks import ProduceHIF__ProduceHIFDrinkAuto


class _ExchangeStopped(Exception):
    """用户停止任务时中断交换页扫描，保留现场。"""


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardSwapAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFCardSwapAuto(CustomAction):
    """
    HIF换卡：逐张点候选卡，只读详情卡名识别「需要的卡」，命中才点次へ。

    默认换卡界面为3张候选卡；开启前台「老师活动」后改用4张候选卡布局。
    点卡后顶部预览显示卡名。
    依次点击每张→等预览更新→按卡名与当前职业 preferred_acquisition_profiles 名单比较。
    命中→点次へ确认；全部候选都无→开启「授业再抽选」时再抽選并重扫；仍无→选最后一轮第一张再点次へ。
    当前职业名单为空→直接次へ。名单条目可为字符串或带 name 的字典。

    再抽選次数由两次授业与支给共用，脚本不设上限：「再抽選」按钮消失说明游戏内次数已用完，
    本轮不再重抽；该标记在每轮第一次授业(计数6)重置。
    """

    CLICK_DELAY = 0.8
    NEXT_POS = (382, 1095)                                   # 底部「次へ」确认按钮(日常实测 次入 350-413,y1078-1114)
    NEXT_ROI = [270, 1060, 200, 60]
    NEXT_EXPECTED = "次[へヘ入]"
    CARD_SLOTS = [(224, 902), (346, 902), (506, 902)]        # 默认3张候选卡中心
    TEACHER_EVENT_CARD_SLOTS = [                              # 老师活动4张候选卡中心(20260921截图)
        (150, 902), (290, 902), (430, 902), (568, 902),
    ]
    REDRAW_PAGE_SAFETY_MAX = 10                              # 单页重抽保险上限，防止按钮未消失时(如变灰仍被 OCR 读到)无限重抽
    NAME_ROI = [185, 505, 370, 50]      # 详情标题行；避开左右两侧的费用和图标
    OCR_ALL = r"[^\n]+"                 # 宽匹配读取卡名，包括名单外候选卡
    REDRAW_ROI = [535, 1070, 145, 55]   # 「再抽選」按钮 OCR 定位区(日常 再抽選 578-645,y1083-1110)
    REDRAW_EXPECTED = "再抽選"
    REDRAW_LOCATE_ATTEMPTS = 3          # 连续多帧都读不到「再抽選」才判定按钮已消失
    REDRAW_LOCATE_INTERVAL = 0.3
    LESSON_REDRAW_NODE = "ProduceHIF__ProduceHIFLessonRedraw"
    SUPPLY_REDRAW_NODE = "ProduceHIF__ProduceHIFSupplyRedraw"
    DETAIL_STABLE_COUNT = 3
    DETAIL_STABLE_TIMEOUT = 3.0
    DETAIL_POLL_INTERVAL = 0.25
    # 前台换卡开关采用与「相谈购买饮料」相同的 enabled 占位节点传值。
    # a/b 仅在当前 HIF 授业场次内保存；培养入口与每场第一次授业(计数6)都会重置。
    SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE = "ProduceHIF__ProduceHIFSecondSwapPreferFirstAcquiredCardOut"
    SECOND_SWAP_CONSULT_DELETE_NODE = "ProduceHIF__ProduceHIFSecondSwapConsultDeleteAcquiredCard"
    TEACHER_EVENT_NODE = "ProduceHIF__ProduceHIFTeacherEvent"
    # 已完成的换出动作数：0=准备第一次，1=准备第二次。以交换完成为准，
    # 不因候选页被重复识别而跳过第二次“替换 a”的分支。
    _delete_card_exchange_count = 0
    _delete_card_a = None
    _delete_card_b = None
    _delete_card_delete_attempted = False
    # 两次授业与支给的再抽選计数(仅日志用)；按钮消失后本轮不再尝试。
    _redraw_used = 0
    _redraw_exhausted = False

    @classmethod
    def reset_redraw_state(cls) -> None:
        """每轮第一次授业时清空再抽選计数与「按钮已消失」标记。"""
        cls._redraw_used = 0
        cls._redraw_exhausted = False

    @classmethod
    def redraw_switch_enabled(cls, context: Context, node_name: str, label: str) -> bool:
        """读取前台授业/支给再抽选开关；读取失败时按关闭处理。"""
        try:
            node = context.get_node_data(node_name)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF{label}再抽選: 读取前台开关异常 {e!r}")
            return False

    @classmethod
    def note_redraw(cls, label: str) -> None:
        cls._redraw_used += 1
        logger.info(f"HIF{label}再抽選: 本轮已重抽 {cls._redraw_used} 次")

    @classmethod
    def mark_redraw_exhausted(cls, label: str) -> None:
        cls._redraw_exhausted = True
        logger.info(f"HIF{label}再抽選: 未找到「再抽選」按钮，视为游戏内次数已用完，本轮不再重抽")

    def locate_redraw(self, context: Context, roi: list) -> Optional[tuple]:
        """多帧 OCR 定位「再抽選」按钮中心；均未命中返回 None(按钮已消失)。"""
        for attempt in range(self.REDRAW_LOCATE_ATTEMPTS):
            if getattr(getattr(context, "tasker", None), "stopping", False):
                return None
            if attempt:
                time.sleep(self.REDRAW_LOCATE_INTERVAL)
            try:
                image = common._hif_nonbattle_screencap(context)
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore", image,
                    pipeline_override={
                        "ProduceHIF__ProduceRecognitionScore": {
                            "recognition": "OCR", "roi": roi,
                            "expected": self.REDRAW_EXPECTED,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF再抽選: OCR异常 {e!r}")
                continue
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                return box[0] + box[2] // 2, box[1] + box[3] // 2
        return None

    @classmethod
    def reset_delete_card_state(cls) -> None:
        """清空当前授业场次的换卡状态，防止跨场沿用旧 OCR 结果。"""
        cls._delete_card_exchange_count = 0
        cls._delete_card_a = None
        cls._delete_card_b = None
        cls._delete_card_delete_attempted = False

    @classmethod
    def _delete_card_enabled(cls, context: Context) -> bool:
        """读取前台「删卡」开关；未配置或读取失败时按关闭处理。"""
        try:
            node = context.get_node_data(cls.SECOND_SWAP_CONSULT_DELETE_NODE)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF删卡: 读取前台开关异常 {e!r}")
            return False

    @classmethod
    def _wanted_card_swap_enabled(cls, context: Context) -> bool:
        """读取前台「第二次优先获取卡」开关；未配置或读取失败时按关闭处理。"""
        try:
            node = context.get_node_data(cls.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF第二次优先获取卡: 读取前台开关异常 {e!r}")
            return False

    @classmethod
    def _tracked_swap_enabled(cls, context: Context) -> bool:
        """两种模式共用第一次换到的 a 与已完成换出次数。"""
        return cls._delete_card_enabled(context) or cls._wanted_card_swap_enabled(context)

    @classmethod
    def _teacher_event_enabled(cls, context: Context) -> bool:
        """读取前台「老师活动」开关；未配置或读取失败时保持默认3格。"""
        try:
            node = context.get_node_data(cls.TEACHER_EVENT_NODE)
            return bool(node and node.get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF老师活动: 读取前台开关异常 {e!r}")
            return False

    @classmethod
    def _candidate_slots(cls, context: Context) -> list:
        return cls.TEACHER_EVENT_CARD_SLOTS if cls._teacher_event_enabled(context) else cls.CARD_SLOTS

    @classmethod
    def _begin_delete_card_swap(cls, enabled: bool) -> Optional[int]:
        """返回当前目标卡顺位；关闭时完全不启用删卡状态机。"""
        if not enabled:
            return None
        index = cls._delete_card_exchange_count
        logger.info(f"HIF换卡记录: 第{index + 1}次授业换卡目标选卡开始")
        return index

    @classmethod
    def finish_delete_card_exchange(cls, enabled: bool) -> None:
        """在一次换出操作结束后推进阶段，供下一次授业换卡判断。"""
        if not enabled:
            return
        cls._delete_card_exchange_count += 1
        logger.info(f"HIF换卡记录: 第{cls._delete_card_exchange_count}次换出已结束")

    @classmethod
    def _remember_delete_card_target(cls, swap_index: Optional[int], name_text: str) -> None:
        """用选中目标卡详情区的 OCR 原文记录 a/b，不以配置目标名代替。"""
        if swap_index not in (0, 1):
            return
        card_name = cls._canonical_remembered_card_name(name_text)
        if not card_name:
            logger.warning(
                f"HIF换卡选卡: 第{swap_index + 1}次目标卡名 OCR 为空，"
                "本次不记录删卡目标"
            )
            return
        if swap_index == 0:
            cls._delete_card_a = card_name
            logger.info(f"HIF换卡记录: 已记住第一次换卡 a=[{card_name}]")
        else:
            cls._delete_card_b = card_name
            logger.info(f"HIF换卡记录: 已记住第二次换卡 b=[{card_name}]")

    @staticmethod
    def _canonical_remembered_card_name(name_text: str) -> str:
        """清理换卡 OCR 中附带的费用/属性尾缀，保留可跨界面比较的卡名。"""
        name = "".join(unicodedata.normalize("NFKC", name_text or "").split())
        # 实测换卡页会把卡名与费用/属性连读为「アドリブ-4X」「魅惑の視線M-3+」；
        # 交换页还会附带卡种/数值前缀和参数尾缀，如「S成功への道筋+5」
        # 或「S脚光+D」；只清理这种位于边界的 ASCII UI 标记，不模糊匹配卡名正文。
        # 候选页也可能把左上数值连进卡名，如「7ひと呼吸+」。
        name = re.sub(r"^[A-Za-z0-9]+(?=[^\x00-\x7f])", "", name)
        name = re.sub(r"(?:[A-Za-z])?[+-][A-Za-z0-9]+[+＋]?$", "", name)
        return name.rstrip("+＋")

    @classmethod
    def remembered_card_name_hit(cls, name_text: str, target: str) -> bool:
        """跨界面严格匹配同一卡名；仅清理已知费用尾缀，不做模糊猜测。"""
        name = cls._canonical_remembered_card_name(name_text)
        wanted = cls._canonical_remembered_card_name(target)
        return bool(name and wanted and name == wanted)

    def _click(self, context: Context, pos, delay=None) -> None:
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else self.CLICK_DELAY)

    def _load_swap_want(self, context: Context) -> list:
        """读取当前职业的优先获取卡名单。"""
        try:
            with open(common.CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"HIF换卡: 读取优先获取卡配置失败 {e!r}")
            return []
        profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
        profiles = cfg.get("preferred_acquisition_profiles")
        raw = profiles.get(profession, []) if isinstance(profiles, dict) else []
        specs = []
        for it in raw:
            if isinstance(it, str):
                specs.append({"name": it})
            elif isinstance(it, dict):
                specs.append({"name": it.get("name")})
        wanted_names = [spec["name"] for spec in specs if spec.get("name")]
        logger.info(
            f"HIF换卡: 角色职业={profession}，优先获取卡名单="
            f"{wanted_names if wanted_names else '空'}"
        )
        return specs

    def _read_ocr(self, context: Context, image, roi: list, expected: str) -> str:
        """宽 OCR 读 roi 内全部文字并拼接(以 expected 为读全锚点)。读不到返回 "". """
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
            logger.warning(f"HIF换卡: OCR异常 roi={roi} {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(r.text for r in reco.filtered_results)

    def _wait_stable_card_name(self, context: Context) -> tuple:
        """等待候选卡详情名连续稳定，避免读取点击动画中的上一张卡名。"""
        deadline = time.time() + self.DETAIL_STABLE_TIMEOUT
        last_name = ""
        stable_count = 0
        last_image = None
        while time.time() < deadline:
            if getattr(getattr(context, "tasker", None), "stopping", False):
                return last_image, ""
            last_image = common._hif_nonbattle_screencap(context)
            name = self._read_ocr(context, last_image, self.NAME_ROI, self.OCR_ALL)
            normalized = "".join((name or "").split())
            if normalized and normalized == last_name:
                stable_count += 1
            elif normalized:
                last_name = normalized
                stable_count = 1
            else:
                last_name = ""
                stable_count = 0
            if stable_count >= self.DETAIL_STABLE_COUNT:
                return last_image, name
            time.sleep(self.DETAIL_POLL_INTERVAL)
        logger.warning("HIF换卡选卡: 候选卡名在等待时间内未稳定，本次按未识别处理")
        return last_image, ""

    def _wait_candidate_page_ready(self, context: Context) -> bool:
        """重抽/复核后同时确认候选页按钮与卡面稳定，避免点击仍在刷新的卡。"""
        deadline = time.time() + 6.0
        previous = None
        stable = 0
        while time.time() < deadline:
            if context.tasker.stopping:
                return False
            image = common._hif_nonbattle_screencap(context)
            ready = self._read_ocr(context, image, self.NEXT_ROI, self.NEXT_EXPECTED)
            current = None
            if cv2 is not None and getattr(image, "shape", ())[:2] == (1280, 720):
                current = image[820:1020, 80:640].copy()
            unchanged = current is None or (previous is not None and cv2.norm(
                current, previous, cv2.NORM_L1) / current.size <= 2.5)
            stable = stable + 1 if ready and unchanged else 0
            previous = current
            if stable >= 3:
                logger.info("HIF换卡: 候选页按钮和卡面已连续稳定")
                return True
            time.sleep(self.DETAIL_POLL_INTERVAL)
        logger.warning("HIF换卡: 候选页仍未稳定，限时等待结束，进入逐格复核")
        return False

    def _read_candidate_card(self, context: Context, pos) -> str:
        if getattr(getattr(context, "tasker", None), "stopping", False):
            return ""
        self._click(context, pos)
        _, name = self._wait_stable_card_name(context)
        if not name:
            self._wait_candidate_page_ready(context)
            if getattr(getattr(context, "tasker", None), "stopping", False):
                return ""
            logger.info(f"HIF换卡: 候选卡名未确认，限时等待后复核同一格 @ {pos}")
            self._click(context, pos)
            _, name = self._wait_stable_card_name(context)
        return name

    def _match_wanted(self, name_text: str, specs: list) -> Optional[str]:
        """规范化后完整匹配详情卡名，不用子串或相似度猜测。"""
        for spec in specs:
            nm = spec.get("name")
            if nm and self.remembered_card_name_hit(name_text, nm):
                logger.info(f"HIF换卡: 卡名命中「{nm}」 OCR=[{name_text}]")
                return nm
        return None

    def _click_redraw(self, context: Context) -> bool:
        """OCR 定位并点击「再抽選」(重抽一次)；按钮已消失时标记次数用尽并返回 False。"""
        pos = self.locate_redraw(context, self.REDRAW_ROI)
        if pos is None:
            if not getattr(getattr(context, "tasker", None), "stopping", False):
                self.mark_redraw_exhausted("授业")
            return False
        logger.info(f"HIF换卡: 点再抽選 @ 识别框中心 {pos}")
        self._click(context, pos)
        self.note_redraw("授业")
        self._wait_candidate_page_ready(context)
        return True

    def _click_next(self, context: Context) -> None:
        """优先 OCR 点击候选页的次へ；识别失败才使用稳定回退坐标。"""
        try:
            image = common._hif_nonbattle_screencap(context)
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.NEXT_ROI,
                        "expected": self.NEXT_EXPECTED,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                logger.info(f"HIF换卡: OCR点击次へ @ {pos}")
                self._click(context, pos)
                return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF换卡: 次へOCR异常 {e!r}")
        logger.info(f"HIF换卡: 次へ未识别，回退 @ {self.NEXT_POS}")
        self._click(context, self.NEXT_POS)

    def _scan(self, context: Context, specs: list, swap_index: Optional[int]) -> bool:
        """依次扫描当前布局的候选卡；命中名单后点次へ。"""
        for i, pos in enumerate(self._candidate_slots(context)):
            logger.info(f"HIF换卡: 点候选卡{i + 1} @ {pos}")
            name_text = self._read_candidate_card(context, pos)
            logger.info(f"HIF换卡: 第{i + 1}张 卡名OCR=[{name_text}]")
            if not name_text:
                logger.warning(f"HIF换卡: 第{i + 1}张复核后仍未识别，按非目标卡处理")
                if getattr(getattr(context, "tasker", None), "stopping", False):
                    return False
                continue
            card = self._match_wanted(name_text, specs)
            if card:
                logger.info(f"HIF换卡: 第{i + 1}张命中「{card}」→ 点次へ确认")
                self._remember_delete_card_target(swap_index, name_text)
                self._click_next(context)
                return True
            logger.info(f"HIF换卡: 第{i + 1}张非需要卡")
        return False

    def _scan_delete_card_target(self, context: Context, swap_index: int) -> bool:
        """直接选卡并记录 a/b；老师活动固定使用第4张。"""
        slots = list(enumerate(self._candidate_slots(context), start=1))
        if self._teacher_event_enabled(context):
            slots = slots[-1:]
            logger.info("HIF换卡记录: 老师活动已开启，本次固定选择第4张候选卡")
        for number, pos in slots:
            logger.info(f"HIF换卡选卡: 检查第{number}张候选卡 @ {pos}")
            name_text = self._read_candidate_card(context, pos)
            card_name = "".join((name_text or "").split())
            logger.info(f"HIF换卡选卡: 第{number}张候选卡名 OCR=[{name_text}]")
            if not card_name:
                logger.warning(f"HIF换卡选卡: 第{number}张未识别到精确卡名")
                if getattr(getattr(context, "tasker", None), "stopping", False):
                    return True
                continue
            self._remember_delete_card_target(swap_index, name_text)
            label = "a" if swap_index == 0 else "b"
            logger.info(f"HIF换卡选卡: 第{number}张为本次目标 {label}=[{card_name}] → 点次へ确认")
            self._click_next(context)
            return True
        # 所有候选均没有卡名 OCR 时仍离开该页，避免阻塞培养；不会伪造 a/b，
        # 后续第二次交换会按规则安全回退到基础卡。
        logger.warning("HIF换卡选卡: 本次目标候选卡未识别到卡名 → 接受当前选中卡但不记录目标")
        self._click_next(context)
        return True

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        delete_card = self._delete_card_enabled(context)
        wanted_card_swap = self._wanted_card_swap_enabled(context)
        swap_index = self._begin_delete_card_swap(delete_card or wanted_card_swap)
        # 相谈删除模式保持前两次都直接记录 a/b；第二次优先获取卡模式只在第一次记录 a，
        # 第二次及之后恢复按 swap_want 扫描与重抽。
        if swap_index == 0 or (delete_card and swap_index == 1):
            return self._scan_delete_card_target(context, swap_index)
        specs = self._load_swap_want(context)
        if not specs:
            logger.info("HIF换卡: 当前职业优先获取卡未配置 → 直接点次へ接受当前卡")
            self._click_next(context)
            return True
        remember_index = swap_index if delete_card else None
        result = self._scan(context, specs, remember_index)
        if result:
            return True
        # 再抽選与支给共用游戏内次数：每次重抽后重扫，命中→次へ；按钮消失→次へ兜底
        if not self.redraw_switch_enabled(context, self.LESSON_REDRAW_NODE, "授业"):
            logger.info("HIF换卡: 授业再抽选已关闭，不重抽")
        elif self._redraw_exhausted:
            logger.info("HIF换卡: 本轮「再抽選」按钮已消失，不再重抽")
        else:
            for _ in range(self.REDRAW_PAGE_SAFETY_MAX):
                logger.info("HIF换卡: 候选卡均非需要卡 → 点再抽選重抽再扫")
                if not self._click_redraw(context):
                    break
                if self._scan(context, specs, remember_index):
                    return True
            else:
                logger.warning(f"HIF换卡: 本页已重抽{self.REDRAW_PAGE_SAFETY_MAX}次仍可见再抽選，按保险上限停止")
        if getattr(getattr(context, "tasker", None), "stopping", False):
            return False
        first_pos = self._candidate_slots(context)[0]
        logger.info(f"HIF换卡: 无需要卡 → 选择最后一轮第一张 @ {first_pos}")
        self._click(context, first_pos)
        self._click_next(context)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFExchangeAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFExchangeAuto(CustomAction):
    """
    HIF交换（被换卡=替换旧卡）：逐个点网格卡→OCR详情卡名→点チェンジ(替换)。

    交换界面为 4列x3行 卡片网格(被换候选卡, 720x1280基准 列x=139/285/432/580, 行y=697/843/990),
    点中某卡后中部详情显示其卡名(观察: 卡名带 y~269-301)。换出顺序为
    第二次记录卡 a、当前职业的优先换出名单、基本卡；首次没有 a。
    同名卡的版本顺序和定制版许可按职业、逐卡配置。
    记录卡 a 连续两轮未命中时回退名单和「基本」；所有目标都未命中则替换第一格。
    卡组超过三行时向下滑动继续扫描；滚动位移由前后截图实测，坐标按位移换算，回点时以详情卡名复核。
    """

    CLICK_DELAY = 0.8
    CARD_GRID = [  # 4列x3行网格卡中心(720x1280基准)
        (139, 697), (285, 697), (432, 697), (580, 697),
        (139, 843), (285, 843), (432, 843), (580, 843),
        (139, 990), (285, 990), (432, 990), (580, 990),
    ]
    NAME_ROI = [190, 260, 420, 48]     # 详情标题行；避开左侧卡面数字和右侧图标
    OCR_ALL = r"[^\n]+"
    TARGET = "基本"                    # 卡名含它才点替换(如 パフォーマンスの基本+)
    CHANGE_ROI = [0, 1050, 720, 180]
    CHANGE_EXPECTED = "チ[ェエヱ]ンジ"
    FALLBACK_CHANGE_POS = (530, 1160)  # 右侧深色交换按钮回退
    DETAIL_STABLE_COUNT = 3
    DETAIL_STABLE_TIMEOUT = 3.0
    DETAIL_POLL_INTERVAL = 0.25
    PAGE_CLOSE_TIMEOUT = 6.0
    PAGE_CLOSE_STABLE_COUNT = 3
    SECOND_EXCHANGE_SCAN_PASSES = 2
    # 卡组列表滚动（按 720x1280 实机截图校准：可视区 y≈624-1098，行距146，卡高≈116）
    LIST_VIEW = (75, 624, 645, 1098)   # x0, y0, x1, y1；不含右侧滚动条
    ROW_CENTER_Y0 = 697                # 列表在顶部时第一行卡中心
    ROW_PITCH = 146
    COLUMN_X = (139, 285, 432, 580)
    ROW_CLICK_RANGE = (674, 1048)      # 卡中心在此范围内整张卡可见，才点击
    MAX_ROWS = 40
    SWIPE_X = 360
    SWIPE_SPAN = (1010, 718)           # 慢速上滑约两行
    SWIPE_DURATION = 1000
    SCROLL_SETTLE_TIMEOUT = 2.0
    SHIFT_TEMPLATE_HEIGHT = 120
    MAX_SCROLLS = 10

    @staticmethod
    def _customized_card(image) -> Optional[bool]:
        """识别详情卡面下方的绿色定制次数圆标；截图不可用时不猜测。"""
        if cv2 is None or image is None or getattr(image, "shape", ()) != (1280, 720, 3):
            return None
        # 720x1280 交换页：只检查详情卡面圆标，避开费用、属性及下方卡组。
        region = image[362:394, 92:124]
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, np.array([35, 100, 100]), np.array([90, 255, 255]))
        return cv2.countNonZero(green) >= 60

    def _read_name(self, context, image) -> str:
        """OCR 中部详情卡名带, 返回拼接文本；读不到返回 "". """
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": self.NAME_ROI, "expected": self.OCR_ALL}},
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF交换: 卡名OCR异常 {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(r.text for r in reco.filtered_results)

    def _click_text(self, context, image, roi, expected, fallback) -> bool:
        """OCR 定位 expected 文本并点击其框中心；失败回退 fallback。返回是否 OCR 命中。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": roi, "expected": expected}},
            )
            if reco and reco.hit and reco.filtered_results:
                box = sorted(reco.filtered_results, key=lambda r: r.box[0])[0].box
                x = box[0] + box[2] // 2
                y = box[1] + box[3] // 2
                logger.info(f"HIF交换: 点击「{expected}」 @ ({x}, {y})")
                context.tasker.controller.post_click(x, y).wait()
                time.sleep(self.CLICK_DELAY)
                return True
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF交换: {expected} OCR异常 {e!r}")
        logger.info(f"HIF交换: 未识别到「{expected}」, 回退 @ {fallback}")
        context.tasker.controller.post_click(fallback[0], fallback[1]).wait()
        time.sleep(self.CLICK_DELAY)
        return False

    def _wait_stable_name(self, context: Context) -> tuple:
        """等待被换卡详情名连续稳定，避免读取上一张卡的残留详情。"""
        deadline = time.time() + self.DETAIL_STABLE_TIMEOUT
        last_name = ""
        stable_count = 0
        last_image = None
        while time.time() < deadline:
            self._check_stop(context)
            last_image = common._hif_nonbattle_screencap(context)
            name = self._read_name(context, last_image)
            normalized = "".join((name or "").split())
            if normalized and normalized == last_name:
                stable_count += 1
            elif normalized:
                last_name = normalized
                stable_count = 1
            else:
                last_name = ""
                stable_count = 0
            if stable_count >= self.DETAIL_STABLE_COUNT:
                return last_image, name
            time.sleep(self.DETAIL_POLL_INTERVAL)
        logger.warning("HIF交换: 被换卡名在等待时间内未稳定，本格按未识别处理")
        return last_image, ""

    @staticmethod
    def _exchange_page_visible(context: Context, image) -> bool:
        try:
            reco = context.run_recognition("ProduceHIF__ProduceHIFExchangeFlag", image)
            return bool(reco and reco.hit)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF交换: 检查交换页状态异常 {e!r}")
            return True

    def _wait_exchange_closed(self, context: Context) -> bool:
        """等待交换页连续稳定消失，确认点击已生效后才推进换卡阶段。"""
        deadline = time.time() + self.PAGE_CLOSE_TIMEOUT
        closed_count = 0
        while time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            if not self._exchange_page_visible(context, image):
                closed_count += 1
                if closed_count >= self.PAGE_CLOSE_STABLE_COUNT:
                    return True
            else:
                closed_count = 0
            time.sleep(self.DETAIL_POLL_INTERVAL)
        return False

    def _replacement_targets(self, context: Context, include_recorded_a: bool = True) -> list:
        """返回本次可替换卡名，顺序即优先级。"""
        swap = ProduceHIF__ProduceHIFCardSwapAuto
        targets = []
        if (
            include_recorded_a
            and swap._tracked_swap_enabled(context)
            and swap._delete_card_exchange_count == 1
        ):
            card_a = swap._delete_card_a
            if card_a:
                targets.append(card_a)
                logger.info(f"HIF换卡记录: 第二次授业换卡，首先查找 a=[{card_a}]")
            else:
                logger.warning("HIF换卡记录: 第二次授业换卡未记录 a，继续查找面板名单与基本卡")
        try:
            with open(common.CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
            profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
            profiles = config.get("swap_out_priority_profiles") or {}
            names = profiles.get(profession, []) if isinstance(profiles, dict) else []
            panel_names = []
            if isinstance(names, list):
                for name in names:
                    if isinstance(name, str) and name.strip() and not any(
                        swap.remembered_card_name_hit(name, existing) for existing in targets
                    ):
                        panel_names.append(name.strip())
                        targets.append(name.strip())
            logger.info(f"HIF交换: 职业={profession}，优先换出名单={panel_names}")
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"HIF交换: 读取优先换出名单失败，沿用已有换出规则 {error!r}")
        return [*targets, self.TARGET]

    def _confirm_exchange(self, context: Context, image) -> bool:
        """点击チェンジ，并在删卡模式中推进已完成换出次数。"""
        self._check_stop(context)
        _hif_recovery.pending = '授业交换提交'
        self._click_text(
            context, image, self.CHANGE_ROI, self.CHANGE_EXPECTED,
            self.FALLBACK_CHANGE_POS,
        )
        if not self._wait_exchange_closed(context):
            if not common._hif_observe_result(context, lambda frame: not self._exchange_page_visible(context, frame)
                    and bool(common._hif_safe_scene(context, frame, exclude=('ProduceHIFExchangeFlag',)))):
                # 交换页仍在时无法判断チェンジ是否生效，不交回入口重放。
                if not common._hif_timeout(context, 'ProduceHIFExchangeFlag', '交换提交后', '未确认交换页关闭，不重复チェンジ',
                                           still_there=lambda frame: self._exchange_page_visible(context, frame)):
                    return False
        _hif_recovery.pending = None
        ProduceHIF__ProduceHIFCardSwapAuto.finish_delete_card_exchange(
            ProduceHIF__ProduceHIFCardSwapAuto._tracked_swap_enabled(context)
        )
        return True

    def _swap_out_options(self, context: Context) -> dict:
        """按职业读取逐卡版本规则；两个开关默认开启。"""
        try:
            with open(common.CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as file:
                config = json.load(file)
            profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
            profiles = config.get("swap_out_options_profiles", {})
            rules = profiles.get(profession, {}) if isinstance(profiles, dict) else {}
            return {
                ProduceHIF__ProduceHIFCardSwapAuto._canonical_remembered_card_name(name): (
                    rule.get("prefer_uncustomized", True) is not False,
                    rule.get("allow_customized", True) is True,
                )
                for name, rule in rules.items() if isinstance(rule, dict)
            } if isinstance(rules, dict) else {}
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"HIF交换: 读取逐卡定制换出规则失败，使用保护默认值 {error!r}")
            return {}

    def _check_stop(self, context: Context) -> None:
        if common._hif_stopping(context):
            raise _ExchangeStopped()

    def _list_image(self, context: Context):
        """截取交换页；截图不可用或尺寸不符时返回 None，此时不滚动列表。"""
        try:
            image = common._hif_nonbattle_screencap(context)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF交换: 列表截图异常，不滚动卡组 {e!r}")
            return None
        return image if getattr(image, "shape", None) == (1280, 720, 3) else None

    @classmethod
    def _list_still(cls, first, second) -> bool:
        x0, y0, x1, y1 = cls.LIST_VIEW
        a, b = first[y0:y1, x0:x1].astype(np.int16), second[y0:y1, x0:x1].astype(np.int16)
        return float(np.abs(a - b).mean()) <= 2.5

    @classmethod
    def _scroll_shift(cls, before, after, direction: int) -> Optional[int]:
        """按滑动前后列表区图像测内容实际位移（上移为正）；匹配不唯一时返回 None，不按滑动距离猜。"""
        if cv2 is None or before is None or after is None:
            return None
        x0, y0, x1, y1 = cls.LIST_VIEW
        first = cv2.GaussianBlur(cv2.cvtColor(before[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (5, 5), 0)
        second = cv2.GaussianBlur(cv2.cvtColor(after[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (5, 5), 0)
        band = cls.SHIFT_TEMPLATE_HEIGHT
        start = len(first) - band - 10 if direction > 0 else 10
        template = first[start:start + band]
        if float(template.std()) < 8:
            return None
        scores = cv2.matchTemplate(second, template, cv2.TM_CCOEFF_NORMED).ravel()
        shifts = start - np.arange(len(scores))
        scores[shifts * direction < -3] = -1
        best = int(np.argmax(scores))
        peak = float(scores[best])
        alternatives = scores.copy()
        alternatives[max(0, best - 8):best + 9] = -1
        if peak < 0.80 or peak - float(alternatives.max()) < 0.06:
            return None
        delta = int(shifts[best])
        return 0 if abs(delta) <= 3 else delta

    def _settled_list_image(self, context: Context):
        """滑动后等列表连续两帧不动，避开惯性和回弹。"""
        deadline = time.time() + self.SCROLL_SETTLE_TIMEOUT
        last = None
        while time.time() < deadline:
            time.sleep(self.DETAIL_POLL_INTERVAL)
            image = self._list_image(context)
            if image is None:
                return None
            if last is not None and self._list_still(last, image):
                return image
            last = image
        return last

    def _save_scroll_images(self, before, after) -> None:
        if cv2 is None:
            return
        try:
            folder = os.path.join(common.BASE_DIR, "debug", "custom", "hif_exchange_scroll")
            os.makedirs(folder, exist_ok=True)
            stamp = time.time_ns()
            for label, image in (("before", before), ("after", after)):
                if image is not None:
                    cv2.imwrite(os.path.join(folder, f"{stamp}_{label}.png"), image)
            logger.info(f"HIF交换: 已保存滚动前后截图 {folder}\\{stamp}_*.png")
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF交换: 保存滚动截图失败 {e!r}")

    def _scroll_list(self, context: Context, direction: int) -> Optional[int]:
        """direction=1 查看下方卡、-1 回到上方；返回实测位移，0 表示已到尽头，None 表示无法确认。"""
        self._check_stop(context)
        before = self._list_image(context)
        if before is None:
            return None
        start, end = self.SWIPE_SPAN if direction > 0 else self.SWIPE_SPAN[::-1]
        job = context.tasker.controller.post_swipe(self.SWIPE_X, start, self.SWIPE_X, end, self.SWIPE_DURATION).wait()
        if getattr(job, "succeeded", True) is False:
            logger.warning("HIF交换: 卡组列表滑动失败，不再滚动")
            return None
        after = self._settled_list_image(context)
        delta = self._scroll_shift(before, after, direction)
        if delta is None:
            logger.warning("HIF交换: 无法确认卡组列表滚动位移，停止滚动以免点错位置")
            self._save_scroll_images(before, after)
            return None
        if delta == 0 and direction < 0:
            self._list_offset = 0  # 已到顶部，消除累计误差
        else:
            self._list_offset += delta
        logger.info(f"HIF交换: 卡组列表{'下' if direction > 0 else '上'}滑，实测位移={delta}，当前偏移={self._list_offset}")
        return delta

    def _visible_rows(self) -> list:
        """返回当前整行可点的 (行号, 卡中心y)。"""
        low, high = self.ROW_CLICK_RANGE
        rows = []
        for row in range(self.MAX_ROWS):
            y = self.ROW_CENTER_Y0 + row * self.ROW_PITCH - self._list_offset
            if low <= y <= high:
                rows.append((row, y))
        return rows

    def _ensure_visible(self, context: Context, location) -> Optional[tuple]:
        """把扫描时记下的卡格滚回可点范围，返回当前坐标；无法确认时返回 None。"""
        offset, (x, y) = location
        low, high = self.ROW_CLICK_RANGE
        for _ in range(self.MAX_SCROLLS + 1):
            if offset == self._list_offset:
                return x, y
            current = y + offset - self._list_offset
            if low <= current <= high:
                return x, current
            if not self._scroll_list(context, 1 if current > high else -1):
                return None
        return None

    def _scroll_to_top(self, context: Context) -> bool:
        for _ in range(self.MAX_SCROLLS + 1):
            if self._list_offset == 0:
                return True
            delta = self._scroll_list(context, -1)
            if delta is None:
                return False
            if delta == 0:
                return True
        return self._list_offset == 0

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run_exchange(context)
        except _ExchangeStopped:
            logger.info("HIF交换: 任务已停止，保留交换页现场，不再点卡或チェンジ")
            return False

    def _run_exchange(self, context: Context) -> bool:
        swap = ProduceHIF__ProduceHIFCardSwapAuto
        options = self._swap_out_options(context)
        self._list_offset = 0  # 交换页打开时卡组列表在顶部

        def version_rank(image, name):
            prefer_plain, allow_custom = options.get(swap._canonical_remembered_card_name(name), (True, True))
            customized = self._customized_card(image)
            if customized is None:
                logger.info(f"HIF交换: 「{name}」定制标记未确认，按非定制卡处理")
                return 0
            if customized and not name:
                logger.info("HIF交换: 已识别为定制版本，但卡名未确认，跳过")
                return None
            if customized and not allow_custom:
                logger.info(f"HIF交换: 「{name}」有定制标记，面板不允许换出定制版本，跳过")
                return None
            if customized:
                logger.info(f"HIF交换: 「{name}」有定制标记，面板允许换出定制版本")
            return int(customized and prefer_plain)

        targets = self._replacement_targets(context)
        primary_targets = targets[:-1]
        fallback_target = targets[-1]
        # 卡格位置记为 (扫描时列表偏移, 坐标)，滚动后按实测偏移换算，不沿用旧坐标。
        state = {"fallback": None, "fallback_version": None, "safe": None, "safe_name": "", "safe_version": None, "best": None}
        recorded_a = bool(
            ProduceHIF__ProduceHIFCardSwapAuto._tracked_swap_enabled(context)
            and ProduceHIF__ProduceHIFCardSwapAuto._delete_card_exchange_count == 1
            and ProduceHIF__ProduceHIFCardSwapAuto._delete_card_a
        )
        scan_passes = self.SECOND_EXCHANGE_SCAN_PASSES if recorded_a else 1

        def target_label(rank: int) -> str:
            if recorded_a and rank == 0:
                return "首次换入卡 a"
            return f"面板优先换出卡(顺位{rank + 1 - int(recorded_a)})"

        def best_found() -> bool:
            return state["best"] is not None and state["best"][:2] == (0, 0)

        def scan_cell(label: str, pos):
            """点一格并按卡名归类；基本卡可直接替换时返回 True。"""
            self._check_stop(context)
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, name = self._wait_stable_name(context)
            logger.info(f"HIF交换: {label} 卡名OCR=[{name}]")
            version = version_rank(image, name)
            if version is None:
                return False
            location = (self._list_offset, pos)
            if state["safe"] is None or (swap.remembered_card_name_hit(name, state["safe_name"]) and version < state["safe_version"]):
                state.update(safe=location, safe_name=name, safe_version=version)
            normalized_name = "".join((name or "").split())
            rank = next((rank for rank, target in enumerate(primary_targets)
                         if ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, target)), None)
            if rank is not None:
                if state["best"] is None or (rank, version) < state["best"][:2]:
                    state["best"] = (rank, version, location, primary_targets[rank])
                    logger.info(f"HIF交换: {label}命中{target_label(rank)}「{primary_targets[rank]}」")
                return False
            if fallback_target and fallback_target in normalized_name:
                if not primary_targets and version == 0:
                    logger.info(f"HIF交换: {label}命中「{fallback_target}」→ 点チェンジ替换")
                    state["confirm_image"] = image
                    return True
                if state["fallback"] is None or version < state["fallback_version"]:
                    state.update(fallback=location, fallback_version=version)
                    logger.info(f"HIF交换: {label}命中基本卡，先保留并继续扫描优先换出名单")
                return False
            logger.info(f"HIF交换: {label}非目标卡")
            return False

        for scan_pass in range(scan_passes):
            if scan_pass and not self._scroll_to_top(context):
                logger.warning("HIF交换: 第二轮扫描前未能确认回到列表顶部，沿用第一轮结果")
                break
            scanned_rows = {0, 1, 2}  # CARD_GRID 即顶部三行
            for i, pos in enumerate(self.CARD_GRID):
                if scan_cell(f"第{scan_pass + 1}轮第{i + 1}格", pos):
                    return self._confirm_exchange(context, state["confirm_image"])
                if best_found():
                    break
            for _ in range(self.MAX_SCROLLS):
                if best_found() or not self._scroll_list(context, 1):
                    break
                for row, y in self._visible_rows():
                    if row in scanned_rows:
                        continue
                    scanned_rows.add(row)
                    for column, x in enumerate(self.COLUMN_X):
                        if scan_cell(f"第{scan_pass + 1}轮第{row + 1}行第{column + 1}列", (x, y)):
                            return self._confirm_exchange(context, state["confirm_image"])
                        if best_found():
                            break
                    if best_found():
                        break
            if best_found():
                break

        def reselect(location, label: str):
            pos = self._ensure_visible(context, location)
            if pos is None:
                logger.warning(f"HIF交换: 无法把{label}滚回可点范围，停止本次换出以免换错卡")
                return None
            self._check_stop(context)
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            return self._wait_stable_name(context)

        if state["best"] is not None:
            rank, selected_version, location, target = state["best"]
            logger.info(f"HIF交换: 选择{target_label(rank)}「{target}」@ {location[1]}，再次点卡后チェンジ")
            picked = reselect(location, f"「{target}」")
            if picked is None:
                return False
            image, name = picked
            if not ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, target):
                logger.warning(f"HIF交换: 再次选择「{target}」后卡名不符=[{name}]，停止本次换出以免换错卡")
                return False
            version = version_rank(image, name)
            if version is None or version > selected_version:
                logger.warning("HIF交换: 复核时卡牌版本变化或不符合该卡换出规则，停止换出")
                return False
            return self._confirm_exchange(context, image)
        if state["fallback"] is not None:
            logger.info(f"HIF交换: 未命中优先换出目标{primary_targets}，回退替换「{fallback_target}」")
            picked = reselect(state["fallback"], "基本卡")
            if picked is None:
                return False
            image, name = picked
            if fallback_target not in "".join((name or "").split()):
                logger.warning(f"HIF交换: 再次选择基本卡后卡名不符=[{name}]，停止本次换出以免换错卡")
                return False
            version = version_rank(image, name)
            if version is None or version > state["fallback_version"]:
                logger.warning("HIF交换: 基本卡复核不符合该卡换出规则，停止换出")
                return False
            return self._confirm_exchange(context, image)
        if state["safe"] is None:
            logger.warning("HIF交换: 未找到符合逐卡版本规则的可换出卡，停止换出")
            return False
        logger.info(f"HIF交换: 完整卡组未找到目标卡{targets}，回退替换首个允许换出的卡「{state['safe_name']}」@ {state['safe'][1]}")
        picked = reselect(state["safe"], "兜底卡")
        if picked is None:
            return False
        image, name = picked
        if not swap.remembered_card_name_hit(name, state["safe_name"]):
            logger.warning(f"HIF交换: 再次选择兜底卡后卡名不符=[{name}]，停止本次换出以免换错卡")
            return False
        version = version_rank(image, name)
        if version is None or version > state["safe_version"]:
            logger.warning("HIF交换: 兜底卡复核不符合该卡换出规则，停止换出")
            return False
        return self._confirm_exchange(context, image)


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardDeleteAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFCardDeleteAuto(CustomAction):
    """
    HIF 删卡页处理。

    相谈入口直接调用 delete_remembered_card，只删除本轮记录的 b。
    流水线 run 处理纯删卡事件：当前职业优先换出名单、基本卡、第一张依次兜底。
    事件处理不读取相谈删卡开关，也不要求返回相谈商店。
    """

    CLICK_DELAY = 0.8
    CARD_FIRST_POS = (138, 457)
    CARD_GRID = [
        (139, 457), (285, 457), (432, 457), (580, 457),
        (139, 604), (285, 604), (432, 604), (580, 604),
        (139, 741), (285, 741),
    ]
    CARD_NAME_ROI = [185, 95, 455, 70]
    OCR_ALL = r"[^\n]+"
    # 首次删除按钮与确认弹窗按钮均位于右下区域；确认弹窗标题用于避免二次误点。
    DELETE_ROI = [350, 1100, 350, 140]
    DELETE_EXPECTED = "削除"
    DELETE_FALLBACK_POS = (520, 1160)
    CANCEL_ROI = [45, 1100, 300, 140]
    CANCEL_EXPECTED = "キャンセル|キヤンセル"
    CANCEL_FALLBACK_POS = (210, 1160)
    DELETE_TITLE_ROI = [0, 0, 300, 100]
    DELETE_TITLE_EXPECTED = "削除"
    CONFIRM_TITLE_ROI = [25, 500, 670, 220]
    CONFIRM_TITLE_EXPECTED = "スキルカード削除"
    CONSULT_TITLE_ROI = [0, 0, 220, 170]
    CONSULT_TITLE_EXPECTED = r"相[谈談]"
    CONSULT_DETAIL_ROI = [40, 240, 650, 125]
    CONSULT_DETAIL_EXPECTED = r"(?:Pポイント.*)?交換するものを選んでください"
    STATE_TIMEOUT = 6.0
    DETAIL_STABLE_TIMEOUT = 3.0
    POLL_INTERVAL = 0.25
    STABLE_COUNT = 2
    DETAIL_STABLE_COUNT = 3
    DELETE_BUTTON_TEMPLATE = "hif/produce/delete.png"
    CONFIRM_TEMPLATE = "hif/produce/delete_confirm.png"
    RECOGNITION_NODE = "ProduceHIF__ProduceHIFCardDeleteRecognition"
    EVENT_FLAG_NODE = "ProduceHIF__ProduceHIFCardDeleteFlag"
    LOG_LABEL = "HIF删卡"

    def _template_box(self, context: Context, image, template):
        reco_detail = context.run_recognition(
            self.RECOGNITION_NODE,
            image,
            pipeline_override={
                self.RECOGNITION_NODE: {
                    "recognition": "TemplateMatch",
                    "template": template,
                }
            },
        )
        if reco_detail and reco_detail.hit and reco_detail.filtered_results:
            return reco_detail.filtered_results[0].box
        return None

    def _click_box_center(self, context: Context, box) -> None:
        x = box[0] + box[2] // 2
        y = box[1] + box[3] // 2
        logger.info(f"HIF删卡: 点击 @ ({x}, {y})")
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)

    def _read_ocr(self, context: Context, image, roi: list, expected: str) -> str:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": expected,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF删卡: OCR异常 roi={roi} {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(result.text for result in reco.filtered_results)

    def _wait_for_text(self, context: Context, roi: list, expected: str, timeout=None):
        """等待指定文字连续稳定出现，成功返回最后一帧，超时返回 None。"""
        deadline = time.time() + (timeout or self.STATE_TIMEOUT)
        stable_count = 0
        while time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            if self._read_ocr(context, image, roi, expected):
                stable_count += 1
                if stable_count >= self.STABLE_COUNT:
                    return image
            else:
                stable_count = 0
            time.sleep(self.POLL_INTERVAL)
        return None

    def wait_for_delete_page(self, context: Context) -> bool:
        """等待相谈的削除选卡页完成转场。"""
        return self._wait_for_text(
            context, self.DELETE_TITLE_ROI, self.DELETE_TITLE_EXPECTED
        ) is not None

    def wait_for_consult(self, context: Context) -> bool:
        """等待删卡页或确认弹窗关闭并稳定返回相谈商店。"""
        return self._wait_for_state(context, ("consult",)) == "consult"

    def _wait_stable_card_name(self, context: Context) -> tuple:
        """等待删卡页顶部详情卡名连续稳定，避开选卡动画的旧详情。"""
        deadline = time.time() + self.DETAIL_STABLE_TIMEOUT
        stable_count = 0
        last_name = ""
        last_image = None
        while time.time() < deadline:
            last_image = common._hif_nonbattle_screencap(context)
            name = self._read_ocr(context, last_image, self.CARD_NAME_ROI, self.OCR_ALL)
            normalized = "".join((name or "").split())
            if normalized and normalized == last_name:
                stable_count += 1
            elif normalized:
                last_name = normalized
                stable_count = 1
            else:
                last_name = ""
                stable_count = 0
            if stable_count >= self.DETAIL_STABLE_COUNT:
                return last_image, name
            time.sleep(self.POLL_INTERVAL)
        logger.warning("HIF相谈删卡: 卡名在等待时间内未稳定，本格按未识别处理")
        return last_image, ""

    def _detect_state(self, context: Context, image) -> Optional[str]:
        """识别删卡确认、删卡选择或相谈商店状态。"""
        if self._read_ocr(
            context, image, self.CONFIRM_TITLE_ROI, self.CONFIRM_TITLE_EXPECTED
        ):
            return "confirm"
        if self._read_ocr(
            context, image, self.DELETE_TITLE_ROI, self.DELETE_TITLE_EXPECTED
        ):
            return "delete"
        consult_title = self._read_ocr(
            context, image, self.CONSULT_TITLE_ROI, self.CONSULT_TITLE_EXPECTED
        )
        consult_detail = self._read_ocr(
            context, image, self.CONSULT_DETAIL_ROI, self.CONSULT_DETAIL_EXPECTED
        )
        if consult_title and consult_detail:
            return "consult"
        return None

    def _wait_for_state(self, context: Context, expected_states: tuple, timeout=None) -> Optional[str]:
        """等待任一页面状态连续稳定两帧。"""
        deadline = time.time() + (timeout or self.STATE_TIMEOUT)
        last_state = None
        stable_count = 0
        while time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            state = self._detect_state(context, image)
            if state in expected_states and state == last_state:
                stable_count += 1
            elif state in expected_states:
                last_state = state
                stable_count = 1
            else:
                last_state = None
                stable_count = 0
            if stable_count >= self.STABLE_COUNT:
                return state
            time.sleep(self.POLL_INTERVAL)
        return None

    def _click_ocr_or_fallback(self, context: Context, image, roi: list, expected: str, fallback, label: str) -> None:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": expected,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                logger.info(f"HIF删卡: 点击{label} @ {pos}")
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
                time.sleep(self.CLICK_DELAY)
                return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF删卡: {label} OCR异常 {e!r}")
        logger.info(f"HIF删卡: 未识别到{label}，回退点击 @ {fallback}")
        context.tasker.controller.post_click(fallback[0], fallback[1]).wait()
        time.sleep(self.CLICK_DELAY)

    def _click_required_ocr(self, context: Context, roi: list, expected: str, label: str) -> bool:
        """连续稳定识别按钮后才点击；用于不可逆的删除操作，不使用坐标兜底。"""
        deadline = time.time() + self.STATE_TIMEOUT
        stable_count = 0
        last_box = None
        while time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceRecognitionScore",
                    image,
                    pipeline_override={
                        "ProduceHIF__ProduceRecognitionScore": {
                            "recognition": "OCR", "roi": roi, "expected": expected,
                        }
                    },
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF删卡: 等待{label}异常 {e!r}")
                reco = None
            if reco and reco.hit and reco.filtered_results:
                last_box = reco.filtered_results[0].box
                stable_count += 1
                if stable_count >= self.STABLE_COUNT:
                    self._click_box_center(context, last_box)
                    return True
            else:
                stable_count = 0
                last_box = None
            time.sleep(self.POLL_INTERVAL)
        logger.warning(f"HIF删卡: 超时未稳定识别到{label}，不执行点击")
        return False

    @classmethod
    def _matches_target(cls, name: str, target: str) -> bool:
        """匹配 b 与删卡页卡名，容忍换卡页 OCR 附带费用/数值等尾部文字。"""
        return ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, target)

    def _click_cancel(self, context: Context, image) -> None:
        """点击当前界面的キャンセル按钮。"""
        self._click_ocr_or_fallback(
            context, image, self.CANCEL_ROI, self.CANCEL_EXPECTED,
            self.CANCEL_FALLBACK_POS, "取消",
        )

    def _cancel_to_consult(self, context: Context) -> bool:
        """从确认弹窗或删卡选择页安全返回相谈。

        确认弹窗的「キャンセル」只会退回删卡选卡页；必须再取消一次才会回到相谈。
        """
        for _ in range(3):
            state = self._wait_for_state(context, ("confirm", "delete", "consult"))
            if state == "consult":
                return True
            if state in ("confirm", "delete"):
                image = common._hif_nonbattle_screencap(context)
                label = "最终确认弹窗" if state == "confirm" else "删卡选择页"
                logger.info(f"HIF相谈删卡: 取消{label}")
                self._click_cancel(context, image)
                continue
            break
        logger.warning("HIF相谈删卡: 取消后未确认返回相谈")
        return False

    def _recover_delete(self, context: Context, from_consult: bool) -> bool:
        if from_consult:
            return self._cancel_to_consult(context)
        # 纯删卡事件需要选卡完成；只取消确认弹窗，不退出事件选择页。
        image = common._hif_nonbattle_screencap(context)
        if self._detect_state(context, image) == "confirm":
            self._click_cancel(context, image)
        return False

    def _wait_event_closed(self, context: Context) -> bool:
        """事件完成后等待删卡页及确认弹窗连续消失，交回 HIF 主流程。"""
        deadline = time.time() + self.STATE_TIMEOUT
        closed_count = 0
        while time.time() < deadline:
            image = common._hif_nonbattle_screencap(context)
            try:
                page = context.run_recognition(self.EVENT_FLAG_NODE, image)
                visible = page is None or page.hit or self._detect_state(context, image) == "confirm"
            except Exception as error:  # pylint: disable=broad-except
                logger.warning(f"{self.LOG_LABEL}: 检查事件页面异常 {error!r}")
                visible = True
            closed_count = 0 if visible else closed_count + 1
            if closed_count >= self.STABLE_COUNT:
                return True
            time.sleep(self.POLL_INTERVAL)
        logger.warning(f"{self.LOG_LABEL}: 确认后事件页未稳定关闭")
        return False

    @staticmethod
    def _fallback_card(name: str) -> bool:
        """名单未命中时的次选卡：删卡事件优先删除基本卡。"""
        return "基本" in name

    def _confirm_title_missing(self, context: Context) -> Optional[bool]:
        """确认标题未出现时的处理；None 表示按原规则补点一次确认按钮。"""
        return None

    def delete_remembered_card(self, context: Context, target: str) -> bool:
        """只删除本轮记录的 b，不使用基本卡兜底。"""
        return self._delete_matching_card(context, [target], from_consult=True)

    def _delete_matching_card(self, context: Context, targets: list[str], allow_basic: bool = False, from_consult: bool = False) -> bool:
        """按名单顺序选择删除目标，重新核对选中卡名后才允许确认。"""
        if not self.wait_for_delete_page(context):
            logger.warning(f"{self.LOG_LABEL}: 未稳定进入选卡页，不开始扫描卡片")
            return False
        best = None
        for index, pos in enumerate(self.CARD_GRID):
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, name = self._wait_stable_card_name(context)
            logger.info(f"{self.LOG_LABEL}: 第{index + 1}张卡名 OCR=[{name}]")
            rank = next((i for i, target in enumerate(targets) if self._matches_target(name, target)), None)
            if rank is None and allow_basic and name and self._fallback_card(name):
                rank = len(targets)
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, pos, name)
            if best is not None and best[0] == 0:
                break
        if best is None and allow_basic:
            pos = self.CARD_GRID[0]
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            _, name = self._wait_stable_card_name(context)
            if name:
                best = (len(targets) + 1, pos, name)
                logger.info(f"{self.LOG_LABEL}: 名单与基本卡均未命中，回退选择第一张")
        if best is None:
            logger.warning(f"{self.LOG_LABEL}: 未找到符合规则的卡或第一张卡名无法确认，取消操作")
            self._recover_delete(context, from_consult)
            return False
        _, pos, target = best
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(self.CLICK_DELAY)
        image, name = self._wait_stable_card_name(context)
        if not self._matches_target(name, target):
            logger.warning(f"{self.LOG_LABEL}: 重新选中后卡名不符=[{name}]，取消操作")
            self._recover_delete(context, from_consult)
            return False
        logger.info(f"{self.LOG_LABEL}: 已核对目标=[{target}]，点击确认按钮")
        if not self._click_required_ocr(
            context, self.DELETE_ROI, self.DELETE_EXPECTED, "确认按钮"
        ):
            self._recover_delete(context, from_consult)
            return False
        image = self._wait_for_text(
            context, self.CONFIRM_TITLE_ROI, self.CONFIRM_TITLE_EXPECTED
        )
        if image is None:
            finished = self._confirm_title_missing(context)
            if finished is not None:
                return finished
            logger.warning(f"{self.LOG_LABEL}: 确认标题未识别，重新稳定识别右下确认按钮后补点一次")
        else:
            logger.info(f"{self.LOG_LABEL}: 已识别最终确认弹窗，点击确认")
        if not self._click_required_ocr(
            context, self.DELETE_ROI, self.DELETE_EXPECTED, "最终确认按钮"
        ):
            self._recover_delete(context, from_consult)
            return False
        if not from_consult:
            return self._wait_event_closed(context)
        state = self._wait_for_state(context, ("consult", "delete"))
        if state == "consult":
            return True
        if state == "delete":
            logger.info("HIF相谈删卡: 删除完成后仍在删卡选择页，取消返回相谈")
            return self._recover_delete(context, from_consult)
        logger.warning("HIF相谈删卡: 确认删除后未识别到相谈或删卡页")
        return False

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """流水线删卡页入口仅处理事件；相谈通过专用方法进入。"""
        targets = ProduceHIF__ProduceHIFExchangeAuto()._replacement_targets(context, include_recorded_a=False)[:-1]
        return self._delete_matching_card(context, targets, allow_basic=True)


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardCopyAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFCardCopyAuto(ProduceHIF__ProduceHIFCardDeleteAuto):
    """
    HIF 复制卡事件（左上「コピー」）：沿用删卡页的选卡、核对与确认流程。

    当前职业优先获取卡名单按顺序优先；未命中时复制第一张非基本卡，仍无则复制第一张。
    """

    # 复制页最多 4 行，第 3、4 行中心按截图 20261101 实测。
    CARD_GRID = [
        (139, 457), (285, 457), (432, 457), (580, 457),
        (139, 604), (285, 604), (432, 604), (580, 604),
        (139, 750), (285, 750), (432, 750), (580, 750),
        (139, 897), (285, 897), (432, 897), (580, 897),
    ]
    # 底部「コピー」按钮居中（232-487），确认弹窗的按钮位于右侧，ROI 覆盖两者但不含左侧キャンセル文字。
    DELETE_ROI = [200, 1090, 500, 160]
    DELETE_EXPECTED = "コピー|复制|複製"
    DELETE_TITLE_EXPECTED = "コピー|复制|複製"
    CONFIRM_TITLE_EXPECTED = "スキルカードコピー|技能卡复制|技能卡複製"
    EVENT_FLAG_NODE = "ProduceHIF__ProduceHIFCardCopyFlag"
    LOG_LABEL = "HIF复制卡"

    @staticmethod
    def _fallback_card(name: str) -> bool:
        return "基本" not in name

    def _confirm_title_missing(self, context: Context) -> Optional[bool]:
        """未出现确认弹窗而复制页已关闭时视为完成，避免在后续画面补点。"""
        image = common._hif_nonbattle_screencap(context)
        try:
            page = context.run_recognition(self.EVENT_FLAG_NODE, image)
            closed = page is not None and not page.hit
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(f"{self.LOG_LABEL}: 检查事件页面异常 {error!r}")
            closed = False
        return self._wait_event_closed(context) if closed else None

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        specs = ProduceHIF__ProduceHIFCardSwapAuto()._load_swap_want(context)
        targets = [spec["name"] for spec in specs if spec.get("name")]
        return self._delete_matching_card(context, targets, allow_basic=True)


@AgentServer.custom_action("ProduceHIF__ProduceHIFSupplyCardAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFSupplyCardAuto(CustomAction):
    """
    HIF 差し入れ过场与「受け取るスキルカード」领取入口。

    过场稳定后借用 ProduceHIF__Click_1 间隔点击两次。仅在中央提示为「受け取るスキルカード」
    时扫描候选卡：默认3张；开启
    「老师活动」后扫描4张。逐张点开后读取顶部详情卡名，按当前职业优先获取卡名单选卡，
    再点受け取る(358,1094)。未命中且开启「支给再抽选」时重抽，直到「再抽選」按钮消失；
    仍读不到目标时回退第1张，避免卡在领取页。

    「受け取るPドリンク」和「Pドリンク所持上限」由各自专用动作处理，不能在此混用。
    """

    CLICK_DELAY = 1.0
    # 支给技能卡页（720x1280）：默认3张；老师活动开启后为4张。
    SUPPLY_CARD_SLOTS = [(220, 885), (360, 885), (500, 885)]
    TEACHER_EVENT_SUPPLY_CARD_SLOTS = [(150, 885), (290, 885), (430, 885), (568, 885)]
    CARD_PROMPT_ROI = [90, 570, 560, 110]
    CARD_PROMPT_EXPECTED = "受け取るスキルカード"
    SUPPLY_TITLE_ROI = [0, 0, 200, 100]
    SUPPLY_TITLE_EXPECTED = "差し入れ"
    # 支给详情的居中标题在分隔线之上；右侧费用及下方效果不属于卡名。
    CARD_NAME_ROI = [120, 490, 440, 50]
    OCR_ALL = r"[^\n]+"
    RECEIVE_POS = (358, 1094)      # 底部「受け取る」
    # 「再抽選」在受け取る右侧，与授业候选页同一行；仅按 OCR 点击，读不到即视为次数已用完。
    REDRAW_ROI = [480, 1060, 240, 70]
    ADVANCE_INTERVAL = 0.4
    ADVANCE_CLICKS_MAX = 20
    CARD_RECEIVED_GUARD_SECONDS = 60.0
    _card_received_task_id = None
    _card_received_until = 0.0
    # 领饮料兜底
    OBTAIN_FALLBACK_POS = (350, 885)   # 待领取第2格(B ENERGY)

    def _click(self, context: Context, pos, delay=None):
        """单击指定坐标并等待。"""
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else self.CLICK_DELAY)

    def _is_card_screen(self, context: Context, image) -> bool:
        """通过「受け取るスキルカード」提示，区分技能卡页和饮料页。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.CARD_PROMPT_ROI,
                        "expected": self.CARD_PROMPT_EXPECTED,
                    }
                },
            )
            return bool(reco and reco.hit)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡领取: 技能卡页 OCR 异常 {e!r}")
            return False

    def _is_supply_scene(self, context: Context, image) -> bool:
        """确认仍在差し入れ场景，避免过场点击落到下一天行动页。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.SUPPLY_TITLE_ROI,
                        "expected": self.SUPPLY_TITLE_EXPECTED,
                    }
                },
            )
            return bool(reco and reco.hit)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF支给过场: 页眉 OCR 异常 {e!r}")
            return False

    def _select_wanted_card(self, context: Context) -> bool:
        """按当前职业优先获取卡名单领取新卡；开启「支给再抽选」时重抽，直到「再抽選」按钮消失。"""
        swap = ProduceHIF__ProduceHIFCardSwapAuto()
        specs = swap._load_swap_want(context)
        if not specs:
            logger.info("HIF技能卡领取: 当前职业优先获取卡名单为空")
            return False
        if self._scan_wanted_card(context, swap, specs):
            return True
        if not swap.redraw_switch_enabled(context, swap.SUPPLY_REDRAW_NODE, "支给"):
            logger.info("HIF技能卡领取: 支给再抽选已关闭，不重抽")
            return False
        if swap._redraw_exhausted:
            logger.info("HIF技能卡领取: 本轮「再抽選」按钮已消失，不再重抽")
            return False
        for _ in range(swap.REDRAW_PAGE_SAFETY_MAX):
            if getattr(context.tasker, "stopping", False):
                return False
            pos = swap.locate_redraw(context, self.REDRAW_ROI)
            if pos is None:
                if not getattr(context.tasker, "stopping", False):
                    swap.mark_redraw_exhausted("支给")
                return False
            logger.info(f"HIF技能卡领取: 候选卡均非优先获取卡 → 点再抽選 @ {pos}")
            self._click(context, pos)
            swap.note_redraw("支给")
            self._wait_card_page_ready(context)
            if self._scan_wanted_card(context, swap, specs):
                return True
        logger.warning(f"HIF技能卡领取: 本页已重抽{swap.REDRAW_PAGE_SAFETY_MAX}次仍可见再抽選，按保险上限停止")
        return False

    def _wait_card_page_ready(self, context: Context) -> bool:
        """重抽后等领取提示可见且候选卡面连续稳定，再逐张扫描。"""
        deadline = time.monotonic() + 6.0
        previous, stable = None, 0
        while time.monotonic() < deadline:
            if getattr(context.tasker, "stopping", False):
                return False
            image = common._hif_nonbattle_screencap(context)
            current = None
            if cv2 is not None and getattr(image, "shape", ())[:2] == (1280, 720):
                current = image[800:980, 80:640].copy()
            unchanged = current is None or (previous is not None and cv2.norm(
                current, previous, cv2.NORM_L1) / current.size <= 2.5)
            stable = stable + 1 if self._is_card_screen(context, image) and unchanged else 0
            previous = current
            if stable >= 3:
                return True
            time.sleep(0.25)
        logger.warning("HIF技能卡领取: 重抽后候选页仍未稳定，限时等待结束，继续扫描")
        return False

    def _scan_wanted_card(self, context: Context, swap, specs: list) -> bool:
        for index, pos in enumerate(self._candidate_slots(context)):
            if getattr(context.tasker, "stopping", False):
                return False
            logger.info(f"HIF技能卡领取: 检查第{index + 1}张候选卡 @ {pos}")
            self._click(context, pos)
            name_text = self._wait_supply_card_name(context)
            logger.info(f"HIF技能卡领取: 第{index + 1}张卡名 OCR=[{name_text}]")
            for spec in specs:
                name = spec.get("name")
                if swap.remembered_card_name_hit(name_text, name):
                    logger.info(f"HIF技能卡领取: 命中优先获取卡「{name}」")
                    return True
        return False

    @staticmethod
    def _supply_name_from_results(results) -> str:
        """只拼接卡名所在的第一行，排除卡种图标和下方效果文字。"""
        text = [r for r in results if 120 <= r.box[0] < 560 and 490 <= r.box[1] <= 520
                and r.box[1] + r.box[3] <= 540 and r.box[3] >= 20
                and not re.fullmatch(r'[A-Za-z0-9+＋\-]+', (r.text or '').strip())]
        if not text:
            return ''
        top = min(r.box[1] for r in text)
        line = sorted((r for r in text if abs(r.box[1] - top) <= 12), key=lambda r: r.box[0])
        return ProduceHIF__ProduceHIFCardSwapAuto._canonical_remembered_card_name(''.join(r.text or '' for r in line))

    def _wait_supply_card_name(self, context: Context) -> str:
        deadline = time.monotonic() + 3.0
        previous, stable = '', 0
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return ''
            image = common._hif_nonbattle_screencap(context)
            reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
                pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                    'recognition': 'OCR', 'roi': self.CARD_NAME_ROI, 'expected': self.OCR_ALL}})
            name = self._supply_name_from_results(reco.filtered_results if reco and reco.hit else [])
            stable = stable + 1 if name and name == previous else (1 if name else 0)
            previous = name
            if stable >= 2:
                return name
            time.sleep(0.2)
        logger.warning('HIF技能卡领取: 卡名行未稳定，不用效果文字猜测卡名')
        return ''

    def _candidate_slots(self, context: Context) -> list:
        if ProduceHIF__ProduceHIFCardSwapAuto._teacher_event_enabled(context):
            return self.TEACHER_EVENT_SUPPLY_CARD_SLOTS
        return self.SUPPLY_CARD_SLOTS

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        image = common._hif_nonbattle_screencap(context)
        task_id = getattr(getattr(argv, "task_detail", None), "task_id", None)
        if self._is_card_screen(context, image):
            if not self._select_wanted_card(context):
                fallback_pos = self._candidate_slots(context)[0]
                logger.info(f"HIF技能卡领取: 未命中优先获取卡，回退第1张 @ {fallback_pos}")
                self._click(context, fallback_pos)
            logger.info(f"HIF技能卡领取: 点击受け取る @ {self.RECEIVE_POS}")
            self._click(context, self.RECEIVE_POS)
            self.__class__._card_received_task_id = task_id
            self.__class__._card_received_until = time.monotonic() + self.CARD_RECEIVED_GUARD_SECONDS
            return True
        if (task_id is not None and task_id == self.__class__._card_received_task_id
                and time.monotonic() < self.__class__._card_received_until):
            logger.info("HIF技能卡领取: 本次任务已领卡，忽略退场动画中的差し入れ页眉")
            return True
        if ProduceHIF__ProduceHIFDrinkAuto()._is_receive_drink_screen(context, image):
            return True  # 饮料领取交给优先级更高的 ProduceHIF__ProduceHIFDrinkFlag
        if not self._is_supply_scene(context, image):
            return True
        # 动画无需静止；每次左上角点击后重新确认页面，领取页出现就立即交回外层。
        for clicks in range(self.ADVANCE_CLICKS_MAX + 1):
            if getattr(context.tasker, "stopping", False):
                return True
            image = common._hif_nonbattle_screencap(context)
            if getattr(context.tasker, "stopping", False):
                return True
            if (self._is_card_screen(context, image)
                    or ProduceHIF__ProduceHIFDrinkAuto()._is_receive_drink_screen(context, image)):
                return True
            if not self._is_supply_scene(context, image):
                logger.info("HIF支给过场: 已离开差し入れ场景，停止左上角点击")
                return True
            if clicks == self.ADVANCE_CLICKS_MAX:
                break
            logger.info(f"HIF支给过场: 点击左上角跳过动画 {clicks + 1}/{self.ADVANCE_CLICKS_MAX}")
            context.run_action("ProduceHIF__Click_1")
            time.sleep(self.ADVANCE_INTERVAL)
        logger.warning("HIF支给过场: 多次点击后仍未进入饮料或技能卡领取页")
        return False
