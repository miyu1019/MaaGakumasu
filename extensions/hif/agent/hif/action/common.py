"""HIF 动作共用：路径常量、错误类型、日期中断装饰器、截图/网络重试/安全场景/安全停止等工具函数（不导入 maa）。"""
from __future__ import annotations

import os
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

from typing import TYPE_CHECKING
from pathlib import Path
from functools import wraps

from utils import logger

if TYPE_CHECKING:
    from maa.context import Context

from extensions.hif.recovery import state as _hif_recovery

BASE_DIR = str(Path(__file__).resolve().parents[5])
EXT_DIR = os.path.join(BASE_DIR, "extensions", "hif")
CARDS_PRIORITY_CONFIG_PATH = os.path.join(BASE_DIR, "config", "hif", "cards_priority.json")
HIF_PRIORITY_CATALOG_PATH = os.path.join(EXT_DIR, "catalog", "hif_priority_card_catalog.json")
HIF_DRINK_CATALOG_PATH = os.path.join(EXT_DIR, "catalog", "hif_drink_catalog.json")
HIF_DRINK_PROFILES_PATH = os.path.join(BASE_DIR, "config", "hif", "hif_drink_profiles.json")
HIF_CARD_RENAMES_PATH = os.path.join(EXT_DIR, "catalog", "hif_card_renames.json")


def _hif_card_renames() -> dict:
    """上游修正卡名时的旧名→正式名；读取失败时不改名，个人配置照旧可读。"""
    try:
        with open(HIF_CARD_RENAMES_PATH, encoding="utf-8") as file:
            renames = json.load(file)
    except (OSError, ValueError) as e:
        logger.warning(f"HIF卡名更名表读取失败: {e}，旧卡名配置不迁移")
        return {}
    if not isinstance(renames, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in renames.items()):
        logger.warning("HIF卡名更名表结构无效，旧卡名配置不迁移")
        return {}
    return renames


def _hif_rename_cards(value, renames: dict):
    """把配置里等于旧卡名的字典键和字符串换成正式名；新旧名并存时保留已有的新名条目。"""
    if isinstance(value, dict):
        return {renames.get(key, key): _hif_rename_cards(item, renames) for key, item in value.items()
                if not (key in renames and renames[key] in value)}
    if isinstance(value, list):
        def identity(item):
            # 优先级条目以 key 区分，优先获取条目以 name 区分；其余列表元素按字符串本身。
            name = item.get("key") or item.get("name") if isinstance(item, dict) else item
            return name if isinstance(name, str) else None
        kept = {identity(item) for item in value if identity(item) not in renames}
        return [_hif_rename_cards(item, renames) for item in value
                if not (identity(item) in renames and renames[identity(item)] in kept)]
    return renames.get(value, value) if isinstance(value, str) else value


def _hif_load_personal_config(path: str):
    """读取 config/hif 下的个人策略，并把旧卡名迁移为目录中的正式名。"""
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
    return _hif_rename_cards(config, _hif_card_renames())


class HifDrinkFlowError(RuntimeError):
    """A drink dialog failed to return safely to the battle."""


class HifConfigError(HifDrinkFlowError):
    """策略或配置无效：不做超时宽限，直接安全停止。"""


# 等待超时后的只读宽限观察时长；同一步骤内最多交回入口的次数，防止无进展循环。
HIF_GRACE_SECONDS = 90.0
HIF_GRACE_MAX = 3


class _HifDateChanged(BaseException):
    """Unwind even broad Exception handlers before navigating away from an action."""


def _hif_date_interrupt(cls):
    original = cls.run

    @wraps(original)
    def run(self, context, argv):
        _hif_recovery.bind(argv)
        try:
            return original(self, context, argv)
        except _HifDateChanged:
            _hif_recovery.date_requested = True
            logger.warning('HIF日期变更: 已退出旧动作，交由日期恢复分支处理')
            return False

    cls.run = run
    return cls


def _hif_date_screen(context, image, *, title=True):
    candidates = ['ProduceHIF__DateChanged'] if _hif_network_dialog_possible(image) else []
    if title:
        candidates.append('ProduceHIF__DateTitle')
    for node in candidates:
        result = context.run_recognition(node, image)
        if result and result.hit:
            return node, result
    return None, None


def _hif_stopping(context) -> bool:
    return bool(getattr(getattr(context, 'tasker', None), 'stopping', False))


def _hif_failure(context, node, stage, reason) -> bool:
    if _hif_stopping(context):
        return False
    logger.error(f'HIF安全停止: 节点={node} 阶段={stage} 原因={reason}；保留当前育成现场')
    try:
        image = context.tasker.controller.post_screencap().wait().get()
        folder = os.path.join(BASE_DIR, 'debug', 'custom', 'hif_recovery_failures')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f'{time.time_ns()}.png')
        if cv2 is not None and image is not None and cv2.imwrite(path, image):
            logger.error(f'HIF恢复现场截图: {path}')
    except Exception as error:
        logger.warning(f'HIF恢复截图保存失败: {error!r}')
    return False


def _hif_timeout(context, node, stage, reason, still_there=None, timeout=HIF_GRACE_SECONDS) -> bool:
    """等待超时的统一处理：只读观察到已知 HIF 场景则交回入口继续（True），否则安全停止（False）。

    有待确认的提交时，必须提供 still_there(image)：原页面仍在说明无法判断提交是否生效，继续等待；
    原页面离开且出现其他已知场景，视为提交已生效并清除待确认标记。无法判断原页面时不交回入口，避免重放提交。
    """
    if _hif_stopping(context):
        return False
    if _hif_recovery.pending and still_there is None:
        return _hif_failure(context, node, stage, f'{reason}；「{_hif_recovery.pending}」结果待确认，不交回入口重放')
    if _hif_recovery.grace >= HIF_GRACE_MAX:
        return _hif_failure(context, node, stage, f'{reason}；本步骤已{HIF_GRACE_MAX}次超时交回入口仍无进展')
    logger.warning(f'HIF超时宽限: 节点={node} 阶段={stage} 原因={reason}；不点击，只读观察最多{timeout:.0f}秒')
    deadline, stable, previous = _hif_recovery.now() + timeout, 0, None
    while not _hif_stopping(context) and _hif_recovery.now() < deadline:
        try:
            image = _hif_nonbattle_screencap(context)
            if _hif_stopping(context):
                return False
            original = still_there is not None and still_there(image)
            scene = None if original else _hif_safe_scene(context, image, deadline=deadline)
        except _HifDateChanged:
            raise
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(f'HIF超时宽限: 读取画面失败 {error!r}')
            scene = None
        stable = stable + 1 if scene and scene == previous else (1 if scene else 0)
        previous = scene
        if stable >= 3:
            _hif_recovery.grace += 1
            if _hif_recovery.pending:
                logger.info(f'HIF超时宽限: 原页面已离开，视为「{_hif_recovery.pending}」已生效，不重复提交')
                _hif_recovery.pending = None
            # 交回入口后重新计算点击前重识别与入口恢复预算，次数由 grace 限制。
            _hif_recovery.deadline = None
            _hif_recovery.entry_recovered = False
            logger.warning(f'HIF超时宽限: 已稳定确认 {scene}，交回入口继续（本步骤第{_hif_recovery.grace}/{HIF_GRACE_MAX}次）')
            return True
        time.sleep(0.5)
    if _hif_stopping(context):
        return False
    return _hif_failure(context, node, stage, f'{reason}；宽限{timeout:.0f}秒仍未确认可识别页面')


def _hif_defer(context, argv, reason) -> bool:
    recovery = _hif_recovery.bind(argv)
    if _hif_stopping(context):
        return True
    if not recovery.defer():
        return _hif_timeout(context, getattr(argv, 'node_name', '?'), '点击前重识别', reason + '，30秒预算耗尽')
    logger.info(f'HIF重新识别: {reason}；不点击，交回入口')
    time.sleep(0.5)
    return True


def _hif_observe_result(context, predicate, timeout=10.0) -> bool:
    """Read-only verification: never replay a submitted operation."""
    deadline, stable = _hif_recovery.now() + timeout, 0
    while not _hif_stopping(context) and _hif_recovery.now() < deadline:
        try:
            image = _hif_nonbattle_screencap(context)
        except Exception as error:
            logger.warning(f'HIF结果复核: 截图失败，不能确认操作结果: {error!r}')
            return False
        if _hif_stopping(context):
            return False
        try:
            confirmed = predicate(image)
        except Exception as error:
            logger.warning(f'HIF结果复核: 状态读取失败，不能确认操作结果: {error!r}')
            return False
        if _hif_recovery.now() >= deadline:
            return False
        stable = stable + 1 if confirmed else 0
        if stable >= 3:
            return True
        time.sleep(0.25)
    return False


# Only recognitions without navigation side effects. Generic buttons do not prove a safe scene.
_HIF_SAFE_SCENES = (
    'ProduceHIFCardsFlag', 'ProduceHIFOptionFlag', 'ProduceHIFLessonFlag', 'ProduceHIFTrainFlag',
    'ProduceHIFRestFlag', 'ProduceHIFCardSwapFlag', 'ProduceHIFExchangeFlag', 'ProduceHIFDrinkFlag',
    'ProduceHIFDrinkFullFlag', 'ProduceHIFDrinkFullKeepFlag', 'ProduceHIFConsultShopFlag',
    'ProduceHIFCardCustomFlag', 'ProduceHIFCardDeleteFlag', 'ProduceHIFCardCopyFlag',
    'ProduceHIFReChallengeFlag', 'ProduceHIFFinalRatingFlag', 'ProduceHIFPhotoMemoryFlag', 'ProduceHIFGenerateFlag',
    'ProduceHIFFinishedFlag', 'ProduceHIFNextFlag', 'ProduceHIFMainScreenFlag',
)


def _hif_safe_scene(context, image, exclude=(), deadline=None):
    for suffix in _HIF_SAFE_SCENES:
        if _hif_stopping(context) or deadline is not None and _hif_recovery.now() >= deadline:
            return None
        if suffix in exclude:
            continue
        name = 'ProduceHIF__' + suffix
        data = context.get_node_data(name)
        if not data or not data.get('enabled', True):
            continue
        result = context.run_recognition(name, image)
        if result and result.hit:
            return name
    return None


def _hif_network_dialog_possible(image) -> bool:
    """先检查橙色弹窗标题带，普通截图不额外跑网络错误 OCR。"""
    try:
        band = image[945:1000, 40:660]
        orange = (band[:, :, 2] > 200) & (band[:, :, 1] > 100) & (band[:, :, 0] < 180)
        return orange.size > 0 and int(orange.sum()) / orange.size > 0.6
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False


def _hif_title_possible(image) -> bool:
    """Cheap prefilter for the title's white data-link button before running OCR."""
    try:
        button = image[105:135, 40:155]
        white = (button[:, :, 0] > 220) & (button[:, :, 1] > 220) & (button[:, :, 2] > 220)
        return white.size > 0 and int(white.sum()) / white.size > 0.65
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False


def _hif_retry_network(context: Context, image) -> bool:
    if getattr(context.tasker, 'stopping', False):
        return False
    dialog = _hif_network_dialog_possible(image)
    if not dialog:
        if _hif_title_possible(image):
            title = context.run_recognition('ProduceHIF__DateTitle', image)
            if title and title.hit and not _hif_stopping(context):
                raise _HifDateChanged()
        return False
    retry = context.run_recognition("ProduceHIF__ProduceNetworkRetry", image)
    if not (retry and retry.hit):
        date, _ = _hif_date_screen(context, image, title=False)
        if date and not _hif_stopping(context):
            raise _HifDateChanged()
        return False
    if getattr(context.tasker, 'stopping', False):
        return False
    started = time.monotonic()
    box = retry.box
    logger.warning("HIF通信错误: 点击重试，等待网络恢复后继续当前步骤")
    try:
        if _hif_stopping(context):
            return False
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        time.sleep(3)
    finally:
        _hif_recovery.paused += time.monotonic() - started
    return True


def _hif_nonbattle_screencap(context: Context, *, retry_limit=None):
    """非战斗动作在原步骤内恢复通信，不重放购买、删卡或定制操作。"""
    started, paused_before = time.monotonic(), _hif_recovery.paused
    image = context.tasker.controller.post_screencap().wait().get()
    retrying = False
    retries = 0
    try:
        while (retry_limit is None or retries < retry_limit) and _hif_retry_network(context, image):
            retrying = True
            retries += 1
            image = context.tasker.controller.post_screencap().wait().get()
    finally:
        if retrying:
            _hif_recovery.paused = paused_before + time.monotonic() - started
    return image


def _hif_drink_name_key(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name or "").split())


def _hif_list_still(first, second, view) -> bool:
    """列表区连续两帧几乎不变，视为滚动已停（避开惯性与回弹）。"""
    x0, y0, x1, y1 = view
    a, b = first[y0:y1, x0:x1].astype(np.int16), second[y0:y1, x0:x1].astype(np.int16)
    return float(np.abs(a - b).mean()) <= 2.5


def _hif_empty_card_cell(image, pos, std: float = 8, half: int = 40) -> bool:
    """卡格中心只有纯色背景时为空位（实机灰度标准差≤3，灰色眠気≈20）；截图不可用时不判空。"""
    if cv2 is None or getattr(image, "shape", ()) != (1280, 720, 3):
        return False
    x, y = pos
    patch = cv2.cvtColor(image[y - half:y + half, x - half:x + half], cv2.COLOR_BGR2GRAY)
    return float(patch.std()) < std


def _hif_list_scroll_shift(before, after, direction: int, view, band: int = 120, overlap_score: float = 0.85):
    """按滑动前后列表区图像测内容实际位移（上移为正）；匹配不唯一时返回 None，不按滑动距离猜。

    卡底费用、M 标和边框各卡相似，模板带可能对上相邻一行；实际位移超出模板可测范围时，
    错一行的位置反而成为唯一峰值。故每个候选位移都要求整个重叠区对齐，重叠区不足一个模板带时不确认。
    """
    if cv2 is None or before is None or after is None:
        return None
    x0, y0, x1, y1 = view
    first = cv2.GaussianBlur(cv2.cvtColor(before[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (5, 5), 0)
    second = cv2.GaussianBlur(cv2.cvtColor(after[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (5, 5), 0)
    start = len(first) - band - 10 if direction > 0 else 10
    template = first[start:start + band]
    if float(template.std()) < 8:
        return None
    scores = cv2.matchTemplate(second, template, cv2.TM_CCOEFF_NORMED).ravel()
    shifts = start - np.arange(len(scores))
    scores[shifts * direction < -3] = -1
    verified = []
    for _ in range(4):
        best = int(np.argmax(scores))
        if float(scores[best]) < 0.80:
            break
        scores[max(0, best - 8):best + 9] = -1
        delta = int(shifts[best])
        overlap = len(first) - abs(delta)
        if overlap < band:
            continue
        a, b = (first[delta:], second[:overlap]) if delta >= 0 else (first[:overlap], second[-delta:])
        verified.append((float(cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED).max()), delta))
    verified.sort(reverse=True)
    if not verified or verified[0][0] < overlap_score:
        return None
    if len(verified) > 1 and verified[0][0] - verified[1][0] < 0.06:
        return None
    delta = verified[0][1]
    return 0 if abs(delta) <= 3 else delta
