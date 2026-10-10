import json
import os
import time
import random
import re
import unicodedata
import numpy as np
from difflib import SequenceMatcher

# opencv 为可选依赖（仅饮料识别用到）。未安装时不让 agent 启动整体崩溃——
# 其它动作照常运行，仅 HIF 饮料识别降级并明确报错。
try:
    import cv2
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    cv2 = None

from typing import Any, Dict, List, Optional
from collections import Counter

# determine project root: <root>/agent/custom/action/produce.py -> <root>
from pathlib import Path
BASE_DIR = str(Path(__file__).resolve().parents[5])
EXT_DIR = os.path.join(BASE_DIR, "extensions", "hif")
CARDS_PRIORITY_CONFIG_PATH = os.path.join(BASE_DIR, "config", "hif", "cards_priority.json")
HIF_PRIORITY_CATALOG_PATH = os.path.join(EXT_DIR, "catalog", "hif_priority_card_catalog.json")
HIF_DRINK_CATALOG_PATH = os.path.join(EXT_DIR, "catalog", "hif_drink_catalog.json")
HIF_DRINK_PROFILES_PATH = os.path.join(BASE_DIR, "config", "hif", "hif_drink_profiles.json")

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer
from extensions.hif.followups import Followups, validate_rules


class HifDrinkFlowError(RuntimeError):
    """A drink dialog failed to return safely to the battle."""


def _hif_drink_priority_names(
    context: Context, purchase_only: bool = False, disabled_only: bool = False,
) -> list[str]:
    """按前台职业读取饮料名单；可筛出购买项或不使用项。"""
    profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
    try:
        with open(HIF_DRINK_PROFILES_PATH, encoding="utf-8") as file:
            profiles = json.load(file).get("profiles", {})
        with open(HIF_DRINK_CATALOG_PATH, encoding="utf-8") as file:
            drinks = json.load(file).get("drinks", [])
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"HIF饮料配置读取失败: {exc!r}，本次不按面板选择")
        return []
    names_by_id = {item.get("id"): item.get("name") for item in drinks if isinstance(item, dict)}
    entries = profiles.get(profession, []) if isinstance(profiles, dict) else []
    if not isinstance(entries, list):
        logger.warning(f"HIF饮料配置无效: 职业={profession}的名单不是数组")
        return []
    names = []
    for entry in entries:
        if not isinstance(entry, dict) or (entry.get("disabled") is True) != disabled_only:
            continue
        if purchase_only and entry.get("purchase_enabled") is not True:
            continue
        name = names_by_id.get(entry.get("id"))
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _hif_drink_name_key(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name or "").split())



class ProduceHIF__ProduceChooseEventBase(CustomAction):
    """
    培育事件选择基类。

    子类需定义以下类属性：
    - SUGGESTION_CONFIG: 老师建议匹配配置
    - EVENT_CONFIG: 事件图片配置
    - RUN_TASK_MAP: 需要执行run_task的事件映射

    可覆盖的钩子方法：
    - _get_preference: 获取属性偏好设置
    - _choose_extra_before_attrs: SP之后、属性课程之前的额外优先事件
    - _choose_extra_after_outing: 外出之后的额外优先事件
    """

    # 子类需覆盖
    SUGGESTION_CONFIG: dict = {}
    EVENT_CONFIG: dict = {}
    RUN_TASK_MAP: dict = {}
    SUGGESTION_ROI: list = [270, 160, 350, 80]
    REST_COUNT_ROI = [580, 755, 135, 75]

    # 阈值常量
    CLICK_DELAY = 0.5
    ACTION_DELAY = 3.0
    PREFERENCE_LIST = ["Da", "Vi", "Vo"]
    FIRST_NEAR_FULL_RATIO = 0.85
    ATTR_STOP_RATIO = 0.8
    LOW_HEALTH_RATIO = 0.2
    LOW_HEALTH_VALUE = 8
    # 是否"读取属性事件/属性满停止"判断: True=按属性占比决定是否停选SP/课程; False=关闭(不因属性满停止选择, 恒不触发ATTR_STOP)
    READ_ATTR_EVENT = False

    def __init__(self):
        super().__init__()
        self.first = "Vi"
        self.second = "Da"

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.success("事件: 选择事件")

        preference = self._get_preference(context, argv)
        self.first = preference["first"]
        self.second = preference["second"]
        logger.info(f"第一属性: {self.first}, 第二属性: {self.second}")

        image = self._get_screenshot(context)

        suggestion = ""
        if context.get_node_data("ProduceHIF__ProduceSuggestion").get("enabled", True):
            suggestion = self._get_suggestion(context, image)

        health_data = self._get_health(context, image) or {"current": 34, "max": 34, "ratio": 1.0}
        points = self._get_current_points(context, image) or 0
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        events = self._get_available_events(context, image)

        best_event = self._choose_best_event(suggestion, health_data, points, score, events)
        if not best_event:
            logger.info("无可用事件")
            return True

        # 休息前检查：次数用完（检测到"あと0回"）时不可休息，改选其他事件
        if best_event.get("run_task") == "ProduceHIF__ProduceChooseRest" and not self._is_rest_available(context, image):
            logger.warning("休息次数已用完，改选其他事件")
            best_event = self._choose_best_event(suggestion, health_data, points, score, events, allow_rest=False)
            if not best_event:
                logger.info("无可用事件")
                return True

        box = best_event["box"]
        logger.info(f"选择事件: {best_event['name']}, 坐标: ({box[0] + box[2] // 2}, {box[1] + box[3] // 2})")

        return self._execute_event(context, best_event)

    def _get_preference(self, context: Context, argv: CustomAction.RunArg) -> dict:
        """获取偏好设置：优先读前台「SP第一优先级/第二优先级」选项，其次 argv.custom_action_param，最后默认。

        前台 SP 优先级选项按**定制卡同款设计**：每个属性占位节点(ProduceHIF__ProduceHIFSPFirstDa/Vo/Vi 等)用 enabled
        布尔承载选中值，代码读哪个节点 enabled(与 _item_enabled 一致)。别用非 enabled 字段(占位节点不落位)。
        """
        def _enabled_attr(prefix):
            for attr in ("Da", "Vo", "Vi"):
                try:
                    if context.get_node_data(prefix + attr).get("enabled"):
                        return attr
                except Exception:  # pylint: disable=broad-except
                    continue
            return None

        first = _enabled_attr("ProduceHIF__ProduceHIFSPFirst")
        second = _enabled_attr("ProduceHIF__ProduceHIFSPSecond")
        # ① 回退 argv.custom_action_param(兼容旧配置/非HIF剧本)
        if first not in self.PREFERENCE_LIST or second not in self.PREFERENCE_LIST:
            try:
                raw = argv.custom_action_param if getattr(argv, "custom_action_param", None) else "{}"
                params = json.loads(raw) if raw else {}
                if first not in self.PREFERENCE_LIST:
                    first = params.get("first")
                if second not in self.PREFERENCE_LIST:
                    second = params.get("second")
            except (json.JSONDecodeError, AttributeError, TypeError):
                logger.warning("SP优先级参数解析失败，使用默认值")
        return {"first": first if first in self.PREFERENCE_LIST else "Da",
                "second": second if second in self.PREFERENCE_LIST else "Vo"}

    def _is_rest_available(self, context: Context, image) -> bool:
        """检测休息是否可用：休息按钮剩余次数区域出现「あと0回」时不可休息。"""
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceRecognitionRestCount",
            image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionRestCount": {
                    "recognition": "OCR",
                    "roi": self.REST_COUNT_ROI,
                    "expected": ["あと[0０]回", "剩余[0０]次"],  # 兼容全角/半角 0
                }
            },
        )
        if reco_detail and reco_detail.hit:
            logger.info("休息次数已用完")
            return False
        return True

    def _choose_best_event(
        self, suggestion: str, health_data: dict, points: int, score: dict, events: list, allow_rest: bool = True
    ) -> Optional[dict]:
        """
        根据获取到的信息从可用事件中选择最佳事件。

        Returns:
            dict: {"name": str, "box": [x, y, w, h], "run_task": str}，None表示无事件可选
        """
        max_score = score.get("max", 1)
        first_ratio = score.get(self.first, 0) / max_score if max_score > 0 else 0
        second_ratio = score.get(self.second, 0) / max_score if max_score > 0 else 0
        # READ_ATTR_EVENT=False 时关闭"属性满停止"判断(不因属性占比触发停选SP/课程), 保留上方读取代码
        if self.READ_ATTR_EVENT:
            is_first_near_full = first_ratio >= self.FIRST_NEAR_FULL_RATIO
            is_first_stopped = first_ratio >= self.ATTR_STOP_RATIO
            is_second_stopped = second_ratio >= self.ATTR_STOP_RATIO
        else:
            is_first_near_full = False
            is_first_stopped = False
            is_second_stopped = False

        # 0. 低体力处理
        current_health = health_data["current"] if health_data else 0
        ratio_health = health_data["ratio"] if health_data else 1.0
        if current_health < self.LOW_HEALTH_VALUE or ratio_health < self.LOW_HEALTH_RATIO:
            go_out = self._find_event_by_name(events, "外出")
            if go_out and points >= 100:
                return self._make_event("外出", go_out)
            if allow_rest:
                logger.info("体力过低，选择休息")
                return {"name": "rest", "box": [0, 0, 0, 0], "run_task": "ProduceHIF__ProduceChooseRest"}
            # 休息不可用（次数用完），继续走后续优先级

        # 1. 老师建议
        suggestion_attr = self._parse_suggestion(suggestion)
        if suggestion_attr:
            event = self._find_attr_event(events, suggestion_attr)
            if event:
                return self._make_event(suggestion_attr, event)

        # 2. SP（第一属性，仅当属性未达80%时）
        if not is_first_stopped:
            event = self._find_attr_event(events, self.first, need_sp=True)
            if event:
                return self._make_event(f"{self.first}_SP", event)

        # 3. SP（第二属性，仅当属性未达80%时）
        if not is_second_stopped:
            event = self._find_attr_event(events, self.second, need_sp=True)
            if event:
                return self._make_event(f"{self.second}_SP", event)

        # 钩子：SP之后、属性课程之前的额外优先事件（NIA的"营业"）
        extra = self._choose_extra_before_attrs(events)
        if extra:
            return extra

        # 4. 第一属性课程（属性未达80%时）
        if not is_first_stopped:
            event = self._find_attr_event(events, self.first, need_sp=False)
            if event:
                return self._make_event(self.first, event)

        # 5. 第二属性课程（第一属性停止或快满时，且第二属性未达80%）
        if (is_first_stopped or is_first_near_full) and not is_second_stopped:
            event = self._find_attr_event(events, self.second, need_sp=False)
            if event:
                return self._make_event(self.second, event)

        # 6. 交谈/活动（优先交谈，不满足则活动）
        if points >= 100:
            event = self._find_event_by_name(events, "交谈")
            if event:
                return self._make_event("交谈", event)
        event = self._find_event_by_name(events, "活动")
        if event:
            return self._make_event("活动", event)

        # 7. 上课（部分子类无此事件，由EVENT_CONFIG控制）
        event = self._find_event_by_name(events, "上课")
        if event:
            return self._make_event("上课", event)

        # 8. 外出
        event = self._find_event_by_name(events, "外出")
        if event:
            return self._make_event("外出", event)

        # 钩子：外出之后、其他属性SP之前的额外优先事件（NIA的"指导"）
        extra = self._choose_extra_after_outing(events)
        if extra:
            return extra

        # 9. SP（其他属性）
        for attr in ["Vo", "Da", "Vi"]:
            if attr not in [self.first, self.second]:
                event = self._find_attr_event(events, attr, need_sp=True)
                if event:
                    return self._make_event(f"{attr}_SP", event)

        # 保底：从剩余事件中随机选择
        if events:
            fallback = self._find_any_event(events)
            if fallback:
                logger.warning(f"所有优先级未命中，保底选择: {fallback['name']}")
                return fallback

        return None

    def _choose_extra_before_attrs(self, events: list) -> Optional[dict]:
        """钩子：SP之后、属性课程之前的额外优先事件。子类可覆盖。"""
        return None

    def _choose_extra_after_outing(self, events: list) -> Optional[dict]:
        """钩子：外出之后的额外优先事件。子类可覆盖。"""
        return None

    def _execute_event(self, context: Context, event: dict) -> bool:
        """执行事件：双击坐标，等待动画后执行后续任务（如果有）。"""
        run_task = event.get("run_task")
        box = event["box"]

        x = box[0] + box[2] // 2
        y = box[1] + box[3] // 2
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.ACTION_DELAY)
        if run_task:
            logger.info(f"执行任务{run_task}")
            context.run_task(run_task)
        return True

    def _find_attr_event(self, events: list, attr: str, need_sp: bool = False) -> Optional[list]:
        """查找指定属性事件的坐标。need_sp=True时只返回有SP标记的属性事件。"""
        for event in events:
            if attr in event:
                if need_sp and not event.get("SP"):
                    continue
                return event[attr]
        return None

    def _find_event_by_name(self, events: list, event_name: str) -> Optional[list]:
        """从事件列表中查找指定名称事件的坐标。"""
        for event in events:
            for key in event:
                if event_name in key:
                    return event[key]
        return None

    def _find_any_event(self, events: list) -> Optional[dict]:
        """从事件列表中获取任意第一个可用事件。"""
        for event in events:
            for key in event:
                if key not in ["SP"] and isinstance(event[key], list):
                    run_task = self.RUN_TASK_MAP.get(key, "")
                    return self._make_event(key, event[key], run_task=run_task)
        return None

    def _make_event(self, name: str, box: list, run_task: str = "") -> dict:
        """创建事件字典，run_task默认为空时从RUN_TASK_MAP自动获取。"""
        return {"name": name, "box": box, "run_task": run_task or self.RUN_TASK_MAP.get(name, "")}

    def _parse_suggestion(self, suggestion: str) -> Optional[str]:
        """从老师建议中解析属性名称，匹配SUGGESTION_CONFIG中的关键词。"""
        if not suggestion:
            return None
        for attr in ["Vo", "Da", "Vi"]:
            for keyword in self.SUGGESTION_CONFIG[attr].get("keyword", []):
                if keyword in suggestion:
                    return attr
        return None

    @staticmethod
    def _get_screenshot(context: Context):
        """获取屏幕截图"""
        return context.tasker.controller.post_screencap().wait().get()

    def _get_suggestion(self, context: Context, image) -> str:
        """获取建议课程"""
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceChooseEventSuggestion",
            image,
            pipeline_override={"ProduceHIF__ProduceChooseEventSuggestion": {"recognition": "OCR", "roi": self.SUGGESTION_ROI}},
        )

        if not (reco_detail and reco_detail.hit):
            return ""

        suggestion_text = "".join(item.text for item in reco_detail.filtered_results)
        logger.info(f"老师建议: {suggestion_text}")
        return suggestion_text

    @staticmethod
    def _get_health(context: Context, image) -> Optional[dict]:
        """获取体力比例"""
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionHealth", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            health_parts = reco_detail.best_result.text.split("/")
            current_health = int("".join(filter(str.isdigit, health_parts[0])))
            max_health = int("".join(filter(str.isdigit, health_parts[1])))
            ratio = current_health / max_health
            logger.info(f"体力: {current_health}/{max_health} ({ratio:.2%})")
            return {"current": current_health, "max": max_health, "ratio": ratio}
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_current_points(context: Context, image) -> Optional[int]:
        """获取当前积分"""
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceRecognitionPoint",
            image,
            pipeline_override={"ProduceHIF__ProduceRecognitionPoint": {"roi": [320, 90, 130, 54]}},
        )
        if not (reco_detail and reco_detail.hit):
            return None
        try:
            points = int(reco_detail.best_result.text.replace(",", ""))
            logger.info(f"积分: {points}")
            return points
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    @staticmethod
    def _get_current_score(context: Context, image) -> Optional[dict]:
        """获取当前得分"""
        score = {
            "Vo": 0,
            "Da": 0,
            "Vi": 0,
            "max": 0,
        }
        for i in range(3):
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"roi": [150 + i * 150, 668, 136, 80]}},
            )
            if reco_detail and reco_detail.hit and len(reco_detail.filtered_results) == 2:
                current_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[0].text)))
                max_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[1].text.replace("/", ""))))
                logger.debug(f"第{i + 1}列得分: {current_score} / {max_score}")
                score[["Vo", "Da", "Vi"][i]] = current_score
                score["max"] = max_score if score["max"] < max_score < 9999 else score["max"]
        try:
            logger.info(f"当前得分: Vo={score['Vo']}, Da={score['Da']}, Vi={score['Vi']}, Max={score['max']}")
            return score
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    def _get_available_events(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用事件列表"""
        available_events = []
        available_events_name = ""

        for event_name, event_img in self.EVENT_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionEvent",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionEvent": {"recognition": "TemplateMatch", "template": event_img, "roi": [0, 880, 720, 220]}
                },
            )
            if reco_detail and reco_detail.hit:
                if event_name in ["Da", "Vi", "Vo"]:
                    available_events_name = "Vo, Da, Vi"
                    available_events = [
                        {"Vo": [190, 1000, 1, 1], "SP": self._get_sp_course(context, image, [70, 900, 80, 80])},
                        {"Da": [360, 1000, 1, 1], "SP": self._get_sp_course(context, image, [250, 900, 80, 80])},
                        {"Vi": [530, 1000, 1, 1], "SP": self._get_sp_course(context, image, [430, 900, 80, 80])},
                    ]
                    break
                available_events.append({event_name: reco_detail.best_result.box})
                available_events_name += f"{event_name}, "

        logger.info(f"可用事件: {available_events_name.rstrip(', ')}")
        logger.debug(available_events)
        return available_events

    def _get_sp_course(self, context: Context, image, sp_roi: List[int]) -> bool:
        """获取SP课程选择"""
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceChooseEventSp",
            image,
            pipeline_override={"ProduceHIF__ProduceChooseEventSp": {"recognition": "TemplateMatch", "template": "hif/produce/sp.png", "roi": sp_roi}},
        )
        if reco_detail and reco_detail.hit:
            logger.debug(f"{sp_roi}存在SP课程")
            return True
        return False


@AgentServer.custom_action("ProduceHIF__ProduceChooseEventAuto")
class ProduceHIF__ProduceChooseEventAuto(ProduceHIF__ProduceChooseEventBase):
    """
    自动选择培育事件。

    选择优先级顺序（从高到低）：
    1. 低体力保护：体力 < 20% 或体力值 < 8时，优先外出恢复体力，其次休息
    2. 老师建议：根据OCR结果匹配Vo/Da/Vi属性关键词
    3. SP课程（第一属性，仅当该属性分数占比 < 80%时）
    4. SP课程（第二属性，仅当该属性分数占比 < 80%时）
    5. 第一属性课程（仅当该属性分数占比 < 80%时）
    6. 第二属性课程（仅当第一属性分数占比 >= 85%时，且第二属性占比 < 80%）
    7. 交谈/活动：优先交谈消耗points商店购物（需points >= 100），不满足则活动获得points
    8. 上课：提升Vo/Da/Vi
    9. 外出：恢复体力
    10. SP课程（非第一/第二属性）
    保底：以上均未命中时，从剩余事件中任选一个
    """

    SUGGESTION_CONFIG = {
        "Vo": {"img": "hif/produce/Vo.png", "keyword": ["ボーカル", "唱歌"]},
        "Da": {"img": "hif/produce/Da.png", "keyword": ["ダンス", "舞蹈"]},
        "Vi": {"img": "hif/produce/Vi.png", "keyword": ["ビジュアル", "视觉"]},
        "体力": {"img": "hif/produce/rest.png", "keyword": ["体力"]},
        "交谈": {"img": "hif/produce/chat.png", "keyword": ["先生に相談して", "咨询"]},
    }

    EVENT_CONFIG = {
        "Vo": "hif/produce/Vo.png",
        "Da": "hif/produce/Da.png",
        "Vi": "hif/produce/Vi.png",
        "交谈": "hif/produce/chat.png",
        "上课": "hif/produce/lesson.png",
        "活动": "hif/produce/event.png",
        "外出": "hif/produce/go_out.png",
    }

    RUN_TASK_MAP = {
        "交谈": "ProduceHIF__ProduceShoppingEntry",
    }

    def _get_preference(self, context: Context, argv: CustomAction.RunArg) -> dict:
        preference = context.get_node_data("ProduceHIF__ProduceChooseNIAEventFlag").get("action", {}).get("param", {}).get("custom_action_param", {})
        if "first" not in preference or "second" not in preference:
            logger.warning("偏好设置解析失败，使用默认值")
        return {"first": preference.get("first", "Vi"), "second": preference.get("second", "Da")}


@AgentServer.custom_action("ProduceHIF__ProduceChooseNIAEventAuto")
class ProduceHIF__ProduceChooseNIAEventAuto(ProduceHIF__ProduceChooseEventBase):
    """
    自动选择NIA培育事件。

    选择优先级顺序（从高到低）：
    1. 低体力保护：体力 < 20% 或体力值 < 8时，优先外出恢复体力，其次休息
    2. 老师建议：根据OCR结果匹配Vo/Da/Vi属性关键词
    3. SP课程（第一属性，仅当该属性分数占比 < 80%时）
    4. SP课程（第二属性，仅当该属性分数占比 < 80%时）
    5. 营业：获得score
    6. 第一属性课程（仅当该属性分数占比 < 80%时）
    7. 第二属性课程（仅当第一属性分数占比 >= 85%时，且第二属性占比 < 80%）
    8. 交谈/活动：优先交谈消耗points商店购物（需points >= 100），不满足则活动获得points
    9. 外出：恢复体力
    10. 指导
    11. SP课程（非第一/第二属性）
    保底：以上均未命中时，从剩余事件中任选一个
    """

    SUGGESTION_CONFIG = {
        "Vo": {"img": "hif/produce/NIA/Vo.png", "keyword": ["ボーカル", "唱歌"]},
        "Da": {"img": "hif/produce/NIA/Da.png", "keyword": ["ダンス", "舞蹈"]},
        "Vi": {"img": "hif/produce/NIA/Vi.png", "keyword": ["ビジュアル", "视觉"]},
        "体力": {"img": "hif/produce/rest.png", "keyword": ["体力"]},
        "指导": {"img": "hif/produce/NIA/guide.png", "keyword": ["特别指導", "特别指导"]},
        "交谈": {"img": "hif/produce/NIA/chat.png", "keyword": ["相談", "咨询"]},
    }

    EVENT_CONFIG = {
        "Vo": "hif/produce/NIA/Vo.png",
        "Da": "hif/produce/NIA/Da.png",
        "Vi": "hif/produce/NIA/Vi.png",
        "交谈": "hif/produce/NIA/chat.png",
        "活动": "hif/produce/NIA/activity.png",
        "指导": "hif/produce/NIA/guide.png",
        "外出": "hif/produce/NIA/go_out.png",
        "工作": "hif/produce/NIA/work.png",
    }

    RUN_TASK_MAP = {
        "交谈": "ProduceHIF__ProduceShoppingEntry",
        "指导": "ProduceHIF__ProduceGuideEntry",
        "工作": "ProduceHIF__ProduceWorkEntry",
    }

    def _choose_extra_before_attrs(self, events: list) -> Optional[dict]:
        """营业：获得score"""
        event = self._find_event_by_name(events, "工作")
        if event:
            return self._make_event("工作", event)
        return None

    def _choose_extra_after_outing(self, events: list) -> Optional[dict]:
        """指导（点击后需执行任务）"""
        event = self._find_event_by_name(events, "指导")
        if event:
            return self._make_event("指导", event)
        return None


@AgentServer.custom_action("ProduceHIF__ProduceCardsAuto")
class ProduceHIF__ProduceCardsAuto(CustomAction):
    """
    自动识别根据系统提示出牌
    15秒未检测到提示牌，则打出最高分的牌
    识别不到体力退出函数
    处理是否打出该牌的弹窗
    """

    # 阈值常量
    PROFESSION_NODE = "ProduceHIF__ProduceHIFProfessionFlag"
    PROFESSION_BY_MAX_HIT = {
        1: "好调", 2: "集中", 3: "好印象", 4: "元気", 5: "全力", 6: "強気",
    }
    DEFAULT_PROFESSION = "集中"
    CLICK_DELAY = 0.3
    TIME_OUT = 15.0
    # 出牌后技能特效动画期会遮盖手牌，用于漏框。判定"特效已结束、可稳定识别手牌"的
    # 标志是"跳过(SKIP)按钮"连续稳定命中 N 次，而非盲等固定时长。
    SKIP_STABLE_COUNT = 3
    # 判定"战斗结束（结算界面）"需连续命中的次数：防单帧误匹配（动画闪光/过渡帧
    # 偶然匹配到类似次へ的橘色元素）导致"还在战斗却误退出"。
    BATTLE_END_STABLE = 2
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
    # 旧普通饮料回合入口暂留作回归对照；运行时改读 HIF 饮料面板。
    DRINK_TURN = 4
    DRINK_TURN_NODE = "ProduceHIF__ProduceHIFRemainingDrinkTurn"
    MAX_DRINK_TURN = 12
    # 开始饮料扫描前等待技能特效/回合切换动画收尾，再截图并点击饮料栏。
    DRINK_ACTION_PRE_DELAY = 3.0
    # 卡牌Y轴位置边界（超出范围视为识别异常，不点击）
    CARD_Y_MIN = 840
    CARD_Y_MAX = 1000
    # 手牌去重: 两卡框 IOU 超过此值视为同一卡(模型会把某些卡"劈成"完整框+半宽窄条,
    # 窄条与完整框重叠是误检, NMS按面积保留大者)。真卡并排不重叠(IOU≈0)。
    CARD_IOU_THRESH = 0.15
    # 所有 HIF 职业共用：整手可出牌需连续两帧数量、身份及横向位置一致才允许点击。
    CARD_HAND_STABLE_COUNT = 2
    CARD_HAND_X_TOLERANCE = 30
    CARD_HAND_POLL_INTERVAL = 0.2
    HIF_CARD_STATE_TIMEOUT = 3.0
    HIF_CARD_CLICK_ATTEMPTS = 2
    HIF_PREVIEW_X_TOLERANCE = 45
    # 手牌识别: 最高分与次高分差距小于此值 → 模板歧义(卡面相似), 用OCR卡名兜底(卡名唯一更准)
    IDENTIFY_MARGIN = 0.05
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
    # 移动技能卡界面(喝棕瓶后出现): 3行4列格位(153344实测) + 顶部详情卡名区(153350实测)
    MOVE_GRID = [(139, 457), (286, 457), (433, 457), (580, 457),
                 (139, 604), (286, 604), (433, 604), (580, 604),
                 (139, 741), (286, 741), (433, 741), (580, 741)]
    MOVE_NAME_ROI = [240, 40, 400, 175]     # 移动界面顶部详情卡名区(详情框位置会漂移,扩宽覆盖y40-215, 卡名'脚光+'实测y50-95/y119-179均出现过)
    # HIF 黑醋移动页：标题位于分隔线上方，避开左侧卡图、右侧消耗和下方效果。
    HIF_MOVE_NAME_ROI = [190, 105, 410, 40]
    # 判定的卡名+效果描述特征(参考技能卡定制 expected 用|连接). 卡名"脚光+"可能被OCR读成"Vć+"等变体,
    # 但效果描述"好調消費2ターン"(脚光最独特)OCR读得准, 用描述特征兜底识别脚光; 不用太通用的描述词避免误判.
    MOVE_TARGET_CARD = "脚光"
    MOVE_TARGET_CARDS = ["脚光", "腳光", "脚光+", "腳光+", "好調消費2ターン"]
    MOVE_OCR_RETRY = 3                       # 每格点卡后OCR重试次数(等详情弹出/读稳,参考技能卡定制)
    MOVE_OCR_ALL = r"[^\n]+"                 # 宽匹配: 读详情区全部卡名文字(含非脚光卡, 供子串判断)
    # 全力职业的保留页按模板优先定位，避免卡数变化后点击空位仍显示上一张详情而误判。
    FULL_POWER_HOLD_ROI = [50, 490, 620, 440]
    FULL_POWER_HOLD_TARGETS = [
        ("アッチェレランド", "hif/cards/accherellando_hold.png"),
        ("頂点へ", "hif/cards/chouten_he_hold.png"),
        ("羽ばたけ！", "hif/cards/habatake_hold.png"),
        ("わたしは、風！", "hif/cards/watashi_wa_kaze_hold.png"),
    ]
    FULL_POWER_HONRYOU_HOLD_GRID = [(139, 570), (286, 570), (433, 570), (580, 570),
                                    (139, 720), (286, 720), (433, 720), (580, 720),
                                    (139, 870), (286, 870), (433, 870), (580, 870)]
    FULL_POWER_HONRYOU_HOLD_NAME_ROI = [170, 70, 500, 160]
    FULL_POWER_HONRYOU_HOLD_TARGETS = ("羽ばたけ！", "わたしは、風！")
    FULL_POWER_HOLD_THRESHOLD = 0.8
    FULL_POWER_HOLD_FIRST_POS = (139, 570)
    # 保留手牌页仅显示一行两张候选，位置高于通常的牌堆保留页及本領発揮的多行网格。
    # 它只在牌堆坐标多次未能离开保留页时使用，不能作为首次选择坐标。
    FULL_POWER_DECK_HOLD_SOURCE = "モチベ+"
    FULL_POWER_UNKNOWN_HOLD_SOURCE = "未知卡"
    FULL_POWER_HAND_HOLD_ROI = [50, 390, 340, 150]
    FULL_POWER_HAND_HOLD_FIRST_POS = (139, 458)
    FULL_POWER_HAND_HOLD_RETRY_LIMIT = 3
    FULL_POWER_HOLD_CANCEL_POS = (220, 1160)
    FULL_POWER_MOVE_ENABLED_TIMEOUT = 3.0
    FULL_POWER_MOVE_ENABLED_STABLE_COUNT = 2
    FULL_POWER_MOVE_ENABLED_POLL = 0.2

    def __init__(self):
        super().__init__()
        self.start_time = time.time()
        # 组合技状态
        self.waiting_combo = False
        # 已点击组合技起始卡、但尚未确认它从手牌消失。点击可能因卡顿漏掉，
        # 在确认前不能进入等待脚光状态。
        self.combo_first_pending = False
        self.combo_first_absent_checks = 0
        self.combo_done = False
        self.combo_failed = False    # 组合技等待失败（空过达上限放弃）→ 脚光改为可正常打
        self.consecutive_skip = 0
        self._drink_brown_tried = False  # 本场是否已试过喝棕色第3瓶(防重复,run()开局重置)
        self._move_done = False          # 喝棕瓶移动流程是否已完成(移动前不打手牌脚光,移动后才可打)
        self._last_turn_count = None     # 上一次可信的剩余回合数，用于排除 OCR 异常跳变
        self._pending_extra_turns = None
        self._extra_turn_read_confirmed = False
        self._extra_turn_transition = False
        self._extra_rollover_seen = None
        self._extra_rollover_reserve = None
        self._full_power_hold_source = None
        self._full_power_hand_hold_pending = False
        self._full_power_hand_hold_retries = 0
        self._active_pointer_state = None
        self._full_power_value = None
        self._pending_card_hand = None
        self._pending_card_hand_count = 0
        self._pending_card_hand_started_at = None
        # HIF 识别卡目录与出牌优先级分别读取；删除优先级不删除识别模板。
        self.cards_list = []          # [{"key": str, "template": str}]
        self.priority_index = {}      # key -> 顺序（越小越优先）
        self.unknown_priority = 11    # HIF 当前职业未配置/暂未识别卡的兜底优先级，沿用原配置字段
        self.conditional_priority_rules = {}  # 当前职业卡牌的条件优先级
        self.no_extra_turn_keys = set()
        self.use_conditions = {}  # 当前职业：组内条件同时满足，组间满足任一组即可出牌
        self.combo_first = None       # 组合技起始卡 A 的 key
        self.combo_second = None      # 组合技收尾卡 B 的 key
        self.combo_wait_enabled = True  # 等待回合为0时不空过等脚光；两张同时在手仍按组合顺序打
        self.fallback = "suggestion"  # 兜底：suggestion / best_score
        self.skip_limit = 4           # 空过上限回合数
        self.match_threshold = 0.75   # 模板匹配阈值（越大越严格）
        self.profession = self.DEFAULT_PROFESSION
        self.drink_turn = self.DRINK_TURN
        self._drink_catalog_by_key = {}
        self._drink_imported_names = []
        self._drink_specific_timings = {}
        self._drink_disabled_names = set()
        self._drink_default_timing = {"mode": "never"}
        self._drink_attempted_triggers = set()
        self._drink_before_targets = []
        self._followups = None
        self._followup_skip_turn = None
        self._followup_skip_deadline = None
        self._followup_move_targets = None
        self._paired_card = None
        self._paired_deadline = None
        self.hif_battle_skip = False  # 当前场是否跳过，由 SKIP本战1/2 与初始回合数决定
        self.hif_battle_number = None
        self._hif_recognition = False
        self._hif_debug_enabled = False
        self._hif_name_index = {}
        self._reset_hif_recognition()
        self._load_config()

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        try:
            return self._run_battle(context, argv)
        except HifDrinkFlowError as exc:
            logger.error(str(exc))
            return False
        finally:
            self._followups = None
            self._paired_card = None
            self._paired_deadline = None
            self._followup_skip_turn = None
            self._followup_skip_deadline = None
            self._followup_move_targets = None
            self._pending_extra_turns = None
            self._extra_turn_read_confirmed = False
            self._extra_turn_transition = False
            self._extra_rollover_seen = None
            self._extra_rollover_reserve = None

    def _run_battle(self, context: Context, argv: CustomAction.RunArg) -> bool:
        self._hif_recognition = getattr(argv, "node_name", "") == "ProduceHIF__ProduceHIFCardsFlag"
        self._hif_debug_enabled = self._hif_recognition and self._load_hif_debug(context)
        self._reset_hif_recognition()
        self._load_config(self._load_hif_profession(context))
        self._load_hif_battle_drink_policy()
        if self._hif_recognition:
            self._load_hif_followups()
        self.combo_wait_enabled = self.skip_limit > 0
        if self._followups is not None:
            logger.info(f"HIF组合配置: {len(self._followups.rules)}组，等待回合按各组设置")
        # 开场演出结束后，要求 SKIP 按钮连续稳定出现，才读取初始回合数并开始 SKIP/出牌。
        # 这样不会把开场动画帧误判为可操作画面。
        self._wait_until_playable(context, confirmation_count=self.SKIP_STABLE_COUNT)
        time.sleep(0.4)

        # 重置组合技状态与本场喝buff标志（避免跨战斗泄漏）
        self.start_time = time.time()
        self.waiting_combo = False
        self.combo_first_pending = False
        self.combo_first_absent_checks = 0
        self.combo_done = False
        self.combo_failed = False
        self.consecutive_skip = 0
        self._drink_brown_tried = False  # 跨战斗清理: 本场是否试过喝棕瓶
        self._move_done = False          # 跨战斗清理: 移动流程是否已完成
        self._drink_sembri_done = False  # 跨战斗清理: 本场是否试过喝センブリソーダ(锁定一次,防重复)
        self._drink_buff_done = False    # 跨战斗清理: 本场喝buff是否已执行(锁定只喝一次,防倒数2/1回合重复判断)
        self._drink_attempted_triggers = set()
        self._followup_skip_turn = None
        self._followup_skip_deadline = None
        self._followup_move_targets = None
        self._paired_card = None
        self._paired_deadline = None
        self._last_turn_count = None     # 跨战斗清理: 不沿用上一场的回合数
        self._pending_extra_turns = None
        self._extra_turn_read_confirmed = False
        self._extra_turn_transition = False
        self._extra_rollover_seen = None
        self._extra_rollover_reserve = None
        self._battle_transition_misses = 0
        self._battle_transition_taps = 0
        self._last_battle_transition_tap = 0.0
        self._active_pointer_state = None
        self._full_power_value = None
        self._full_power_hold_source = None
        self._full_power_hand_hold_pending = False
        self._full_power_hand_hold_retries = 0
        self._reset_card_hand_stability()
        start_image = context.tasker.controller.post_screencap().wait().get()
        self.hif_battle_number = self._detect_battle_number(context, start_image)
        self.hif_battle_skip = self._load_hif_battle_skip(context, self.hif_battle_number)
        if self.hif_battle_number is None:
            logger.warning("HIF战斗: 未能以初始回合数识别本战1/2，安全起见不启用SKIP")
        elif self.hif_battle_skip:
            logger.info(
                f"HIF SKIP本战{self.hif_battle_number}已开启："
                "本场不出牌，所有可操作回合均点击SKIP"
            )
        else:
            logger.info(f"HIF战斗: 初始回合数判定为本战{self.hif_battle_number}，正常出牌")
        if not self.hif_battle_skip and self._last_turn_count in self.BATTLE_START_TURNS:
            self._process_battle_drink_timing(context, self._last_turn_count, first_turn=True)
        elif not self.hif_battle_skip and self._last_turn_count is not None:
            logger.info("HIF本战饮料: 首次可操作时已非本场第1回合，不补用开场时机饮料")
        _battle_end_count = [0]  # 「战斗结束」次へ模板 连续命中计数（跨帧累计）
        while True:
            # 处理手动终止任务
            if context.tasker.stopping:
                logger.info("任务中断")
                return True
            if self._paired_card is not None and time.monotonic() >= self._paired_deadline:
                self._abort_drink_flow(context, 'HIF饮料配对: 用饮后未能确认目标出牌，停止任务')

            # 截图
            image = context.tasker.controller.post_screencap().wait().get()

            # 反向判定战斗是否结束：检测到结算界面（排名榜/次へ按钮）才退出出牌。
            # 不再依赖数字环/体力条/绿心/SKIP 等"战斗中信号"（它们都会变或消失），
            # 而"次へ橘色按钮"只在战斗结束的结算界面出现（战斗中恒为 miss），方向反过来最稳定。
            if self._is_battle_end(context, image, consecutive=_battle_end_count):
                if self._paired_card is not None:
                    self._abort_drink_flow(context, 'HIF饮料配对: 战斗结束但目标出牌尚未确认，停止任务')
                logger.info("检测到结算界面（次へ）")
                logger.success("事件: 退出本战处理")
                break
            if _battle_end_count[0]:
                time.sleep(0.2)
                continue

            # HIF 本战SKIP：不识别或打出任何技能卡，只在可操作时点击游戏内 SKIP。
            # 仍保留结算页和星级获得页的处理，确保战斗结束后可正常推进。
            if self.hif_battle_skip:
                if self._handle_star_get(context, image):
                    continue
                skip_reco = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                if skip_reco and skip_reco.hit:
                    self._battle_transition_misses = 0
                    self._read_turn_count(context, image)
                    logger.info(f"HIF SKIP本战{self.hif_battle_number}：跳过当前回合")
                    self._skip_round(context)
                else:
                    self._skip_battle_end_animation(context, image)
                    time.sleep(0.3)
                continue

            # 移动技能卡界面（喝棕色第3瓶后出现）：逐格找脚光并移动，处理完重截继续。
            # 必须在战斗识别前处理，否则会被当成"无手牌/卡死"误判。
            if self._handle_move_cards(context, image):
                if self._hif_recognition:
                    self._reset_hif_recognition()
                logger.info("移动界面处理完成,继续")
                continue

            # 结算后过渡页「スタ性獲得」：无次へ按钮也无手牌，需点击 TAP 继续。
            # 若不做处理，会因检测不到次へ而在 run() 里无限空转。识别到即点 TAP 并重试。
            if self._handle_star_get(context, image):
                continue

            if self._hif_recognition and self._is_drink_detail_open(context, image):
                if not self._close_drink_detail(context):
                    self._abort_drink_flow(context, "饮料详情无法关闭，停止当前HIF任务")
                self._reset_hif_recognition()
                continue

            turn = None if self._hif_recognition else self._read_turn_count(context, image)

            # 「能看见 SKIP 才校验手牌」：SKIP = 可出牌/可跳过的标志。SKIP 不可见说明
            # 处于动画过渡/非可出牌状态，此时识别手牌会漏框（YOLO 框不到 cards），
            # 导致误判"无手牌"而 skip（即"出完牌后下一回合直接跳过"）。故 SKIP 不可见时不进识别，等下一帧。
            skip_reco = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
            if not (skip_reco and skip_reco.hit):
                self._reset_card_hand_stability()
                if self._hif_recognition:
                    # 单帧 SKIP 漏检不证明换手；实际动作/换回合才清空补认进度。
                    self._hif_empty_started_at = None
                    self._hif_empty_count = 0
                    self._hif_unusable_signature = None
                    self._hif_unusable_count = 0
                self._skip_battle_end_animation(context, image)
                time.sleep(0.3)
                continue
            self._battle_transition_misses = 0
            if self._hif_recognition:
                # 过场已没有可操作 HUD，不能把背景文字当成剩余回合数。
                turn = self._read_turn_count(context, image)
                if turn is not None and turn != self._hif_recognition_turn:
                    self._reset_hif_recognition()
                    self._hif_recognition_turn = turn
            self._observe_followup_turn(turn)
            if self._paired_card is None and self._process_battle_drink_timing(context, turn):
                if self._hif_recognition:
                    self._reset_hif_recognition()
                continue

            # 识别手牌
            reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionCards", image)
            hif_results = list(reco_detail.all_results or []) if self._hif_recognition and reco_detail else []
            hif_boxes = self._hif_card_boxes(hif_results) if self._hif_recognition else []
            if self._hif_recognition and hif_boxes:
                self._hif_unusable_signature = None
                self._hif_unusable_count = 0
            if self._hif_recognition and not hif_boxes:
                if self._hif_empty_hand(context, image, hif_results):
                    if self._paired_card is not None:
                        time.sleep(self.CARD_HAND_POLL_INTERVAL)
                        continue
                    if self._followups is not None and self._followups.active:
                        if self._decide(context, [])['type'] == 'retry':
                            time.sleep(self.CARD_HAND_POLL_INTERVAL)
                            continue
                    if self.waiting_combo and not self.combo_done and not self.combo_failed:
                        if self._drink_brown_bottle(context):
                            self._reset_hif_recognition()
                            continue
                    self._skip_round(context)
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                continue
            if (reco_detail and reco_detail.hit) or (self._hif_recognition and hif_results):
                # 目前模型识别的准确度不够高，暂时使用all_results
                results = reco_detail.all_results

                # 获取卡牌信息（建议牌/最高分牌，用于兜底与超时）
                suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)

                # 收集可出牌(cards)的框, 先按面积NMS去重: 模型会把某些卡"劈成"完整框+半宽窄条,
                # 窄条与完整框重叠(IOU>阈值)是误检的额外卡, 保留面积大者。
                filtered_boxes = []
                if self._hif_recognition:
                    kept = hif_boxes
                    if self._hif_hand_geometry and not self._hif_same_geometry(kept):
                        self._hif_log_detection(image, results, "手牌数量或横向位置变化")
                    self._hif_bind_hand(kept)
                    self._hif_empty_started_at = None
                    self._hif_empty_count = 0
                else:
                    cards_boxes = []
                    for r in results:
                        if r.label != "cards":
                            continue
                        if not self._in_range(r.box):
                            filtered_boxes.append(r.box)
                            continue
                        cards_boxes.append((r.box, r.score))
                    cards_boxes.sort(key=lambda bc: bc[0][2] * bc[0][3], reverse=True)
                    kept = []
                    for box, conf in cards_boxes:
                        if any(self._iou(box, kb[0]) > self.CARD_IOU_THRESH for kb in kept):
                            logger.info(f"手牌去重: 丢弃与已保留框重叠的窄条 {box}")
                            continue
                        kept.append((box, conf))

                # 识别每张可出牌（cards）的身份
                identified = []
                for index, (box, conf) in enumerate(kept):
                    if self._hif_recognition and index in self._hif_detail_keys:
                        key, score = self._hif_detail_keys[index], 0.0
                    else:
                        key, score = self._identify_card(context, image, box)
                    identified.append({"box": box, "key": key, "score": score, "conf": conf})

                if not identified and filtered_boxes:
                    logger.warning(f"cards全被range过滤: filtered_boxes={filtered_boxes}")

                stability = self._confirm_card_hand(identified)
                if stability == "wait":
                    time.sleep(self.CARD_HAND_POLL_INTERVAL)
                    continue
                paired_visible = self._paired_card is not None and any(card['key'] == self._paired_card['key'] for card in identified)
                if self._hif_recognition and not paired_visible and self._hif_probe_unknowns(context, image, identified):
                    self._reset_card_hand_stability()
                    continue

                self._active_pointer_state = (
                    self._read_active_pointer_state(context, image)
                    if self.profession in ("全力", "強気")
                    else None
                )
                if self.profession == "全力":
                    self._full_power_value = self._read_full_power_value(context, image)
                decision = self._decide(context, identified)
                if decision['type'] == 'play' and self._prepare_paired_drink(context, decision):
                    self._reset_hif_recognition()
                    continue
                before_play = {name: getattr(self, name) for name in (
                    "waiting_combo", "combo_done", "combo_first_pending", "combo_first_absent_checks",
                    "consecutive_skip", "_move_done", "_full_power_hold_source",
                    "_full_power_hand_hold_pending", "_full_power_hand_hold_retries")} if self._hif_recognition else {}
                if decision["type"] == "play":
                    box = decision["box"]
                    if self.profession == "全力":
                        self._full_power_hold_source = (
                            decision.get("key") or self.FULL_POWER_UNKNOWN_HOLD_SOURCE
                        )
                        if self._full_power_hold_source not in (
                            self.FULL_POWER_DECK_HOLD_SOURCE, "本領発揮+"
                        ):
                            self._full_power_hand_hold_pending = True
                            self._full_power_hand_hold_retries = 0
                        else:
                            self._full_power_hand_hold_pending = False
                            self._full_power_hand_hold_retries = 0
                    tag = decision.get("combo")
                    if tag == "first":
                        # 不能在点击前就进入等待脚光：卡顿时点击可能没有命中，
                        # 此时必须在下一帧继续优先点国民，直至确认它已离开手牌。
                        logger.info("点击组合技起始卡，等待确认国民已离开手牌")
                        self.combo_first_pending = True
                        self.combo_first_absent_checks = 0
                        self.waiting_combo = False
                        self.combo_done = False
                        self.consecutive_skip = 0
                        self._move_done = False       # 新一轮组合技: 移动流程未完成(移动前不打手牌脚光)
                    elif tag == "second":
                        logger.info("打出组合技收尾卡，组合技触发")
                        self.waiting_combo = False
                        self.combo_done = True  # 组合技已打完：此后不再跳过，除非无手牌
                        self.consecutive_skip = 0
                    time.sleep(1)  # 防止点击过早导致只命中一次
                    played = self._play_a_card(context, box)
                    if self._hif_recognition and not played:
                        for name, value in before_play.items():
                            setattr(self, name, value)
                        continue
                    if played and self._followups is not None:
                        self._followup_skip_turn = None
                        self._followup_skip_deadline = None
                        self._followups.committed(decision.get('key'), decision.get('followup_role'))
                        self._paired_card = None
                        self._paired_deadline = None
                elif decision["type"] == "fallback":
                    self._play_fallback(context, image, results)
                elif decision["type"] == "skip":
                    # 等待脚光空过时：先试喝「初星黒酢」(组合技道具), 喝后进移动找脚光, 处理完继续
                    if self.waiting_combo and not self.combo_done and not self.combo_failed:
                        if self._drink_brown_bottle(context):
                            logger.info("喝「初星黒酢」后进移动界面找脚光, 重截")
                            continue   # 喝了 → 重截等待移动界面出现(由主循环移动界面分支处理)
                    self._skip_round(context)
                elif decision["type"] == "retry":
                    # 国民刚点击完、正在做离手确认；不出牌也不空过，短暂等待后重截。
                    self._reset_card_hand_stability()
                    time.sleep(0.2)
                    continue

                # 超时兜底：等待组合技期间不触发，坚持空过
                end_time = time.time()
                if not self._hif_recognition and end_time - self.start_time > self.TIME_OUT:
                    if self.waiting_combo:
                        self.start_time = time.time()
                        continue
                    logger.warning("检测超时")
                    if best_box[2] > 1 and self._in_range(best_box):
                        key, _ = self._identify_card(context, image, best_box)
                        if key != self.combo_second:
                            self._play_a_card(context, best_box)
                        else:
                            self._skip_round(context)
                    else:
                        self._skip_round(context)

            else:
                self._reset_card_hand_stability()
                reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionNoCards", image)
                if reco_detail.hit:
                    logger.info("无手牌")
                    # 等待脚光空过(无手牌)时：试喝「初星黒酢」, 喝后进移动找脚光, 处理完继续
                    if self.waiting_combo and not self.combo_done and not self.combo_failed:
                        if self._drink_brown_bottle(context):
                            logger.info("喝「初星黒酢」后进移动界面找脚光, 重截")
                            continue   # 喝了 → 重截等待移动界面出现(由主循环移动界面分支处理)
                    self._skip_round(context)

            time.sleep(self.CLICK_DELAY)

        return True

    @staticmethod
    def _load_hif_battle_skip(context: Context, battle_number: Optional[int]) -> bool:
        """读取前台「SKIP本战1/2」开关；场次未识别时默认不跳过。"""
        if battle_number not in (1, 2):
            return False
        try:
            node = context.get_node_data(f"ProduceHIF__ProduceHIFBattle{battle_number}Skip")
            return bool(node and node.get("enabled", False))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF SKIP本战{battle_number}：读取开关异常 {e!r}")
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

    def _load_hif_followups(self) -> None:
        try:
            with open(CARDS_PRIORITY_CONFIG_PATH, encoding='utf-8') as file:
                config = json.load(file)
            with open(HIF_PRIORITY_CATALOG_PATH, encoding='utf-8') as file:
                catalog = json.load(file)
        except (OSError, ValueError) as exc:
            raise HifDrinkFlowError(f'HIF组合配置读取失败: {exc}') from exc
        profiles = config.get('followup_profiles')
        if not isinstance(profiles, dict) or self.profession not in profiles:
            raise HifDrinkFlowError('HIF组合尚未迁移，请打开卡牌面板保存组合配置')
        rules = profiles[self.profession]
        try:
            validate_rules(rules, catalog)
        except ValueError as exc:
            raise HifDrinkFlowError(f'HIF组合配置无效: {exc}') from exc
        self._followups = Followups(rules)
        # The native HIF path now uses only profession-scoped rules.
        self.combo_first = self.combo_second = None
        referenced = set(rules) | {key for rule in rules.values() for key in rule['targets']}
        referenced.update(self._drink_before_targets)
        for key in referenced:
            entry = catalog.get(key)
            if entry and all(card['key'] != key for card in self.cards_list):
                self.cards_list.append({'key': key, 'template': entry.get('template', ''), 'name': entry.get('name', '')})

    def _observe_followup_turn(self, turn) -> None:
        if self._followups is not None and self._followup_skip_turn is not None and turn is not None:
            if turn < self._followup_skip_turn or self._extra_turn_transition:
                source = self._followups.active
                self._followups.skipped()
                logger.info(f'HIF组合: {source} 已确认空过，等待计数={self._followups.skips}')
                self._followup_skip_turn = None
                self._followup_skip_deadline = None

    def _prepare_paired_drink(self, context, decision) -> bool:
        if (not self._hif_recognition or self._paired_card is not None
                or not decision.get('key') or decision['key'] not in self._drink_before_targets
                or self._followups is not None and self._followups.active):
            return False
        name = '特製ハツボシエキス'
        if name in self._drink_disabled_names:
            return False
        if self._consume_battle_drinks(context, [name], 'HIF出牌前配对', max_uses=1):
            self._paired_card = dict(decision)
            self._paired_deadline = time.monotonic() + self.TIME_OUT
            logger.info(f"HIF饮料配对: 已用一瓶，锁定目标={decision['key']}，等待重新定位出牌")
            return True
        return False

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

    def _load_hif_battle_drink_policy(self) -> None:
        """加载当前职业的本战用饮规则；配置损坏时停止自动喝饮料。"""
        self._drink_catalog_by_key = {}
        self._drink_imported_names = []
        self._drink_specific_timings = {}
        self._drink_disabled_names = set()
        self._drink_default_timing = {"mode": "never"}
        self._drink_before_targets = []
        try:
            with open(HIF_DRINK_CATALOG_PATH, encoding="utf-8") as file:
                catalog = json.load(file)["drinks"]
            with open(HIF_DRINK_PROFILES_PATH, encoding="utf-8") as file:
                config = json.load(file)
            by_id = {item["id"]: item["name"] for item in catalog}
            self._drink_catalog_by_key = {
                _hif_drink_name_key(name): name for name in by_id.values()
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
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") not in by_id:
                    continue
                name = by_id[entry["id"]]
                if name in self._drink_imported_names:
                    continue
                self._drink_imported_names.append(name)
                if entry.get("disabled") is True:
                    self._drink_disabled_names.add(name)
                    continue
                timing = entry.get("use_timing")
                targets = entry.get('before_card_targets')
                if targets is not None:
                    with open(os.path.join(EXT_DIR, 'catalog', 'hif_target_card_catalog.json'), encoding='utf-8') as file:
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
                _hif_drink_name_key(self.BROWN_TARGET_NAME): self.BROWN_TARGET_NAME
            }
            self._drink_imported_names = []
            self._drink_specific_timings = {}
            self._drink_disabled_names = set()
            self._drink_default_timing = {"mode": "never"}
            self._drink_before_targets = []
            logger.warning(f"HIF本战饮料: 配置读取失败，停止自动使用饮料: {exc!r}")

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
            with open(CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"未找到或解析失败 cards_priority.json ({e})，使用默认行为")
            return

        if self._hif_recognition:
            self.combo_first = self.combo_second = None
            self.skip_limit = 0  # HIF waits are owned exclusively by Followups.
        else:
            combo = cfg.get("combo", {}) or {}
            self.combo_first = combo.get("first")
            self.combo_second = combo.get("second")
            self.skip_limit = int(cfg.get("skip_limit", 4))
        self.fallback = cfg.get("fallback", "suggestion")
        self.match_threshold = float(cfg.get("match_threshold", 0.75))

        profiles = cfg.get("priority_profiles", {}) or {}
        priority = profiles.get(profession)
        using_default = priority is None
        if using_default:
            priority = profiles.get(self.DEFAULT_PROFESSION, []) or cfg.get("priority", []) or []
        if profession != self.DEFAULT_PROFESSION and using_default:
            logger.warning(f"HIF角色职业={profession}不存在优先级表，沿用{self.DEFAULT_PROFESSION}")

        self.profession = profession
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
        configured = (cfg.get("use_condition_profiles") or {}).get(profession, {})
        if isinstance(configured, dict):
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
                    continue
                conditions[key] = clauses
        self.use_conditions = conditions
        self.priority_index = {}
        for i, entry in enumerate(priority):
            key = entry.get("key")
            if not key:
                continue
            self.priority_index[key] = entry.get("priority", i)
        try:
            with open(HIF_PRIORITY_CATALOG_PATH, encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"未找到或解析失败 HIF 卡目录 ({e})，使用当前优先级表识别")
            catalog = {}
        if self._hif_recognition:
            self._hif_name_index = {}
            for key in dict.fromkeys([*(entry.get("key") for entry in priority), *catalog]):
                if key:
                    self._hif_name_index.setdefault(self._hif_card_name(key), [key])
        recognition = (cfg.get("recognition_profiles") or {}).get(profession)
        if recognition is None:
            recognition = [entry.get("key") for entry in priority]
        profile_cards = {entry.get("key"): entry for entry in priority}
        self.cards_list = []
        for key in dict.fromkeys([*recognition, *profile_cards]):
            entry = catalog.get(key) or profile_cards.get(key)
            if key and entry:
                self.cards_list.append({"key": key, "template": entry.get("template", ""), "name": entry.get("name", "")})
        if self.combo_first not in self.priority_index or self.combo_second not in self.priority_index:
            self.combo_first = self.combo_second = None

    def _in_range(self, box: list) -> bool:
        """校验卡牌Y轴坐标，超出范围视为识别异常，不点击。"""
        return self.CARD_Y_MIN <= box[1] <= self.CARD_Y_MAX

    @staticmethod
    def _hif_card_name(text: str) -> str:
        return "".join(unicodedata.normalize("NFKC", text or "").split()).rstrip("+")

    def _hif_name_key(self, text: str, *, partial: bool = False) -> Optional[str]:
        name = self._hif_card_name(text)
        keys = self._hif_name_index.get(name, [])
        # 强化符号在标题末尾；「アドリブ+」不是「アドリブの基本」的截断前缀。
        complete = "".join(unicodedata.normalize("NFKC", text or "").split()).endswith("+")
        if partial and not complete and (keys or len(name) >= 3):
            keys = [key for base, entries in self._hif_name_index.items()
                    if base.startswith(name) for key in entries]
        return keys[0] if len(keys) == 1 else None

    def _reset_hif_recognition(self) -> None:
        self._hif_hand_geometry = ()
        self._hif_hand_boxes = ()
        self._hif_missing_cards = []
        self._hif_detail_keys = {}
        self._hif_detail_attempted = set()
        self._hif_probe_started_at = None
        self._hif_probe_expired = False
        self._hif_empty_started_at = None
        self._hif_empty_count = 0
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

    def _hif_bind_hand(self, boxes: list) -> None:
        current = self._hif_geometry(boxes)
        previous_boxes = list(self._hif_hand_boxes)
        previous_geometry = list(self._hif_hand_geometry)
        previous_keys = dict(self._hif_detail_keys)
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
        logger.warning(f"HIF手牌诊断: {reason}; "
                       f"results={[(r.label, r.box, round(r.score, 3)) for r in results]}")
        if self._hif_debug_enabled and cv2 is not None and image is not None:
            try:
                folder = os.path.join(BASE_DIR, "debug", "custom", "hif_cards")
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"{time.time_ns()}.png")
                if cv2.imwrite(path, image):
                    logger.info(f"HIF手牌诊断截图: {path}")
            except Exception as exc:  # 诊断失败不能阻止战斗。
                logger.warning(f"HIF手牌诊断截图失败: {exc!r}")

    def _hif_empty_hand(self, context: Context, image, results: list) -> bool:
        if image is not None and self._is_drink_detail_open(context, image):
            self._hif_empty_started_at = None
            self._hif_empty_count = 0
            self._hif_unusable_signature = None
            self._hif_unusable_count = 0
            return False
        self._reset_card_hand_stability()
        self._hif_log_detection(image, results, "没有有效手牌框")
        now = time.time()
        if self._hif_empty_started_at is None:
            self._hif_empty_started_at = now
        empty = context.run_recognition("ProduceHIF__ProduceRecognitionNoCards", image)
        self._hif_empty_count = self._hif_empty_count + 1 if empty and empty.hit else 0
        if self._hif_empty_count >= 2:
            logger.info("HIF连续两次确认无手牌")
            return True
        # Use the same hand geometry/score rules as playable cards, but require all detections to be unusable.
        hand = [r for r in results if r.score >= 0.5 and self._in_range(r.box)
                and 0 <= r.box[0] < 720 and r.box[2] >= 90
                and 180 <= r.box[3] <= 330 and r.box[1] + r.box[3] <= 1170]
        unusable = bool(hand) and all(r.label == "useless" for r in hand)
        playable = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image) if unusable else None
        turn = self._read_turn_count(context, image) if unusable and playable and playable.hit else None
        if unusable and playable and playable.hit and turn is not None and not self._is_drink_detail_open(context, image):
            signature = (turn, tuple((round((r.box[0] + r.box[2] / 2) / 20), r.label) for r in sorted(hand, key=lambda r: r.box[0])))
            self._hif_unusable_count = self._hif_unusable_count + 1 if signature == self._hif_unusable_signature else 1
            self._hif_unusable_signature = signature
            if self._hif_unusable_count >= 2:
                logger.info("HIF连续两帧确认仅有不可用牌，跳过回合")
                return True
        else:
            self._hif_unusable_signature = None
            self._hif_unusable_count = 0
        if now - self._hif_empty_started_at >= self.TIME_OUT:
            logger.warning("HIF持续15秒没有可靠卡框，按配置约定点击SKIP")
            return True
        return False

    def _hif_log_selection_pair(self, before, after, index: int) -> None:
        if not self._hif_debug_enabled or self._hif_selection_pair_saved or cv2 is None:
            return
        self._hif_selection_pair_saved = True
        try:
            folder = os.path.join(BASE_DIR, "debug", "custom", "hif_cards")
            os.makedirs(folder, exist_ok=True)
            prefix = f"{time.time_ns()}-select-{index + 1}"
            for phase, image in (("before", before), ("after", after)):
                path = os.path.join(folder, f"{prefix}-{phase}.png")
                if image is not None and cv2.imwrite(path, image):
                    logger.info(f"HIF选中点击对照截图: 第{index + 1}张 {phase}={path}")
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

    def _hif_preview_box(self, boxes: list, index: int):
        """选中期间允许邻牌暂时漏框，但不接受新卡位或跨卡位关联。"""
        if index >= len(self._hif_hand_geometry):
            return None
        matches = {}
        for box, _ in boxes:
            cx = box[0] + box[2] // 2
            nearby = [i for i, x in enumerate(self._hif_hand_geometry)
                      if abs(x - cx) <= self.HIF_PREVIEW_X_TOLERANCE]
            if not nearby:
                nearby = [i for i, previous in enumerate(self._hif_hand_boxes)
                          if self._hif_duplicate_box(box, previous)]
            if len(nearby) != 1 or nearby[0] in matches:
                return None
            matches[nearby[0]] = box
        return matches.get(index)

    @staticmethod
    def _hif_click_card(context: Context, box: list, index: int, phase: str, attempt: int) -> bool:
        point = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        job_id = None
        try:
            job = context.tasker.controller.post_click(*point)
            job_id = job.job_id
            logger.info(f"HIF{phase}点击提交: 第{index + 1}张 point={point} attempt={attempt + 1} job_id={job_id}")
            job.wait()
            if not job.succeeded:
                logger.warning(f"HIF{phase}点击执行未成功: job_id={job_id}，重新识别")
                return False
            logger.info(f"HIF{phase}点击控制器执行成功: job_id={job_id}，等待画面确认")
            return True
        except Exception as exc:
            logger.warning(f"HIF{phase}点击执行异常: job_id={job_id} error={exc!r}，重新识别")
            return False

    def _hif_select_card(self, context: Context, image, index: int):
        if context.tasker.stopping:
            return None
        image = context.tasker.controller.post_screencap().wait().get()
        boxes = self._hif_frame_boxes(context, image)
        if self._hif_preview_box(boxes, index) is None:
            return None
        skip = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
        if not (skip and skip.hit):
            return None
        selected = self._hif_selected_index(context, image, boxes)
        if selected == -2:
            logger.warning("HIF选中卡位置未确认，本次不点击")
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
                logger.info(f"HIF选中复核: 第{index + 1}张已选中，本次未发出选中点击")
            deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
            stable = missed = 0
            while time.time() < deadline and not context.tasker.stopping:
                image = context.tasker.controller.post_screencap().wait().get()
                boxes = self._hif_frame_boxes(context, image)
                target = self._hif_preview_box(boxes, index)
                if target is None:
                    return None
                target_selected = self._hif_has_select(context, image, target)
                stable = stable + 1 if target_selected and (cv2 is None or self._hif_panel_top(image) is not None) else 0
                if stable >= 2:
                    self._hif_selection_index = index
                    logger.info(f"HIF画面选中确认: 第{index + 1}张 SELECT及详情连续两帧，选中点击={clicked}")
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
                logger.info("HIF选中复核: 本次未发出点击，原选中状态已消失，重新识别")
                return None
            selected = -1
            if attempt == 0:
                logger.warning("HIF选中点击控制器已执行，但画面仍未选中目标，重试一次")
                self._hif_log_selection_pair(before_image, image, index)
        logger.warning("HIF选中未确认，重新识别；状态不明确时不连续点击")
        return None

    def _hif_read_detail(self, context: Context, index: int) -> tuple:
        deadline = time.time() + self.HIF_CARD_STATE_TIMEOUT
        previous = ""
        count = 0
        while time.time() < deadline and not context.tasker.stopping:
            image = context.tasker.controller.post_screencap().wait().get()
            boxes = self._hif_frame_boxes(context, image)
            top = self._hif_panel_top(image)
            target = self._hif_preview_box(boxes, index)
            if target is None:
                return None, ""
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

    def _hif_probe_unknowns(self, context: Context, image, identified: list) -> bool:
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
        reco = context.run_recognition("ProduceHIF__ProduceRecognitionCards", image)
        self._hif_log_detection(image, reco.all_results if reco else [], "暂未识别或存在歧义的卡进入详情补认")
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
            image = context.tasker.controller.post_screencap().wait().get()
            fresh = self._hif_frame_boxes(context, image)
            if not self._hif_same_geometry(fresh):
                self._hif_bind_hand(fresh)
                break
        return True

    def _play_hif_card(self, context: Context, box: list) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        boxes = self._hif_frame_boxes(context, image)
        cx = box[0] + box[2] // 2
        if not self._hif_hand_geometry:
            return False
        index = min(range(len(self._hif_hand_geometry)), key=lambda i: abs(self._hif_hand_geometry[i] - cx))
        if abs(self._hif_hand_geometry[index] - cx) > self.CARD_HAND_X_TOLERANCE:
            return False
        if not boxes or self._hif_preview_box(boxes, index) is None:
            self._reset_card_hand_stability()
            if boxes:
                self._hif_bind_hand(boxes)
            return False
        image = self._hif_select_card(context, image, index)
        if image is None:
            return False
        boxes = self._hif_frame_boxes(context, image)
        target = self._hif_preview_box(boxes, index)
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
                    logger.info("HIF出牌: 确认点击后，已观察到详情关闭或动画转换")
                    self._reset_hif_recognition()
                    self._wait_until_playable(context, confirmation_count=self.SKIP_STABLE_COUNT)
                    self.start_time = time.time()
                    return True
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
            target = self._hif_preview_box(self._hif_frame_boxes(context, image), index)
            if still_selected < 2 or target is None or context.tasker.stopping:
                break
            if attempt == 0:
                logger.warning("HIF出牌点击未命中，已确认原卡仍选中，重试确认一次")
        logger.warning("HIF出牌点击后未确认画面转换，重新识别，不盲连点")
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

    def _card_label(self, key: Optional[str]) -> str:
        return key or ("暂未识别" if self._hif_recognition else "未知卡")

    def _hif_priority_source(self, key: Optional[str]) -> str:
        if not key:
            return "兜底（暂未识别）"
        return "卡牌配置" if key in self.priority_index else "兜底（未单独配置）"

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
            if self._hif_recognition:
                # 两帧稳定的名字/模板身份与详情身份共用本手缓存，邻牌被遮挡时不重复补认。
                self._hif_detail_keys.update({index: card["key"] for index, card in enumerate(identified)
                                              if card.get("key")})
            logger.info(
                f"整手牌识别已稳定: {[self._card_label(key) for key, _ in hand]} "
                f"({self._pending_card_hand_count}/{self.CARD_HAND_STABLE_COUNT})"
            )
            self._reset_card_hand_stability()
            return "stable"
        if now - self._pending_card_hand_started_at >= self.TIME_OUT:
            logger.warning(
                f"整手牌连续 {self.TIME_OUT:.0f} 秒未稳定，"
                f"采用最后一次识别: {[self._card_label(key) for key, _ in hand]}"
            )
            self._reset_card_hand_stability()
            return "stable"
        logger.info(
            f"整手牌识别待稳定: {[self._card_label(key) for key, _ in hand]} "
            f"({self._pending_card_hand_count}/{self.CARD_HAND_STABLE_COUNT})"
        )
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

    def _identify_card(self, context: Context, image, box: list):
        """
        在单张手牌区域内做模板匹配，识别其属于固定卡组的哪张卡。

        Returns:
            (key, score): 命中固定卡返回其key与匹配分；未命中返回 (None, 0.0)。
        """
        if not self.cards_list and not (self._hif_recognition and self._hif_name_index):
            return None, 0.0

        # 优先用 OCR 读卡名识别(卡名唯一, 比卡面模板可靠)：能读出卡名即返回。
        # 读不到(卡名条被遮挡/OCR弱)再退回卡面模板匹配(含歧义判断)。用户要求"优先读卡名"。
        name_key = self._identify_by_name(context, image, box)
        if name_key:
            logger.info(f"手牌识别(卡名优先) box={box} -> {name_key}")
            return name_key, 0.0

        # 手牌区域外扩，且保证 ROI 至少容纳最大模板，防止模板比ROI大导致匹配失败
        height, width = getattr(image, "shape", (1280, 720))[:2]
        roi_width = min(width, max(box[2] + 40, 240))
        roi_height = min(height, max(box[3] + 40, 210))
        # Maa interprets negative coordinates relative to the far edge, rather than clipping to zero.
        roi = [min(max(box[0] - 20, 0), width - roi_width),
               min(max(box[1] - 20, 0), height - roi_height), roi_width, roi_height]

        # 收集所有命中模板的 (key, score)，排序判断是否歧义
        hits = []   # [(score, key)]
        for entry in self.cards_list:
            tmpl = entry["template"]
            if not tmpl:
                continue
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceIdentityMatch",
                    image,
                    pipeline_override={
                        "ProduceHIF__ProduceIdentityMatch": {
                            "recognition": "TemplateMatch",
                            "template": tmpl,
                            "roi": roi,
                            "threshold": self.match_threshold,
                        }
                    },
                )
            except Exception as e:
                logger.warning(f"模板匹配异常 ({tmpl}): {e}")
                continue
            if not (reco and reco.hit):
                continue
            if self._hif_recognition:
                hit_box = reco.best_result.box
                if not (box[0] <= hit_box[0] + hit_box[2] // 2 <= box[0] + box[2]
                        and box[1] <= hit_box[1] + hit_box[3] // 2 <= box[1] + box[3]):
                    continue
            scoring = getattr(reco.best_result, "scoring", None)
            if scoring is None:
                scoring = getattr(reco.best_result, "score", 0.0) or 0.0
            scoring = scoring or 0.0
            hits.append((scoring, entry["key"]))

        if hits:
            hits.sort(reverse=True)   # 按 score 降序
            top_score, top_key = hits[0]
            best_key, best_score = top_key, top_score
            if self._hif_recognition and len(hits) > 1 and top_score - hits[1][0] < self.IDENTIFY_MARGIN:
                logger.info(f"HIF卡面模板有歧义: {hits[:2]}，等待详情补认")
                best_key = None
        else:
            best_key, best_score = None, 0.0

        # 模板命中失败（可能是强化后卡面变化）→ 用部分卡名 OCR 兜底
        if best_key is None and not self._hif_recognition:
            key = self._identify_by_name(context, image, box)
            if key:
                logger.info(f"手牌名字识别 fallback: box={box} -> {key}")
                return key, 0.0

        logger.info(f"手牌识别 box={box} roi={roi} -> ({best_key}, {best_score:.3f})")
        return best_key, best_score

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

    def _identify_by_name(self, context: Context, image, box: list):
        """模板匹配失败时，OCR 卡名条识别【部分名字】。

        临时强化后卡名会变成 XX++（名字被改/截断），但前缀仍显示；用卡名前缀做子串匹配，
        不必匹配完整名字，即可在模板对强化卡面失配时仍然认出卡来。
        """
        if self._hif_recognition:
            try:
                # 名字居中；优先读可见整宽、避开上一行属性。建议框的光晕可延伸到牌底之外。
                bottom = min(box[1] + box[3], 1134)
                rois = [[max(0, box[0]), bottom - 32, box[2], 32],
                        [max(0, box[0]), box[1] + box[3] - 52, max(int(box[2] * 0.75), 78), 58]]
                for roi in rois:
                    reco = context.run_recognition("ProduceHIF__ProduceIdentityName", image, pipeline_override={
                        "ProduceHIF__ProduceIdentityName": {"recognition": "OCR", "roi": roi, "expected": r"[^\n]+"}})
                    keys = {self._hif_name_key(r.text or "", partial=True)
                            for r in (reco.filtered_results if reco and reco.hit else [])}
                    keys.discard(None)
                    if roi is rois[0] and reco and reco.hit:
                        for result in reco.filtered_results:
                            exact = self._hif_name_key(result.text or "")
                            if exact and exact not in keys and self._hif_full_name_visible(image, box, roi, result):
                                keys.add(exact)
                    if len(keys) == 1:
                        return next(iter(keys))
                    if len(keys) > 1:
                        return None
                return None
            except Exception as exc:
                logger.warning(f"HIF手牌名字OCR异常: {exc!r}")
                return None
        try:
            # 较短前缀若也是另一张卡的开头，会误命中后者；该卡改走模板兜底。
            named_cards = [
                entry for entry in self.cards_list
                if entry.get("name") and not any(
                    other is not entry and (other.get("name") or "").startswith(entry["name"])
                    for other in self.cards_list
                )
            ]
            names = [e["name"] for e in named_cards]
            if not names:
                return None
            # 卡名在卡牌底部：取 box 下缘约 58px 高的名字条。
            # 关键字是卡名前缀（2~4 字），位于名字文本左端且名字在卡内居中——
            # 只需读左段前缀即可。宽度收窄到卡的 ~0.75，右边多留缓冲，
            # 避免把邻卡的名字读进来（会把邻卡误识别成本卡 / 出现"两张あなた"）。
            text_w = max(int(box[2] * 0.75), 78)
            roi = [box[0], box[1] + box[3] - 52, text_w, 58]
            reco = context.run_recognition(
                "ProduceHIF__ProduceIdentityName",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceIdentityName": {
                        "recognition": "OCR",
                        "roi": roi,
                        "expected": "|".join(names),
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                for r in reco.filtered_results:
                    text = (r.text or "").replace(" ", "").replace("　", "")
                    for e in named_cards:
                        nm = e.get("name") or ""
                        if nm and nm in text:
                            return e["key"]
        except Exception as e:
            logger.warning(f"HIF手牌名字OCR异常: {e}")
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

    def _full_power_priority(self, card: dict, remaining_turns: Optional[int]) -> tuple:
        """返回基础优先级、生效优先级和条件优先级状态。"""
        key = card["key"]
        base = self.priority_index.get(key, self.unknown_priority if self._hif_recognition else (8 if self.profession == '全力' else 11)) if key else self.unknown_priority
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
            logger.info("全力有效手牌为空：当前卡牌均未满足使用条件，空过")
            return {"type": "skip"}

        scored = [(card, *self._full_power_priority(card, remaining_turns)) for card in candidates]
        best, base, effective, reason = min(
            scored,
            key=lambda item: (item[2], 0 if item[0]["key"] else 1, -item[0]["conf"]),
        )
        _, use_status = self._use_condition_status(best["key"])
        priority_source = f", 优先级来源={self._hif_priority_source(best['key'])}" if self._hif_recognition else ""
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
        if self._hif_recognition and self._paired_card is not None:
            key = self._paired_card['key']
            if time.monotonic() >= self._paired_deadline:
                self._abort_drink_flow(context, f'HIF饮料配对: 未确认{key}出牌，停止任务')
            matches = [card for card in identified if card['key'] == key and self._allowed_by_use_conditions(key)]
            if matches:
                return {**self._paired_card, 'box': max(matches, key=lambda c: c['conf'])['box']}
            return {'type': 'retry'}
        if self._hif_recognition and self._followups is not None:
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
                            self._followup_skip_deadline = time.monotonic() + self.TIME_OUT
                        if time.monotonic() >= self._followup_skip_deadline:
                            self._abort_drink_flow(context, 'HIF组合: 无法确认空过后的回合变化，停止任务')
                        return {'type': 'retry'}
                    return {'type': 'skip'}
            logger.info(f'HIF组合: {state.active} 本次无可用候选，解除衔接')
            state.release()
        candidates = [card for card in candidates if state.ordinary_allowed(card['key'])]
        if not candidates:
            return {'type': 'skip'}
        decision = (self._decide_full_power(candidates, {card['key'] for card in identified if card['key']})
                    if self.profession == '全力' else self._decide_legacy(context, candidates))
        if decision['type'] == 'play' and decision.get('key') in state.rules:
            decision['followup_role'] = 'start'
        return decision

    def _decide_legacy(self, context: Context, identified: list) -> dict:
        """
        根据优先级与组合技状态，决定本回合要打哪张牌。

        identified: [{"box", "key", "score", "conf"}]，仅含 YOLO 判定为可出牌(cards) 的手牌。
                    key 为 None 表示未命中固定模板 → 视为可变卡。

        Returns:
            决策字典：
                {"type": "play", "box": [...], "combo": None|"first"|"second"}
                {"type": "fallback"}
                {"type": "skip"}
                {"type": "retry"}  # 组合技起始卡离手确认中，不操作本回合
        """
        if self.profession == "全力":
            return self._decide_full_power(identified)

        # 点击国民后先确认它确实离开手牌。_play_a_card 只能确认画面恢复可操作，
        # 无法证明点击已命中；若国民仍在手牌，必须继续点国民，不能先出脚光。
        # 连续两帧未见国民才通过，避免单帧检测漏框时误把脚光当作收尾打出。
        if self.combo_first_pending:
            combo_first_cards = [c for c in identified if c["key"] == self.combo_first]
            if combo_first_cards:
                self.combo_first_absent_checks = 0
                best_combo_first = max(combo_first_cards, key=lambda c: c["conf"])
                logger.warning(
                    f"国民点击后仍在手牌，继续优先打出{self.combo_first}"
                )
                return {"type": "play", "box": best_combo_first["box"], "combo": "first", "key": self.combo_first}

            self.combo_first_absent_checks += 1
            if self.combo_first_absent_checks < 2:
                logger.info(
                    f"未见{self.combo_first}，等待二次确认 "
                    f"({self.combo_first_absent_checks}/2)"
                )
                return {"type": "retry"}

            logger.info(f"已确认{self.combo_first}离开手牌，进入等待{self.combo_second}")
            self.combo_first_pending = False
            self.combo_first_absent_checks = 0
            self.waiting_combo = True
            self.consecutive_skip = 0

        # 组合技的离手确认使用真实手牌；之后才按职业使用限制筛选候选。
        restricted = [card for card in identified if not self._allowed_by_use_conditions(card["key"])]
        identified = [card for card in identified if card not in restricted]
        if restricted and not identified:
            logger.info("HIF手牌均未满足使用限制，本回合不使用受限卡")
            return {"type": "skip"}

        # 等B状态：优先认收尾卡B；若无B但手牌又有起始卡A(国民)，说明摸到了第二张国民，
        # 应打出它重新进入等待（用户要求：等待脚光期间遇到国民要打，而非空过）。
        if self.waiting_combo:
            # 手牌有脚光 → 直接打(完成组合技)。移动流程(喝初星黒酢→移动界面)是用于
            # 脚光不在手牌时把它移进手牌; 脚光已自然在手牌时无需等移动——否则喝不到
            # 初星黒酢时 _move_done 恒为 False, 脚光在手牌也永远不打, 空过4次组合技失败(03:22日志实证)。
            for c in identified:
                if self.combo_second and c["key"] == self.combo_second:
                    logger.info(f"组合技收尾: 打出{self.combo_second}")
                    return {"type": "play", "box": c["box"], "combo": "second", "key": self.combo_second}
            # 等待回合为0时，waiting_combo 只用于“两张同时在手”时衔接下一张脚光。
            # 若脚光因识别或手牌变化已不可用，立即取消衔接，不允许空过等待。
            if not self.combo_wait_enabled:
                logger.info(f"国民脚光等待回合=0，当前无{self.combo_second}: 取消衔接并正常出牌")
                self.waiting_combo = False
                self.consecutive_skip = 0
            else:
                # 等B时若再摸到 A(组合技起始卡)，打它并保持等待（重新计时等B）
                for c in identified:
                    if self.combo_first and c["key"] == self.combo_first:
                        logger.info(f"等待{self.combo_second}期间又见起始卡{self.combo_first}: 打出，重新等待")
                        return {"type": "play", "box": c["box"], "combo": "first", "key": self.combo_first}
                logger.info(f"等待{self.combo_second}: 空过 (手牌={[self._card_label(c['key']) if self._hif_recognition else c['key'] or '可变' for c in identified]})")
                return {"type": "skip"}

        # 组合技已经成功完成后，再次上手起始卡A（国民）时将其置于绝对最高优先级，
        # 但作为普通卡单独打出，不重新进入等待脚光的组合状态。
        if self.combo_done and self.combo_first:
            combo_first_cards = [c for c in identified if c["key"] == self.combo_first]
            if combo_first_cards:
                best_combo_first = max(combo_first_cards, key=lambda c: c["conf"])
                logger.info(f"组合技完成后再次上手{self.combo_first}: 最高优先级单独打出")
                return {"type": "play", "box": best_combo_first["box"], "combo": None, "key": self.combo_first}

        # 等待回合为0时不主动囤脚光，但若国民和脚光此刻同时可出，仍先打国民。
        # 点击后同样先经过上方的“国民离手确认”，确认成功才用 waiting_combo
        # 衔接下一次决策立即打脚光，不产生任何空过等待。
        if not self.combo_wait_enabled and self.combo_first and self.combo_second:
            combo_first_cards = [c for c in identified if c["key"] == self.combo_first]
            has_combo_second = any(c["key"] == self.combo_second for c in identified)
            if combo_first_cards and has_combo_second:
                best_combo_first = max(combo_first_cards, key=lambda c: c["conf"])
                logger.info("国民脚光等待回合=0，但两张同时在手: 先打国民，再立即衔接脚光")
                return {"type": "play", "box": best_combo_first["box"], "combo": "first", "key": self.combo_first}

        # HIF 已配置卡按逐卡优先级，未配置或暂未识别的卡按职业兜底优先级参与排序。
        # 优先级升序；同级优先身份已确认的卡，再按检测置信度排序。
        # 收尾卡B（脚光）是否排除：
        #   - combo_done=True（组合技已成功打完）→ 放出来直接打（用户规则4：打完后手牌有脚光就打）
        #   - combo_failed=True（放弃等待）→ 放出来直接打
        #   - 否则（还没出过国民/仍等待）→ 按住等国民
        # 所以脚光放行条件 = combo_done or combo_failed；仅当两者皆 False 才排除。
        pool = [
            c for c in identified
            if c["key"] and (
                c["key"] != self.combo_second
                or not self.combo_wait_enabled
                or self.combo_done
                or self.combo_failed
            )
        ]
        unknown = [c for c in identified if not c["key"]]

        def _pr(c) -> int:
            if self._hif_recognition:
                return self._full_power_priority(c, self._last_turn_count)[1]
            return self.priority_index.get(c["key"], self.unknown_priority if self._hif_recognition else 11) if c["key"] else self.unknown_priority

        if pool or unknown:
            best = min(pool + unknown, key=lambda c: (_pr(c), 0 if c["key"] else 1, -c["conf"]))
            pr = _pr(best)
            _, use_status = self._use_condition_status(best["key"])
            priority_source = f", 优先级来源={self._hif_priority_source(best['key'])}" if self._hif_recognition else ""
            logger.info(
                f"候选手牌={[(self._card_label(c['key']), _pr(c)) for c in pool + unknown]} "
                f"| 按优先级拟出牌={self._card_label(best['key'])}"
                f"(优先级={pr}, 使用限制={use_status}{priority_source})"
            )
            if self.combo_wait_enabled and not self.combo_done and self.combo_first and best["key"] == self.combo_first:
                return {"type": "play", "box": best["box"], "combo": "first", "key": best["key"]}
            return {"type": "play", "box": best["box"], "combo": None, "key": best["key"]}

        # 无牌可打 → 交给兜底（系统建议 / 最高分）
        logger.info("无可打牌，走兜底")
        return {"type": "fallback"}

    def _play_fallback(self, context: Context, image, results) -> None:
        """兜底出牌：按 fallback 配置跟系统建议或打最高分牌；候选若为被按住的B则跳过回合。"""
        suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)
        if self.fallback == "suggestion" and suggestions > 0:
            box = suggestions_box
        else:
            box = best_box

        if box and box[2] > 1 and self._in_range(box):
            key, _ = self._identify_card(context, image, box)
            if self._followups is not None and (self._followups.active or not self._followups.ordinary_allowed(key)):
                logger.info('HIF兜底候选受组合留卡限制，不单独出牌')
                self._skip_round(context)
                return
            if not self._allowed_by_use_conditions(key):
                logger.info(f"兜底候选「{key}」未满足卡牌使用限制，跳过回合")
                self._skip_round(context)
                return
            # 兜底候选若为"被按住的B（脚光）"则跳过；但组合技已打完(combo_done)或已失败(combo_failed)时脚光放行，可直接打
            if key != self.combo_second or not self.combo_wait_enabled or self.combo_done or self.combo_failed:
                logger.info("兜底出牌：" + ("系统建议牌" if box is suggestions_box else "最高分牌"))
                self._play_a_card(context, box)
                return

        logger.warning("无可用牌，跳过回合")
        self._skip_round(context)

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

    def _skip_round(self, context: Context) -> None:
        """空过：跳过本回合。等待B期间累计空过次数，达到上限则放弃等待。"""
        if self._hif_recognition:
            self._reset_hif_recognition()
        self._reset_card_hand_stability()
        logger.warning("空过（跳过回合）")
        if self._followups is not None and self._followups.active and self._last_turn_count is not None:
            self._followup_skip_turn = self._last_turn_count
            self._followup_skip_deadline = time.monotonic() + self.TIME_OUT
        context.run_task("ProduceHIF__ProduceRecognitionSkipRound")
        self._wait_until_playable(context)
        self.start_time = time.time()
        if self.waiting_combo:
            self.consecutive_skip += 1
            if self.consecutive_skip >= self.skip_limit:
                logger.warning(f"空过已达上限({self.skip_limit})，组合技失败，放弃等待{self.combo_second}")
                self.waiting_combo = False
                self.combo_failed = True  # 失败后脚光不再按住，改为可正常打
                self.consecutive_skip = 0

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

    def _play_a_card(self, context: Context, box: list) -> bool:
        """
        出牌并处理移动卡牌界面。

        出牌后等"跳过(SKIP)按钮"连续稳定命中（滤掉特效动画帧，避免下一帧识别时手牌漏框），
        再返回主循环重新识别手牌。若牌真未打出（罕见卡顿），主循环下一轮会重新识别并再次出牌（自愈）。

        Args:
            context: maa的Context类
            box: 点击范围，格式为[x, y, w, h]（x、y为点击范围左上角的坐标）

        Returns:
            bool: 如果执行没有问题，返回True；否则返回False。
        """

        if self._hif_recognition:
            return self._play_hif_card(context, box)
        self._reset_card_hand_stability()
        # 出牌（连点两次确保命中）
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        logger.info("出牌 耗时:{:.2f}秒".format(time.time() - self.start_time))

        # 出牌后的技能特效动画期会遮盖部分手牌，导致下一帧识别时 YOLO 漏框
        # （被特效盖住的牌识别不出来，组合技等待被误判为"空过"）。
        # 不盲等固定时长，而是等"跳过(SKIP)按钮"连续稳定命中（continu 次）——
        # 那是画面真正进入可出牌状态的标志；特效期 skip 按钮未稳定出现，故能滤掉动画帧。
        self._wait_until_playable(context, confirmation_count=self.SKIP_STABLE_COUNT)
        self.start_time = time.time()

        return True

    def _handle_move_cards(self, context: Context, image=None) -> bool:
        """处理移动技能卡界面（喝棕色第3瓶饮料后出现）：逐格点卡OCR识别「脚光」，移动。

        点卡(3行4列) → 顶部弹卡名详情 → OCR识别是否「脚光」：
          - 找到脚光 → 保持选中该卡 → run_task("ProduceHIF__ProduceMoveCards") 点底部移动 → return True
          - 一轮遍历完没找到 → 点左上第一张(MOVE_GRID[0]) → run_task("ProduceHIF__ProduceMoveCards") → return True
          - 中途任务停止 → return False

        Args:
            context: maa的Context类
            image: 截图

        Returns:
            bool: 成功处理移动界面并移动返回True；无移动界面/失败返回False。
        """
        if image is None:
            image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionChooseMoveCards", image)
        if not (reco_detail and reco_detail.hit):
            # モチベ+ 打出后会先经过一两帧非保留页；不能在这里清空来源，
            # 否则真正进入保留页时会绕过四目标模板而直接点兜底坐标。
            if not self._full_power_hold_source:
                self._full_power_hand_hold_pending = False
                self._full_power_hand_hold_retries = 0
            return False
        if self._followup_move_targets is not None:
            return self._handle_followup_move(context)
        if self.profession == "全力":
            return self._handle_full_power_hold(context, image)
        logger.info("移动界面: 识别到移动技能卡界面,逐格找脚光")
        try:
            for i, pos in enumerate(self.MOVE_GRID):
                if context.tasker.stopping:
                    return False
                # 点第i格卡, 留详情弹出时间
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
                time.sleep(0.8)
                # 参考技能卡定制 _handle_select: 每格多次OCR重试(等详情弹出/读稳), 3次仍读不到才当非脚光
                card = None
                for _try in range(self.MOVE_OCR_RETRY):
                    image = context.tasker.controller.post_screencap().wait().get()
                    card = (self._read_hif_move_card_title(context, image) if self._hif_recognition
                            else self._match_move_card_name(context, image))
                    logger.info(f"移动界面: 格{i}({pos})卡名OCR尝试第{_try + 1}次=[{card or 'None'}]")
                    if card:
                        break   # 读到卡名(含脚光或非脚光)即不再重试
                    time.sleep(0.6)   # 详情可能未弹, 稍等重试
                if card and (not self._hif_recognition or
                             self._hif_card_name(card).replace("腳", "脚") == self.MOVE_TARGET_CARD):
                    # 找到脚光: 保持选中, 点底部移动
                    logger.info(f"移动界面: 找到脚光(卡名OCR=[{card}]) @ 格{i}({pos}) → 点移动")
                    context.run_task("ProduceHIF__ProduceMoveCards")
                    self._move_done = True   # 移动流程完成: 之后可打手牌脚光
                    return True
                if card:
                    logger.info(f"移动界面: 格{i}({pos})已识别卡名=[{card}]，非脚光，试下一格")
                else:
                    logger.info(f"移动界面: 格{i}({pos})多次OCR均未读到，试下一格")
            # 一轮遍历完没找到脚光 → 选左上第一张移动
            logger.info("移动界面: 一轮未找到脚光 → 选左上第一张移动")
            context.tasker.controller.post_click(self.MOVE_GRID[0][0], self.MOVE_GRID[0][1]).wait()
            time.sleep(0.8)
            context.run_task("ProduceHIF__ProduceMoveCards")
            self._move_done = True   # 移动流程完成: 之后可打手牌脚光
            return True
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF移动界面异常: {e!r}")
            return False

    def _handle_followup_move(self, context) -> bool:
        """Only vinegar-origin move pages use the current ordered follow-up targets."""
        found = {}
        normalize = lambda name: self._hif_card_name(name).replace('腳', '脚')
        for index, pos in enumerate(self.MOVE_GRID):
            if context.tasker.stopping:
                return False
            context.tasker.controller.post_click(*pos).wait()
            text = None
            for _ in range(self.MOVE_OCR_RETRY):
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                image = context.tasker.controller.post_screencap().wait().get()
                text = self._read_hif_move_card_title(context, image)
                if text:
                    break
            if text:
                found.setdefault(normalize(text), index)
        target = next((key for key in self._followup_move_targets if normalize(key) in found), None)
        index = found[normalize(target)] if target else 0
        logger.info(f'HIF组合检索: 选择{target or "首格兜底"}，格位={index}')
        context.tasker.controller.post_click(*self.MOVE_GRID[index]).wait()
        if target:
            for _ in range(self.MOVE_OCR_RETRY):
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                image = context.tasker.controller.post_screencap().wait().get()
                title = self._read_hif_move_card_title(context, image)
                if title and normalize(title) == normalize(target):
                    break
            else:
                return False
        if not self._wait_for_full_power_move_enabled(context):
            return False
        context.run_task('ProduceHIF__ProduceMoveCards')
        self._move_done = True
        return True

    def _read_honryou_hold_target(self, context: Context, image) -> Optional[str]:
        """读取本領発揮保留页中首次点选后展开的卡名。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.FULL_POWER_HONRYOU_HOLD_NAME_ROI,
                        "expected": "|".join(self.FULL_POWER_HONRYOU_HOLD_TARGETS)
                        + "|" + self.MOVE_OCR_ALL,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF本領発揮保留: 详情OCR异常 {e!r}")
            return None
        text = "".join(result.text for result in reco.filtered_results) if reco and reco.hit else ""
        return next((card for card in self.FULL_POWER_HONRYOU_HOLD_TARGETS if card in text), None)

    def _handle_full_power_honryou_hold(self, context: Context) -> bool:
        """本領発揮可保留两张：先点开识别，命中后再点一次确认选择。"""
        selected = set()
        for pos in self.FULL_POWER_HONRYOU_HOLD_GRID:
            if context.tasker.stopping or len(selected) == len(self.FULL_POWER_HONRYOU_HOLD_TARGETS):
                break
            context.tasker.controller.post_click(*pos).wait()
            time.sleep(0.8)
            image = context.tasker.controller.post_screencap().wait().get()
            card = self._read_honryou_hold_target(context, image)
            if not card or card in selected:
                continue
            logger.info(f"HIF本領発揮保留: 首次点开识别到「{card}」 @ {pos}，再次点击确认选择")
            context.tasker.controller.post_click(*pos).wait()
            time.sleep(0.8)
            selected.add(card)

        if context.tasker.stopping:
            return False
        self._full_power_hold_source = None
        if not selected:
            logger.info(f"HIF本領発揮保留: 未找到目标 → 点击取消 @ {self.FULL_POWER_HOLD_CANCEL_POS}")
            context.tasker.controller.post_click(*self.FULL_POWER_HOLD_CANCEL_POS).wait()
            time.sleep(0.8)
            return True

        logger.info(f"HIF本領発揮保留: 已选择{sorted(selected)} → 点移动")
        context.run_task("ProduceHIF__ProduceMoveCards")
        self._move_done = True
        return True

    def _wait_for_full_power_move_enabled(self, context: Context) -> bool:
        """等待「移动」按钮连续可用，确认保留卡点选已生效。"""
        deadline = time.time() + self.FULL_POWER_MOVE_ENABLED_TIMEOUT
        stable_count = 0
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition("ProduceHIF__ProduceMoveCards", image)
            if reco and reco.hit:
                stable_count += 1
                if stable_count >= self.FULL_POWER_MOVE_ENABLED_STABLE_COUNT:
                    return True
            else:
                stable_count = 0
            time.sleep(self.FULL_POWER_MOVE_ENABLED_POLL)
        logger.warning("HIF全力保留: 点选后移动按钮未稳定可用，不执行移动")
        return False

    def _handle_full_power_hold(self, context: Context, image) -> bool:
        """全力保留页按アッチェレランド→頂点へ→羽ばたけ！→わたしは、風！选择并移动。"""
        if self._full_power_hold_source == "本領発揮+":
            self._full_power_hand_hold_pending = False
            self._full_power_hand_hold_retries = 0
            return self._handle_full_power_honryou_hold(context)
        if self._full_power_hold_source == self.FULL_POWER_DECK_HOLD_SOURCE:
            for card, template in self.FULL_POWER_HOLD_TARGETS:
                try:
                    reco = context.run_recognition(
                        "ProduceHIF__ProduceIdentityMatch",
                        image,
                        pipeline_override={
                            "ProduceHIF__ProduceIdentityMatch": {
                                "recognition": "TemplateMatch",
                                "template": template,
                                "roi": self.FULL_POWER_HOLD_ROI,
                                "threshold": self.FULL_POWER_HOLD_THRESHOLD,
                            }
                        },
                    )
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(f"HIF全力保留: 模板识别「{card}」异常 {e!r}")
                    continue
                if not (reco and reco.hit and reco.filtered_results):
                    logger.info(f"HIF全力保留: 未找到「{card}」")
                    continue
                box = reco.filtered_results[0].box
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                logger.info(f"HIF全力保留: 选中「{card}」 @ {pos}，等待移动按钮可用")
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
                if not self._wait_for_full_power_move_enabled(context):
                    return False
                logger.info(f"HIF全力保留: 已确认选中「{card}」 → 点移动")
                context.run_task("ProduceHIF__ProduceMoveCards")
                self._move_done = True
                self._full_power_hold_source = None
                self._full_power_hand_hold_retries = 0
                return True

        # 未知保留不扫描四张目标，但首次仍使用通常牌堆的第一格。只有该页连续未退出，
        # 才尝试位置不同的保留手牌坐标。
        self._full_power_hand_hold_retries += 1
        first_pos = (
            self.FULL_POWER_HAND_HOLD_FIRST_POS
            if self._full_power_hand_hold_retries >= self.FULL_POWER_HAND_HOLD_RETRY_LIMIT
            else self.FULL_POWER_HOLD_FIRST_POS
        )
        if first_pos == self.FULL_POWER_HAND_HOLD_FIRST_POS:
            logger.warning(f"HIF全力保留: 连续保留页未退出，兜底选择保留手牌 @ {first_pos}")
        else:
            logger.info(f"HIF全力保留: 未找到目标或来源未知 → 选择牌堆第一格 @ {first_pos}")
        context.tasker.controller.post_click(*first_pos).wait()
        time.sleep(0.8)
        context.run_task("ProduceHIF__ProduceMoveCards")
        self._move_done = True
        self._full_power_hold_source = None
        return True

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
        text = "".join(r.text or "" for r in sorted(reco.filtered_results, key=lambda r: r.box[0]))
        logger.info(f"HIF移动界面: 标题OCR=[{text}]")
        return text if text.strip() else None

    def _match_move_card_name(self, context: Context, image) -> Optional[str]:
        """识别移动页是否为脚光；HIF 完整匹配标题，不使用效果或相似度兜底。"""
        if self._hif_recognition:
            text = self._read_hif_move_card_title(context, image)
            return self.MOVE_TARGET_CARD if self._hif_card_name(text).replace("腳", "脚") == self.MOVE_TARGET_CARD else None
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.MOVE_NAME_ROI,
                        "expected": "|".join(self.MOVE_TARGET_CARDS) + "|" + self.MOVE_OCR_ALL,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"移动界面: 详情OCR异常 {e!r}")
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None   # 本帧未读到详情文字(过渡/OCR弱)
        text = "".join(r.text for r in reco.filtered_results)
        # 详情区可能读到卡效果；原始 OCR 文本不等同于卡名。
        logger.info(f"移动界面: 详情OCR读出=[{text}]")
        # ① 子串精确匹配(容忍脚光/脚光+/繁体等, 去掉符号再比)
        for card in self.MOVE_TARGET_CARDS:
            if card in text:
                logger.info(f"移动界面: 识别卡名「{card}」(子串命中) → 判定脚光")
                return self.MOVE_TARGET_CARD
        # ② 相似度匹配(OCR误差容忍)
        best, best_s = None, 0.0
        for card in self.MOVE_TARGET_CARDS:
            s = SequenceMatcher(None, text, card).ratio()
            if s > best_s:
                best_s, best = s, card
        if best is not None and best_s >= 0.5:
            logger.info(f"移动界面: 识别卡名「{best}」(相似度={best_s:.2f}) → 判定脚光")
            return self.MOVE_TARGET_CARD
        return None

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
        need = ProduceHIF__ProduceCardsAuto.BATTLE_END_STABLE
        if consecutive is None:
            consecutive = [0]
        if hit:
            consecutive[0] += 1
        else:
            consecutive[0] = 0
        # 只在命中(检测到次へ/结算)时打日志——战斗期间每帧 hit=False 打日志太刷屏
        if hit:
            logger.info(f"战斗结束判定(次へ模板): hit=True score={_score:.3f} 连续={consecutive[0]}/{need}")
        return consecutive[0] >= need

    def _skip_battle_end_animation(self, context: Context, image) -> None:
        """最后一回合的战斗 HUD 稳定消失后，点击可跳过的结算过场。"""
        if self._last_turn_count != 1 or self._battle_transition_taps >= 3:
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
        logger.info(f"HIF最后一回合过场: 战斗HUD已消失，点击跳过 ({self._battle_transition_taps}/3)")
        context.tasker.controller.post_click(360, 640).wait()

    @staticmethod
    def _handle_star_get(context: Context, image) -> bool:
        """处理结算后过渡页「スター性獲得」：识别标题→点击 TAP 继续。

        该页面无次へ按钮、无手牌，`_is_battle_end` 判不到结束，主循环会无限空转。
        此处识别「スター性獲得」标题，命中则点击屏幕中间触发 TAP。

        Returns:
            bool: True=已识别并触发 TAP（继续下一帧）；False=非该界面。
        """
        reco = context.run_recognition(
            "ProduceHIF__ProduceRecognitionStarGet",
            image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionStarGet": {
                    "recognition": "OCR",
                    "roi": [0, 355, 720, 68],
                    "expected": "スター性獲得",
                }
            },
        )
        if not (reco and reco.hit):
            return False
        logger.info("识别到スタ性獲得界面，点击 TAP 继续")
        context.tasker.controller.post_click(360, 640).wait()
        time.sleep(0.8)
        return True

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
        extra = self._read_extra_turn_count(context, image) if self._hif_recognition else None
        previous_extra = self._extra_rollover_reserve if self._extra_rollover_reserve is not None else self._pending_extra_turns
        self._pending_extra_turns = extra  # 储备标记独立更新，倒计时OCR失败也不能放行受限卡
        self._extra_turn_read_confirmed = extra is not None
        if self._last_turn_count == 1 and previous_extra and extra in (0, None):
            self._extra_rollover_reserve = previous_extra
            self._extra_turn_read_confirmed = False  # 待连续确认储备已转入倒计时
        elif extra is not None:
            self._extra_rollover_reserve = None
        try:
            primary = {"recognition": "OCR", "expected": self.TURN_COUNT_EXPECTED,
                       "roi": self.TURN_COUNT_ROI}
            if self._hif_recognition:
                # 固定数字区已经定位；省去文字检测，避免4被裁成窄框后识别成X。
                primary.update(expected=r"^[1-9]\d?$", only_rec=True, threshold=0.9)
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
                        "expected": r"^[1-9]\d?$", "only_rec": not self._hif_recognition,
                        "threshold": 0.3 if self._hif_recognition else 0.9,
                    }},
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF剩余回合OCR复核异常: {e!r}")
                return None
            if not (reco and reco.hit and reco.filtered_results):
                return None
            mode = "直接读取为空，文字检测补读=" if self._hif_recognition else "常规读取为空，整个数字区直接读取="
            logger.info(f"HIF剩余回合OCR复核: {mode}{[result.text for result in reco.filtered_results]}")
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

    @classmethod
    def _battle_drink_identity_from_results(cls, results, catalog_by_key: dict) -> tuple:
        """详情已由按钮确认打开；以 Pドリンク 定位标题下方的名称行。"""
        titles = [
            result for result in results
            if result.box[0] < 130
            and "Pドリンク" in _hif_drink_name_key(result.text)
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
        return catalog_by_key.get(_hif_drink_name_key(raw)), raw

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

    def _battle_drink_boxes(self, context: Context) -> list:
        try:
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
        """从左到右读名，遇到允许使用的目标就直接使用；补位后从首瓶继续。"""
        wanted = set(wanted_names) - self._drink_disabled_names
        if not wanted:
            return False
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        if not self._wait_for_drink_list_stable(context, after_use=False):
            return False
        used = False
        used_count = 0
        rescans = 0
        for _ in range(max_uses + self.DRINK_DETAIL_RESCAN_ATTEMPTS):
            if used_count >= max_uses:
                return used
            if context.tasker.stopping:
                return used
            boxes = self._battle_drink_boxes(context)
            before_count = len(boxes)
            open_failed = False
            for index, box in enumerate(boxes):
                if context.tasker.stopping:
                    return used
                pos = (box[0] + box[2] // 2, box[1] + box[3] // 2)
                if not self._open_drink_detail(context, pos, f"{label}第{index + 1}瓶"):
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
                if name not in wanted:
                    if not self._close_drink_detail(context):
                        self._abort_drink_flow(context, f"{label}: 详情未能关闭，停止当前HIF任务")
                    continue
                logger.info(f"{label}: 「{name}」符合当前使用条件，直接使用")
                if not self._use_drink(context):
                    self._abort_drink_flow(context, f"{label}: 「{name}」使用后未恢复，停止当前HIF任务")
                # 已执行使う并关闭详情；即使后续校验失败，主循环也必须重新截图。
                used = True
                if not verify_post_use:
                    logger.info(f"{label}: 已使用「{name}」")
                    return True
                if not self._wait_for_battle_drink_return(context):
                    self._abort_drink_flow(context, f"{label}: 「{name}」使用后未确认返回战斗页")
                if not self._wait_for_drink_list_stable(context, after_use=False, expected_count=before_count - 1):
                    self._abort_drink_flow(context, f"{label}: 「{name}」详情已关闭，但饮料栏未稳定")
                after_count = len(self._battle_drink_boxes(context))
                if after_count != before_count - 1:
                    self._abort_drink_flow(context, f"{label}: 「{name}」使用后饮料栏数量{before_count}→{after_count}，未确认少一瓶")
                logger.info(f"{label}: 已使用「{name}」，饮料栏{before_count}→{after_count}瓶")
                used_count += 1
                break  # 补位改变了后续位置，重新定位并从当前第一瓶检查。
            else:
                if (open_failed and rescans < self.DRINK_DETAIL_RESCAN_ATTEMPTS
                        and self._wait_for_drink_list_stable(context, after_use=False)):
                    rescans += 1
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
                or self.combo_first and self.combo_second and not self.combo_done and not self.combo_failed
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
        """国民/脚光等待时优先使用一瓶初星黒酢，身份由统一详情 OCR 确认。"""
        if self._followups is not None:
            state = self._followups
            if not state.active or not state.full_wait or state.vinegar_attempted:
                return False
            state.vinegar_attempted = True
            if self.BROWN_TARGET_NAME in self._drink_disabled_names:
                return False
            self._followup_move_targets = state.rules[state.active]['targets']
            used = self._consume_battle_drinks(context, [self.BROWN_TARGET_NAME], 'HIF组合检索', max_uses=1)
            self._followup_move_targets = None
            return used
        if self._drink_brown_tried:
            return False
        self._drink_brown_tried = True
        if self.BROWN_TARGET_NAME in self._drink_disabled_names:
            logger.info("HIF国民/脚光饮料: 初星黒酢已勾选不使用，跳过")
            return False
        return self._consume_battle_drinks(
            context, [self.BROWN_TARGET_NAME], "HIF国民/脚光饮料",
            max_uses=1, verify_post_use=False,
        )

    def _drink_brown_bottle_legacy(self, context: Context) -> bool:
        """旧初星黒酢读名路径，暂留作回归对照，当前组合技不调用。

        打断国民后依次点开饮料栏每瓶, OCR弹窗名找「初星黒酢」。

        打出国民后手牌无国民、等待脚光空过时调用。用 ProduceHIF__ProduceCheckDrinkButton 识别饮料瓶box,
        依次点开每瓶 → Pドリンク詳細弹窗OCR名字 → 是「初星黒酢」则点使う; 不是点キャンセル返回下一瓶。
        找不到目标则全部返回(不喝)。只试一次(_drink_brown_tried), 防死循环。

        Returns:
            bool: True=点开「初星黒酢」并点使う使用; False=无瓶/未找到目标/已试过/异常。
        """
        if self._drink_brown_tried:
            return False
        logger.info(
            f"喝初星黒酢: 等待 {self.DRINK_ACTION_PRE_DELAY:.1f} 秒，确保战斗动画结束"
        )
        time.sleep(self.DRINK_ACTION_PRE_DELAY)
        try:
            image = context.tasker.controller.post_screencap().wait().get()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝初星黒酢: 截图失败 {e!r}")
            return False
        self._drink_brown_tried = True
        # 识别饮料栏瓶子box(ProduceHIF__ProduceCheckDrinkButton)
        try:
            reco = context.run_recognition("ProduceHIF__ProduceCheckDrinkButton", image)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"喝初星黒酢: 识别饮料瓶异常 {e!r}")
            return False
        boxes = [r.box for r in (reco.filtered_results if reco and reco.hit else [])]
        if not boxes:
            logger.info("喝初星黒酢: 饮料栏无瓶,跳过")
            return False
        boxes = boxes[:self.BROWN_DRINK_MAX]
        logger.info(f"喝初星黒酢: 识别到{len(boxes)}瓶,依次点开找「{self.BROWN_TARGET_NAME}」")
        for i, box in enumerate(boxes):
            if context.tasker.stopping:
                return False
            cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
            logger.info(f"喝初星黒酢: 点第{i + 1}瓶 @({cx},{cy}) 看名字")
            if not self._open_drink_detail(context, (cx, cy), f"喝初星黒酢: 第{i + 1}瓶"):
                continue
            name = self._read_drink_name(context)
            if self._is_target_drink(name):
                logger.info(f"喝初星黒酢: 命中「{self.BROWN_TARGET_NAME}」→ 点使う")
                return self._use_drink(context)
            logger.info(f"喝初星黒酢: 第{i + 1}瓶名字[{name}]非目标 → 点キャンセル返回")
            if not self._close_drink_detail(context):
                return False
        logger.info(f"喝初星黒酢: 未找到「{self.BROWN_TARGET_NAME}」,全部跳过")
        return False

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
            tpl = cv2.imread(os.path.join(EXT_DIR, "resource/base/image", self.SEMBRI_BOTTLE_TPL))
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

    def _wait_for_drink_detail(self, context: Context, should_be_open: bool, timeout: float) -> bool:
        """等待详情打开或关闭连续稳定，避免在动画帧操作按钮。"""
        deadline = time.time() + timeout
        stable = 0
        while time.time() < deadline:
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
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
                if stable >= self.DRINK_DETAIL_STABLE_COUNT:
                    if should_be_open:
                        time.sleep(self.DRINK_DETAIL_SETTLE_DELAY)
                    return True
            else:
                stable = 0
            time.sleep(self.DRINK_DETAIL_POLL_INTERVAL)
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
        """关闭当前详情；首次点击未生效时只重试同一详情一次。"""
        identity = None
        for attempt in range(1, self.DRINK_DETAIL_ACTION_ATTEMPTS + 1):
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
                f"Pドリンク詳細: 第{attempt}/{self.DRINK_DETAIL_ACTION_ATTEMPTS}次"
                "点キャンセル后未确认关闭"
            )
        self._abort_drink_flow(context, "饮料详情连续两次取消后未关闭，停止当前HIF任务")

    def _use_drink(self, context: Context) -> bool:
        """使用当前详情饮料；首次点击未生效时只重试同一详情一次。"""
        identity = None
        for attempt in range(1, self.DRINK_DETAIL_ACTION_ATTEMPTS + 1):
            image = context.tasker.controller.post_screencap().wait().get()
            if "use" not in self._find_drink_detail_actions(context, image):
                return self._wait_for_drink_detail(context, should_be_open=False, timeout=self.DRINK_DETAIL_CLOSE_TIMEOUT)
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
        self._abort_drink_flow(context, "饮料连续两次点击使う后未恢复，停止当前HIF任务")

    def _abort_drink_flow(self, context: Context, message: str) -> None:
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            folder = os.path.join(BASE_DIR, "debug", "custom", "hif_drink_failures")
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
        deadline = time.monotonic() + self.TIME_OUT if self._hif_recognition else None
        while True:
            if self._hif_recognition:
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
            elif self._hif_recognition:
                count_exit = 0

            if self._hif_recognition and context.tasker.stopping:
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


@AgentServer.custom_action("ProduceHIF__ProduceChooseWorkAuto")
class ProduceHIF__ProduceChooseWorkAuto(CustomAction):
    """
    处理选择工作类型的窗口

    选择逻辑：
    - 体力 > 10 时：选择会扣除体力对应的工作（health_position 指定的位置）
      - 单个位置 [2] 或 [3]：选择对应的工作
      - 无位置或多位置 [2,3]：随机选择
    - 体力 <= 10 时：选择不扣除体力对应的工作
      - [2] → 选择第三个（位置3不扣体力）
      - [3] → 选择第二个（位置2不扣体力）
      - 其他情况 → 选择第一个
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        first_roi = [100, 760]
        second_roi = [100, 880]
        third_roi = [100, 1000]
        image = context.tasker.controller.post_screencap().wait().get()
        health_data = self._get_health(context, image)
        current = health_data["current"] if health_data else 0

        health_position = self._get_health_position(context, image)

        if current > 10:
            if health_position and len(health_position) == 1:
                box = second_roi if health_position[0] == 2 else third_roi
            else:
                box = random.choice([first_roi, second_roi, third_roi])
        else:
            if health_position == [2]:
                box = third_roi
            elif health_position == [3]:
                box = second_roi
            else:
                box = first_roi
        context.tasker.controller.post_click(box[0], box[1]).wait()
        return True

    @staticmethod
    def _get_health(context: Context, image) -> Optional[dict]:
        """获取当前体力数值"""
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionHealth", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            health_parts = reco_detail.best_result.text.split("/")
            current_health = int("".join(filter(str.isdigit, health_parts[0])))
            max_health = int("".join(filter(str.isdigit, health_parts[1])))
            ratio = current_health / max_health
            logger.info(f"体力: {current_health}/{max_health} ({ratio:.2%})")
            return {"current": current_health, "max": max_health, "ratio": ratio}
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_health_position(context: Context, image) -> Optional[list]:
        """
        获取扣除体力的图标位置

        Returns:
            None: 未检测到扣体力图标
            [2]: 第二个位置扣体力
            [3]: 第三个位置扣体力
            [2,3]: 第二和第三个位置都扣体力
        """
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceRecognitionHealthFlag",
            image,
            pipeline_override={"ProduceHIF__ProduceRecognitionHealthFlag": {"roi": [370, 830, 320, 290]}},
        )
        if reco_detail.hit:
            position = []
            for result in reco_detail.filtered_results:
                health_box_y = result.box[1]
                if health_box_y > 970:
                    position.append(3)
                else:
                    position.append(2)
            return position
        return None


@AgentServer.custom_action("ProduceHIF__ProduceChooseOptionsAuto")
class ProduceHIF__ProduceChooseOptionsAuto(CustomAction):
    """
    处理选择选项的窗口

    根据当前得分自动选择属性选项：
    1. 获取第一、第二、第三属性的当前分数
    2. THIRD_ATTR_PROBABILITY 概率选择第三属性，剩余概率选择第二属性
    3. 在可用选项中执行双击选择
    4. 若目标选项不可用，按优先级 fallback：null > 第二属性 > 第三属性 > 随机
    """

    OPTIONS_CONFIG = {
        "Vo": "hif/produce/choose_Vo.png",
        "Da": "hif/produce/choose_Da.png",
        "Vi": "hif/produce/choose_Vi.png",
        "null": "hif/produce/choose_null.png",
    }
    CLICK_DELAY = 0.5
    THIRD_ATTR_PROBABILITY = 0.6  # 选择第三属性的概率，剩余概率选择第二属性

    def __init__(self):
        super().__init__()
        self.first = "Vi"
        self.second = "Da"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        """
        执行自动选择选项

        Returns:
            bool: 选择成功返回 True，否则返回 False
        """
        preference = (
            context.get_node_data("ProduceHIF__ProduceChooseNIAEventFlag")
            .get("action", {})
            .get("param", {})
            .get("custom_action_param", {"first": "Vo", "second": "Vi"})
        )
        self.first = preference["first"]
        self.second = preference["second"]
        image = context.tasker.controller.post_screencap().wait().get()
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        options = self._get_available_options(context, image)

        # 计算选择
        first_score = score.get(self.first, 0)
        second_score = score.get(self.second, 0)
        # 计算第三属性（Vo、Da、Vi 中非 first/second 的那个）
        all_attrs = ["Vo", "Da", "Vi"]
        third = next(attr for attr in all_attrs if attr != self.first and attr != self.second)
        third_score = score.get(third, 0)
        logger.info(f"第一属性 {self.first}={first_score}, 第二属性 {self.second}={second_score}, 第三属性 {third}={third_score}")

        # 判断逻辑：根据 THIRD_ATTR_PROBABILITY 概率选择第三属性，剩余概率选择第二属性
        if random.random() < self.THIRD_ATTR_PROBABILITY:
            choice = third
            logger.debug(f"随机数 < {self.THIRD_ATTR_PROBABILITY}，选择第三属性: {choice}")
        else:
            choice = self.second
            logger.debug(f"随机数 >= {self.THIRD_ATTR_PROBABILITY}，选择第二属性: {choice}")

        # 找到目标选项
        target_box = None
        for opt in options:
            if choice in opt:
                target_box = opt[choice]
                break

        # 如果目标选项不可用，尝试 fallback（null 优先）
        if target_box is None:
            logger.debug(f"选项 {choice} 不可用，尝试 fallback")
            # 按 null > 第二属性 > 第三属性 > 随机的优先级选择
            priority = ["null", self.second, third, None]
            for p in priority:
                if p is None:
                    # 最后从所有可用选项中随机选择
                    if options:
                        random_opt = random.choice(options)
                        choice = next(iter(random_opt.keys()))
                        target_box = random_opt[choice]
                        logger.debug(f"Fallback 随机选择: {choice}")
                    break
                for opt in options:
                    if p in opt:
                        target_box = opt[p]
                        choice = p
                        logger.debug(f"Fallback 选择 {p}")
                        break
                if target_box:
                    break

        # 执行双击
        if target_box:
            # box 格式为 [x, y, w, h]，计算中心点
            center_x = target_box[0] + target_box[2] // 2
            center_y = target_box[1] + target_box[3] // 2
            self._double_click(context, center_x, center_y)
            logger.info(f"已选择选项: {choice}, 坐标: ({center_x}, {center_y})")
        else:
            logger.warning("没有可用选项，无法选择")
            return True  # 没有选项可选时默认返回True，避免卡死在这里
        return True

    @staticmethod
    def _get_current_score(context: Context, image) -> Optional[dict]:
        """获取当前得分"""
        score = {
            "Vo": 0,
            "Da": 0,
            "Vi": 0,
            "max": 0,
        }
        roi0_list = [[70 + i * 230, 430, 136, 80] for i in range(3)]
        roi1_list = [[150 + i * 150, 325, 136, 80] for i in range(3)]
        first_reco = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore",
            image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"roi": roi0_list[0]}},
        )
        use_roi_list = roi1_list if not (first_reco and first_reco.hit) else roi0_list

        for i in range(3):
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"roi": use_roi_list[i]}},
            )
            if reco_detail and reco_detail.hit and len(reco_detail.filtered_results) == 2:
                current_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[0].text)))
                max_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[1].text.replace("/", ""))))
                logger.debug(f"第{i + 1}列得分: {current_score} / {max_score}")
                score[["Vo", "Da", "Vi"][i]] = current_score
                score["max"] = max_score if score["max"] < max_score < 9999 else score["max"]
        try:
            logger.info(f"当前得分: Vo={score['Vo']}, Da={score['Da']}, Vi={score['Vi']}, Max={score['max']}")
            return score
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    def _get_available_options(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用选项列表"""
        available_options = []
        available_options_name = ""

        for option_name, option_img in self.OPTIONS_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionWorkOptions",
                image,
                pipeline_override={"ProduceHIF__ProduceRecognitionWorkOptions": {"template": option_img, "focus": None}},
            )
            if reco_detail and reco_detail.hit:
                available_options.append({option_name: reco_detail.best_result.box})
                available_options_name += f"{option_name}, "

        logger.info(f"可用选项: {available_options_name.rstrip(', ')}")
        return available_options

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


@AgentServer.custom_action("ProduceHIF__ProduceChooseMirrorAuto")
class ProduceHIF__ProduceChooseMirrorAuto(CustomAction):
    """
    自动选择试镜挑战难度

    根据当前投票数在对应试镜的阈值列表中选择满足条件的最高难度：
    - 获取当前投票数和试镜各门槛分数及坐标
    - 选取满足 vote >= threshold 的最高门槛
    - 根据降档配置先跳过指定档数，再检测锁定，锁定则继续降档
    - 降档至 threshold=0 或找到未锁定选项为止
    """

    CLICK_DELAY = 0.5

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        vote = self._get_current_vote(context, image) or 1
        mirror = self._get_current_mirror(context, image)
        lowering_difficulty = self._get_lowering_difficulty(context, argv)

        # 门槛分数降序排列
        thresholds = sorted((int(k) for k in mirror.keys()), reverse=True)

        # 找到满足当前投票的最高门槛索引
        start_idx = 0
        for i, threshold in enumerate(thresholds):
            if vote >= threshold:
                start_idx = i
                break

        # 先降指定档数，再检查锁定，锁定则继续降档直至成功或为0档
        target_threshold = 0
        x = y = 0
        start = min(start_idx + lowering_difficulty, len(thresholds) - 1)
        for i in range(start, len(thresholds)):
            target_threshold = thresholds[i]
            target_box = mirror[str(target_threshold)]
            x = target_box[0] + target_box[2] // 2
            y = target_box[1] + target_box[3] // 2

            if target_threshold == 0:
                break
            if not self._check_lock(context, image, target_box):
                break

            logger.info(f"分数 {target_threshold:,} 已锁定，继续降档")

        # 在点击前判断当前是第几镜，若启用 focus 则点击后进入手动接管
        mirror_idx = self._get_focus_mirror_index(context, image)

        logger.info(f"当前投票: {vote:,}, 目标分数: {target_threshold:,}, 点击坐标: ({x}, {y - 20})")
        self._double_click(context, x, y - 20)

        if not mirror_idx:
            return True

        logger.info(f"检测到 focus 配置，进入手动接管模式（第{mirror_idx}镜）")
        context.run_action(
            "ProduceHIF__ProduceMirrorFocus",
            pipeline_override={
                "ProduceHIF__ProduceMirrorFocus": {
                    "focus": {
                        "Node.ActionNode.Succeeded": {
                            "content": f"[color:LimeGreen]已暂停自动培育进入手动接管状态（第{mirror_idx}镜）[/color]\n"
                            '退出手动接管请点击下方"确定"按钮',
                            "display": ["log", "modal", "notification"],
                        }
                    }
                }
            },
        )

        return True

    @staticmethod
    def _get_focus_mirror_index(context: Context, image) -> int:
        """获取当前试镜对应的 focus 镜号。"""
        node_data = context.get_node_data("ProduceHIF__ProduceMirrorFlag")
        if not node_data:
            return 0
        attach = node_data.get("attach", {})
        logger.debug(f"focus 配置: {attach}")
        # 仅当有 focus_x 为 true 时才识别
        has_focus = any(attach.get(f"focus_{i}", False) for i in range(1, 4))
        if not has_focus:
            return 0

        for i in range(1, 4):
            reco_detail = context.run_recognition(
                f"ProduceHIF__ProduceMirrorFlag_{i}",
                image,
                pipeline_override={
                    f"ProduceHIF__ProduceMirrorFlag_{i}": {
                        "recognition": "TemplateMatch",
                        "roi": [12, 630, 696, 530],
                        "template": f"hif/produce/NIA/mirror_{i}.png",
                        "threshold": 0.9,
                    }
                },
            )
            if reco_detail and reco_detail.hit:
                return i if attach.get(f"focus_{i}", False) else 0
        return 0

    @staticmethod
    def _check_lock(context: Context, image, target_box) -> bool:
        """检查目标分数附近是否有锁定图标"""
        roi = [
            target_box[0] + 330,
            target_box[1] - 80,
            100,
            100,
        ]
        reco_detail = context.run_recognition(
            "ProduceHIF__ProduceRecognitionLock",
            image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionLock": {
                    "recognition": "TemplateMatch",
                    "roi": roi,
                    "template": "hif/produce/lock.png",
                    "green_mask": True,
                }
            },
        )
        return reco_detail is not None and reco_detail.hit

    @staticmethod
    def _get_lowering_difficulty(context: Context, argv) -> int:
        """检查是否启动降档"""
        params = json.loads(argv.custom_action_param)
        lowering_level = params.get("level") if isinstance(params, dict) else None
        if not lowering_level:
            return 0
        else:
            logger.info(f"检测到启动降档，降低{lowering_level}档")
            return int(lowering_level)

    @staticmethod
    def _get_current_mirror(context: Context, image):
        """获取当前试镜"""
        mirror = {"0": [360, 1050, 1, 1]}
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionMirror", image)
        if reco_detail and reco_detail.hit:
            for result in reco_detail.filtered_results:
                box = result.box
                text = "".join(filter(lambda c: c.isdigit(), result.text.replace(",", "")))
                mirror[text] = [box[0], box[1], box[2], box[3]]
        mirror = dict(sorted(mirror.items(), key=lambda x: int(x[0])))
        logger.info(f"当前试镜门槛分数: {list(mirror.keys())}")
        return mirror

    @staticmethod
    def _get_current_vote(context: Context, image) -> Optional[int]:
        """获取当前投票"""
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionVote", image)
        if reco_detail and reco_detail.hit:
            try:
                vote = int(reco_detail.best_result.text.replace(",", ""))
                return vote
            except ValueError:
                logger.warning("投票数据解析失败")
                return None

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


@AgentServer.custom_action("ProduceHIF__ProduceKeepDrinkAuto")
class ProduceHIF__ProduceKeepDrinkAuto(CustomAction):
    """
    处理保留饮料的窗口
    原本是在ProduceButton节点直接按保留按钮处理
    但是需要处理只有2瓶饮料时，一次性获得2瓶饮料时不会自动勾选3瓶的特殊情况
    所以转为独立处理
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionUncheckedMark", image)
        if reco_detail.hit:
            for result in reco_detail.filtered_results:
                box = result.box
                context.tasker.controller.post_click(box[0] + int(box[2] / 2), box[1] + int(box[3] / 2)).wait()
                time.sleep(0.1)
            context.run_task("ProduceHIF__ProduceDrinkNoButton")
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFKeepDrinkAuto")
class ProduceHIF__ProduceHIFKeepDrinkAuto(CustomAction):
    """处理所持上限页的现有补选与确认。"""

    UNCHECKED_NODE = "ProduceHIF__ProduceRecognitionUncheckedMark"
    WINDOW_TITLE_ROI = [0, 60, 720, 180]
    WINDOW_TITLE_EXPECTED = "Pドリンク所持上限"
    SELECTION_COUNT_ROI = [250, 1180, 220, 80]
    SELECTION_COUNT_EXPECTED = r"(?:あと)?[0-9]+個選択"
    KEEP_POS = (360, 1158)
    HELD_TITLE_ROI = [20, 300, 680, 750]
    HELD_TITLE_EXPECTED = r"手持ち.*ドリンク"
    CHECKBOX_X = 629
    LOG_INTERVAL = 30.0
    _log_times = {}
    TIMEOUT = 4.0
    POLL_INTERVAL = 0.2
    EXIT_STABLE_COUNT = 2

    @staticmethod
    def _screencap(context: Context):
        return context.tasker.controller.post_screencap().wait().get()

    @staticmethod
    def _results(reco):
        return reco.filtered_results if reco and reco.hit else []

    @classmethod
    def _log_throttled(cls, key: str, level: str, message: str) -> None:
        now = time.time()
        if now - cls._log_times.get(key, -cls.LOG_INTERVAL) < cls.LOG_INTERVAL:
            return
        getattr(logger, level)(message)
        cls._log_times[key] = now

    def _selection_remaining(self, context: Context, image) -> Optional[int]:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.SELECTION_COUNT_ROI,
                        "expected": self.SELECTION_COUNT_EXPECTED,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料所持上限页: 读取剩余选择数异常 {e!r}")
            return None
        text = "".join(getattr(result, "text", "") for result in self._results(reco))
        values = re.findall(r"[0-9]+", text)
        return int(values[0]) if values else None

    def _window_open(self, context: Context, image) -> bool:
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.WINDOW_TITLE_ROI,
                        "expected": self.WINDOW_TITLE_EXPECTED,
                    }
                },
            )
            return bool(reco and reco.hit)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料所持上限页: 检查窗口是否关闭异常 {e!r}")
            return True

    @staticmethod
    def _keep_button_enabled(image) -> bool:
        if image is None or not hasattr(image, "shape"):
            return False
        crop = image[1125:1185, 260:345]
        if crop.size == 0:
            return False
        orange = total = 0
        for row in crop[::2]:
            for b, g, r in row[::2]:
                total += 1
                if r > 220 and r - b > 160:
                    orange += 1
        return total > 0 and orange / total > 0.2



    def _page_headers(self, context, image):
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {'recognition': 'OCR',
                'roi': [40, 275, 620, 790], 'expected': '受け取った.*ドリンク|手持ち.*ドリンク'}})
        headers = {}
        for result in self._results(reco):
            text = _hif_drink_name_key(result.text)
            if '受け取った' in text:
                headers['received'] = result.box[1] + result.box[3]
            elif '手持ち' in text:
                headers['held'] = result.box[1] + result.box[3]
        return headers

    def _priority_enabled(self, context):
        try:
            node = context.get_node_data('ProduceHIF__ProduceHIFKeepDrinkPriority')
            return bool(node and node.get('enabled') is True)
        except (AttributeError, RuntimeError):
            return False

    def _resolve_keep_name(self, context, row):
        from extensions.hif.drink_keep import icon_name, KeepPageError
        try:
            with open(HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
                catalog = json.load(file)['drinks']
        except (OSError, ValueError, KeyError):
            return None
        name = icon_name(row['icon'], catalog, Path(EXT_DIR) / 'resource/base/image/hif/hif_drink_icons')
        if name or row.get('info') is None:
            return name
        # 只点击识别到的白色 i，不用图标边角的估算坐标，避免切换整行。
        before = self._selection_remaining(context, self._screencap(context))
        if before is None:
            raise KeepPageError('信息按钮点击前无法确认选择数')
        context.tasker.controller.post_click(*row['info']).wait()
        by_key = {_hif_drink_name_key(d['name']): d['name'] for d in catalog}
        deadline, stable, previous = time.monotonic() + 8, 0, None
        name, close = None, None
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                raise KeepPageError('任务停止，取消读取信息详情')
            image = self._screencap(context)
            results = self._keep_info_results(context, image)
            name, raw = ProduceHIF__ProduceCardsAuto._battle_drink_identity_from_results(results, by_key)
            close = next((list(r.box) for r in results if '閉じる' in r.text and r.box[1] > 1050), None)
            title = any('ドリンク詳細' in _hif_drink_name_key(r.text) for r in results)
            key = (name, raw, close)
            ready = title and close is not None
            same = previous and key[:2] == previous[:2] and close and previous[2] and all(abs(a-b) <= 6 for a,b in zip(close, previous[2]))
            stable = stable + 1 if ready and same else (1 if ready else 0)
            previous = key
            if stable >= 3:
                break
            time.sleep(.2)
        else:
            # 未打开详情且未改变选择，可以安全降级；已打开却不稳定则停止。
            if not title and self._window_open(context, image) and self._selection_remaining(context, image) == before:
                return None
            raise KeepPageError('信息详情名称区域或关闭按钮未稳定确认')
        deadline, returned, attempts, last_click = time.monotonic() + 8, 0, 0, -10
        stable, previous = 0, None
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                raise KeepPageError('任务停止，取消关闭信息详情')
            image = self._screencap(context)
            results = self._keep_info_results(context, image)
            title = any('ドリンク詳細' in _hif_drink_name_key(r.text) for r in results)
            back = not title and self._window_open(context, image)
            returned = returned + 1 if back else 0
            if returned >= 3:
                if self._selection_remaining(context, image) != before:
                    raise KeepPageError('信息按钮操作改变了选择数，停止后续调整')
                return name
            close = next((list(r.box) for r in results if '閉じる' in r.text and r.box[1] > 1050), None)
            stable = stable + 1 if close and previous and all(abs(a-b) <= 6 for a,b in zip(close, previous)) else (1 if close else 0)
            previous = close
            if stable >= 3 and attempts < 2 and time.monotonic() - last_click >= 1:
                context.tasker.controller.post_click(close[0] + close[2] // 2, close[1] + close[3] // 2).wait()
                attempts, last_click, stable = attempts + 1, time.monotonic(), 0
            time.sleep(.2)
        raise KeepPageError('信息详情未确认关闭，不检查下一瓶')

    def _keep_info_results(self, context, image):
        # 名称位于标题下方、图标右侧；包含 y≈230，不能从效果区 y=350 开始。
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {'recognition': 'OCR',
                'roi': [20, 80, 680, 1160], 'expected': r'[^\n]+'}})
        return self._results(reco)

    def _keep_after_submit_state(self, context, image):
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {'recognition': 'OCR',
                'roi': [0, 0, 720, 1180], 'expected': r'[^\n]+'}})
        results = self._results(reco)
        if any('Pドリンク所持上限' in _hif_drink_name_key(r.text) and r.box[1] < 240 for r in results):
            return 'cap', None
        # 普通领取页的瓶身也有饮料名（如初星水），须先认领取提示与底部按钮。
        # 提示已出现而按钮仍在过渡时只等待，不能继续当领取动画点击。
        receive_prompt = any(420 <= r.box[1] < 800 and '受け取るPドリンクを選' in _hif_drink_name_key(r.text)
                             for r in results)
        if receive_prompt:
            ready = any(r.box[1] >= 1000 and _hif_drink_name_key(r.text) == '受け取る' for r in results)
            return ('page' if ready else 'unknown'), None
        with open(HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
            names = {_hif_drink_name_key(d['name']): d['name'] for d in json.load(file)['drinks']}
        for result in results:
            if result.box[0] >= 170 and result.box[1] >= 740 and result.box[3] >= 20:
                text = _hif_drink_name_key(result.text)
                # 领取横幅的饮料图标可能被 OCR 合并为名称前的一两个字符。
                # 只容忍完整目录名称前的短前缀，不模糊匹配名称或效果文案。
                matches = [name for key, name in names.items()
                           if text.endswith(key) and len(text) - len(key) <= 2]
                if len(matches) == 1:
                    return 'reward', matches[0]
        # 效果文案中的「レッスン」不能证明领取动画退出，只认标题或完整操作项。
        if any(re.fullmatch(r'(?:受け取る|差し入れ|(?:SP)?授業|公開トレーニング|最終試験|おでかけ|休む|次へ)',
                           _hif_drink_name_key(r.text)) for r in results):
            return 'page', None
        if any(r.box[1] >= 740 and re.search(r'レッスン|ターン|体力|スキルカード|パラメータ|好印象|元気', r.text) for r in results):
            # 名称暂时漏识别时，不能把领取效果后面的主页标题当成可操作主页。
            return 'unknown', None
        if any(r.box[1] < 180 and 'HIF本戦' in _hif_drink_name_key(r.text).upper().replace('.', '') for r in results):
            return 'page', None
        return 'unknown', None

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        from extensions.hif.drink_keep import KeepDrinkFlow, KeepPageError
        image = self._screencap(context)
        if not self._window_open(context, image):
            logger.warning('HIF饮料所持上限页: 未识别到窗口，不执行列表操作')
            return False
        enabled = self._priority_enabled(context)
        priority = _hif_drink_priority_names(context) if enabled else None
        disabled = _hif_drink_priority_names(context, disabled_only=True) if enabled else ()
        logger.info(f'HIF饮料所持上限页: 优先级保留={enabled}，关闭时仅补缺位')
        try:
            return KeepDrinkFlow(self, context, priority, disabled).run()
        except (KeepPageError, ValueError, OSError) as error:
            logger.error(f'HIF饮料所持上限页: {error}；停止本次操作，不再补点')
            return False


class ProduceHIF__ProduceHIFHomeActionBase(ProduceHIF__ProduceChooseEventBase):
    """
    HIF剧本主界面行动基类。

    复用 ProduceHIF__ProduceChooseEventBase 的属性读取能力，
    提供“选择属性数值最低的一列”的通用逻辑。
    """

    ATTRS = ["Vo", "Da", "Vi"]
    # 是否读取属性得分选列: True=读分+失败点屏幕重试; False=关闭(不读分,默认选第一列)。保留代码,不删。
    READ_ATTR_SCORE = False

    def _lowest_attr_index(self, context: Context, image) -> int:
        """返回当前属性数值最低的列索引（0/1/2），识别失败时默认返回0。"""
        if not self.READ_ATTR_SCORE:
            # 关闭读取属性得分(不读分/不点屏幕重试), 直接默认选第一列
            logger.info("HIF行动: 已关闭读取属性得分, 默认选第一列")
            return 0
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        values = [score[attr] for attr in self.ATTRS]
        if sum(value > 0 for value in values) == 0:
            # 数值读取失败：多半是授业/SP课程的结果弹层(☆A+ 等)遮挡了统计条。
            # 先点击屏幕中间关闭弹层，再重试读取一次。
            logger.warning("属性数值读取失败，尝试点击屏幕中间关闭结果弹层后重试")
            self._click_pos(context, 360, 640, delay=0.6)
            image = self._get_screenshot(context)
            score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
            values = [score[attr] for attr in self.ATTRS]
        if sum(value > 0 for value in values) == 0:
            logger.warning("属性数值识别失败，默认选择第一列")
            return 0
        # 未识别到的列按最大值处理，避免误选
        readable = [value if value > 0 else 10**9 for value in values]
        idx = readable.index(min(readable))
        logger.info(f"当前属性: {dict(zip(self.ATTRS, values))}, 选择 {self.ATTRS[idx]}")
        return idx

    @staticmethod
    def _click_pos(context: Context, x: int, y: int, delay: float = 0.5):
        """单击指定坐标并等待。"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(delay)

    def _read_day_counter(self, context: Context, image) -> Optional[int]:
        """OCR行动屏左上「N日」计数值（距本战天数），返回 int；识别失败返回 None。

        「H.I.F本戦まで N日」是倒计时，用户天数 = 7 - 计数（第1天=6、第4天=3、第5天=2、第6天=1）。
        子类需定义 DAY_COUNTER_ROI（左上「N日」文字区）。
        """
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "expected": "\\d+日",
                        "roi": self.DAY_COUNTER_ROI,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                for r in reco.filtered_results:
                    m = re.search(r"(\d+)", r.text or "")
                    if m:
                        return int(m.group(1))
        except Exception as e:  # 识别异常不阻塞，交由调用方按各自兜底
            logger.warning(f"HIF行动: 读日期计数异常 {e}")
        return None


@AgentServer.custom_action("ProduceHIF__ProduceHIFLessonAuto")
class ProduceHIF__ProduceHIFLessonAuto(ProduceHIF__ProduceHIFHomeActionBase):
    """
    HIF授业：按左上「N日」计数固定选属性(由 HIF 路线决定)，不读属性得分。

    授业屏顶部「H.I.F本戦まで N日」是倒计时（距本战天数），用户天数=7-计数。
    计数6为第一次授业，计数3为第二次授业；属性由各次配置决定。日期或按钮未确认时不点击。
    """

    # 实测：三个授業卡片中心(由彩色属性徽章定位) Vo/Da/Vi ≈ (202,/377,/533, y~1045)
    LESSON_BUTTONS = [(202, 1045), (377, 1045), (533, 1045)]
    # 授业屏左上「N日」计数文字区(x82-141,y85-125)
    DAY_COUNTER_ROI = [72, 80, 78, 50]
    # 计数值 → 前台配置的属性选择; 未绑定天数按 IFN 路线默认选。max_hit编码: Vo=1, Da=2, Vi=3
    # 属性经专用占位节点(ProduceHIF__ProduceHIFLesson1/2AttrFlag, enabled:false 不触发)的 max_hit 传递,
    # 不能放在授业/训练 flag 上——否则 max_hit 同时被 MAA 当"最多触发几次"限制,
    # 训练flag.max_hit=1 会在第2天用掉后拦掉第5天训练(日志 max_hit reached 实证)。
    MAXHIT_NODE = {"lesson1": "ProduceHIF__ProduceHIFLesson1AttrFlag", "lesson2": "ProduceHIF__ProduceHIFLesson2AttrFlag"}
    MAXHIT_TO_ATTR = {1: "Vo", 2: "Da", 3: "Vi"}
    DEFAULT_LESSON_ATTR = "Vo"   # 未绑定天数的路线兜底属性(授业不读属性)
    # 点击授业后，旧的「授業」按钮会在转场动画里保留数秒；门控避免再次点到另一属性。
    RECOGNITION_GATE_MIN = 8.0
    RECOGNITION_GATE_ROI = [0, 850, 720, 300]
    _recognition_gate_until = 0.0
    _recognition_screen_snapshot = None

    @classmethod
    def _arm_recognition_gate(cls, image) -> None:
        cls._recognition_gate_until = time.time() + cls.RECOGNITION_GATE_MIN
        cls._recognition_screen_snapshot = None
        if cv2 is not None and image is not None:
            x, y, w, h = cls.RECOGNITION_GATE_ROI
            region = image[y:y + h, x:x + w]
            if region.size:
                cls._recognition_screen_snapshot = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).copy()

    @classmethod
    def _recognition_gate_active(cls, image) -> bool:
        if time.time() < cls._recognition_gate_until:
            return True
        snapshot = cls._recognition_screen_snapshot
        same_screen = False
        if cv2 is not None and image is not None and snapshot is not None:
            x, y, w, h = cls.RECOGNITION_GATE_ROI
            region = image[y:y + h, x:x + w]
            if region.size and region.shape[:2] == snapshot.shape[:2]:
                current = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
                score = float(cv2.matchTemplate(current, snapshot, cv2.TM_CCOEFF_NORMED)[0, 0])
                same_screen = score >= 0.82
        cls._recognition_gate_until = 0.0
        cls._recognition_screen_snapshot = None
        if same_screen:
            logger.warning("HIF授业: 点击后仍停留在授业选择，解除门控重试")
        return False

    def _read_lesson_attr(self, context, node: str) -> Optional[str]:
        """读前台「授业选属性」下拉框选中的属性(经 max_hit 编码)。读不到返回 None。"""
        try:
            nd = context.get_node_data(node)
            if nd is not None and nd.get("max_hit"):
                attr = self.MAXHIT_TO_ATTR.get(int(nd["max_hit"]))
                if attr:
                    return attr
        except Exception as e:
            logger.info(f"HIF授业: 读前台属性配置异常 {e!r}")
        return None

    @classmethod
    def _lesson_buttons_ready(cls, context: Context, image) -> bool:
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                'recognition': 'OCR', 'roi': [0, 1000, 720, 150], 'expected': '授業'}})
        columns = {min(range(3), key=lambda i: abs(r.box[0] + r.box[2] / 2 - cls.LESSON_BUTTONS[i][0]))
                   for r in (reco.filtered_results if reco and reco.hit else [])}
        return columns == {0, 1, 2}

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        if day not in (6, 3):
            logger.info(f'HIF授业: 日期未确认或非授业日(计数={day})，不点击、不推进授业次数')
            return False
        time.sleep(0.25)
        image = self._get_screenshot(context)
        if self._read_day_counter(context, image) != day:
            logger.info('HIF授业: 日期仍在转场变化，等待页面稳定')
            return False
        if not self._lesson_buttons_ready(context, image):
            logger.info('HIF授业: 日期可读但三列授業按钮未确认，不点击、不推进授业次数')
            return False
        ProduceHIF__ProduceHIFOptionAuto._lesson_index = 2 if day == 3 else 1
        # 第1次授业=计数6, 第2次授业=计数3, 分别读前台配置的属性
        if day == 6:
            # 每轮本战准备期都会从计数6重新开始；换卡 a/b 只能在本轮两次授业间共享，
            # 不能沿用上一轮已完成的计数。重复识别本页时尚未产生 a，重复清空是安全的。
            ProduceHIF__ProduceHIFCardSwapAuto.reset_delete_card_state()
            logger.info("HIF换卡记录: 新授业场次开始，已清空上一场换卡记录")
            attr = self._read_lesson_attr(context, self.MAXHIT_NODE["lesson1"]) or self.DEFAULT_LESSON_ATTR
            logger.info(f"HIF授业: 第1次授业(计数6)选{attr}")
        elif day == 3:
            attr = self._read_lesson_attr(context, self.MAXHIT_NODE["lesson2"]) or self.DEFAULT_LESSON_ATTR
            logger.info(f"HIF授业: 第2次授业(计数3)选{attr}")
        else:
            # 其它授业天未绑定, 按 HIF 路线默认选
            attr = self.DEFAULT_LESSON_ATTR
            logger.info(f"HIF授业: 计数={day}(未绑定授业日), 按路线默认选{attr}")
        idx = self.ATTRS.index(attr)
        x, y = self.LESSON_BUTTONS[idx]
        logger.info(f"HIF授业: 点击第{idx + 1}个授業({attr}) @ ({x}, {y})")
        self._arm_recognition_gate(image)
        self._click_pos(context, x, y)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFTrainAuto")
class ProduceHIF__ProduceHIFTrainAuto(ProduceHIF__ProduceHIFHomeActionBase):
    """
    HIF训练：优先点击SP，否则按前台「SP第一优先级/第二优先级」排序点公开按钮。

    底部三个公開按钮从左到右依次为 Vo/Da/Vi（对应属性显示顺序），
    按钮可点击面（「公開レッスン」标签带）位于 y≈1080-1110。
    SP 优先级由前台「SP第一优先级」「SP第二优先级」SELECT 配置(默认 Da>Vo)，
    顺序=[第一,第二,剩余属性]；SP 徽章(sp.png)位于卡片顶部偏左(实测 Da 徽章中心约(290,950))，
    依次检查各属性课的徽章区域，首个命中即点。都不含 SP 时按同一优先级选择普通课。
    """

    # 三个公开按钮中心（卡片可点击面），按实测坐标映射到点击坐标
    TRAIN_BUTTONS = {"Vo": (172, 1092), "Da": (362, 1092), "Vi": (548, 1092)}
    # SP 徽章检查区域与点击位置（映射到 Vo/Da/Vi）：徽章在卡片顶部偏左，y≈900-980
    SP_ROIS = {"Vo": [70, 900, 80, 80], "Da": [250, 900, 80, 80], "Vi": [430, 900, 80, 80]}
    SP_POSITIONS = {"Vo": (110, 945), "Da": (290, 945), "Vi": (470, 945)}
    DAY_COUNTER_ROI = [72, 80, 78, 50]
    # 点击训练后，TrainFlag 在动画和结果过渡期间不得再次触发。识别层会持续阻止同一
    # 日期计数，直到进入下一天；最短门控过滤点击后的即时残影，最长门控防中断后残留。
    RECOGNITION_GATE_MIN = 8.0
    RECOGNITION_GATE_MAX = 90.0
    RECOGNITION_GATE_ROI = [0, 850, 720, 300]
    RECOGNITION_SAME_SCREEN_THRESHOLD = 0.82
    _recognition_blocked_day = None
    _recognition_block_until = 0.0
    _recognition_block_expires = 0.0
    _recognition_screen_snapshot = None
    _recognition_blocked_task_id = None

    @classmethod
    def _arm_recognition_gate(cls, day: Optional[int], image, task_id: Optional[int]) -> None:
        now = time.time()
        cls._recognition_blocked_day = day
        cls._recognition_block_until = now + cls.RECOGNITION_GATE_MIN
        cls._recognition_block_expires = now + cls.RECOGNITION_GATE_MAX
        cls._recognition_blocked_task_id = task_id
        cls._recognition_screen_snapshot = None
        if cv2 is not None and image is not None:
            x, y, w, h = cls.RECOGNITION_GATE_ROI
            region = image[y:y + h, x:x + w]
            if region.size:
                cls._recognition_screen_snapshot = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).copy()

    @classmethod
    def _same_screen_as_before_click(cls, image) -> bool:
        """判断训练选择区是否仍与点击前一致；一致表示点击可能因卡顿未生效。"""
        snapshot = cls._recognition_screen_snapshot
        if cv2 is None or image is None or snapshot is None:
            return False
        x, y, w, h = cls.RECOGNITION_GATE_ROI
        region = image[y:y + h, x:x + w]
        if region.size == 0 or region.shape[:2] != snapshot.shape[:2]:
            return False
        current = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        score = float(cv2.matchTemplate(current, snapshot, cv2.TM_CCOEFF_NORMED)[0, 0])
        return score >= cls.RECOGNITION_SAME_SCREEN_THRESHOLD

    @classmethod
    def _clear_recognition_gate(cls) -> None:
        cls._recognition_blocked_day = None
        cls._recognition_block_until = 0.0
        cls._recognition_block_expires = 0.0
        cls._recognition_screen_snapshot = None
        cls._recognition_blocked_task_id = None

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        # SP 优先级由前台「SP第一优先级/第二优先级」配置，顺序=[第一,第二,剩余属性]
        pref = self._get_preference(context, argv)
        order = [pref["first"], pref["second"]] + [
            a for a in self.ATTRS if a != pref["first"] and a != pref["second"]
        ]
        logger.info(f"HIF训练: SP优先级顺序={order} (第一={pref['first']}, 第二={pref['second']})")
        for attr in order:
            roi = self.SP_ROIS[attr]
            if self._get_sp_course(context, image, roi):
                pos = self.SP_POSITIONS[attr]
                logger.info(f"HIF训练: 优先{attr}的SP @ {pos}")
                self._arm_recognition_gate(day, image, argv.task_detail.task_id)
                self._click_pos(context, pos[0], pos[1])
                return True
        # SP 不存在时仍沿用前台优先级，选择第一优先属性的普通课。
        attr = order[0]
        x, y = self.TRAIN_BUTTONS[attr]
        logger.info(f"HIF训练: 无SP，按属性优先级{order}点击{attr}公開レッスン @ ({x}, {y})")
        self._arm_recognition_gate(day, image, argv.task_detail.task_id)
        self._click_pos(context, x, y)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFRestAuto")
class ProduceHIF__ProduceHIFRestAuto(ProduceHIF__ProduceHIFHomeActionBase):
    """
    HIF第3天/第6天行动：按前台配置选择休息、支给或相谈。

    第3天（计数4）可选「差し入れ」或「休む」；第6天（计数1）可选「相談」或「休む」。
    本动作由 ProduceHIF__ProduceHIFRestFlag (OCR 休む) 触发，并根据左上计数限定各日的选项，
    防止第3天误点相谈或第6天误点支给。
    """

    # 各天「休む」按钮中心（按左上计数区分；实测：第3天=计数4在 (638,861)，第6天=计数1在 (643,840)）
    REST_POS = {4: (638, 861), 1: (643, 840)}
    DEFAULT_REST_POS = (643, 840)
    # 「相談」(相谈)按钮中心（第6天行动屏，实测 y900-1090 中心 ≈(360,1000); 与休む并列在下方中部）
    CONSULT_POS = (360, 1000)
    # 第3天「差し入れ」文字按钮搜索区；点击其识别框中心，与原 SupplyFlag 的识别范围一致。
    DAY3_SUPPLY_ROI = [0, 1000, 720, 150]
    DAY3_SUPPLY_EXPECT = "差し入れ"
    # 行动屏左上「N日」计数文字区（与授业屏一致）
    DAY_COUNTER_ROI = [72, 80, 78, 50]
    # 休み確認门控：点击休む后此时间戳之前才允许 RestConfirm 确认。
    # 防止通信错误弹窗等界面底部按钮误匹配 hif_rest_confirm.png(模板无状态门控会误触发)。
    REST_CONFIRM_WINDOW = 8.0          # 点击休む后 8s 内才允许确认
    _confirm_until = 0.0               # 最近一次点击休む的确认截止时间戳(类属性,供 RestConfirmFlagAuto 读取)

    @staticmethod
    def _enabled(context: Context, node_name: str) -> bool:
        """读取前台动作选项；节点缺失或读取失败时保持默认休息。"""
        try:
            return bool(context.get_node_data(node_name).get("enabled"))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF行动: 读{node_name}选项异常 {e!r}")
            return False

    def _click_day3_supply(self, context: Context, image) -> bool:
        """识别并点击第3天「差し入れ」按钮；未命中交由调用方回退休息。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "expected": self.DAY3_SUPPLY_EXPECT,
                        "roi": self.DAY3_SUPPLY_ROI,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
                logger.info(f"HIF第3天行动: 选支给(差し入れ) @ ({x}, {y})")
                self._click_pos(context, x, y)
                return True
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF第3天行动: 识别支给按钮异常 {e!r}")
        logger.warning("HIF第3天行动: 未识别支给按钮，回退休息")
        return False

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        n = (7 - day) if day else None
        # 第3天仅可支给/休息；支给按钮由 OCR 直接定位，未命中时安全回退休息。
        if day == 4 and self._enabled(context, "ProduceHIF__ProduceHIFDay3Supply"):
            if self._click_day3_supply(context, image):
                return True
        # 第6天仅可相谈/休息，默认休息。
        if day == 1 and self._enabled(context, "ProduceHIF__ProduceHIFDay6Consult"):
            # 相谈(仅第6天, 计数1)：由专用动作从行动页进入商店并在返回管线前完成购饮/终了。
            # 若只在这里点相談后返回，入口中的 ProduceHIF__ProduceHIFEndFlag 会在商店页抢先点終了。
            logger.info(f"HIF第6天行动: 计数={day}，选择相谈并进入商店处理流程")
            return ProduceHIF__ProduceHIFConsultAuto().run(context, argv)
        # 休息(默认/非第6天): 点休む
        x, y = self.REST_POS.get(day, self.DEFAULT_REST_POS)
        logger.info(f"HIF休息: 计数={day}(第{n}天), 点击休む @ ({x}, {y})")
        self._click_pos(context, x, y)
        # 打开确认窗口: 让 RestConfirmFlagAuto 仅在随后短暂窗口内允许确认休み
        ProduceHIF__ProduceHIFRestAuto._confirm_until = time.time() + ProduceHIF__ProduceHIFRestAuto.REST_CONFIRM_WINDOW
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFConsultAuto")
class ProduceHIF__ProduceHIFConsultAuto(CustomAction):
    """HIF 相谈：按当前职业饮料名单购买，随后结束相谈。"""

    CLICK_DELAY = 0.8
    # 从行动页进入商店时，必须先同时看到左上「相谈」和中央兑换说明，才说明真正
    # 进入了商店；随后饮料格仍会播放展开动画。不能从点击相谈起直接计时。
    SHOP_READY_DELAY = 10.0
    # 等待期间每秒点一次屏幕左上角，用于跳过可点击的动画/演出（原先为纯静止等待）。
    SHOP_READY_TAP_POS = (10, 20)
    SHOP_READY_TAP_INTERVAL = 1.0
    SHOP_READY_TIMEOUT = 15.0
    SHOP_READY_POLL_INTERVAL = 0.2
    SHOP_TITLE_ROI = [0, 0, 220, 170]
    SHOP_TITLE_EXPECTED = r"相[谈談]"
    SHOP_DETAIL_ROI = [40, 240, 650, 125]
    SHOP_DETAIL_EXPECTED = r"(?:Pポイント.*)?交換するものを選んでください"
    # 点击终了后，背景仍会短暂显示相谈商店。该窗口内禁止商店识别再次触发购饮。
    SHOP_FINISHED_GUARD_SECONDS = 6.0
    _shop_finished_until = 0.0
    CONSULT_ROI = [0, 1000, 720, 150]
    CONSULT_EXPECTED = r"相[谈談]"
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
    # 只框住 P 点数字；旧框会同时扫到上方的体力「33/33」，可能拼成大数。
    SHOP_POINTS_ROI = [375, 88, 80, 52]
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
        image = context.tasker.controller.post_screencap().wait().get()
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
        text = self._read_ocr(context, image, self.SHOP_POINTS_ROI, r"^\d{1,3}$")
        match = re.fullmatch(r"\s*(\d{1,3})\s*", text)
        return int(match.group(1)) if match else None

    def _shop_main_visible(self, context: Context, image) -> bool:
        return bool(
            self._read_ocr(context, image, self.SHOP_TITLE_ROI, self.CUSTOM_END_SHOP_TITLE_EXPECTED)
            and self._read_ocr(context, image, self.SHOP_DETAIL_ROI, self.CUSTOM_END_SHOP_DETAIL_EXPECTED)
        )

    def _wait_after_custom_end_exchange(self, context: Context) -> bool:
        """购买后固定等待动画，再点屏幕并等待インターバル商店恢复。"""
        time.sleep(self.CUSTOM_END_POST_EXCHANGE_DELAY)
        logger.info("HIF技能卡定制后购饮: 购买动画等待完成 → 点屏幕继续")
        self._click(context, self.CUSTOM_END_CONTINUE_POS, delay=1.0)
        deadline = time.time() + self.CUSTOM_END_POST_EXCHANGE_TIMEOUT
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            if self._shop_main_visible(context, image):
                return True
            time.sleep(self.CUSTOM_END_POST_EXCHANGE_POLL)
        logger.warning("HIF技能卡定制后购饮: 交易后未确认返回商店，停止后续购买")
        return False

    def _confirm_custom_end_exchange(self, context: Context) -> bool:
        """只在确认弹窗确实出现时点击；未出现时停止购买。"""
        deadline = time.time() + self.CUSTOM_END_CONFIRM_TIMEOUT
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
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
                logger.info(f"HIF商店饮料: 点击确认交换 @ {pos}")
                self._click(context, pos, delay=1.0)
                return True
            time.sleep(self.CUSTOM_END_POST_EXCHANGE_POLL)
        logger.info("HIF商店饮料: 交换确认弹窗未出现，停止购买")
        return False

    def buy_custom_end_drinks(self, context: Context) -> None:
        """技能卡定制后按名单比较右下两格，买不到时限次刷新。"""
        if not self._buy_custom_end_drinks_enabled(context):
            return
        priority_names = _hif_drink_priority_names(context, purchase_only=True)
        if not priority_names:
            logger.info("HIF技能卡定制后购饮: 当前职业饮料名单为空，不购买")
            return
        refresh_count = 0
        while not context.tasker.stopping:
            image = context.tasker.controller.post_screencap().wait().get()
            if ProduceHIF__ProduceHIFDrinkAuto._bar_full(image):
                logger.info("HIF技能卡定制后购饮: 饮料栏已满，停止")
                return
            points_before = self._read_shop_points(context, image)
            if points_before is None:
                logger.warning("HIF技能卡定制后购饮: 未读到 P 点，停止购买和刷新")
                return
            if points_before < self.CUSTOM_END_MIN_POINTS:
                logger.info(f"HIF技能卡定制后购饮: P 点={points_before}，不足{self.CUSTOM_END_MIN_POINTS} P，停止")
                return
            chosen = self._select_priority_shop_drink(
                context, self.CUSTOM_END_DRINK_SLOTS, priority_names
            )
            if chosen is False:
                logger.info("HIF技能卡定制后购饮: 名单饮料当前无法确认购买，停止刷新")
                return
            if chosen:
                logger.info(f"HIF技能卡定制后购饮: 选择「{chosen}」，准备交换")
                image = context.tasker.controller.post_screencap().wait().get()
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
        image = context.tasker.controller.post_screencap().wait().get()
        logger.info(f"HIF相谈删卡: 进入削除页，目标 b=[{target}]")
        self._click_ocr_or_fallback(
            context, image, self.DELETE_ENTRY_ROI, self.DELETE_ENTRY_EXPECTED,
            self.DELETE_ENTRY_FALLBACK_POS, "削除入口",
        )
        delete_action = ProduceHIF__ProduceHIFCardDeleteAuto()
        delete_action.delete_remembered_card(context, target)
        if not delete_action.wait_for_consult(context):
            logger.warning("HIF相谈删卡: 删卡流程结束后未确认返回相谈，暂停后续商店操作")
            return False
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
        """选中饮料格后只读名称；未见该格选中边框时不沿用旧详情。"""
        for attempt in range(1, self.DRINK_DETAIL_MAX_ATTEMPTS + 1):
            self._click(context, pos, delay=self.DRINK_DETAIL_WAIT)
            image = context.tasker.controller.post_screencap().wait().get()
            if not self._shop_slot_selected(image, pos):
                logger.info(f"HIF商店饮料: 第{index + 1}格 第{attempt}/{self.DRINK_DETAIL_MAX_ATTEMPTS}次 未见选中边框")
                continue
            name = self._read_ocr(context, image, self.NAME_ROI, self.OCR_ALL)
            logger.info(f"HIF商店饮料: 第{index + 1}格 第{attempt}/{self.DRINK_DETAIL_MAX_ATTEMPTS}次 名称=[{name}]")
            if name:
                return name, image
        return "", None

    def _select_priority_shop_drink(self, context: Context, slots, priority_names: list[str]):
        """读完本页饮料格并择优；None=无名单命中，False=复选未确认。"""
        priority = {_hif_drink_name_key(name): rank for rank, name in enumerate(priority_names)}
        candidates = []
        blocked = False
        for index, pos in enumerate(slots):
            name, image = self._read_shop_drink(context, pos, index)
            rank = priority.get(_hif_drink_name_key(name))
            if rank is None or image is None:
                continue
            candidates.append((rank, index, pos, name))
        for rank, index, pos, name in sorted(candidates):
            selected_name, image = self._read_shop_drink(context, pos, index)
            if image is not None and _hif_drink_name_key(selected_name) == _hif_drink_name_key(name):
                logger.info(f"HIF商店饮料: 比较后选择第{index + 1}格「{name}」(名单第{rank + 1}位，待交换确认)")
                return name
            logger.warning(f"HIF商店饮料: 第{index + 1}格复选未确认「{name}」，尝试下一个候选")
            blocked = True
        return False if blocked else None

    def _finish_consult(self, context: Context) -> None:
        # 先打开门控再点击，覆盖点击到实际退场之间仍可见商店标题的动画帧。
        self.__class__._shop_finished_until = time.time() + self.SHOP_FINISHED_GUARD_SECONDS
        image = context.tasker.controller.post_screencap().wait().get()
        self._click_ocr_or_fallback(
            context, image, self.END_ROI, self.END_EXPECTED,
            self.END_FALLBACK_POS, "终了",
        )
    def _sleep_with_taps(self, context: Context, duration: float) -> None:
        """等待 duration 秒，期间每隔 SHOP_READY_TAP_INTERVAL 秒点击一次左上角。

        用于替代纯静止等待：某些演出/过渡需要点击才能推进。等待开始时立即点一次，
        之后按间隔重复，总时长仍为 duration（允许最后一次点击略微超出）。
        """
        pos = self.SHOP_READY_TAP_POS
        interval = max(self.SHOP_READY_TAP_INTERVAL, 0.05)
        deadline = time.time() + duration
        taps = 0
        while True:
            try:
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
                taps += 1
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f"HIF相谈: 等待期间点击 {pos} 异常 {e!r}")
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        logger.info(
            f"HIF相谈: 等待 {duration:.1f} 秒结束，期间共点击 {taps} 次 @ {pos}"
        )
    def _wait_for_shop_ready(self, context: Context) -> bool:
        """确认已进入相谈商店后，再等待饮料选项展开完成。"""
        deadline = time.time() + self.SHOP_READY_TIMEOUT
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            title = self._read_ocr(
                context, image, self.SHOP_TITLE_ROI, self.SHOP_TITLE_EXPECTED
            )
            detail = self._read_ocr(
                context, image, self.SHOP_DETAIL_ROI, self.SHOP_DETAIL_EXPECTED
            )
            if title and detail:
                logger.info(
                    "HIF相谈: 已确认左上相谈与P点兑换说明，"
                    f"等待商店饮料动画结束 {self.SHOP_READY_DELAY:.1f} 秒"
                    f"(期间每 {self.SHOP_READY_TAP_INTERVAL:.1f} 秒点击一次"
                    f" {self.SHOP_READY_TAP_POS} 以跳过可点击演出)"
                )
                self._sleep_with_taps(context, self.SHOP_READY_DELAY)
                return True
            time.sleep(self.SHOP_READY_POLL_INTERVAL)
        logger.warning(
            f"HIF相谈: {self.SHOP_READY_TIMEOUT:.1f} 秒内未同时识别到"
            "相谈与P点兑换说明，不开始饮料扫描"
        )
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
        if not buy_drinks and not delete_card:
            logger.info("HIF相谈: 前台未开启购买饮料或删卡 → 直接终了")
            self._finish_consult(context)
            return True
        if wait_for_ready and not self._wait_for_shop_ready(context):
            return False
        # 删卡优先于饮料兑换，确保 200 P 点不会被先行购买消耗；失败时会取消并回到商店。
        if delete_card:
            if not self._delete_remembered_card(context):
                return False
        if not buy_drinks:
            logger.info("HIF相谈: 删卡流程结束，前台未开启购买饮料 → 终了")
            self._finish_consult(context)
            return True
        priority_names = _hif_drink_priority_names(context, purchase_only=True)
        if not priority_names:
            logger.info("HIF相谈: 当前职业饮料名单为空，不购买")
            self._finish_consult(context)
            return True
        for purchase_index in range(self.MAX_PURCHASES):
            image = context.tasker.controller.post_screencap().wait().get()
            if ProduceHIF__ProduceHIFDrinkAuto._bar_full(image):
                logger.info("HIF相谈: 左下饮料栏已满 → 终了")
                break
            logger.info(f"HIF相谈: 饮料未满，检查商店饮料（第{purchase_index + 1}次购买）")
            if not self._select_priority_shop_drink(context, self.DRINK_SLOTS, priority_names):
                logger.info("HIF相谈: 商店无可购买的名单饮料 → 终了")
                break
            image = context.tasker.controller.post_screencap().wait().get()
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
class ProduceHIF__ProduceHIFConsultShopAuto(CustomAction):
    """任务从相谈商店页恢复时，抢在通用「終了」前处理商店。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.info("HIF相谈: 已在相谈商店，进入商店处理流程")
        return ProduceHIF__ProduceHIFConsultAuto().run_shop(context, wait_for_ready=True)


@AgentServer.custom_action("ProduceHIF__ProduceHIFOptionAuto")
class ProduceHIF__ProduceHIFOptionAuto(CustomAction):
    """HIF 授业后的三项选择，按前台配置选择好调、集中或不限对应的选项。"""

    # 720x1280 基线：图片中三个选项从上到下的可点击区域中心。
    OPTION_POSITIONS = {
        "好调": (250, 715),
        "集中": (250, 810),
        "不限": (250, 900),
    }
    CATEGORY_NODES = ("ProduceHIF__ProduceHIFLesson1CategoryFlag", "ProduceHIF__ProduceHIFLesson2CategoryFlag")
    _lesson_index = 1
    MAXHIT_TO_CATEGORY = {1: "好调", 2: "集中", 3: "不限"}
    DEFAULT_CATEGORY = "好调"
    CATEGORY_LABELS = {"好调": "好调/好印象/強気", "集中": "集中/干劲/全力", "不限": "不限"}
    # 三选项先点选中、再点同一项确认；留出选中框显示时间，避免第二击落在动画前。
    OPTION_CONFIRM_DELAY = 0.4
    # 选项被点击后，原选项标题会短暂残留；与授业门控相同，超时后允许未生效的点击重试。
    RECOGNITION_GATE_MIN = 8.0
    RECOGNITION_GATE_ROI = [0, 250, 720, 800]
    _recognition_gate_until = 0.0
    _recognition_screen_snapshot = None

    @classmethod
    def _arm_recognition_gate(cls, image) -> None:
        cls._recognition_gate_until = time.time() + cls.RECOGNITION_GATE_MIN
        cls._recognition_screen_snapshot = None
        if cv2 is not None and image is not None:
            x, y, w, h = cls.RECOGNITION_GATE_ROI
            region = image[y:y + h, x:x + w]
            if region.size:
                cls._recognition_screen_snapshot = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).copy()

    @classmethod
    def _recognition_gate_active(cls, image) -> bool:
        if time.time() < cls._recognition_gate_until:
            return True
        snapshot = cls._recognition_screen_snapshot
        same_screen = False
        if cv2 is not None and image is not None and snapshot is not None:
            x, y, w, h = cls.RECOGNITION_GATE_ROI
            region = image[y:y + h, x:x + w]
            if region.size and region.shape[:2] == snapshot.shape[:2]:
                current = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
                score = float(cv2.matchTemplate(current, snapshot, cv2.TM_CCOEFF_NORMED)[0, 0])
                same_screen = score >= 0.82
        cls._recognition_gate_until = 0.0
        cls._recognition_screen_snapshot = None
        if same_screen:
            logger.warning("HIF授业选项: 点击后仍停留在选项页，解除门控重试")
        return False

    def _read_category(self, context: Context) -> str:
        """读取本次授业的卡类别；无法确认次数时使用第一次配置。"""
        try:
            node = context.get_node_data(self.CATEGORY_NODES[1 if self._lesson_index == 2 else 0])
            if node is not None and node.get("max_hit"):
                category = self.MAXHIT_TO_CATEGORY.get(int(node["max_hit"]))
                if category:
                    return category
        except Exception as e:  # pylint: disable=broad-except
            logger.info(f"HIF授业选项: 读前台类别配置异常 {e!r}")
        return self.DEFAULT_CATEGORY

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        category = self._read_category(context)
        pos = self.OPTION_POSITIONS[category]
        label = self.CATEGORY_LABELS[category]
        logger.info(f"HIF授业选项: 选中{label} @ {pos}")
        image = context.tasker.controller.post_screencap().wait().get()
        self._arm_recognition_gate(image)
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(self.OPTION_CONFIRM_DELAY)
        logger.info(f"HIF授业选项: 确认{label} @ {pos}")
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(0.5)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFEntryConfirmAuto")
class ProduceHIF__ProduceHIFEntryConfirmAuto(CustomAction):
    """
    HIF開始確認：按前台「使用道具」的勾选状态点相应道具，再点プロデュース開始。

    道具提升为「获得量上昇アイテム」的「使う」开关——其实点道具图标即可切换使用/不使用
    （点图标比点下方小√更稳，且坐标更高不易误触）。不能无条件点击：道具未勾选时，
    `ProduceHIF__ProduceUseNote` / `ProduceHIF__ProduceUsePt` 均为 disabled，必须保持游戏默认的「不使用」。
    开始确认后只执行一次，随后进入培育。
    """

    CLICK_DELAY = 0.4
    # 两个道具卡片本体中心（模板按卡片本体裁,命中中心=卡片中心,抗坐标漂移）。
    # 模板图 resource/base/image/produce/hif_item_ticket.png(票券)、hif_item_skillball.png(S球)。
    # ITEM_ICONS 为模板未命中时的回退坐标(卡片本体中心,实测720x1280基线)。
    ITEM_ICONS = [(575, 970), (660, 975)]
    ITEM_TPLS = ["hif/produce/hif_item_ticket.png", "hif/produce/hif_item_skillball.png"]
    ITEM_ROI = [510, 900, 210, 170]   # 两道具卡片搜索区(720x1280)
    ITEM_THRESHOLD = 0.8
    # プロデュース開始 橙色按钮中心（实测 x232-487, y1055-1134）
    START_POS = (360, 1095)
    # 调试: 只点道具不点开始按钮, 便于单独校准/测试道具坐标. 测试时置True, 测完改回False
    TEST_ITEMS_ONLY = False

    def _find_item_icon(self, context, tpl: str) -> Optional[tuple]:
        """模板匹配道具卡片, 返回命中框中心(点卡片本身); 匹配不到返回 None。"""
        try:
            image = context.tasker.controller.post_screencap().wait().get()
        except Exception:
            logger.error(f"HIF开始确认: 截图失败(tpl={tpl})")
            return None
        h, w = image.shape[:2]
        logger.info(f"HIF开始确认: [_find_item_icon]截图 {w}x{h} 模板={tpl} 搜索ROI={self.ITEM_ROI} 阈值={self.ITEM_THRESHOLD}")
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceIdentityMatch", image,
                pipeline_override={"ProduceHIF__ProduceIdentityMatch": {"recognition": "TemplateMatch",
                                                            "template": tpl, "roi": self.ITEM_ROI,
                                                            "threshold": self.ITEM_THRESHOLD}},
            )
        except Exception as e:
            logger.error(f"HIF开始确认: 模板匹配异常(tpl={tpl}): {e!r}")
            return None
        if reco and reco.hit and reco.filtered_results:
            b = reco.filtered_results[0].box
            cx, cy = b[0] + b[2] // 2, b[1] + b[3] // 2
            logger.info(f"HIF开始确认: 模板命中! box={list(b)} → 点击中心=({cx},{cy})")
            return (cx, cy)
        logger.warning(f"HIF开始确认: 模板未命中(tpl={tpl}), 将用回退坐标")
        return None

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        ProduceHIF__ProduceHIFCardSwapAuto.reset_delete_card_state()
        ProduceHIF__ProduceHIFOptionAuto._lesson_index = 1
        logger.info("HIF换卡记录: 开始新培养，已清空 a/b 换卡记录")
        # 与普通培育的 ProduceHIF__ProduceUseItem 分支共用前台配置节点。HIF 入口由本 action
        # 自行处理道具，若不读取 enabled 会绕过「使用道具」复选框而强制使用两个道具。
        enabled_items = []
        for node_name, item_index in (("ProduceHIF__ProduceUseNote", 0), ("ProduceHIF__ProduceUsePt", 1)):
            try:
                if context.get_node_data(node_name).get("enabled"):
                    enabled_items.append(item_index)
            except Exception as e:  # 配置读取异常时保守地不使用道具
                logger.warning(f"HIF开始确认: 读取{node_name}.enabled异常，不使用该道具: {e!r}")

        if not enabled_items:
            logger.info("HIF开始确认: 前台未勾选使用道具，跳过道具点击")

        for i in enabled_items:
            pos = self.ITEM_ICONS[i]
            # 模板匹配命中框中心(点卡片本体,抗漂移), 匹配不到回退卡片中心坐标
            hit = self._find_item_icon(context, self.ITEM_TPLS[i])
            click_pos = hit or pos
            logger.info(f"HIF开始确认: 点击道具{i + 1} @ {click_pos}"
                        + ("" if hit else " (模板未命中,用回退坐标)"))
            context.tasker.controller.post_click(click_pos[0], click_pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
        if self.TEST_ITEMS_ONLY:
            logger.info("HIF开始确认: [调试]已点道具图标, 跳过开始按钮(TEST_ITEMS_ONLY=True)")
            return True
        logger.info(f"HIF开始确认: 点击プロデュース開始 @ {self.START_POS}")
        context.tasker.controller.post_click(self.START_POS[0], self.START_POS[1]).wait()
        time.sleep(0.5)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceUseItemClickAuto")
class ProduceHIF__ProduceUseItemClickAuto(CustomAction):
    """使用道具界面(獲得量上昇アイテム)点道具本身。模板匹配命中框中心(抗漂移),未命中回退固定坐标。
    供 ProduceHIF__ProduceUseNote/ProduceHIF__ProduceUsePt 节点调用(替代 JSON 纯坐标点击,增加点击日志)。

    注意: MAA 无法把 custom_action_param 传给 action(读出来都是默认值),所以按道具拆成
    两个独立注册的子类,各自只点一个道具,避免重复点击导致"使用"被取消。
    """

    # 道具卡片本体中心(实测720x1280校准): L票券(575,970)、S球(660,975)
    # 模板按卡片本体裁,命中中心=卡片中心(resource/base/image/produce/hif_item_*.png)
    # 子类通过 _TARGET 指定只点哪个道具
    ITEM_ROI = [510, 900, 210, 170]   # 两道具卡片搜索区(720x1280)
    ITEM_THRESHOLD = 0.8
    CLICK_DELAY = 0.4
    TPL = ""            # 子类填充: 模板
    FALLBACK = (0, 0)   # 子类填充: 回退坐标
    LABEL = ""          # 子类填充: 日志名

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 由子类决定点哪个道具
        try:
            image = context.tasker.controller.post_screencap().wait().get()
        except Exception as e:
            logger.error(f"使用道具: 截图失败({self.LABEL}): {e!r}")
            image = None
        hit = None
        box_str = ""
        if image is not None:
            try:
                reco = context.run_recognition(
                    "ProduceHIF__ProduceIdentityMatch", image,
                    pipeline_override={"ProduceHIF__ProduceIdentityMatch": {"recognition": "TemplateMatch",
                                                                "template": self.TPL, "roi": self.ITEM_ROI,
                                                                "threshold": self.ITEM_THRESHOLD}},
                )
                if reco and reco.hit and reco.filtered_results:
                    b = reco.filtered_results[0].box
                    hit = (b[0] + b[2] // 2, b[1] + b[3] // 2)
                    box_str = f" (模板命中box={list(b)})"
            except Exception as e:
                logger.error(f"使用道具: 模板匹配异常({self.LABEL}): {e!r}")
        click_pos = hit or self.FALLBACK
        logger.info(f"使用道具: 点{self.LABEL} @ {click_pos}"
                    + (box_str if hit else " (模板未命中,用回退坐标)"))
        context.tasker.controller.post_click(click_pos[0], click_pos[1]).wait()
        time.sleep(self.CLICK_DELAY)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceUseNoteClickAuto")
class ProduceHIF__ProduceUseNoteClickAuto(ProduceHIF__ProduceUseItemClickAuto):
    """点 L票券道具(ProduceHIF__ProduceUseNote)。只点节这个,避免重复。"""
    TPL = "hif/produce/hif_item_ticket.png"
    FALLBACK = (575, 970)
    LABEL = "L票券"


@AgentServer.custom_action("ProduceHIF__ProduceUsePtClickAuto")
class ProduceHIF__ProduceUsePtClickAuto(ProduceHIF__ProduceUseItemClickAuto):
    """点 S球道具(ProduceHIF__ProduceUsePt)。只点这个,避免重复。"""
    TPL = "hif/produce/hif_item_skillball.png"
    FALLBACK = (660, 975)
    LABEL = "S球"


@AgentServer.custom_action("ProduceHIF__ProduceHIFClickRecoBoxAuto")
class ProduceHIF__ProduceHIFClickRecoBoxAuto(CustomAction):
    """点前序识别框中心，并打日志说明是哪个节点点击的。

    供识别返回 box 的节点使用(如最终评价TAP、回忆照片次へ)：
    读 argv.box(前序识别框) 点其中心, 日志标注 node_name + 点击坐标。
    """

    CLICK_DELAY = 0.3

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        node = getattr(argv, "node_name", None) or "?"
        box = getattr(argv, "box", None)
        if box is not None:
            try:
                cx = int(box[0]) + int(box[2]) // 2
                cy = int(box[1]) + int(box[3]) // 2
            except Exception:
                cx = int(box[0])
                cy = int(box[1])
        else:
            logger.warning(f"{node}: 无前序识别框, 无法点击")
            return True
        logger.info(f"{node}: 点击识别框中心 @ ({cx},{cy}) (box={list(box)})")
        context.tasker.controller.post_click(cx, cy).wait()
        time.sleep(self.CLICK_DELAY)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardSwapAuto")
class ProduceHIF__ProduceHIFCardSwapAuto(CustomAction):
    """
    HIF换卡：逐张点候选卡，只读详情卡名识别「需要的卡」，命中才点次へ。

    默认换卡界面为3张候选卡；开启前台「老师活动」后改用4张候选卡布局。
    点卡后顶部预览显示卡名。
    依次点击每张→等预览更新→按卡名与当前职业 preferred_acquisition_profiles 名单比较。
    命中→点次へ确认；全部候选都无→最多再抽選4次并重扫；仍无→选最后一轮第一张再点次へ。
    当前职业名单为空→直接次へ。名单条目可为字符串或带 name 的字典。
    """

    CLICK_DELAY = 0.8
    NEXT_POS = (382, 1095)                                   # 底部「次へ」确认按钮(日常实测 次入 350-413,y1078-1114)
    NEXT_ROI = [270, 1060, 200, 60]
    NEXT_EXPECTED = "次[へヘ入]"
    CARD_SLOTS = [(224, 902), (346, 902), (506, 902)]        # 默认3张候选卡中心
    TEACHER_EVENT_CARD_SLOTS = [                              # 老师活动4张候选卡中心(20260921截图)
        (150, 902), (290, 902), (430, 902), (568, 902),
    ]
    REDRAW_MAX = 4                                           # 再抽選(重抽)次数上限: 用户设定4次(重抽4次仍无→次へ兜底)
    NAME_ROI = [185, 505, 370, 50]      # 详情标题行；避开左右两侧的费用和图标
    OCR_ALL = r"[^\n]+"                 # 宽匹配读取卡名，包括名单外候选卡
    SIM_THRESH = 0.6                    # 卡名相识度(SequenceMatcher)阈值: 容忍OCR读缺字(精神統一->精神統)
    REDRAW_ROI = [535, 1070, 145, 55]   # 「再抽選」按钮 OCR 定位区(日常 再抽選 578-645,y1083-1110)
    REDRAW_EXPECTED = "再抽選"
    REDRAW_FALLBACK = (612, 1095)       # OCR 失败时的回退坐标(日常 再抽選 中心)
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
            with open(CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
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
            last_image = context.tasker.controller.post_screencap().wait().get()
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
            image = context.tasker.controller.post_screencap().wait().get()
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

    def _name_hit(self, name_text: str, nm: Optional[str]) -> bool:
        """卡名匹配：子串优先；读缺字/变体时用相识度兜底(名≥3字才走相识度防短词误判)。"""
        if not nm or not name_text:
            return False
        if nm in name_text:
            return True
        if len(nm) >= 3:
            return SequenceMatcher(None, name_text.strip(), nm).ratio() >= self.SIM_THRESH
        return False

    def _match_wanted(self, name_text: str, specs: list) -> Optional[str]:
        """只按详情卡名判别候选是否命中名单。"""
        for spec in specs:
            nm = spec.get("name")
            if self._name_hit(name_text, nm):
                logger.info(f"HIF换卡: 卡名命中「{nm}」 OCR=[{name_text}]")
                return nm
        return None

    def _click_redraw(self, context: Context) -> bool:
        """OCR 定位并点击「再抽選」(重抽一次)；失败回退 REDRAW_FALLBACK，均视为尝试过。"""
        if getattr(getattr(context, "tasker", None), "stopping", False):
            return False
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.REDRAW_ROI,
                        "expected": self.REDRAW_EXPECTED,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                box = reco.filtered_results[0].box
                x = box[0] + box[2] // 2
                y = box[1] + box[3] // 2
                logger.info(f"HIF换卡: 点再抽選 @ 识别框中心 ({x}, {y})")
                self._click(context, (x, y))
                self._wait_candidate_page_ready(context)
                return True
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF换卡: 再抽選OCR异常 {e!r}")
        logger.info(f"HIF换卡: 未识别到再抽選, 回退 @ {self.REDRAW_FALLBACK}")
        self._click(context, self.REDRAW_FALLBACK)
        self._wait_candidate_page_ready(context)
        return True

    def _click_next(self, context: Context) -> None:
        """优先 OCR 点击候选页的次へ；识别失败才使用稳定回退坐标。"""
        try:
            image = context.tasker.controller.post_screencap().wait().get()
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
        # 重抽上限 REDRAW_MAX 次：每次重抽后重扫，命中→次へ；重抽完仍无→次へ兜底
        for i in range(self.REDRAW_MAX):
            logger.info(f"HIF换卡: 第{i + 1}/{self.REDRAW_MAX}次 候选卡均非需要卡 → 点再抽選重抽再扫")
            if not self._click_redraw(context):
                return False
            result = self._scan(context, specs, remember_index)
            if result:
                return True
        first_pos = self._candidate_slots(context)[0]
        logger.info(f"HIF换卡: 重抽{self.REDRAW_MAX}次后仍无需要卡 → 选择最后一轮第一张 @ {first_pos}")
        self._click(context, first_pos)
        self._click_next(context)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFExchangeAuto")
class ProduceHIF__ProduceHIFExchangeAuto(CustomAction):
    """
    HIF交换（被换卡=替换旧卡）：逐个点网格卡→OCR详情卡名→点チェンジ(替换)。

    交换界面为 4列x3行 卡片网格(被换候选卡, 720x1280基准 列x=139/285/432/580, 行y=697/843/990),
    点中某卡后中部详情显示其卡名(观察: 卡名带 y~269-301)。换出顺序为
    第二次记录卡 a、当前职业的优先换出名单、基本卡；首次没有 a。
    记录卡 a 连续两轮未命中时回退名单和「基本」；所有目标都未命中则替换第一格。
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
            last_image = context.tasker.controller.post_screencap().wait().get()
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
            image = context.tasker.controller.post_screencap().wait().get()
            if not self._exchange_page_visible(context, image):
                closed_count += 1
                if closed_count >= self.PAGE_CLOSE_STABLE_COUNT:
                    return True
            else:
                closed_count = 0
            time.sleep(self.DETAIL_POLL_INTERVAL)
        return False

    def _replacement_targets(self, context: Context) -> list:
        """返回本次可替换卡名，顺序即优先级。"""
        swap = ProduceHIF__ProduceHIFCardSwapAuto
        targets = []
        if (
            swap._tracked_swap_enabled(context)
            and swap._delete_card_exchange_count == 1
        ):
            card_a = swap._delete_card_a
            if card_a:
                targets.append(card_a)
                logger.info(f"HIF换卡记录: 第二次授业换卡，首先查找 a=[{card_a}]")
            else:
                logger.warning("HIF换卡记录: 第二次授业换卡未记录 a，继续查找面板名单与基本卡")
        try:
            with open(CARDS_PRIORITY_CONFIG_PATH, encoding="utf-8") as f:
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
        self._click_text(
            context, image, self.CHANGE_ROI, self.CHANGE_EXPECTED,
            self.FALLBACK_CHANGE_POS,
        )
        if not self._wait_exchange_closed(context):
            logger.warning("HIF交换: 点击チェンジ后交换页未关闭，不推进换卡阶段")
            return False
        ProduceHIF__ProduceHIFCardSwapAuto.finish_delete_card_exchange(
            ProduceHIF__ProduceHIFCardSwapAuto._tracked_swap_enabled(context)
        )
        return True

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        targets = self._replacement_targets(context)
        primary_targets = targets[:-1]
        fallback_target = targets[-1]
        fallback_pos = None
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

        best_match = None  # (换出目标顺位, 卡格位置, 卡名)
        for scan_pass in range(scan_passes):
            for i, pos in enumerate(self.CARD_GRID):
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
                time.sleep(self.CLICK_DELAY)
                image, name = self._wait_stable_name(context)
                logger.info(f"HIF交换: 第{scan_pass + 1}轮第{i + 1}格 卡名OCR=[{name}]")
                normalized_name = "".join((name or "").split())
                rank = next((rank for rank, target in enumerate(primary_targets)
                             if ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, target)), None)
                if rank is not None:
                    if best_match is None or rank < best_match[0]:
                        best_match = (rank, pos, primary_targets[rank])
                        logger.info(f"HIF交换: 第{i + 1}格命中{target_label(rank)}「{primary_targets[rank]}」")
                    if rank == 0:
                        break
                    continue
                if fallback_target and fallback_target in normalized_name:
                    if not primary_targets:
                        logger.info(f"HIF交换: 第{i + 1}格命中「{fallback_target}」→ 点チェンジ替换")
                        return self._confirm_exchange(context, image)
                    if fallback_pos is None:
                        fallback_pos = pos
                        logger.info(f"HIF交换: 第{i + 1}格命中基本卡，先保留并继续扫描优先换出名单")
                    continue
                logger.info(f"HIF交换: 第{i + 1}格非目标卡")
            if best_match is not None and best_match[0] == 0:
                break
        if best_match is not None:
            rank, pos, target = best_match
            logger.info(f"HIF交换: 选择{target_label(rank)}「{target}」@ {pos}，再次点卡后チェンジ")
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, name = self._wait_stable_name(context)
            if not ProduceHIF__ProduceHIFCardSwapAuto.remembered_card_name_hit(name, target):
                logger.warning(f"HIF交换: 再次选择「{target}」后卡名不符=[{name}]，停止本次换出以免换错卡")
                return False
            return self._confirm_exchange(context, image)
        if fallback_pos is not None:
            logger.info(f"HIF交换: 未命中优先换出目标{primary_targets}，回退替换「{fallback_target}」")
            context.tasker.controller.post_click(fallback_pos[0], fallback_pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, name = self._wait_stable_name(context)
            if fallback_target not in "".join((name or "").split()):
                logger.warning(f"HIF交换: 再次选择基本卡后卡名不符=[{name}]，停止本次换出以免换错卡")
                return False
            return self._confirm_exchange(context, image)
        if recorded_a:
            first_pos = self.CARD_GRID[0]
            logger.info("HIF交换: 两轮均未识别记录卡、面板名单或基本卡，回退替换第一格")
            context.tasker.controller.post_click(first_pos[0], first_pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, _ = self._wait_stable_name(context)
            return self._confirm_exchange(context, image)
        first_pos = self.CARD_GRID[0]
        logger.info(f"HIF交换: 未找到目标卡{targets}，回退替换第一格 @ {first_pos}")
        context.tasker.controller.post_click(first_pos[0], first_pos[1]).wait()
        time.sleep(self.CLICK_DELAY)
        image, _ = self._wait_stable_name(context)
        return self._confirm_exchange(context, image)


@AgentServer.custom_action("ProduceHIF__ProduceHIFDrinkAuto")
class ProduceHIF__ProduceHIFDrinkAuto(CustomAction):
    """
    HIF「受け取るPドリンク」领取入口。

    「受け取るPドリンクを選んでください」界面：
    - 读完三个候选名称，按当前职业饮料面板顺序领取；无命中时选第一瓶。
    满4瓶且候选有面板目标时，逐瓶识别手持饮料，舍弃最低优先级的非目标饮料，
    确认4→3瓶后复选原目标再领取。没有目标或没有非目标可舍弃时拒领。
    勾选式所持上限页仍由独立动作处理。
    """

    CLICK_DELAY = 1.0
    # 左下角饮料栏检测区仅覆盖左侧4瓶（x=42..340），不含右侧底部功能按钮
    BAR_BAND = (42, 1192, 340, 1240)  # x0, y0, x1, y1
    BAR_FULL_RATIO = 0.5
    BAR_SLOT_X = [62, 146, 230, 314]  # 饮料栏4槽中心 x（y=BAR_SLOT_Y）
    BAR_SLOT_Y = 1210
    BAR_STABLE_TIMEOUT = 3.0
    BAR_STABLE_COUNT = 2
    BAR_STABLE_POLL_INTERVAL = 0.2
    RECEIVE_POS = (359, 1094)         # 底部「受け取る」按钮（实测坐标，OCR 失败时回退用）
    # 饮料领取页（3瓶）选项及选中后详情 OCR 区。
    SUPPLY_DRINK_SLOTS = [(220, 885), (360, 885), (500, 885)]
    SUPPLY_DRINK_NAME_ROI = [110, 480, 500, 70]
    RECEIVE_PROMPT_ROI = [80, 530, 560, 200]
    RECEIVE_PROMPT_EXPECTED = "受け取るPドリンク"
    OCR_ALL = r"[^\n]+"
    CARD_FIRST_POS = (221, 885)       # 无名单命中时选待领取第1瓶
    BUTTON_ROI = [200, 1030, 320, 140]  # 底部「受け取る」按钮 OCR 定位区
    BUTTON_EXPECTED = "受け取る"
    RECEIVE_BUTTON_TIMEOUT = 4.0
    RECEIVE_BUTTON_STABLE_COUNT = 2
    RECEIVE_BUTTON_POLL_INTERVAL = 0.2
    HELD_DETAIL_ROI = [20, 440, 680, 810]
    INVENTORY_ACTION_TIMEOUT = 8.0
    # 满栏无可换入目标时拒领：OCR「受け取らないで」及其下方确认按钮。
    DECLINE_ROI = [0, 560, 720, 120]
    DECLINE_EXPECTED = "受け取らない"   # 前缀宽松: OCR 常把"受け取らないで"末端"で"读丢(饮料描述文字多/长时尤甚)
    DECLINE_OFFSET_Y = 466

    @staticmethod
    def _bar_full(image) -> bool:
        """是否已满4瓶：逐个槽位看内碟是否显示彩色饮料瓶。

        此前用「非木地板色占比」判断——但饮料获取弹窗是模态遮罩，会把背后的地板
        压暗、或换成对话框背景，硬编码的地板色(BGR≈[39,72,116])随之失效，导致
        只有3瓶时也被误判成满栏，进而触发不该发生的丢弃流程。改为逐槽检测：
        空槽显示灰色杯子占位（饱和度≈0），满槽显示彩色饮料瓶（饱和度高）。
        """
        return ProduceHIF__ProduceHIFDrinkAuto._bar_filled_count(image) >= 4

    @staticmethod
    def _bar_filled_count(image) -> int:
        """返回左下饮料栏已显示的饮料槽数。"""
        filled = 0
        for sx in ProduceHIF__ProduceHIFDrinkAuto.BAR_SLOT_X:
            if ProduceHIF__ProduceHIFDrinkAuto._slot_has_drink(
                image, sx, ProduceHIF__ProduceHIFDrinkAuto.BAR_SLOT_Y
            ):
                filled += 1
        return filled

    def _wait_for_bar_stable(self, context: Context, image):
        """领取页动画会压暗/补位饮料栏；连续两帧槽数相同后才判断是否满栏。"""
        deadline = time.time() + self.BAR_STABLE_TIMEOUT
        previous = None
        stable = 0
        filled = 0
        while time.time() < deadline:
            filled = self._bar_filled_count(image)
            if filled == previous:
                stable += 1
                if stable >= self.BAR_STABLE_COUNT:
                    return image, filled
            else:
                previous = filled
                stable = 1
            time.sleep(self.BAR_STABLE_POLL_INTERVAL)
            image = context.tasker.controller.post_screencap().wait().get()
        logger.warning(f"HIF饮料领取页: 饮料栏未稳定，按最后识别的{filled}瓶处理")
        return image, filled

    @staticmethod
    def _slot_has_drink(
        image, cx: int, cy: int,
        radius: int = 20, sat_thresh: int = 80, frac_thresh: float = 0.10,
    ) -> bool:
        """槽 (cx,cy) 内碟是否显示彩色饮料瓶：饱和像素占比 > frac_thresh。

        空槽=灰色杯子占位(弱饱和)，满槽=彩色饮料罐(强饱和)。采样半径取 ring 内侧，
        避开外圈彩虹光环以免把空槽误判成满。
        """
        h, w = image.shape[:2]
        cnt = strong = 0
        for dy in range(-radius, radius + 1, 2):
            y = cy + dy
            if y < 0 or y >= h:
                continue
            for dx in range(-radius, radius + 1, 2):
                x = cx + dx
                if x < 0 or x >= w:
                    continue
                b, g, r = image[y, x]
                cnt += 1
                if (max(r, g, b) - min(r, g, b)) > sat_thresh:
                    strong += 1
        return cnt > 0 and (strong / cnt) > frac_thresh

    def _click(self, context: Context, pos, delay=None):
        """单击指定坐标并等待。"""
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else self.CLICK_DELAY)

    def _read_supply_drink_ocr(self, context: Context, image, roi: list) -> str:
        """读取待领取饮料名称行，忽略同一 ROI 内的小图标或背景字。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": self.OCR_ALL,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料领取页: OCR异常 roi={roi} {e!r}")
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        names = [
            result for result in reco.filtered_results
            if result.box[0] >= 160 and result.box[3] >= 20
        ]
        return max(names, key=lambda result: result.box[3]).text if names else ""

    def _select_priority_supply_drink(
        self, context: Context, priority_names: list[str], disabled_names=(),
    ):
        """读完三瓶后择优；禁用饮料不参与兜底。"""
        priority = {_hif_drink_name_key(name): rank for rank, name in enumerate(priority_names)}
        disabled = {_hif_drink_name_key(name) for name in disabled_names}
        self._supply_fallback_pos = None
        self._supply_target_name = None
        candidates = []
        allowed_positions = []
        for index, pos in enumerate(self.SUPPLY_DRINK_SLOTS):
            logger.info(f"HIF饮料领取页: 检查第{index + 1}瓶 @ {pos}")
            self._click(context, pos)
            image = context.tasker.controller.post_screencap().wait().get()
            name = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            logger.info(f"HIF饮料领取页: 第{index + 1}瓶 名称=[{name}]")
            key = _hif_drink_name_key(name)
            if key in disabled:
                logger.info(f"HIF饮料领取页: 第{index + 1}瓶「{name}」已勾选不使用，跳过")
                continue
            if not key and disabled:
                logger.warning(f"HIF饮料领取页: 第{index + 1}瓶名称不明，不能排除不使用，跳过")
                continue
            if self._supply_fallback_pos is None:
                self._supply_fallback_pos = pos
            allowed_positions.append(pos)
            rank = priority.get(key)
            if rank is not None:
                candidates.append((rank, index, pos, name))
        if candidates:
            rank, index, pos, name = min(candidates)
            logger.info(f"HIF饮料领取页: 三瓶比较后选择第{index + 1}瓶「{name}」(名单第{rank + 1}位)")
            self._click(context, pos)
            image = context.tasker.controller.post_screencap().wait().get()
            confirmed = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            if _hif_drink_name_key(confirmed) == _hif_drink_name_key(name):
                self._supply_target_name = name
                return pos
            self._supply_fallback_pos = next((other for other in allowed_positions if other != pos), None)
            logger.warning(f"HIF饮料领取页: 复选第{index + 1}瓶后名称=[{confirmed}]，未确认目标，改用可选兜底")
            return None
        logger.info("HIF饮料领取页: 三瓶均未命中当前职业名单，改选首个未禁用候选")
        return None

    def _is_receive_drink_screen(self, context: Context, image) -> bool:
        """确认当前为普通的「受け取るPドリンク」选择页。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.RECEIVE_PROMPT_ROI,
                        "expected": self.RECEIVE_PROMPT_EXPECTED,
                    }
                },
            )
            if reco and reco.hit:
                return True
            # 满栏领取页的红色标签；勾选式上限弹窗的标题不在此区域。
            full = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
                pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                    'recognition': 'OCR', 'roi': [200, 1090, 320, 100], 'expected': 'Pドリンク所持上限'}})
            return bool(full and full.hit)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料领取页: 领取页 OCR 异常 {e!r}")
            return False

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run_receive(context, argv)
        except HifDrinkFlowError as error:
            logger.error(f'HIF饮料领取页: {error}；返回任务失败状态，不让异常越过 Agent 回调')
            return False

    def _run_receive(self, context: Context, argv: CustomAction.RunArg) -> bool:
        first_image = context.tasker.controller.post_screencap().wait().get()
        if not self._is_receive_drink_screen(context, first_image):
            logger.warning("HIF饮料领取页: 未识别到受け取るPドリンク，不执行领取逻辑")
            return False
        first_image, filled = self._wait_for_bar_stable(context, first_image)
        bar_full = filled >= 4
        logger.info(f"HIF饮料领取页: 饮料栏稳定，已识别{filled}瓶")
        priority_names = _hif_drink_priority_names(context)
        disabled_names = _hif_drink_priority_names(context, disabled_only=True)
        target_pos = self._select_priority_supply_drink(context, priority_names, disabled_names)
        target_selected = target_pos is not None
        if not target_selected and self._supply_fallback_pos is None:
            logger.warning("HIF饮料领取页: 三瓶均不可确认可领，尝试拒领后停止")
            return self._try_decline(context)

        if bar_full:
            if not target_selected:
                logger.info('HIF满栏换饮料: 待领取栏没有已确认的面板目标，不舍弃手持饮料')
                return self._try_decline(context)
            incoming = self._supply_target_name
            if not incoming or not self._make_inventory_room(context, incoming, priority_names, disabled_names):
                logger.info('HIF满栏换饮料: 没有可舍弃的非目标饮料，保留手持并拒领')
                return self._try_decline(context)
            # 舍弃会清空待领取栏的选中态；必须复选原目标并重新核对名称。
            self._click(context, target_pos)
            image = context.tasker.controller.post_screencap().wait().get()
            confirmed = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            if _hif_drink_name_key(confirmed) != _hif_drink_name_key(incoming):
                self._stop_inventory_flow(context, 'HIF满栏换饮料: 舍弃后复选的新饮料名称未确认，停止领取')
            logger.info(f'HIF满栏换饮料: 已重新选中并确认待领取「{incoming}」')

        if not target_selected:
            self._click(context, self._supply_fallback_pos)
            image = context.tasker.controller.post_screencap().wait().get()
            fallback_name = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            disabled_keys = {_hif_drink_name_key(name) for name in disabled_names}
            fallback_key = _hif_drink_name_key(fallback_name)
            if fallback_key in disabled_keys or (not fallback_key and disabled_keys):
                logger.warning(f"HIF饮料领取页: 兜底复选名称=[{fallback_name}]不可确认未禁用，停止领取")
                return self._try_decline(context)

        # 等待领取按钮 OCR 连续稳定，避开舍去/勾选后的过渡帧。
        box = self._wait_for_receive_button(context)
        if box is not None:
            x = box[0] + box[2] // 2
            y = box[1] + box[3] // 2
            logger.info(f"HIF饮料领取页: 点击受け取る @ ({x}, {y})")
            self._click(context, (x, y))
        else:
            logger.info(f"HIF饮料领取页: 未识别到受け取る，回退 @ {self.RECEIVE_POS}")
            self._click(context, self.RECEIVE_POS)
        return True

    def _inventory_popup_results(self, context, image):
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                'recognition': 'OCR', 'roi': self.HELD_DETAIL_ROI, 'expected': self.OCR_ALL}})
        return reco.filtered_results if reco and reco.hit else []

    @staticmethod
    def _inventory_actions(results):
        actions = {}
        for r in results:
            text = _hif_drink_name_key(r.text)
            if 'キャンセル' in text or 'キヤンセル' in text:
                actions['cancel'] = list(r.box)
            elif text == '捨てる' and r.box[0] >= 480:
                actions['discard'] = list(r.box)
            elif text == '使う' and r.box[0] >= 350:
                actions['use'] = list(r.box)
        return actions

    @staticmethod
    def _inventory_boxes_stable(current, previous):
        return bool(current and previous and current.keys() == previous.keys() and all(
            abs((box[0] + box[2] / 2) - (previous[key][0] + previous[key][2] / 2)) <= 6
            and abs((box[1] + box[3] / 2) - (previous[key][1] + previous[key][3] / 2)) <= 6
            for key, box in current.items()))

    def _inventory_returned(self, context, image, results):
        # 动画中按钮暂时移出底部 ROI，不代表详情关闭。
        return (not self._inventory_actions(results)
                and not any('ドリンク詳細' in _hif_drink_name_key(r.text) for r in results)
                and not any(re.search(r'捨て.*(?:ますか|[?？])', r.text or '') for r in results)
                and self._is_receive_drink_screen(context, image))

    def _inventory_detail(self, context: Context, pos) -> Optional[dict]:
        """展开普通持有饮料，连续读到同一完整名称后才允许比较或舍弃。"""
        with open(HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
            catalog = {_hif_drink_name_key(d['name']): d['name'] for d in json.load(file)['drinks']}
        self._click(context, pos, delay=0.2)
        reader = ProduceHIF__ProduceCardsAuto()
        previous_name, previous_actions, stable = None, None, 0
        deadline = time.monotonic() + self.INVENTORY_ACTION_TIMEOUT
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return None
            image = context.tasker.controller.post_screencap().wait().get()
            results = self._inventory_popup_results(context, image)
            name, _ = reader._battle_drink_identity_from_results(results, catalog)
            actions = self._inventory_actions(results)
            ready = bool(name and 'cancel' in actions and 'discard' in actions)
            stable = stable + 1 if ready and name == previous_name and self._inventory_boxes_stable(actions, previous_actions) else (1 if ready else 0)
            previous_name, previous_actions = name, actions
            if stable >= 3:
                logger.info(f'HIF满栏换饮料: 详情名称及按钮位置已稳定「{name}」')
                return {'name': name, 'discard': actions['discard']}
            time.sleep(0.2)
        return None

    def _cancel_inventory_detail(self, context: Context) -> bool:
        deadline, stable = time.monotonic() + self.INVENTORY_ACTION_TIMEOUT, 0
        previous, button_stable, attempts, last_click = None, 0, 0, 0
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            results = self._inventory_popup_results(context, image)
            closed = self._inventory_returned(context, image, results)
            stable = stable + 1 if closed else 0
            if stable >= 3:
                logger.info('HIF满栏换饮料: 详情已关闭，领取页连续稳定，允许检查下一瓶')
                return True
            actions = self._inventory_actions(results)
            current = {'cancel': actions['cancel']} if 'cancel' in actions else {}
            button_stable = button_stable + 1 if self._inventory_boxes_stable(current, previous) else (1 if current else 0)
            previous = current
            if button_stable >= 3 and attempts < 2 and time.monotonic() - last_click >= 1:
                box = current['cancel']
                logger.info(f'HIF满栏换饮料: 点击キャンセル，尝试{attempts + 1}/2 @ {box}')
                self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
                attempts, last_click, button_stable = attempts + 1, time.monotonic(), 0
            time.sleep(0.2)
        return False

    @staticmethod
    def _replacement_slot(held: list[str], incoming: str, priority_names: list[str], disabled_names=()) -> Optional[int]:
        ranks = {_hif_drink_name_key(name): i for i, name in enumerate(priority_names)}
        disabled = {_hif_drink_name_key(name) for name in disabled_names}
        if not incoming or not held or any(not name for name in held):
            return None
        def rank(name):
            key = _hif_drink_name_key(name)
            return len(ranks) + 1 if key in disabled else ranks.get(key, len(ranks))
        # 面板目标保留；只从非目标里选择最低优先级，同级时取最右一瓶。
        candidates = [i for i, name in enumerate(held) if _hif_drink_name_key(name) not in ranks]
        if _hif_drink_name_key(incoming) not in ranks or not candidates:
            return None
        worst = max(candidates, key=lambda i: (rank(held[i]), i))
        return worst if rank(incoming) < rank(held[worst]) else None

    def _stop_inventory_flow(self, context: Context, message: str):
        logger.error(message)
        stop = getattr(context.tasker, 'post_stop', None)
        if stop:
            stop()  # 不等待当前自定义动作自身退出
        raise HifDrinkFlowError(message)

    def _discard_inventory_drink(self, context: Context, pos, expected_name: str) -> bool:
        detail = self._inventory_detail(context, pos)
        if not detail or detail['name'] != expected_name or not detail['discard']:
            self._stop_inventory_flow(context, 'HIF满栏换饮料: 无法复核待舍弃饮料及捨てる按钮，停止')
        box = detail['discard']
        logger.info(f'HIF满栏换饮料: 确认舍弃「{expected_name}」@ {pos}')
        self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
        deadline, stable = time.monotonic() + self.INVENTORY_ACTION_TIMEOUT, 0
        previous, button_stable, attempts, last_click = None, 0, 0, 0
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            results = self._inventory_popup_results(context, image)
            ready = (self._bar_filled_count(image) == 3
                     and self._inventory_returned(context, image, results))
            stable = stable + 1 if ready else 0
            if stable >= 3:
                logger.info('HIF满栏换饮料: 已确认4→3瓶，返回领取页')
                return True
            questions = [r for r in results if re.search(r'捨て.*(?:ますか|[?？])', r.text or '')
                         and _hif_drink_name_key(expected_name) in _hif_drink_name_key(r.text)]
            question_bottom = max((r.box[1] + r.box[3] for r in questions), default=1280)
            buttons = {_hif_drink_name_key(r.text): r.box for r in results
                       if _hif_drink_name_key(r.text) in ('はい', 'いいえ') and r.box[1] > question_bottom}
            current = {'question': questions[0].box, **buttons} if questions and 'はい' in buttons and 'いいえ' in buttons else {}
            button_stable = button_stable + 1 if self._inventory_boxes_stable(current, previous) else (1 if current else 0)
            previous = current
            if button_stable >= 3 and time.monotonic() - last_click >= 1:
                if attempts < 2:
                    box = buttons['はい']
                    logger.info(f'HIF满栏换饮料: 同名废弃确认及按钮位置已稳定，点击はい，尝试{attempts + 1}/2')
                    self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
                    attempts, last_click, button_stable = attempts + 1, time.monotonic(), 0
                else:
                    # 同一确认窗仍存在，明确没有执行舍弃；取消并拒领即可继续培育。
                    box = buttons['いいえ']
                    self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.5)
                    if self._cancel_inventory_detail(context):
                        image = context.tasker.controller.post_screencap().wait().get()
                        if self._bar_filled_count(image) == 4:
                            logger.warning('HIF满栏换饮料: 确认未生效，已取消且保留4瓶，本次拒领并继续任务')
                            return False
                    break
            time.sleep(0.2)
        self._stop_inventory_flow(context, 'HIF满栏换饮料: 舍弃后未确认数量减少，停止，避免重复舍弃')

    def _make_inventory_room(self, context, incoming: str, priority_names: list[str], disabled_names=()) -> bool:
        held = []
        for i, x in enumerate(self.BAR_SLOT_X):
            detail = self._inventory_detail(context, (x, self.BAR_SLOT_Y))
            if not self._cancel_inventory_detail(context):
                self._stop_inventory_flow(context, 'HIF满栏换饮料: 持有饮料详情未关闭，停止')
            if not detail:
                logger.warning(f'HIF满栏换饮料: 第{i + 1}瓶名称未确认，保留全部手持饮料')
                return False
            held.append(detail['name'])
        index = self._replacement_slot(held, incoming, priority_names, disabled_names)
        logger.info(f'HIF满栏换饮料: 持有={held}，新饮料={incoming}，待舍弃槽={index + 1 if index is not None else "无"}')
        if index is None:
            return False
        return self._discard_inventory_drink(context, (self.BAR_SLOT_X[index], self.BAR_SLOT_Y), held[index])

    def _try_decline(self, context) -> bool:
        """满栏拒领：OCR「受け取らないで」文本，点击其下方按钮。成功返回 True。"""
        image = context.tasker.controller.post_screencap().wait().get()
        try:
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.DECLINE_ROI,
                        "expected": self.DECLINE_EXPECTED,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料领取页: 受け取らないで OCR 异常 {e!r}")
            return False
        if not (reco_detail and reco_detail.hit and reco_detail.filtered_results):
            # 领取满栏页先切换「受け取らない」，随后才出现拒领确认文字。
            option = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
                pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                    'recognition': 'OCR', 'roi': [180, 960, 350, 95], 'expected': '受け取らない'}})
            if not (option and option.hit and option.filtered_results):
                return False
            box = option.filtered_results[0].box
            self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
            deadline = time.monotonic() + self.RECEIVE_BUTTON_TIMEOUT
            while time.monotonic() < deadline:
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
                    pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                        'recognition': 'OCR', 'roi': self.DECLINE_ROI, 'expected': self.DECLINE_EXPECTED}})
                if reco_detail and reco_detail.hit and reco_detail.filtered_results:
                    break
                time.sleep(0.2)
            else:
                logger.warning('HIF饮料领取页: 拒领选项点击后未确认文字变化，不点领取')
                return False
        res = sorted(reco_detail.filtered_results, key=lambda r: r.box[0])[0]
        box = res.box
        x = box[0] + box[2] // 2
        y = box[1] + box[3] // 2 + self.DECLINE_OFFSET_Y
        logger.info(f"HIF饮料领取页: 点击受け取らないで @ ({x}, {y})")
        self._click(context, (x, y))
        return True

    def _ocr_receive_button(self, context, image):
        """OCR 定位底部「受け取る」，返回 [x,y,w,h]；识别不到返回 None。"""
        try:
            reco_detail = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.BUTTON_ROI,
                        "expected": self.BUTTON_EXPECTED,
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF饮料领取页: 受け取る OCR 异常 {e!r}")
            return None
        if reco_detail and reco_detail.hit and reco_detail.filtered_results:
            res = sorted(reco_detail.filtered_results, key=lambda r: r.box[0])[0]
            return list(res.box)
        return None

    def _wait_for_receive_button(self, context):
        deadline = time.time() + self.RECEIVE_BUTTON_TIMEOUT
        stable = 0
        box = None
        while time.time() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            current = self._ocr_receive_button(context, image)
            if current == box and current is not None:
                stable += 1
                if stable >= self.RECEIVE_BUTTON_STABLE_COUNT:
                    return current
            else:
                box = current
                stable = 1 if current is not None else 0
            time.sleep(self.RECEIVE_BUTTON_POLL_INTERVAL)
        return None



@AgentServer.custom_action("ProduceHIF__ProduceHIFSupplyCardAuto")
class ProduceHIF__ProduceHIFSupplyCardAuto(CustomAction):
    """
    HIF 差し入れ过场与「受け取るスキルカード」领取入口。

    过场稳定后借用 ProduceHIF__Click_1 间隔点击两次。仅在中央提示为「受け取るスキルカード」
    时扫描候选卡：默认3张；开启
    「老师活动」后扫描4张。逐张点开后读取顶部详情卡名，按当前职业优先获取卡名单选卡，
    再点受け取る(358,1094)。OCR 读不到目标时回退第1张，避免卡在领取页。

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
    SCENE_ROI = (180, 420, 540, 1060)
    SCENE_STABLE_TIMEOUT = 20.0
    SCENE_STABLE_POLL = 0.4
    SCENE_STABLE_FRAMES = 4
    SCENE_DIFF_LIMIT = 6.0
    ADVANCE_INTERVAL = 1.0
    PROMPT_TIMEOUT = 12.0
    ADVANCE_PAIRS_MAX = 10
    CARD_RECEIVED_GUARD_SECONDS = 60.0
    _card_received_task_id = None
    _card_received_until = 0.0
    # 领饮料兜底
    OBTAIN_FALLBACK_POS = (350, 885)   # 待领取第2格(B ENERGY)

    def _click(self, context: Context, pos, delay=None):
        """单击指定坐标并等待。"""
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else self.CLICK_DELAY)

    def _wait_scene_stable(self, context: Context) -> bool:
        """观察支给演出主体，避免只因静止的页眉就提前点击。"""
        if cv2 is None:
            logger.warning("HIF支给过场: 缺少 cv2，无法确认动画结束")
            return False
        x0, y0, x1, y1 = self.SCENE_ROI
        deadline = time.monotonic() + self.SCENE_STABLE_TIMEOUT
        previous = None
        stable = 0
        while time.monotonic() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            region = image[y0:y1, x0:x1]
            if region.size:
                current = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
                if (previous is not None and current.shape == previous.shape
                        and float(cv2.absdiff(current, previous).mean()) <= self.SCENE_DIFF_LIMIT):
                    stable += 1
                    if stable >= self.SCENE_STABLE_FRAMES:
                        return True
                else:
                    stable = 0
                previous = current
            time.sleep(self.SCENE_STABLE_POLL)
        logger.warning("HIF支给过场: 画面未稳定，不点击跳过")
        return False

    def _wait_for_reward_prompt(self, context: Context) -> bool:
        deadline = time.monotonic() + self.PROMPT_TIMEOUT
        while time.monotonic() < deadline:
            image = context.tasker.controller.post_screencap().wait().get()
            if (self._is_card_screen(context, image)
                    or ProduceHIF__ProduceHIFDrinkAuto()._is_receive_drink_screen(context, image)):
                return True
            time.sleep(self.SCENE_STABLE_POLL)
        return False

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
        """依次扫描支给候选卡，按当前职业优先获取卡名单领取新卡。"""
        swap = ProduceHIF__ProduceHIFCardSwapAuto()
        specs = swap._load_swap_want(context)
        if not specs:
            logger.info("HIF技能卡领取: 当前职业优先获取卡名单为空")
            return False
        for index, pos in enumerate(self._candidate_slots(context)):
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
            image = context.tasker.controller.post_screencap().wait().get()
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
        image = context.tasker.controller.post_screencap().wait().get()
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
        for attempt in range(1, self.ADVANCE_PAIRS_MAX + 1):
            for click_index in (1, 2):
                if not self._wait_scene_stable(context):
                    return False
                image = context.tasker.controller.post_screencap().wait().get()
                if not self._is_supply_scene(context, image):
                    logger.info("HIF支给过场: 已离开差し入れ场景，停止左上角点击")
                    return True
                logger.info(f"HIF支给过场: 第{attempt}轮画面稳定，调用Click_1 {click_index}/2")
                context.run_action("ProduceHIF__Click_1")
                time.sleep(self.ADVANCE_INTERVAL)
            if self._wait_for_reward_prompt(context):
                return True
            logger.info(f"HIF支给过场: 第{attempt}轮点击后未进入领取页，重新等待画面稳定")
        logger.warning("HIF支给过场: 多轮点击后仍未进入饮料或技能卡领取页")
        return False


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardDeleteAuto")
class ProduceHIF__ProduceHIFCardDeleteAuto(CustomAction):
    """
    HIF 删卡页处理。

    开启前台「删卡」时，从相谈的「削除」入口进入后，逐张 OCR 顶部详情卡名，
    只删除第二次授业换到的 b；未找到 b 或确认弹窗未出现时取消，绝不退化为删除第一张。
    关闭该开关时保留旧行为：删除第一张卡。
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
    CONFIRM_TITLE_ROI = [0, 520, 720, 200]
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
            image = context.tasker.controller.post_screencap().wait().get()
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
        return self._wait_for_state(("consult",)) == "consult"

    def _wait_stable_card_name(self, context: Context) -> tuple:
        """等待删卡页顶部详情卡名连续稳定，避开选卡动画的旧详情。"""
        deadline = time.time() + self.DETAIL_STABLE_TIMEOUT
        stable_count = 0
        last_name = ""
        last_image = None
        while time.time() < deadline:
            last_image = context.tasker.controller.post_screencap().wait().get()
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
            image = context.tasker.controller.post_screencap().wait().get()
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
            image = context.tasker.controller.post_screencap().wait().get()
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
            state = self._wait_for_state(("confirm", "delete", "consult"))
            if state == "consult":
                return True
            if state in ("confirm", "delete"):
                image = context.tasker.controller.post_screencap().wait().get()
                label = "最终确认弹窗" if state == "confirm" else "删卡选择页"
                logger.info(f"HIF相谈删卡: 取消{label}")
                self._click_cancel(context, image)
                continue
            logger.warning(f"HIF相谈删卡: 页面状态读不到 → 补点底部「削除」 @ {self.RESIDUAL_DELETE_POS} 收尾")
            context.tasker.controller.post_click(*self.RESIDUAL_DELETE_POS).wait()
            time.sleep(self.CLICK_DELAY)
        logger.warning("HIF相谈删卡: 取消后未确认返回相谈")
        return False

    def delete_remembered_card(self, context: Context, target: str) -> bool:
        """在已打开的删卡页中定位 b，删除并处理最终确认弹窗。"""
        if not self.wait_for_delete_page(context):
            logger.warning("HIF相谈删卡: 未稳定进入删卡选择页，不开始扫描卡片")
            return False
        for index, pos in enumerate(self.CARD_GRID):
            logger.info(f"HIF相谈删卡: 检查第{index + 1}张卡 @ {pos}")
            context.tasker.controller.post_click(pos[0], pos[1]).wait()
            time.sleep(self.CLICK_DELAY)
            image, name = self._wait_stable_card_name(context)
            logger.info(f"HIF相谈删卡: 第{index + 1}张卡名 OCR=[{name}]")
            if not self._matches_target(name, target):
                continue
            logger.info(f"HIF相谈删卡: 第{index + 1}张命中 b=[{target}]，请求删除")
            if not self._click_required_ocr(
                context, self.DELETE_ROI, self.DELETE_EXPECTED, "删除按钮"
            ):
                self._cancel_to_consult(context)
                return False
            image = self._wait_for_text(
                context, self.CONFIRM_TITLE_ROI, self.CONFIRM_TITLE_EXPECTED
            )
            if image is None:
                logger.warning("HIF相谈删卡: 未确认进入最终删除弹窗，取消以避免停在删卡页")
                self._cancel_to_consult(context)
                return False
            logger.info("HIF相谈删卡: 已确认最终删除弹窗，点击确认删除")
            if not self._click_required_ocr(
                context, self.DELETE_ROI, self.DELETE_EXPECTED, "确认删除按钮"
            ):
                self._cancel_to_consult(context)
                return False
            state = self._wait_for_state(("consult", "delete"))
            if state == "consult":
                return True
            if state == "delete":
                logger.info("HIF相谈删卡: 删除完成后仍在删卡选择页，取消返回相谈")
                return self._cancel_to_consult(context)
            logger.warning("HIF相谈删卡: 确认删除后未识别到相谈或删卡页")
            return False
        logger.warning(f"HIF相谈删卡: 未找到 b=[{target}]，取消删卡")
        self._cancel_to_consult(context)
        return False

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        if ProduceHIF__ProduceHIFCardSwapAuto._delete_card_enabled(context):
            target = ProduceHIF__ProduceHIFCardSwapAuto._delete_card_b
            if target:
                if ProduceHIF__ProduceHIFCardSwapAuto._delete_card_delete_attempted:
                    logger.info("HIF删卡: 本局已尝试过删除，取消残留删卡页")
                    return self._cancel_to_consult(context)
                ProduceHIF__ProduceHIFCardSwapAuto._delete_card_delete_attempted = True
                self.delete_remembered_card(context, target)
                # 调度器只关心 action 是否回到可继续的安全页面。删除失败但已安全
                # 取消回相谈时不应重试整个删卡动作。
                return self.wait_for_consult(context)
            logger.warning("HIF删卡: 前台已开启但未记录 b，取消删卡以避免误删")
            return self._cancel_to_consult(context)
        # 1. 选中第一张卡
        logger.info(f"HIF删卡: 选中第一张卡 @ {self.CARD_FIRST_POS}")
        context.tasker.controller.post_click(self.CARD_FIRST_POS[0], self.CARD_FIRST_POS[1]).wait()
        time.sleep(self.CLICK_DELAY)
        # 2. 点击底部"削除"按钮
        image = context.tasker.controller.post_screencap().wait().get()
        box = self._template_box(context, image, self.DELETE_BUTTON_TEMPLATE)
        if not box:
            logger.warning("HIF删卡: 未识别到削除按钮")
            return False
        self._click_box_center(context, box)
        # 3. 处理确认弹窗（若出现）
        image = context.tasker.controller.post_screencap().wait().get()
        cbox = self._template_box(context, image, self.CONFIRM_TEMPLATE)
        if cbox:
            logger.info("HIF删卡: 出现确认框,点击确认删除")
            self._click_box_center(context, cbox)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFReChallengeAuto")
class ProduceHIF__ProduceHIFReChallengeAuto(CustomAction):
    """本战1(ラウンド1)顺位非第一 → 点「再挑戦」；由 MAA 前台「本战1再挑战」开关控制。

    识别顶部「H.I.F本戦 ラウンド1結果」(判定是本战1)，OCR 顺位1行名字：
      非用户(olive → 再挑战);  是用户 → 第一(不处理,交 NextFlag 点次へ)。
    config 固定偶像名(cards_customize.json 的 idol_name)用于判定顺位1是不是用户。
    """

    RE_CHALLENGE_POS = (134, 1131)   # 底部「再挑戦」按钮中心(230319实测,仅确认仍有次数时回退)
    NEXT_POS         = (360, 1095)   # 「次へ」按钮中心(不处理时交NextFlag,此仅兜底)
    RECHALLENGE_TPL  = "hif/produce/hif_rechallenge.png"   # 「再挑戦」按钮模板(抗坐标漂移)
    RECHALLENGE_ROI  = [40, 1050, 280, 130]
    # 「あとN回」徽章在再挑战按钮左上；N=0 或徽章/按钮不出现时必须推进，不能再点固定坐标。
    RECHALLENGE_COUNT_ROI = [40, 1050, 120, 55]
    RECHALLENGE_COUNT_EXPECT = r"(?:あと)?\d+回"
    # 顶部「H.I.F本戦 ラウンド1結果」判定区
    # 用「ラウンド1結果」完整匹配: 不能带「本戦」(授业/训练行动屏顶部「H.I.F本戦まで N日」含「本戦」会误判,
    # 卡死授业); 也不能只用「ラウンド1」(本战过渡/结算屏顶部也会出现孤立的「ラウンド1」,日志02:11:33实证
    # 会二次触发再点次へ)。只有真正的本战1结果屏才含「ラウンド1結果」。
    ROUND1_ROI = [0, 70, 420, 80]
    ROUND1_EXPECT = r"ラウンド1結果"
    # 顺位1行名字(截图: 第1名区域 x230-620,y255-345, 名字在左段)
    FIRST_NAME_ROI = [150, 250, 260, 80]
    # 用户分数判定区覆盖四人顺位列表的名字与分数；分数<门槛才再挑战
    SCORE_ROI = [0, 250, 720, 700]
    THRESHOLD = 700000   # 用户分数低于此分才再挑戦(70万)
    CLICK_DELAY = 0.5

    def _find_rechallenge(self, context, image) -> Optional[tuple]:
        """模板匹配「再挑戦」按钮，返回中心坐标；匹配不到返回 None。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceIdentityMatch", image,
                pipeline_override={"ProduceHIF__ProduceIdentityMatch": {"recognition": "TemplateMatch", "template": self.RECHALLENGE_TPL, "roi": self.RECHALLENGE_ROI, "threshold": 0.85}},
            )
        except Exception:
            return None
        if reco and reco.hit and reco.filtered_results:
            b = reco.filtered_results[0].box
            return (b[0] + b[2] // 2, b[1] + b[3] // 2)
        return None

    def _read_rechallenge_count(self, context, image) -> Optional[int]:
        """读取「あとN回」；返回 None 表示该按钮或其次数徽章不在当前结果页。"""
        text = self._ocr(context, image, self.RECHALLENGE_COUNT_ROI, self.RECHALLENGE_COUNT_EXPECT)
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None

    def _ocr(self, context, image, roi, expected):
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": roi, "expected": expected}},
            )
        except Exception:
            return ""
        if not (reco and reco.hit and reco.filtered_results):
            return ""
        return "".join(r.text for r in reco.filtered_results)

    def _load_idol(self) -> str:
        """从 config 读用户偶像名(固定)。"""
        try:
            if hasattr(self, "custom_cards") and self.custom_cards.get("idol_name"):
                return self.custom_cards["idol_name"]
            with open(os.path.join(EXT_DIR, "catalog", "cards_customize.json"), encoding="utf-8") as f:
                return json.load(f).get("idol_name", "")
        except Exception:
            return ""

    def _read_my_score(self, context, image, idol) -> Optional[int]:
        """OCR 顺位列表，找含用户偶像名那行，读其分数(数字)返回 int；找不到返回 None。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": self.SCORE_ROI, "expected": idol + r"|\d{1,3}(,\d{3})*"}},
            )
        except Exception:
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        results = reco.filtered_results
        idol_box = None
        for r in results:
            if idol and idol in r.text:
                idol_box = r.box
                break
        if idol_box is None:
            return None
        # 找与偶像名相邻的纯分数box: 分数可能在名字【右侧(同排)】或【正下方(同列)】。
        # 排名榜实测"名字在上、分数在下", 但分数框 x0 可比名字 x0 略左/略右(如223 vs 224, 03:17:36实证),
        # 故正下方判定用同列x容差(±60) + y在名字下方0~100px; 右侧同排判定 x>名字x + y相距<70。
        # 此前 b[0] >= 名字x 在"分数略左偏"时匹配不上 → 分数读不到。
        for r in results:
            b = r.box
            digits = re.sub(r"[^0-9]", "", r.text)
            if len(digits) < 5:
                continue
            right = b[0] > idol_box[0] and abs(b[1] - idol_box[1]) < 70          # 右侧同排
            below = abs(b[0] - idol_box[0]) < 60 and 0 <= b[1] - idol_box[1] < 100  # 正下方同列
            if right or below:
                return int(digits)
        return None

    def _read_threshold(self, context) -> int:
        """读 MAA 前台「重开分数线」输入值(存于 ReChallengeFlag.max_hit)；读不到回退 700000。"""
        try:
            nd = context.get_node_data("ProduceHIF__ProduceHIFReChallengeFlag")
            if nd is not None and nd.get("max_hit"):
                return int(nd["max_hit"])
        except Exception:
            pass
        return self.THRESHOLD

    def run(self, context, argv) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        # 前台「SKIP本战1」优先级高于「本战1再挑战」：即使该 action 因旧配置或
        # 管线覆盖顺序被调用，也直接推进结果页，绝不点「再挑戦」。
        if ProduceHIF__ProduceCardsAuto._load_hif_battle_skip(context, 1):
            logger.info(f"HIF本战1再挑战: SKIP本战1已开启，忽略再挑战 → 点次へ @ {self.NEXT_POS}")
            context.tasker.controller.post_click(self.NEXT_POS[0], self.NEXT_POS[1]).wait()
            time.sleep(self.CLICK_DELAY)
            return True
        top = self._ocr(context, image, self.ROUND1_ROI, self.ROUND1_EXPECT)
        if not top:
            logger.info("HIF本战1再挑战: 非本战1结果界面,跳过")
            return True
        idol = self._load_idol()
        my_score = self._read_my_score(context, image, idol)
        threshold = self._read_threshold(context)
        logger.info(f"HIF本战1再挑战: 顶部判定=[{top}] 用户分数={my_score} 阈值={threshold}")
        # 分数 < 前台填的分数线才再挑战；达标/读不到 → 点「次へ」推进(否则ReChallengeFlag在NextFlag之前会死循环)
        # 达标(读到且≥线)与读不到(None)分开记日志,便于排查分数识别
        if my_score is not None and my_score < threshold:
            pos = self._find_rechallenge(context, image)
            remaining = self._read_rechallenge_count(context, image)
            # 模板可能因「あとN回」数字变化而不命中；有正数次数时仍允许安全地按坐标点击。
            if remaining == 0:
                logger.info(f"HIF本战1再挑战: 分数{my_score}<{threshold}，再挑战剩余0次 → 点次へ推进 @ {self.NEXT_POS}")
                context.tasker.controller.post_click(self.NEXT_POS[0], self.NEXT_POS[1]).wait()
            elif remaining is not None and remaining > 0:
                pos = pos or self.RE_CHALLENGE_POS
                logger.info(f"HIF本战1再挑战: 分数{my_score}<{threshold}，剩余{remaining}次 → 点再挑戦 @ {pos}")
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
            elif pos is not None:
                # OCR 可能漏读徽章；若已可靠识别到可点击的再挑战按钮，仍执行重开。
                logger.info(f"HIF本战1再挑战: 分数{my_score}<{threshold}，次数OCR未读到但按钮可见 → 点再挑戦 @ {pos}")
                context.tasker.controller.post_click(pos[0], pos[1]).wait()
            else:
                logger.info(f"HIF本战1再挑战: 分数{my_score}<{threshold}，再挑战次数已用完或按钮不存在 → 点次へ推进 @ {self.NEXT_POS}")
                context.tasker.controller.post_click(self.NEXT_POS[0], self.NEXT_POS[1]).wait()
            time.sleep(self.CLICK_DELAY)
        elif my_score is not None:
            logger.info(f"HIF本战1再挑战: 分数{my_score}≥{threshold} 达标 → 点次へ推进 @ {self.NEXT_POS}")
            context.tasker.controller.post_click(self.NEXT_POS[0], self.NEXT_POS[1]).wait()
            time.sleep(self.CLICK_DELAY)
        else:
            logger.info(f"HIF本战1再挑战: 分数读不到(None) → 点次へ推进 @ {self.NEXT_POS}")
            context.tasker.controller.post_click(self.NEXT_POS[0], self.NEXT_POS[1]).wait()
            time.sleep(self.CLICK_DELAY)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFCardCustomAuto")
class ProduceHIF__ProduceHIFCardCustomAuto(CustomAction):
    """HIF 技能卡定制（スキルカードカスタマイズ）状态机。

    触发：`ProduceHIF__ProduceHIFCardCustomFlag` 识别到 P点商店界面（「スキルカードカスタマイズ」绿钮）。
    内部循环完成「主界面→选卡→卡详情→待执行→完成提示→返回→…→退出」整个定制流程，
    直到所有需定制的卡均完成（选卡界面「あとN枚」=0）。

    界面判定与决策（依 定制功能开发.md；坐标/锚点来自 094632/094642/095236/095241/095246/095304）：
      A 商店主界面(094632)   读「カスタマイズ可能枚数あとN」→ N≠0 点スキルカードカスタマイズ,N=0 点リフレッシュ
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
    HEAL_NODE = "ProduceHIF__ProduceHIFIntervalHeal"
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
    CARD_GRID = [  # 4列x多行候选格位中心(185300检测: 列x=132/286/433/580)
        (132, 624), (286, 624), (433, 624), (580, 624),
        (132, 771), (286, 771), (433, 771), (580, 771),
        (132, 880), (286, 880), (433, 880), (580, 880),
    ]
    # 2026-09-27 实机截图：可选卡中心亮像素占比 0.899～0.982，灰态卡最高 0.003。
    # 只在灰态证据明确时跳过；截图尺寸/内容异常时继续按原流程点卡确认。
    DISABLED_CARD_BRIGHT_RATIO = 0.05
    TARGET_CARD_NAME = "精神統一"   # 被点卡顶部详情卡名含它即命中
    # 选中卡详情的标题行位于 x≈194, y≈114（MuMu 720×1280）。此前 ROI
    # 从 y=140 开始，实际读到的是标题下方的效果文字，导致卡名匹配失效。
    NAME_ROI   = [185, 105, 450, 45]
    OCR_ALL    = r"[^\n]+"             # 宽匹配: 读该区全部卡名文字(含非配置卡名, 供子串判断). 勿只列配置卡名否则纯假名卡被过滤读不到(卡面兜底不触发)
    # 详情左上角卡面：共享缩略图为 96×96，720×1280 截图中的卡面约 146×146。
    DETAIL_FACE_ROI = [15, 100, 240, 230]
    FACE_THRESH = 0.90                # 旧截图模板阈值
    FACE_ICON_THRESH = 0.83           # 16 张实机截图的共享缩略图正确分数为 0.859～0.930
    FACE_MARGIN = 0.12                # 两张候选过近时不猜卡名
    FACE_SCALES = [round(s, 2) for s in np.arange(0.7, 1.31, 0.05)]
    FACE_ICON_SCALES = [round(s, 2) for s in np.arange(1.35, 1.71, 0.05)]

    # —— C/D 卡详情与待执行(185641实测) ——
    DO_CUSTOMIZE_POS = (500, 1155)                     # 「→ カスタマイズ」(目测,TODO)
    MENU_ITEMS = [(140, 929), (360, 929), (580, 929)]  # 720×1280 画面的三列按钮中心；两项菜单使用前两列
    MENU_LEFT_ROIS = [[115, 850, 140, 30], [290, 850, 140, 30], [490, 850, 140, 30]]  # 菜单项顶部「あとN回」徽章(192151)
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
        self._card_selected = False  # 选卡界面是否已选中某卡(等OCR确认)
        self._try_idx = 0            # 逐卡点取当前尝试的格位索引
        self._cur_card = None        # 当前要定制的卡名(OCR卡名匹配配置)
        self._menu_attempt = 0       # 待执行时在勾选项间轮换的索引(读合計自动点满)
        self._total_none = 0         # 合計连续读不到的计数(防到上限合計标签消失不停止)
        self._exec_stall = 0         # 待执行时「合計」连续不减少的计数(点実行する不生效=P点不足/上限)
        self._last_total = None      # 上一次读到的「合計」值(比较是否减少)
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
        legacy_cards, legacy_names = {}, {}
        try:
            with open(os.path.join(EXT_DIR, "catalog", "cards_customize.json"), encoding="utf-8") as f:
                legacy_cards = json.load(f)
            with open(os.path.join(EXT_DIR, "catalog", "custom_card_images.json"), encoding="utf-8") as f:
                legacy_names = {item["gk_img_id"]: item["card_name"] for item in json.load(f)}
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"HIF技能卡定制: 旧卡识别模板不可用，继续使用卡名 OCR {e!r}")
        try:
            with open(os.path.join(EXT_DIR, "catalog", "hif_customization_catalog.json"), encoding="utf-8") as f:
                catalog = json.load(f)["cards"]
            selection_path = os.path.join(BASE_DIR, "config", "hif", "hif_custom_card_selection.json")
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
                selected_ids = set(selected.get(str(card_id), []))
                options = card["options"]
                if not selected_ids or len(options) not in (2, 3):
                    continue
                menu = [
                    {"id": option["id"], "name": option["name"], "pos": self.MENU_ITEMS[index]}
                    for index, option in enumerate(options) if option["id"] in selected_ids
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
            logger.warning(f"HIF技能卡定制: 读取面板选择或卡牌目录失败 {e!r}")
        # 所有导入卡共用缩略图；已验证的旧模板继续作为额外回退。
        self.FACE_TEMPLATES = {
            name: [f"hif/hif_card_icons/{cfg['card_id']}.webp"]
                  + ([cfg["template"]] if cfg["template"] else [])
            for name, cfg in self.custom_cards.items()
        }

    @staticmethod
    def _click(context: Context, pos, delay=None):
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(delay if delay is not None else ProduceHIF__ProduceHIFCardCustomAuto.CLICK_DELAY)

    @staticmethod
    def _heal_dialog_visible(context: Context, image) -> bool:
        dialog = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore", image,
            pipeline_override={"ProduceHIF__ProduceRecognitionScore": {
                "recognition": "OCR", "roi": [20, 600, 390, 75],
                "expected": "体力回復確認",
            }},
        )
        return bool(dialog and dialog.hit)

    @staticmethod
    def _wait_heal_shop(context: Context) -> bool:
        stable = 0
        for _ in range(30):
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            stable = stable + 1 if shop and shop.hit else 0
            # 商店按钮会先于回体退出动画出现；连续四帧（至少1.5秒）后再点击。
            if stable >= 4:
                return True
            time.sleep(0.5)
        return False

    def _heal_before_customization(self, context: Context) -> bool:
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
                image = context.tasker.controller.post_screencap().wait().get()
                if self._heal_dialog_visible(context, image):
                    time.sleep(0.5)  # 弹窗标题可能先于按钮动画出现
                    break
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

        for _ in range(10):
            if context.tasker.stopping:
                return False
            self._click(context, self.HEAL_PLUS_POS, delay=0.3)
        logger.info("HIF中场回血: 已点＋10次，确认回復")
        self._click(context, self.HEAL_CONFIRM_POS, delay=1.0)
        if self._wait_heal_shop(context):
            logger.info("HIF中场回血: 商店页已恢复，继续技能卡定制或购饮")
            return True
        if context.tasker.stopping:
            return False
        image = context.tasker.controller.post_screencap().wait().get()
        if self._heal_dialog_visible(context, image):
            logger.warning("HIF中场回血: 回復未确认，取消弹窗并继续商店流程")
            self._click(context, self.HEAL_CANCEL_POS, delay=0.5)
            return self._wait_heal_shop(context)
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

    def _handle_main(self, context: Context, image) -> str:
        """商店主界面(A)：返回 'enter'(进选卡) / 'refresh'(点刷新) / 'exit'(全部完成点终了) / 'unknown'。"""
        if self._done_all:
            ProduceHIF__ProduceHIFConsultAuto().buy_custom_end_drinks(context)
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
            image = context.tasker.controller.post_screencap().wait().get()
            shop = context.run_recognition("ProduceHIF__ProduceHIFCardCustomFlag", image)
            if shop and shop.hit:
                logger.info("HIF技能卡定制: 点击后仍在商店页，等待并重试自定义入口")
                return "unknown"
            if self._read_left(context, image, self.SELECT_LEFT_ROI) is not None:
                return "enter"
            logger.info("HIF技能卡定制: 尚未确认选卡页，等待页面稳定")
            return "unknown"
        logger.info(f"HIF技能卡定制: 主界面 可选0张 → 点リフレッシュ @ {self.REFRESH_POS}")
        self._click(context, self.REFRESH_POS, delay=1.0)
        time.sleep(2.0)   # 等刷新动画完成、列表重新稳定后，再回主界面读枚数
        return "refresh"

    def _handle_select(self, context: Context, image) -> str:
        """选卡界面(B)：'pick'(选卡) / 'detail'(确认目标卡进待执行) / 'back'(枚数=0返回) / 'unknown'。"""
        # 已选中某卡 → OCR 顶部卡名：当前列表中先处理任意尚未完成的目标卡。
        if self._card_selected:
            self._last_card_name_ocr = ""
            card = self._match_card_name(context, image)
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
                logger.info(f"HIF技能卡定制: 找到当前目标卡「{card}」(队列{self._queue_idx}) → 点→カスタマイズ(第{self._card_custom_click}/{self.MAX_CUSTOM_CLICK}次) @ {self.DO_CUSTOMIZE_POS}")
                self._click(context, self.DO_CUSTOMIZE_POS)
                return "detail"
            if card is not None:
                # 识别出卡名,但不是勾选的目标 → 非目标卡
                self._ocr_none = 0
                reason = f"识别到非目标卡「{card}」"
            else:
                # 保留读出的非目标卡名；详情动画或弱 OCR 仍重试三帧。
                raw_name = self._last_card_name_ocr.strip()
                if re.search(r"[一-龯ぁ-ゟ゠-ヿ]", raw_name):
                    self._unmatched_card_name = raw_name
                self._ocr_none += 1
                time.sleep(0.6)
                if self._ocr_none < 3:
                    return "pick"   # 保持选中态,下帧再读(等详情弹出)
                self._ocr_none = 0
                reason = (f"识别到非目标卡「{self._unmatched_card_name}」"
                          if self._unmatched_card_name else "该格卡名未能识别")
            self._unmatched_card_name = ""
            self._try_idx = self._next_selectable_card(image, self._try_idx + 1)
            if self._try_idx >= len(self.CARD_GRID):
                return self._finish_select_scan(context)
            time.sleep(0.4)
            self._click(context, self.CARD_GRID[self._try_idx])
            logger.info(f"HIF技能卡定制: {reason}, 点下一候选 @ {self.CARD_GRID[self._try_idx]}")
            return "pick"
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
        logger.info(f"HIF技能卡定制: 点候选卡{self._try_idx} @ {self.CARD_GRID[self._try_idx]}")
        return "pick"

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
                    logger.info(f"HIF技能卡定制: 跳过灰态候选格 {skipped}")
                return index
            bright_ratio = float((region.max(axis=2) > 180).mean())
            if bright_ratio >= self.DISABLED_CARD_BRIGHT_RATIO or float(region.mean()) >= 155:
                if skipped:
                    logger.info(f"HIF技能卡定制: 跳过灰态候选格 {skipped}")
                return index
            skipped.append(index)
        if skipped:
            logger.info(f"HIF技能卡定制: 跳过灰态候选格 {skipped}，后续无可选卡")
        return len(self.CARD_GRID)

    def _finish_select_scan(self, context: Context) -> str:
        """本批次已无可处理候选，返回商店主界面。"""
        self._card_selected = False
        self._try_idx = 0
        missing = self._queue[self._queue_idx:]
        self.skipped_cards.update({name: "当前列表未找到可定制目标" for name in missing})
        self._queue_idx = len(self._queue)
        self._cur_card = None
        self._done_all = True
        logger.info(f"HIF技能卡定制: 当前列表没有可定制的目标卡 {missing}，点<<もどる结束")
        self._click(context, self.BACK_POS)
        return "back"

    def _match_card_name(self, context: Context, image) -> Optional[str]:
        """读取顶部卡名区域，返回匹配到配置里的卡名；读不到/不匹配返回 None。

        用配置的卡名做预期(OR 匹配)，读到的卡名含某配置卡名即命中(前缀/子串)，返回该卡。
        """
        if not self.custom_cards:
            return None
        keys = sorted(self.custom_cards, key=len, reverse=True)
        text = ""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": self.NAME_ROI,
                        "expected": "|".join(keys) + "|" + self.OCR_ALL,  # 宽expected读全部文字, 防纯假名卡名被过滤读不到(否则卡面兜底不触发)
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 卡名区域OCR异常 {e!r}")
            reco = None
        if reco and reco.hit and reco.filtered_results:
            text = "".join(r.text for r in reco.filtered_results)
            self._last_card_name_ocr = text
            # 标题区域可能混入其他文字；原始 OCR 文本仍需与配置卡名匹配。
            logger.info(f"HIF技能卡定制: 卡名区域OCR读出=[{text}]")
            # ① 卡名匹配允许 OCR 丢失强化标记，但先匹配较长名称。
            normalized_text = unicodedata.normalize("NFKC", re.sub(r"\s+", "", text))
            for card in keys:
                base_name = unicodedata.normalize("NFKC", card.rstrip("+"))
                if base_name in normalized_text:
                    logger.info(f"HIF技能卡定制: 识别卡名「{card}」(子串命中)")
                    return card
            # ② 配置的 OCR 标记：仅当一组特异标记全都出现时才命中。
            # 定制列表会把卡名与效果分区显示；例如インフルエンサー的卡名
            # 不会被读到，但「メンタルスキルカード」「+6」「+1」稳定可读。
            normalized = normalized_text
            for card in keys:
                config = self.custom_cards.get(card)
                markers = config.get("ocr_markers", []) if isinstance(config, dict) else []
                if markers and all(marker in normalized for marker in markers):
                    logger.info(
                        f"HIF技能卡定制: 识别卡「{card}」(OCR标记全命中={markers})"
                    )
                    return card
        # ③ OCR 未确定时，先比对当前已选卡的共享缩略图。
        face = self._match_card_face(context, image)
        if face:
            return face
        if text:
            # ④ 卡面不可用时再用相似度容忍残缺 OCR。
            best, best_s = None, 0.0
            for card in keys:
                s = SequenceMatcher(None, normalized_text, unicodedata.normalize("NFKC", card.rstrip("+"))).ratio()
                if s > best_s:
                    best_s, best = s, card
            if best is not None and best_s >= 0.8:
                logger.info(f"HIF技能卡定制: 识别卡名「{best}」(相似度={best_s:.2f})")
                return best
        # ⑤ 都匹配不上 → 识别不了(供上层跳过该格)
        return None

    def _match_card_face(self, context: Context, image) -> Optional[str]:
        """OCR 读不出卡名时，比对面板中已选卡的共享缩略图与旧识别模板。"""
        if not self.FACE_TEMPLATES or cv2 is None:
            return None
        x0, y0, w, h = self.DETAIL_FACE_ROI
        region = image[y0:y0 + h, x0:x0 + w]
        if region.size == 0 or region.shape[0] == 0:
            return None
        scores = []
        for card, templates in self.FACE_TEMPLATES.items():
            best_score = 0.0
            for tpl_name in templates:
                path = os.path.join(EXT_DIR, "resource/base/image", tpl_name)
                try:
                    tpl = cv2.imread(path)
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(f"HIF技能卡定制: 读取卡面模板{tpl_name}异常 {e!r}")
                    continue
                if tpl is None or tpl.size == 0:
                    logger.warning(f"HIF技能卡定制: 卡面模板不存在 {tpl_name}")
                    continue
                scales = self.FACE_ICON_SCALES if tpl_name.endswith(".webp") else self.FACE_SCALES
                for scale in scales:
                    resized = cv2.resize(tpl, None, fx=scale, fy=scale)
                    height, width = resized.shape[:2]
                    if height >= region.shape[0] or width >= region.shape[1]:
                        continue
                    _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(region, resized, cv2.TM_CCOEFF_NORMED))
                    threshold = self.FACE_ICON_THRESH if tpl_name.endswith(".webp") else self.FACE_THRESH
                    if score >= threshold:
                        best_score = max(best_score, float(score))
            scores.append((best_score, card))
        scores.sort(reverse=True)
        if scores and scores[0][0] > 0:
            if len(scores) > 1 and scores[0][0] - scores[1][0] < self.FACE_MARGIN:
                logger.warning(f"HIF技能卡定制: 卡面匹配候选分数接近 {scores[:2]}，跳过猜测")
                return None
            logger.info(f"HIF技能卡定制: 卡面识别到「{scores[0][1]}」 score={scores[0][0]:.3f}")
            return scores[0][1]
        return None

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
                    "recognition": "OCR", "roi": [30, 950, 660, 130], "expected": r"[^\n]+",
                }},
            )
            text = "".join(result.text or "" for result in reco.filtered_results) if reco and reco.hit else ""
            text = re.sub(r"\s+", "", text)
            card = re.sub(r"\s+", "", self._cur_card or "")
            return bool(card and card in text and "カスタマイズしました" in text)
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

    def _handle_execution_confirmation(self, context: Context, image) -> str:
        """提交后最多观察5秒，不在未知画面重复提交。"""
        deadline = time.monotonic() + 5.0
        success_frames = stable_frames = unchanged_frames = 0
        stable_total = None
        success_seen = False
        while not context.tasker.stopping:
            success_frames = success_frames + 1 if self._confirm_completed(context, image) else 0
            if context.tasker.stopping:
                return "stopped"
            if success_frames >= 2 and not success_seen:
                success_seen = True
                logger.info(f"HIF技能卡定制: 「{self._cur_card}」本次操作成功，卡名及完成提示连续两帧")
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
                if stable_frames >= 2 and observed is not None:
                    if context.tasker.stopping:
                        return "stopped"
                    if observed == 0:
                        logger.info(f"HIF技能卡定制: 「{self._cur_card}」剩余0次或成功后已到上限，确认整卡完成")
                        self._click(context, self.BACK_TO_LIST_POS)
                        self._advance_queue(completed=True)
                        return "list"
                    if self._last_total is None:
                        return "execute"   # 提交前动画已稳定，现在才允许首次提交。
                    if observed < self._last_total:
                        logger.info(f"HIF技能卡定制: 「{self._cur_card}」本次操作成功，剩余次数{self._last_total}→{observed}")
                        self._exec_stall = 0
                        return "execute"
                    unchanged_frames += 1
            if context.tasker.stopping:
                return "stopped"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if context.tasker.stopping:
                return "stopped"
            image = context.tasker.controller.post_screencap().wait().get()
        if context.tasker.stopping:
            return "stopped"
        reason = "执行结果未确认"
        if unchanged_frames >= self.EXEC_STALL_LIMIT:
            reason += f"（合計{stable_total}连续不减少，P点不足/无法定制）"
        logger.warning(f"HIF技能卡定制: 「{self._cur_card}」{reason}，不重复提交，返回列表")
        self._click(context, self.BACK_TO_LIST_POS)
        self._advance_queue(reason=reason)
        return "list"

    def _menu_available(self, context: Context, image, idx: int) -> int:
        """读第 idx 个菜单项顶部「あとN回」，返回 N；读不到/已达上限(カスタマイズ上限)返回 0。"""
        roi = self.MENU_LEFT_ROIS[idx]
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR", "roi": roi, "expected": r"あと(\d+)",
                    }
                },
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 菜单可用性识别异常 {e!r}")
            return 0
        if not (reco and reco.hit and reco.filtered_results):
            return 0
        for r in reco.filtered_results:
            m = re.search(r"あと(\d+)", r.text or "")
            if m:
                return int(m.group(1))
        return 0

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

    def _handle_detail_execute(self, context: Context, image) -> str:
        """待执行界面(D)：读「合計あとN回」自动点满【勾选项】。

        N>0 → 在勾选项间轮换点一项 + 実行する(点一次合計-1，自动适配"集中+可点2次"等)；
        提交后或次数未知时只观察确认，不重复点击。次数由游戏实际剩余决定。
        """
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
        if total is None or total == 0:
            # 未提交前OCR缺失也只等待；零次数需连续两帧确认。
            self._last_total = None
            return "confirm"
        self._last_total = total
        chosen = self._menu_attempt % len(menu_items)
        self._menu_attempt = chosen + 1
        pos = menu_items[chosen]
        option = self.custom_cards[self._cur_card]["menu"][chosen]
        logger.info(
            f"HIF技能卡定制: 「{self._cur_card}」合計剩余{total if total is not None else '?'}次 "
            f"→ 选择「{option['name']}」(定制项目ID={option['id']}) @ {pos} → 点実行する @ {self.EXEC_POS}"
        )
        self._click(context, pos)
        if context.tasker.stopping:
            return "stopped"
        self._click(context, self.EXEC_POS)
        logger.info(f"HIF技能卡定制: 「{self._cur_card}」已提交，进入等待确认，提交前剩余{total}次")
        return "confirm"

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """显式状态机：主界面→选卡→待执行→提交后确认。

        不靠每帧 OCR 猜当前界面（选卡/待执行底部都有「あとN枚」，会互相误判），
        而是用 stage 明确当前阶段，只推进对应 handler，阶段流转清晰可靠。
        """
        logger.info("HIF技能卡定制: CardCustomAuto.run 已调用, 开始构建队列")
        try:
            return self._run_inner(context, argv)
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f"HIF技能卡定制: run 异常 {e!r}")
            return False

    def _run_inner(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not self._heal_before_customization(context):
            return False
        stage, step, unknown = "main", 0, 0
        profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
        self._load_custom_cards(profession)
        # 面板文件只保留已导入卡；每张卡的 menu 只含已勾选的定制项目。
        enabled = self._customization_enabled(context)
        self._queue = list(self.custom_cards) if enabled else []
        if not enabled:
            logger.info("HIF技能卡定制: 前台开关已关闭，跳过面板卡牌定制")
        self._queue = self._queue[: self.MAX_CARDS]   # 一次育成最多6张,再多钱不够
        self._queue_idx = 0
        self.done_cards.clear()
        self.skipped_cards.clear()
        self._done_all = not bool(self._queue)
        self._cur_card = self._queue[0] if self._queue else None
        logger.info(f"HIF技能卡定制: 职业={profession}, 进入状态机, 需定制队列={self._queue}")
        while not context.tasker.stopping and step < self.STEP_LIMIT:
            step += 1
            image = context.tasker.controller.post_screencap().wait().get()

            if stage == "main":
                act = self._handle_main(context, image)
                if act == "exit":
                    return True   # 已在商店主界面完成购饮和終了，不再误点もどる
                if act == "enter":
                    stage = "select"; unknown = 0; continue
                if act == "refresh":
                    continue   # 保持main：等刷新动画完成，下帧重新读枚数(不直接进选卡)
                unknown += 1
                if unknown >= self.BETWEEN_LIMIT:
                    logger.warning("HIF技能卡定制: 自定义入口未能进入选卡页，停止后续点击")
                    return False
                time.sleep(0.5)
                continue
            if stage == "select":
                act = self._handle_select(context, image)
                if act == "pick":
                    unknown = 0; continue
                if act == "detail":
                    stage = "execute"; unknown = 0; continue
                if act == "back":
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
                if act == "stopped":
                    return True
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
                    stage = "select"; unknown = 0; continue # 本卡菜单项点完→回选卡
                unknown += 1
                if unknown >= self.BETWEEN_LIMIT:
                    logger.warning(f"HIF技能卡定制: 连续{self.BETWEEN_LIMIT}步无进展，退出防卡死")
                    break

        if context.tasker.stopping:
            logger.info("HIF技能卡定制: 任务已停止，跳过退出收尾点击")
            return True
        logger.info("HIF技能卡定制: 退出状态机, 先点もどる回主界面再点終了")
        try:
            # 退出时可能在选卡/详情界面,须先点「もどる」回主界面,再点「終了」关闭(否则点不到終了会停留)
            self._click(context, self.BACK_POS, delay=1.0)
            if context.tasker.stopping:
                logger.info("HIF技能卡定制: 收尾期间任务已停止，跳过終了点击")
                return True
            time.sleep(0.8)
            if context.tasker.stopping:
                logger.info("HIF技能卡定制: 收尾期间任务已停止，跳过終了点击")
                return True
            self._click(context, self.TERMINATE_POS, delay=1.0)
            time.sleep(0.6)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF技能卡定制: 退出收尾异常 {e!r}")
        return True
