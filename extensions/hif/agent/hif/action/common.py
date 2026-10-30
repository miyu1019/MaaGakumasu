"""HIF 动作共用：路径常量、错误类型、日期中断装饰器、截图/网络重试/安全场景/安全停止等工具函数（不导入 maa）。"""
from __future__ import annotations

import os
import time
import unicodedata

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


class HifDrinkFlowError(RuntimeError):
    """A drink dialog failed to return safely to the battle."""


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


def _hif_defer(context, argv, reason) -> bool:
    recovery = _hif_recovery.bind(argv)
    if _hif_stopping(context):
        return True
    if not recovery.defer():
        return _hif_failure(context, getattr(argv, 'node_name', '?'), '点击前重识别', reason + '，30秒预算耗尽')
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
    'ProduceHIFCardCustomFlag', 'ProduceHIFCardDeleteFlag', 'ProduceHIFReChallengeFlag',
    'ProduceHIFFinalRatingFlag', 'ProduceHIFPhotoMemoryFlag', 'ProduceHIFGenerateFlag',
    'ProduceHIFNextFlag', 'ProduceHIFMainScreenFlag',
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
