"""HIF 饮料：按职业面板读取饮料名单，P饮料领取页与所持上限保留页。"""
import re
import json
import time
from typing import Optional
from pathlib import Path

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from extensions.hif.recovery import state as _hif_recovery

from . import common
from .battle import ProduceHIF__ProduceCardsAuto
from .common import HifDrinkFlowError, _hif_date_interrupt


def _hif_drink_priority_names(
    context: Context, purchase_only: bool = False, disabled_only: bool = False, include_disabled: bool = False,
) -> list[str]:
    """按前台职业读取饮料名单；购买保护可包含不使用项。"""
    profession = ProduceHIF__ProduceCardsAuto._load_hif_profession(context)
    try:
        with open(common.HIF_DRINK_PROFILES_PATH, encoding="utf-8") as file:
            profiles = json.load(file).get("profiles", {})
        with open(common.HIF_DRINK_CATALOG_PATH, encoding="utf-8") as file:
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
        if not isinstance(entry, dict):
            continue
        legacy_enabled = entry.get("disabled") is not True
        disabled = (entry.get("first_battle_enabled", legacy_enabled) is False
                    and entry.get("second_battle_enabled", legacy_enabled) is False)
        if not include_disabled and disabled != disabled_only:
            continue
        if purchase_only and entry.get("purchase_enabled") is not True:
            continue
        name = names_by_id.get(entry.get("id"))
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


@AgentServer.custom_action("ProduceHIF__ProduceHIFKeepDrinkAuto")
@_hif_date_interrupt
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
        return common._hif_nonbattle_screencap(context)

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
            text = common._hif_drink_name_key(result.text)
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

    @staticmethod
    def _keep_catalog():
        try:
            with open(common.HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
                return json.load(file)['drinks']
        except (OSError, ValueError, KeyError):
            return None

    def _keep_icon_name(self, row):
        """只比对已截取的图标，不点击、不滑动。"""
        from extensions.hif.drink_keep import icon_name
        catalog = self._keep_catalog()
        if catalog is None:
            return None
        return icon_name(row.get('icon'), catalog, Path(common.EXT_DIR) / 'resource/base/image/hif/hif_drink_icons')

    def _resolve_keep_name(self, context, row):
        from extensions.hif.drink_keep import KeepPageError
        catalog = self._keep_catalog()
        if catalog is None:
            return None
        name = self._keep_icon_name(row)
        if name or row.get('info') is None:
            return name
        # 只点击识别到的白色 i，不用图标边角的估算坐标，避免切换整行。
        before = self._selection_remaining(context, self._screencap(context))
        if before is None:
            raise KeepPageError('信息按钮点击前无法确认选择数')
        context.tasker.controller.post_click(*row['info']).wait()
        by_key = {common._hif_drink_name_key(d['name']): d['name'] for d in catalog}
        deadline, stable, previous = time.monotonic() + 8, 0, None
        name, close = None, None
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                raise KeepPageError('任务停止，取消读取信息详情')
            image = self._screencap(context)
            results = self._keep_info_results(context, image)
            name, raw = ProduceHIF__ProduceCardsAuto._battle_drink_identity_from_results(results, by_key)
            close = next((list(r.box) for r in results if '閉じる' in r.text and r.box[1] > 1050), None)
            title = any('ドリンク詳細' in common._hif_drink_name_key(r.text) for r in results)
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
            title = any('ドリンク詳細' in common._hif_drink_name_key(r.text) for r in results)
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
                'roi': [0, 0, 720, 1280], 'expected': r'[^\n]+'}})
        results = self._results(reco)
        if any(r.box[0] >= 500 and r.box[1] >= 1200 and 'CONNECTING' in (r.text or '').upper() for r in results):
            return 'connecting', None
        if any('Pドリンク所持上限' in common._hif_drink_name_key(r.text) and r.box[1] < 240 for r in results):
            return 'cap', None
        # 普通领取页的瓶身也有饮料名（如初星水），须先认领取提示与底部按钮。
        # 提示已出现而按钮仍在过渡时只等待，不能继续当领取动画点击。
        receive_prompt = any(420 <= r.box[1] < 800 and '受け取るPドリンクを選' in common._hif_drink_name_key(r.text)
                             for r in results)
        if receive_prompt:
            ready = any(r.box[1] >= 1000 and common._hif_drink_name_key(r.text) == '受け取る' for r in results)
            return ('page' if ready else 'unknown'), None
        banner = [common._hif_drink_name_key(r.text) for r in results if 420 <= r.box[1] <= 560]
        if (any(re.fullmatch(r'(?:ダンス|ボーカル|ビジュアル)SPレッスン終了時[、,]?', text) for text in banner)
                and any(re.fullmatch(r'ランダムなPドリンクを[12１２]つ獲得', text) for text in banner)):
            return 'reward', 'SPレッスン追加飲料'
        with open(common.HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
            names = {common._hif_drink_name_key(d['name']): d['name'] for d in json.load(file)['drinks']}
        for result in results:
            if result.box[0] >= 170 and result.box[1] >= 740 and result.box[3] >= 20:
                text = common._hif_drink_name_key(result.text)
                # 领取横幅的饮料图标可能被 OCR 合并为名称前的一两个字符。
                # 只容忍完整目录名称前的短前缀，不模糊匹配名称或效果文案。
                matches = [name for key, name in names.items()
                           if text.endswith(key) and len(text) - len(key) <= 2]
                if len(matches) == 1:
                    return 'reward', matches[0]
        # 效果文案中的「レッスン」不能证明领取动画退出，只认标题或完整操作项。
        if any(re.fullmatch(r'(?:受け取る|差し入れ|相談|(?:SP)?授業|公開トレーニング|最終試験|おでかけ|休む|次へ)',
                           common._hif_drink_name_key(r.text)) for r in results):
            return 'page', None
        if any(r.box[1] >= 740 and re.search(r'レッスン|ターン|体力|スキルカード|パラメータ|好印象|元気', r.text) for r in results):
            # 名称暂时漏识别时，不能把领取效果后面的主页标题当成可操作主页。
            return 'unknown', None
        if any(r.box[1] < 180 and 'HIF本戦' in common._hif_drink_name_key(r.text).upper().replace('.', '') for r in results):
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
            # 上限页仍在时交回入口会重新扫描；离开后出现其他已知场景即继续。
            return common._hif_timeout(context, 'ProduceHIFDrinkFullKeepFlag', '所持上限页', str(error))


class _InventoryHandedBack(BaseException):
    """满栏换饮料超时后已宽限确认已知场景，结束本动作并交回入口；不能被流程内的 except Exception 吞掉。"""


@AgentServer.custom_action("ProduceHIF__ProduceHIFDrinkAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceHIFDrinkAuto(CustomAction):
    """
    HIF「受け取るPドリンク」领取入口。

    「受け取るPドリンクを選んでください」界面：
    - 读完三个候选名称，按当前职业饮料面板顺序领取；无命中时选第一瓶。
    满4瓶且候选有面板目标时，逐瓶识别手持饮料，舍弃未勾选购买中优先级最低的饮料，
    确认4→3瓶后复选原目标再领取。没有目标或全部手持受购买保护时拒领。
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
            image = common._hif_nonbattle_screencap(context)
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
        priority = {common._hif_drink_name_key(name): rank for rank, name in enumerate(priority_names)}
        disabled = {common._hif_drink_name_key(name) for name in disabled_names}
        self._supply_fallback_pos = None
        self._supply_target_name = None
        candidates = []
        allowed_positions = []
        for index, pos in enumerate(self.SUPPLY_DRINK_SLOTS):
            logger.info(f"HIF饮料领取页: 检查第{index + 1}瓶 @ {pos}")
            self._click(context, pos)
            image = common._hif_nonbattle_screencap(context)
            name = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            logger.info(f"HIF饮料领取页: 第{index + 1}瓶 名称=[{name}]")
            key = common._hif_drink_name_key(name)
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
            image = common._hif_nonbattle_screencap(context)
            confirmed = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            if common._hif_drink_name_key(confirmed) == common._hif_drink_name_key(name):
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
        except _InventoryHandedBack:
            return True
        except HifDrinkFlowError as error:
            if context is not None and getattr(context.tasker, 'stopping', False):
                logger.info('HIF饮料领取页: 任务已停止，取消后续操作')
            else:
                logger.error(f'HIF饮料领取页: {error}；返回任务失败状态，不让异常越过 Agent 回调')
            return False

    def _run_receive(self, context: Context, argv: CustomAction.RunArg) -> bool:
        first_image = common._hif_nonbattle_screencap(context)
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
                logger.info('HIF满栏换饮料: 没有可舍弃的未购买保护饮料，保留手持并拒领')
                return self._try_decline(context)
            # 舍弃会清空待领取栏的选中态；必须复选原目标并重新核对名称。
            self._click(context, target_pos)
            image = common._hif_nonbattle_screencap(context)
            confirmed = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            if common._hif_drink_name_key(confirmed) != common._hif_drink_name_key(incoming):
                self._stop_inventory_flow(context, 'HIF满栏换饮料: 舍弃后复选的新饮料名称未确认，停止领取')
            logger.info(f'HIF满栏换饮料: 已重新选中并确认待领取「{incoming}」')

        if not target_selected:
            self._click(context, self._supply_fallback_pos)
            image = common._hif_nonbattle_screencap(context)
            fallback_name = self._read_supply_drink_ocr(context, image, self.SUPPLY_DRINK_NAME_ROI)
            disabled_keys = {common._hif_drink_name_key(name) for name in disabled_names}
            fallback_key = common._hif_drink_name_key(fallback_name)
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
            text = common._hif_drink_name_key(r.text)
            if 'キャンセル' in text or 'キヤンセル' in text:
                actions['cancel'] = list(r.box)
            elif text == '捨てる' and r.box[0] >= 480:
                actions['discard'] = list(r.box)
            elif text == '使う' and r.box[0] >= 350:
                actions['use'] = list(r.box)
        return actions

    @staticmethod
    def _inventory_boxes_stable(current, previous):
        # OCR 左边界可轻微变化；保留纵向约束，避免把弹窗滑入动画当成稳定。
        return bool(current and previous and current.keys() == previous.keys() and all(
            abs((box[0] + box[2] / 2) - (previous[key][0] + previous[key][2] / 2)) <= 10
            and abs((box[1] + box[3] / 2) - (previous[key][1] + previous[key][3] / 2)) <= 6
            for key, box in current.items()))

    def _inventory_returned(self, context, image, results):
        # 动画中按钮暂时移出底部 ROI，不代表详情关闭。
        return (not self._inventory_actions(results)
                and not any('ドリンク詳細' in common._hif_drink_name_key(r.text) for r in results)
                and not any(re.search(r'捨て.*(?:ますか|[?？])', r.text or '') for r in results)
                and self._is_receive_drink_screen(context, image))

    def _inventory_detail(self, context: Context, pos) -> Optional[dict]:
        """展开普通持有饮料，连续读到同一完整名称后才允许比较或舍弃。"""
        with open(common.HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
            catalog = {common._hif_drink_name_key(d['name']): d['name'] for d in json.load(file)['drinks']}
        self._click(context, pos, delay=0.2)
        reader = ProduceHIF__ProduceCardsAuto()
        previous_name, previous_actions, stable = None, None, 0
        deadline = time.monotonic() + self.INVENTORY_ACTION_TIMEOUT
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return None
            image = common._hif_nonbattle_screencap(context)
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
        identity = None
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return False
            image = common._hif_nonbattle_screencap(context)
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
            retry_delay = 10.0 if attempts == 2 else 1.0
            if button_stable >= 3 and attempts < 3 and time.monotonic() - last_click >= retry_delay:
                with open(common.HIF_DRINK_CATALOG_PATH, encoding='utf-8') as file:
                    catalog = {common._hif_drink_name_key(d['name']): d['name'] for d in json.load(file)['drinks']}
                name, _ = ProduceHIF__ProduceCardsAuto._battle_drink_identity_from_results(results, catalog)
                if not name or identity is not None and name != identity:
                    logger.warning('HIF满栏换饮料: 无法确认同一详情，取消退出补点')
                    return False
                identity = name
                box = current['cancel']
                logger.info(f'HIF满栏换饮料: 点击キャンセル，尝试{attempts + 1}/3 @ {box}')
                self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
                attempts, last_click, button_stable = attempts + 1, time.monotonic(), 0
                # 按钮稳定识别与点击后的关闭确认分别计时。
                deadline = last_click + self.INVENTORY_ACTION_TIMEOUT
                if attempts == 2:
                    logger.warning('HIF满栏换饮料: 两次取消后继续观察10秒，再复核并最后重试')
                    deadline = last_click + 10.0 + self.INVENTORY_ACTION_TIMEOUT
            time.sleep(0.2)
        if common._hif_observe_result(context, lambda frame: self._inventory_returned(
                context, frame, self._inventory_popup_results(context, frame)), timeout=self.INVENTORY_ACTION_TIMEOUT):
            logger.info('HIF满栏换饮料: 超时复核确认详情已关闭，返回领取页')
            return True
        return False

    @staticmethod
    def _replacement_slot(held: list[str], incoming: str, priority_names: list[str], disabled_names=(), protected_names=()) -> Optional[int]:
        ranks = {common._hif_drink_name_key(name): i for i, name in enumerate(priority_names)}
        disabled = {common._hif_drink_name_key(name) for name in disabled_names}
        protected = {common._hif_drink_name_key(name) for name in protected_names}
        if not incoming or not held or any(not name for name in held):
            return None
        def rank(name):
            key = common._hif_drink_name_key(name)
            return len(ranks) + 1 if key in disabled else ranks.get(key, len(ranks))
        # 只保护勾选购买的饮料；其余取最低优先级，同级时取最右一瓶。
        candidates = [i for i, name in enumerate(held) if common._hif_drink_name_key(name) not in protected]
        if common._hif_drink_name_key(incoming) not in ranks or not candidates:
            return None
        return max(candidates, key=lambda i: (rank(held[i]), i))

    def _stop_inventory_flow(self, context: Context, message: str):
        if getattr(context.tasker, 'stopping', False):
            raise HifDrinkFlowError('任务已停止，取消后续饮料操作')
        # 舍弃待确认时 _hif_timeout 直接停止，避免重复舍弃；其余情况宽限观察后交回入口。
        if common._hif_timeout(context, 'ProduceHIFDrinkFullFlag', '满栏饮料操作', message):
            raise _InventoryHandedBack(message)
        stop = getattr(context.tasker, 'post_stop', None)
        if stop:
            stop()  # 不等待当前自定义动作自身退出
        raise HifDrinkFlowError(message)

    def _discard_inventory_drink(self, context: Context, pos, expected_name: str) -> bool:
        detail = self._inventory_detail(context, pos)
        if not detail or detail['name'] != expected_name or not detail['discard']:
            self._stop_inventory_flow(context, 'HIF满栏换饮料: 无法复核待舍弃饮料及捨てる按钮，停止')
        box = detail['discard']
        _hif_recovery.pending = f'舍弃饮料:{expected_name}'
        logger.info(f'HIF满栏换饮料: 确认舍弃「{expected_name}」@ {pos}')
        self._click(context, (box[0] + box[2] // 2, box[1] + box[3] // 2), delay=0.2)
        deadline, stable = time.monotonic() + self.INVENTORY_ACTION_TIMEOUT, 0
        previous, button_stable, attempts, last_click = None, 0, 0, 0
        while time.monotonic() < deadline:
            if getattr(context.tasker, 'stopping', False):
                return False
            image = common._hif_nonbattle_screencap(context)
            results = self._inventory_popup_results(context, image)
            ready = (self._bar_filled_count(image) == 3
                     and self._inventory_returned(context, image, results))
            stable = stable + 1 if ready else 0
            if stable >= 3:
                logger.info('HIF满栏换饮料: 已确认4→3瓶，返回领取页')
                _hif_recovery.pending = None
                return True
            questions = [r for r in results if re.search(r'捨て.*(?:ますか|[?？])', r.text or '')
                         and common._hif_drink_name_key(expected_name) in common._hif_drink_name_key(r.text)]
            question_bottom = max((r.box[1] + r.box[3] for r in questions), default=1280)
            buttons = {common._hif_drink_name_key(r.text): r.box for r in results
                       if common._hif_drink_name_key(r.text) in ('はい', 'いいえ') and r.box[1] > question_bottom}
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
                        image = common._hif_nonbattle_screencap(context)
                        if self._bar_filled_count(image) == 4:
                            logger.warning('HIF满栏换饮料: 确认未生效，已取消且保留4瓶，本次拒领并继续任务')
                            _hif_recovery.pending = None
                            return False
                    break
            time.sleep(0.2)
        if common._hif_observe_result(context, lambda frame: self._bar_filled_count(frame) == 3
                and self._inventory_returned(context, frame, self._inventory_popup_results(context, frame))):
            _hif_recovery.pending = None
            logger.info('HIF结果复核: 舍弃延迟生效，已确认4→3瓶，不重复舍弃')
            return True
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
        protected_names = _hif_drink_priority_names(context, purchase_only=True, include_disabled=True)
        index = self._replacement_slot(held, incoming, priority_names, disabled_names, protected_names)
        logger.info(f'HIF满栏换饮料: 持有={held}，新饮料={incoming}，待舍弃槽={index + 1 if index is not None else "无"}')
        if index is None:
            return False
        return self._discard_inventory_drink(context, (self.BAR_SLOT_X[index], self.BAR_SLOT_Y), held[index])

    def _try_decline(self, context) -> bool:
        """满栏拒领：OCR「受け取らないで」文本，点击其下方按钮。成功返回 True。"""
        image = common._hif_nonbattle_screencap(context)
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
                image = common._hif_nonbattle_screencap(context)
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
            image = common._hif_nonbattle_screencap(context)
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
