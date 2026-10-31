"""HIF 日程行动：授业、训练、休息/支给/相谈入口、选项、道具、演出跳过与本战1再挑战。"""
import re
import json
import time
import random

# opencv 为可选依赖。未安装时不让 agent 启动整体崩溃；
# 饮料识别降级，授业定制标记未确认时按非定制卡处理。
try:
    import cv2
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    cv2 = None

from typing import Any, Dict, List, Optional

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from extensions.hif.recovery import state as _hif_recovery

from . import common
from .deck import ProduceHIF__ProduceHIFCardSwapAuto
from .shop import ProduceHIF__ProduceHIFConsultAuto
from .battle import ProduceHIF__ProduceCardsAuto
from .common import _hif_date_interrupt


class ProduceHIF__ProduceChooseEventBase(CustomAction):
    """
    HIF 行动页共用读取能力：SP 优先级、截图、属性得分与 SP 课程徽章识别。

    原培育事件选择逻辑只服务于上游通用培育流程，HIF 不再使用，已移除。
    """

    PREFERENCE_LIST = ["Da", "Vi", "Vo"]

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

    @staticmethod
    def _get_screenshot(context: Context):
        """获取屏幕截图"""
        return common._hif_nonbattle_screencap(context)

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


@AgentServer.custom_action("ProduceHIF__ProduceChooseOptionsAuto")
@_hif_date_interrupt
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
        image = common._hif_nonbattle_screencap(context)
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
        if common._hif_stopping(context):
            return
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
@_hif_date_interrupt
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
        _hif_recovery.bind(argv)
        if common._hif_stopping(context):
            return True
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        if day not in (6, 3):
            return common._hif_defer(context, argv, f'HIF授业日期未确认(计数={day})')
        time.sleep(0.25)
        image = self._get_screenshot(context)
        if self._read_day_counter(context, image) != day:
            return common._hif_defer(context, argv, 'HIF授业日期仍在转场变化')
        if not self._lesson_buttons_ready(context, image):
            return common._hif_defer(context, argv, 'HIF授业三列按钮未确认')
        if common._hif_stopping(context):
            return True
        _hif_recovery.bind(argv).progress(('行动日期', day))
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
@_hif_date_interrupt
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
    TRAIN_DAYS = (5, 2)
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

    @classmethod
    def _training_buttons_ready(cls, context: Context, image) -> bool:
        reco = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                'recognition': 'OCR', 'roi': [0, 1000, 720, 150], 'expected': '公開'}})
        positions = list(cls.TRAIN_BUTTONS.values())
        columns = {min(range(3), key=lambda i: abs(r.box[0] + r.box[2] / 2 - positions[i][0]))
                   for r in (reco.filtered_results if reco and reco.hit else [])}
        return columns == {0, 1, 2}

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        if day not in self.TRAIN_DAYS:
            logger.info(f'HIF训练: 非训练日或日期未确认(计数={day})，交回入口重新识别')
            return True
        time.sleep(0.25)
        image = self._get_screenshot(context)
        if self._read_day_counter(context, image) != day or not self._training_buttons_ready(context, image):
            logger.info('HIF训练: 日期变化或三个训练按钮未齐全，不点击，交回入口重新识别')
            return True
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
@_hif_date_interrupt
class ProduceHIF__ProduceHIFRestAuto(ProduceHIF__ProduceHIFHomeActionBase):
    """
    HIF第3天/第6天行动：按前台配置选择休息、支给或相谈。

    第3天（计数4）可选「差し入れ」或「休む」；第6天（计数1）可选「相談」或「休む」。
    本动作由 ProduceHIF__ProduceHIFRestFlag (OCR 休む) 触发，并根据左上计数限定各日的选项，
    防止第3天误点相谈或第6天误点支给。
    """

    # 各天「休む」按钮中心（按左上计数区分；实测：第3天=计数4在 (638,861)，第6天=计数1在 (643,840)）
    REST_POS = {4: (638, 861), 1: (643, 840)}
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

    @staticmethod
    def _rest_buttons_ready(context, image) -> bool:
        result = context.run_recognition('ProduceHIF__ProduceRecognitionScore', image,
            pipeline_override={'ProduceHIF__ProduceRecognitionScore': {
                'recognition': 'OCR', 'roi': [580, 755, 140, 150], 'expected': '休む'}})
        return bool(result and result.hit)

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        _hif_recovery.bind(argv)
        if common._hif_stopping(context):
            return True
        image = self._get_screenshot(context)
        day = self._read_day_counter(context, image)
        if day not in self.REST_POS:
            return common._hif_defer(context, argv, f'HIF休息行动日期未确认(计数={day})')
        time.sleep(0.25)
        image = self._get_screenshot(context)
        if self._read_day_counter(context, image) != day:
            return common._hif_defer(context, argv, 'HIF休息行动日期仍在转场变化')
        if not self._rest_buttons_ready(context, image):
            return common._hif_defer(context, argv, 'HIF休息行动按钮未确认')
        if common._hif_stopping(context):
            return True
        _hif_recovery.bind(argv).progress(('行动日期', day))
        n = 7 - day
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
        # 已确认的第3/6天：按配置或默认点休む。
        x, y = self.REST_POS[day]
        logger.info(f"HIF休息: 计数={day}(第{n}天), 点击休む @ ({x}, {y})")
        self._click_pos(context, x, y)
        if common._hif_stopping(context):
            return True
        # 打开确认窗口: 让 RestConfirmFlagAuto 仅在随后短暂窗口内允许确认休み
        ProduceHIF__ProduceHIFRestAuto._confirm_until = time.time() + ProduceHIF__ProduceHIFRestAuto.REST_CONFIRM_WINDOW
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFOptionAuto")
@_hif_date_interrupt
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
        image = common._hif_nonbattle_screencap(context)
        self._arm_recognition_gate(image)
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(self.OPTION_CONFIRM_DELAY)
        logger.info(f"HIF授业选项: 确认{label} @ {pos}")
        context.tasker.controller.post_click(pos[0], pos[1]).wait()
        time.sleep(0.5)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceUseItemClickAuto")
@_hif_date_interrupt
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
            image = common._hif_nonbattle_screencap(context)
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
@_hif_date_interrupt
class ProduceHIF__ProduceUseNoteClickAuto(ProduceHIF__ProduceUseItemClickAuto):
    """点 L票券道具(ProduceHIF__ProduceUseNote)。只点节这个,避免重复。"""
    TPL = "hif/produce/hif_item_ticket.png"
    FALLBACK = (575, 970)
    LABEL = "L票券"


@AgentServer.custom_action("ProduceHIF__ProduceUsePtClickAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceUsePtClickAuto(ProduceHIF__ProduceUseItemClickAuto):
    """点 S球道具(ProduceHIF__ProduceUsePt)。只点这个,避免重复。"""
    TPL = "hif/produce/hif_item_skillball.png"
    FALLBACK = (660, 975)
    LABEL = "S球"


@AgentServer.custom_action("ProduceHIF__ProduceSkipShowAuto")
class ProduceHIF__ProduceSkipShowAuto(CustomAction):
    """跳过演出：右上两次、左上一次，仅在横屏时点击并限制坐标在当前画面内。

    移植自上游 b3da0cb 的 ProduceSkipShowAuto。与上游不同：截图或点击失败时
    不判节点失败，交还流水线，由演出切屏期间的截图恢复逻辑处理。
    """

    TARGETS = ((1192, 80), (1192, 80), (20, 20))
    CLICK_INTERVAL = 0.3

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        node = getattr(argv, "node_name", None) or "?"
        controller = context.tasker.controller
        for target_x, target_y in self.TARGETS:
            if context.tasker.stopping:
                return True
            try:
                image = controller.post_screencap().wait().get()
            except Exception as exc:
                logger.warning(f"{node}: 跳过演出截图异常，交还流水线: {exc}")
                return True
            if image is None or getattr(image, "size", 0) == 0:
                logger.warning(f"{node}: 跳过演出截图失败，交还流水线")
                return True
            height, width = image.shape[:2]
            if height >= width:
                # 演出已结束或尚在切屏，交还流水线判断，避免误点结算页面。
                return True
            x = max(0, min(width - 1, target_x))
            y = max(0, min(height - 1, target_y))
            if not controller.post_click(x, y).wait().succeeded:
                logger.warning(f"{node}: 跳过演出点击失败 @ ({x},{y})，交还流水线")
                return True
            time.sleep(self.CLICK_INTERVAL)
        return True


@AgentServer.custom_action("ProduceHIF__ProduceHIFClickRecoBoxAuto")
@_hif_date_interrupt
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


@AgentServer.custom_action("ProduceHIF__ProduceHIFReChallengeAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFReChallengeAuto(CustomAction):
    """本战1结果页按玩家分数决定再挑战；用玩家行独有的「ログ」按钮定位。"""

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
    # 覆盖四人顺位列表和玩家行的「ログ」按钮；分数<门槛才再挑战。
    SCORE_ROI = [0, 240, 720, 710]
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

    def _read_my_score(self, context, image) -> Optional[int]:
        """读取「ログ」按钮左侧同排的分数，不依赖固定偶像名或排名。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore", image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": self.SCORE_ROI, "expected": r"ログ|日志|日誌|\d+"}},
            )
        except Exception:
            return None
        if not (reco and reco.hit and reco.filtered_results):
            return None
        results = reco.filtered_results
        log_boxes = [r.box for r in results if re.sub(r"\s", "", r.text) in ("ログ", "日志", "日誌") and r.box[0] >= 600]
        if len(log_boxes) != 1:
            logger.warning(f"HIF本战1分数: 无法唯一定位日志按钮 OCR={[(r.text, r.box) for r in results]}")
            return None
        log_box = log_boxes[0]
        scores = []
        for r in results:
            b = r.box
            text = re.sub(r"\s", "", r.text)
            if not re.fullmatch(r"[0-9]+(?:[,，][0-9]{3})*", text):
                continue
            if 200 <= b[0] and b[0] + b[2] <= log_box[0] and abs(b[1] + b[3] / 2 - log_box[1] - log_box[3] / 2) < 40:
                scores.append(int(re.sub(r"[^0-9]", "", text)))
        if len(scores) == 1:
            return scores[0]
        logger.warning(f"HIF本战1分数: 玩家行分数不唯一 OCR={[(r.text, r.box) for r in results]}")
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
        image = common._hif_nonbattle_screencap(context)
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
        my_score = self._read_my_score(context, image)
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
