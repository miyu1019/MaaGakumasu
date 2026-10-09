import os
import json
import re
import time
import unicodedata
from typing import Tuple, Union, Optional
from difflib import SequenceMatcher

from utils import logger
from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from ..action.produce import (
    ProduceHIF__ProduceCardsAuto,
    ProduceHIF__ProduceHIFConsultAuto,
    ProduceHIF__ProduceHIFLessonAuto,
    ProduceHIF__ProduceHIFOptionAuto,
    ProduceHIF__ProduceHIFRestAuto,
    ProduceHIF__ProduceHIFTrainAuto,
)


@AgentServer.custom_recognition('ProduceHIF__ProduceExtraTurns')
class ProduceHIF__ProduceExtraTurns(CustomRecognition):
    """独立查询待转入倒计时的额外回合；不触发出牌或用饮。"""
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        count = ProduceHIF__ProduceCardsAuto._read_extra_turn_count(context, argv.image)
        return CustomRecognition.AnalyzeResult(
            box=ProduceHIF__ProduceCardsAuto.EXTRA_TURN_ROI if count and count > 0 else None,
            detail={'extra_turns': count, 'confirmed': count is not None})


@AgentServer.custom_recognition("ProduceHIF__ProduceChooseIdolAuto")
class ProduceHIF__ProduceChooseIdolAuto(CustomRecognition):
    """
    自动识别当前偶像名称和歌曲
    """

    SIMILARITY_THRESHOLD = 0.7

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        idol_name = json.loads(argv.custom_recognition_param)["idol_name"]
        song_name = json.loads(argv.custom_recognition_param)["song_name"]
        recognized_name = ""
        recognized_song = ""

        true_end_detail = context.run_recognition(
            "ProduceHIF__ProduceChooseIdolTrueEnd",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceChooseIdolTrueEnd": {
                    "recognition": "OCR",
                    "expected": ["True", "End"],
                    "roi": [430, 34, 266, 48],
                }
            },
        )
        if true_end_detail and true_end_detail.hit:
            logger.debug("识别到True End")
            idol_name_roi = [440, 128, 280, 64]
            song_name_roi = [380, 90, 320, 45]
        else:
            logger.debug("未识别到True End")
            idol_name_roi = [400, 98, 320, 64]
            song_name_roi = [340, 60, 380, 45]

        name_detail = context.run_recognition(
            "ProduceHIF__ProduceChooseIdolName",
            argv.image,
            pipeline_override={"ProduceHIF__ProduceChooseIdolName": {"recognition": "OCR", "roi": idol_name_roi}},
        )
        name_score = 0.0
        if name_detail and name_detail.hit:
            recognized_name = "".join([item.text for item in name_detail.all_results]).replace(" ", "")
            name_score = self.similarity_ratio(recognized_name, idol_name)
            logger.info(f"识别到偶像名称: {recognized_name}，相似度: {name_score:.2f}")

        song_detail = context.run_recognition(
            "ProduceHIF__ProduceChooseIdolSong",
            argv.image,
            pipeline_override={"ProduceHIF__ProduceChooseIdolSong": {"recognition": "OCR", "roi": song_name_roi}},
        )
        song_score = 0.0
        if song_detail and song_detail.hit:
            recognized_song = "".join([item.text for item in song_detail.all_results]).replace("[", "").replace("]", "")
            song_score = self.similarity_ratio(recognized_song, song_name)
            logger.info(f"识别到歌曲名称: {recognized_song}，相似度: {song_score:.2f}")

        if name_score >= self.SIMILARITY_THRESHOLD and song_score >= self.SIMILARITY_THRESHOLD:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别偶像卡成功"})
        else:
            logger.debug(f"识别偶像卡失败，偶像相似度: {name_score:.2f}，歌曲相似度: {song_score:.2f}")
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "识别偶像卡失败"})

    @staticmethod
    def normalize_text(text: str) -> str:
        """NFKC 归一化后忽略 OCR 中不稳定的空格与标点。"""
        return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isalnum())

    @staticmethod
    def similarity_ratio(str1, str2):
        """返回0-1之间的相似度分数，比较时忽略符号差异。"""
        return SequenceMatcher(
            None,
            ProduceHIF__ProduceChooseIdolAuto.normalize_text(str1),
            ProduceHIF__ProduceChooseIdolAuto.normalize_text(str2),
        ).ratio()


@AgentServer.custom_recognition("ProduceHIF__ProduceShowStart")
class ProduceHIF__ProduceShowStart(CustomRecognition):
    """
    检测通过屏幕是否旋转判断演出开始
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        context.run_action("ProduceHIF__Click_1")
        height, width = image.shape[0], image.shape[1]
        if height < width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceHIF__ProduceShowEnd")
class ProduceHIF__ProduceShowEnd(CustomRecognition):
    """
    检测通过屏幕是否旋转判断演出结束
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height, width = image.shape[0], image.shape[1]
        context.run_action("ProduceHIF__Click_1")
        if height > width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceHIF__ProduceCardsFlagAuto")
class ProduceHIF__ProduceCardsFlagAuto(CustomRecognition):
    """
    自动识别出牌场景
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        context.run_action("ProduceHIF__Click_1")
        cards_reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionCards", argv.image)
        health_reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionHealthFlag", argv.image)
        if cards_reco_detail and cards_reco_detail.hit and health_reco_detail and health_reco_detail.hit:
            logger.success("事件: 出牌场景")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别到出牌场景"})
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到选择场景"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFCardsFlagAuto")
class ProduceHIF__ProduceHIFCardsFlagAuto(CustomRecognition):
    """HIF 出牌入口：回合数、战斗HUD锚点及可操作标志同时存在时进入。"""

    TURN_TITLE_ROI = [0, 0, 150, 45]
    TURN_TITLE_EXPECTED = "残りターン"
    RANKING_ROI = [200, 0, 380, 175]
    RANKING_EXPECTED = r"(?:1st|2nd|3rd|4th)"

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        title = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR",
                    "expected": self.TURN_TITLE_EXPECTED,
                    "roi": self.TURN_TITLE_ROI,
                }
            },
        )
        if not (title and title.hit and title.filtered_results):
            ranking = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                argv.image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "expected": self.RANKING_EXPECTED,
                        "roi": self.RANKING_ROI,
                    }
                },
            )
            if not (ranking and ranking.hit and ranking.filtered_results):
                return CustomRecognition.AnalyzeResult(
                    box=None, detail={"detail": "未识别到HIF本战场景"}
                )

        turn = context.run_recognition(
            "ProduceHIF__ProduceRecognitionScore",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceRecognitionScore": {
                    "recognition": "OCR",
                    "expected": ProduceHIF__ProduceCardsAuto.TURN_COUNT_EXPECTED,
                    "roi": ProduceHIF__ProduceCardsAuto.TURN_COUNT_ROI,
                }
            },
        )
        if not (turn and turn.hit and turn.filtered_results):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到HIF本战场景"})

        skip = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", argv.image)
        cards = None if skip and skip.hit else context.run_recognition("ProduceHIF__ProduceRecognitionCards", argv.image)
        if (skip and skip.hit) or (cards and cards.hit):
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1], detail={"detail": "识别到HIF本战场景"}
            )
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到HIF本战场景"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFCardCustomFlagAuto")
class ProduceHIF__ProduceHIFCardCustomFlagAuto(CustomRecognition):
    """自动识别 P点商店界面(スキルカードカスタマイズ绿钮)，触发技能卡定制。"""

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        reco = context.run_recognition(
            "ProduceHIF__ProduceIdentityMatch",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceIdentityMatch": {
                    "recognition": "TemplateMatch",
                    "template": "hif/produce/hif_customize_btn.png",
                    "roi": [270, 850, 340, 150],
                    "threshold": 0.8,
                }
            },
        )
        if reco and reco.hit:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别到技能卡定制入口"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到技能卡定制入口"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFRestConfirmFlagAuto")
class ProduceHIF__ProduceHIFRestConfirmFlagAuto(CustomRecognition):
    """休み確認确认按钮识别：仅在自己刚点过休む后的短窗口内触发。

    原 RestConfirmFlag 是无状态模板匹配(hif_rest_confirm.png 底部带),通信错误弹窗的
    「リトライ」按钮与之同型且位置几乎重叠,会误触发(日志 01:57:20 实证)。现由
    ProduceHIF__ProduceHIFRestAuto 点击休む时设置 _confirm_until 时间窗,本识别仅在窗口内且模板
    命中才返回;其它界面(含通信错误弹窗)一律不触发。
    """

    TPL = "hif/produce/hif_rest_confirm.png"
    ROI = [0, 1080, 720, 125]
    THRESHOLD = 0.8

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        # 门控: 仅在自己刚点过休む后的窗口内才允许确认(防通信错误弹窗等误触发)
        if time.time() > ProduceHIF__ProduceHIFRestAuto._confirm_until:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "非休み確認窗口,不触发"})
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceIdentityMatch",
                argv.image,
                pipeline_override={"ProduceHIF__ProduceIdentityMatch": {"recognition": "TemplateMatch", "template": self.TPL, "roi": self.ROI, "threshold": self.THRESHOLD}},
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF休み確認: 模板匹配异常 {e!r}")
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "识别异常"})
        if reco and reco.hit:
            logger.info("HIF休み確認: 窗口内命中确认按钮 → 确认休み")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "休み確認弹窗"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "模板未命中"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFConsultShopFlagAuto")
class ProduceHIF__ProduceHIFConsultShopFlagAuto(CustomRecognition):
    """相谈商店识别：终了后的退场动画内不重复触发购饮。"""

    SHOP_ROI = [0, 0, 220, 170]
    SHOP_EXPECTED = r"相[谈談]"

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        if ProduceHIF__ProduceHIFConsultAuto._shop_finish_guard_active():
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"detail": "相谈已终了，忽略退场动画"}
            )
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                argv.image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.SHOP_ROI,
                        "expected": self.SHOP_EXPECTED,
                    }
                },
            )
            if reco and reco.hit:
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 1, 1], detail={"detail": "识别到相谈商店"}
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF相谈商店flag: OCR异常 {e!r}")
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "非相谈商店"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFRestFlagAuto")
class ProduceHIF__ProduceHIFRestFlagAuto(CustomRecognition):
    """HIF休息识别：训练点击后的动画期不把残留「休む」误判成休息动作。"""

    REST_ROI = [580, 755, 140, 150]
    REST_EXPECTED = "休む"

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        day = ProduceHIF__ProduceHIFTrainFlagAuto._read_day_counter(context, argv.image)
        if ProduceHIF__ProduceHIFTrainFlagAuto._click_gate_active(
            day, argv.image, argv.task_detail.task_id
        ):
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"detail": "训练点击后的动画期，忽略残留休む"}
            )
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                argv.image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.REST_ROI,
                        "expected": self.REST_EXPECTED,
                    }
                },
            )
            if reco and reco.hit:
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 1, 1], detail={"detail": "识别到休む"}
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF休息flag: OCR异常 {e!r}")
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到休む"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFLessonFlagAuto")
class ProduceHIF__ProduceHIFLessonFlagAuto(CustomRecognition):
    """HIF授业识别：点击授业后的动画期不重复触发另一列授业。"""

    LESSON_ROI = [0, 1000, 720, 150]
    LESSON_EXPECTED = "授業"

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        if ProduceHIF__ProduceHIFLessonAuto._recognition_gate_active(argv.image):
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"detail": "授业点击后的动画期，忽略残留授業"}
            )
        day = ProduceHIF__ProduceHIFTrainFlagAuto._read_day_counter(context, argv.image)
        if day not in (6, 3):
            return CustomRecognition.AnalyzeResult(box=None, detail={'detail': '非已确认的授业日', 'day': day})
        try:
            if ProduceHIF__ProduceHIFLessonAuto._lesson_buttons_ready(context, argv.image):
                return CustomRecognition.AnalyzeResult(
                    box=ProduceHIF__ProduceHIFLessonAuto.RECOGNITION_GATE_ROI,
                    detail={'detail': '授业日期与三列授業按钮均已确认', 'day': day}
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF授业flag: OCR异常 {e!r}")
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到授業"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFOptionFlagAuto")
class ProduceHIF__ProduceHIFOptionFlagAuto(CustomRecognition):
    """HIF授业后三选项识别：选择后不重复点击残留选项。"""

    OPTION_ROI = [100, 290, 520, 140]
    OPTION_EXPECTED = "3200"
    CHOICE_EDGES = (660, 760, 860)
    CHOICE_EDGE_DIFF_MIN = 70.0

    @classmethod
    def _has_three_choice_rows(cls, image) -> bool:
        """按三条选项栏的上边界判断，允许蓝、黄、红等不同配色。"""
        if image is None or not hasattr(image, "shape") or image.shape[0] < 870:
            return False
        for y in cls.CHOICE_EDGES:
            above = image[y - 8, 100:600].astype("int16")
            below = image[y + 8, 100:600].astype("int16")
            if above.size == 0 or float(abs(above - below).mean()) < cls.CHOICE_EDGE_DIFF_MIN:
                return False
        return True

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        if ProduceHIF__ProduceHIFOptionAuto._recognition_gate_active(argv.image):
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"detail": "授业选项点击后的动画期，忽略残留标题"}
            )
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                argv.image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "roi": self.OPTION_ROI,
                        "expected": self.OPTION_EXPECTED,
                    }
                },
            )
            if reco and reco.hit:
                if not self._has_three_choice_rows(argv.image):
                    return CustomRecognition.AnalyzeResult(
                        box=None, detail={"detail": "属性上限可见，但未见授业三条选项"}
                    )
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 1, 1], detail={"detail": "识别到授业后三选项"}
                )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF授业选项flag: OCR异常 {e!r}")
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到授业后三选项"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFTrainFlagAuto")
class ProduceHIF__ProduceHIFTrainFlagAuto(CustomRecognition):
    """HIF训练flag：OCR「公開」命中 或 训练日(计数=5/2)强制训练。

    正常训练日靠底部「公開」OCR 触发。但训练日(第2天=计数5、第5天=计数2)的「公開」按钮
    偶发识别不到(布局/转场差异),导致落到休息。这里在 OCR 未命中时读左上「N日」计数,
    若计数=5/2(第2/5天)仍强制触发训练,不依赖「公開」OCR(用户天数 = 7 - 计数)。
    """

    TRAIN_ROI = [0, 1000, 720, 150]     # 底部公開按钮OCR区(与 ProduceHIF__ProduceHIFTrainFlag 原OCR一致)
    TRAIN_EXPECT = "公開"
    DAY_COUNTER_ROI = [72, 80, 78, 50]  # 行动屏左上「N日」计数(同 ProduceHIF__ProduceHIFHomeActionBase)
    FORCE_TRAIN_DAYS = {5, 2}           # 计数=5(第2天)/计数=2(第5天) 都强制训练
    # 返回完整操作区，让 pipeline 的 pre_wait_freezes 等待换卡提示、按钮与 SP 图标稳定。
    STABLE_BOX = ProduceHIF__ProduceHIFTrainAuto.RECOGNITION_GATE_ROI

    @classmethod
    def _read_day_counter(cls, context: Context, image) -> Optional[int]:
        """读取行动页日期计数；动画或非行动页返回 None。"""
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                image,
                pipeline_override={
                    "ProduceHIF__ProduceRecognitionScore": {
                        "recognition": "OCR",
                        "expected": r"\d+日",
                        "roi": cls.DAY_COUNTER_ROI,
                    }
                },
            )
            if reco and reco.hit and reco.filtered_results:
                for result in reco.filtered_results:
                    match = re.search(r"(\d+)", result.text or "")
                    if match:
                        return int(match.group(1))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF训练flag: 读计数异常 {e!r}")
        return None

    @staticmethod
    def _click_gate_active(day: Optional[int], image, task_id: Optional[int]) -> bool:
        """阻止动画期重复命中；若点击前画面仍存在，则解锁以便重试。"""
        blocked_task_id = ProduceHIF__ProduceHIFTrainAuto._recognition_blocked_task_id
        if (
            task_id is not None
            and blocked_task_id is not None
            and task_id != blocked_task_id
        ):
            ProduceHIF__ProduceHIFTrainAuto._clear_recognition_gate()
            return False
        now = time.time()
        if now >= ProduceHIF__ProduceHIFTrainAuto._recognition_block_expires:
            ProduceHIF__ProduceHIFTrainAuto._clear_recognition_gate()
            return False
        if now < ProduceHIF__ProduceHIFTrainAuto._recognition_block_until:
            return True
        blocked_day = ProduceHIF__ProduceHIFTrainAuto._recognition_blocked_day
        if day is not None and blocked_day is not None and day != blocked_day:
            ProduceHIF__ProduceHIFTrainAuto._clear_recognition_gate()
            return False
        if ProduceHIF__ProduceHIFTrainAuto._same_screen_as_before_click(image):
            logger.warning("HIF训练flag: 点击后仍停留在训练选择画面，判定点击未生效，解除门控重试")
            ProduceHIF__ProduceHIFTrainAuto._clear_recognition_gate()
            return False
        if day is None or blocked_day is None or day == blocked_day:
            return True
        return False

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        day = self._read_day_counter(context, argv.image)
        if self._click_gate_active(day, argv.image, argv.task_detail.task_id):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "训练点击后的动画/同日门控"})

        # ① 正常路径: 底部「公開」OCR 命中即触发训练
        try:
            reco = context.run_recognition(
                "ProduceHIF__ProduceRecognitionScore",
                argv.image,
                pipeline_override={"ProduceHIF__ProduceRecognitionScore": {"recognition": "OCR", "roi": self.TRAIN_ROI, "expected": self.TRAIN_EXPECT}},
            )
            if reco and reco.hit and reco.filtered_results:
                return CustomRecognition.AnalyzeResult(box=self.STABLE_BOX, detail={"detail": "识别到公開按钮"})
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"HIF训练flag: OCR公開异常 {e!r}")
        # ② 兜底: 「公開」漏检时, 训练日(计数=5/2, 第2/5天)强制触发训练
        if day in self.FORCE_TRAIN_DAYS:
            logger.info(f"HIF训练flag: 计数={day}(训练日)强制训练")
            return CustomRecognition.AnalyzeResult(box=self.STABLE_BOX, detail={"detail": "训练日强制训练"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "非训练屏"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFPhotoMemoryFlagAuto")
class ProduceHIF__ProduceHIFPhotoMemoryFlagAuto(CustomRecognition):
    """回忆照片选择(メモリーにするフォト)：只在【竖屏且「次へ」按钮出现】时才触发。

    演出(横屏)结束→屏幕旋转回竖屏的回忆选择界面才有点次へ；横屏/旋转期不触发，
    避免演出过程或横竖屏切换时误判。
    """

    # 诊断开关: True=打印回忆照片识别诊断(刷屏), False=不显示(默认)。排查时临时置True。
    DIAG = False
    STABLE_N = 2                   # 连续命中N帧才点次へ(抗转场特效瞬帧)
    _stable = 0

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        h, w = argv.image.shape[:2]
        if self.DIAG:
            logger.info(f"回忆照片诊断: 尺寸={w}x{h} {'横屏' if h < w else '竖屏'}")
        if h < w:
            # 横屏(演出中/旋转期) → 不触发
            self._stable = 0
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "横屏(演出中),不处理回忆选择"})
        reco = context.run_recognition(
            "ProduceHIF__ProduceIdentityMatch",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceIdentityMatch": {
                    "recognition": "TemplateMatch",
                    "template": "hif/produce/hif_next_button.png",
                    "roi": [230, 1105, 260, 100],
                    "threshold": 0.78,
                }
            },
        )
        if reco and reco.hit and reco.filtered_results:
            self._stable += 1
            if self.DIAG:
                logger.info(f"回忆照片: 命中次へ(连续{self._stable}/{self.STABLE_N}帧)")
            if self._stable >= self.STABLE_N:
                self._stable = 0
                # 返回次へ实际命中框,action 点其中心(自适应位置,抗坐标漂移)
                b = reco.filtered_results[0].box
                return CustomRecognition.AnalyzeResult(box=list(b), detail={"detail": "竖屏+次へ,识别到回忆选择"})
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "次へ命中但未达稳定帧"})
        self._stable = 0
        if self.DIAG:
            logger.warning("回忆照片诊断: 竖屏但次へ模板未命中")
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "竖屏但未见次へ"})


@AgentServer.custom_recognition("ProduceHIF__ProduceHIFFinalRatingFlagAuto")
class ProduceHIF__ProduceHIFFinalRatingFlagAuto(CustomRecognition):
    """演出结束后的「最終プロデュース評価」界面(SSS评价+TAPで次へ)：
    模板匹配顶部标题, 连续STABLE_N帧命中才点TAP(抗转场特效)。命中返回屏幕中部让action点TAP推进。"""

    # 最终评价标题模板(竖屏, 顶部"最終プロデュース評価" 实测 y165-240)
    TPL = "hif/produce/hif_final_rating.png"
    ROI = [0, 150, 720, 120]     # 标题搜索区(720x1280基线)
    THRESHOLD = 0.85
    TAP_BOX = [120, 560, 480, 200]   # 点TAP: 屏幕中部(该界面"TAPで次へ"任意点击推进)
    STABLE_N = 2                   # 连续命中N帧才点(抗转场特效瞬帧)
    _stable = 0                    # 连续命中计数(类属性,跨帧)

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        reco = context.run_recognition(
            "ProduceHIF__ProduceIdentityMatch",
            argv.image,
            pipeline_override={
                "ProduceHIF__ProduceIdentityMatch": {
                    "recognition": "TemplateMatch",
                    "template": self.TPL,
                    "roi": self.ROI,
                    "threshold": self.THRESHOLD,
                }
            },
        )
        if reco and reco.hit:
            self._stable += 1
            logger.info(f"最终评价识别: 命中(连续{self._stable}/{self.STABLE_N}帧)")
            if self._stable >= self.STABLE_N:
                self._stable = 0
                return CustomRecognition.AnalyzeResult(box=list(self.TAP_BOX), detail={"detail": "识别到最终评价界面,点TAP"})
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "最终评价命中但未达稳定帧"})
        self._stable = 0
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "非最终评价界面"})
