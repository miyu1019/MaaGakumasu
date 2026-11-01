"""Screen rules for HIF shows (Live/ED) whose emulator failed to rotate to landscape."""
import numpy as np

# 2026-11-01 MuMu 实测：模拟器未转横屏时截图仍为 720x1280，演出画面落在 y=487..793 的横带，
# 上下为纯黑，仅 y≈300、y≈960 各有一行演出控件。黑区取开控件行，横带取内缩区域。
BLACK_ROWS = ((0, 280), (345, 470), (810, 940), (990, 1280))
BAND_ROWS = (505, 775)
BLACK_LEVEL = 24
BLACK_RATIO = 0.995
BAND_RATIO = 0.5


def unrotated_show(image) -> bool:
    """竖屏截图里出现上下纯黑、中间整条非黑横带时，判定为未旋转的演出画面。"""
    if image is None or image.ndim != 3 or image.shape[0] != 1280 or image.shape[1] != 720:
        return False
    lit = image.max(axis=2) > BLACK_LEVEL
    if any(1.0 - lit[top:bottom].mean() < BLACK_RATIO for top, bottom in BLACK_ROWS):
        return False
    top, bottom = BAND_ROWS
    return bool(lit[top:bottom].mean() >= BAND_RATIO)
