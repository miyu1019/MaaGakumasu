from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from test_hif_behavior import PRODUCE, MODULES
from extensions.hif.show_screen import unrotated_show


def unrotated_frame(band_value=160):
    """仿 2026-11-01 实测：720x1280 竖屏截图，演出画面在 y=487..793，y≈300/960 各一行控件。"""
    image = np.zeros((1280, 720, 3), np.uint8)
    image[487:793] = band_value
    image[300:330, 20:170] = 230
    image[945:975, 30:135] = 230
    return image


class UnrotatedShowTest(unittest.TestCase):
    def test_detects_letterboxed_portrait_show(self):
        self.assertTrue(unrotated_show(unrotated_frame()))
        dark = unrotated_frame(band_value=60)
        dark[600:700] = 0
        self.assertTrue(unrotated_show(dark))

    def test_rejects_normal_and_black_screens(self):
        self.assertFalse(unrotated_show(np.zeros((1280, 720, 3), np.uint8)))
        self.assertFalse(unrotated_show(np.full((1280, 720, 3), 200, np.uint8)))
        self.assertFalse(unrotated_show(unrotated_frame()[:720, :720]))
        self.assertFalse(unrotated_show(np.zeros((720, 1280, 3), np.uint8)))
        logo = np.zeros((1280, 720, 3), np.uint8)
        logo[600:680, 300:420] = 255
        self.assertFalse(unrotated_show(logo))
        subtitle = unrotated_frame()
        subtitle[1100:1140, 200:520] = 255
        self.assertFalse(unrotated_show(subtitle))

    def test_skip_action_waits_without_clicking_on_unrotated_show(self):
        clicks = []
        frames = iter([unrotated_frame(), np.zeros((1280, 720, 3), np.uint8)])
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: next(frames))),
            post_click=lambda x, y: clicks.append((x, y)))
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False))
        action = PRODUCE.ProduceHIF__ProduceSkipShowAuto()
        with patch.object(MODULES['schedule'].time, 'sleep') as sleep:
            self.assertTrue(action.run(context, SimpleNamespace(node_name='test')))
            sleep.assert_called_once_with(action.UNROTATED_WAIT)
            self.assertTrue(action.run(context, SimpleNamespace(node_name='test')))
            sleep.assert_called_once()
        self.assertEqual(clicks, [])


if __name__ == '__main__':
    unittest.main()
