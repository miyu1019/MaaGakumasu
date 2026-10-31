"""定制仅在刷新前后检查余额，保持连续定制流程。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_hif_behavior import COMMON, PRODUCE, hif_patch

Custom = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto
Consult = PRODUCE.ProduceHIF__ProduceHIFConsultAuto


class ShopBudgetTest(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))

    def test_refresh_checks_balance_before_and_after_one_click(self):
        for before, after, clicks, ended in (
            (None, None, 0, True), (0, None, 0, True), (39, None, 0, True),
            (40, 30, 1, True), (49, 39, 1, True), (50, 40, 1, False),
            (80, 60, 1, False), (80, 80, 1, True), (80, 90, 1, True),
            (80, None, 1, True),
        ):
            with self.subTest(before=before, after=after):
                action = Custom()
                action._queue = ["目标卡"]
                action._cur_card = "目标卡"
                action._click = Mock()
                # 刷新成功后名额稍后才显示，期间不可再次点击刷新。
                action._read_left = Mock(side_effect=[0, None, 2, 2])
                values = [before, after] if clicks else [before]
                with patch.object(Consult, "_wait_shop_points", side_effect=values) as read, \
                        hif_patch("_hif_nonbattle_screencap", return_value=object()), \
                        patch.object(COMMON.time, "sleep"):
                    self.assertEqual(action._refresh_custom_shop(self.context, object()), "refresh")
                self.assertEqual(action._click.call_count, clicks)
                self.assertEqual(read.call_count, 1 + clicks)
                self.assertEqual(action._done_all, ended)
                self.assertFalse(action.done_cards)
                if ended:
                    self.assertTrue(action._shop_consumption_stopped)
                    self.assertIn("目标卡", action.skipped_cards)

    def test_deducted_balance_with_unreadable_slots_never_repeats_refresh(self):
        action = Custom()
        action._queue = ["目标卡"]
        action._click = Mock()
        action._read_left = Mock(return_value=None)
        with patch.object(Consult, "_wait_shop_points", side_effect=[80, 60]), \
                hif_patch("_hif_nonbattle_screencap", return_value=object()), \
                patch.object(COMMON.time, "sleep"):
            self.assertEqual(action._refresh_custom_shop(self.context, object()), "refresh")
        action._click.assert_called_once()
        self.assertTrue(action._done_all)
        self.assertIn("刷新成功", action.skipped_cards["目标卡"])

    def test_stop_during_refresh_does_not_read_or_click_again(self):
        action = Custom()
        action._click = Mock(side_effect=lambda *_a, **_kw: setattr(self.context.tasker, "stopping", True))
        with patch.object(Consult, "_wait_shop_points", return_value=80) as read, \
                hif_patch("_hif_nonbattle_screencap") as capture:
            self.assertEqual(action._refresh_custom_shop(self.context, object()), "stopped")
        action._click.assert_called_once()
        read.assert_called_once()
        capture.assert_not_called()

    def test_existing_slots_enter_without_balance_reads(self):
        action = Custom()
        action._read_left = Mock(return_value=1)
        action._click = Mock()
        self.context.run_recognition = lambda *_a: SimpleNamespace(hit=False)
        with patch.object(Consult, "_wait_shop_points") as read, \
                hif_patch("_hif_nonbattle_screencap", return_value=object()), \
                patch.object(COMMON.time, "sleep"):
            self.assertEqual(action._handle_main(self.context, object()), "enter")
        read.assert_not_called()
        action._click.assert_called_once_with(self.context, action.CUSTOMIZE_POS, delay=1.0)

    def test_confirmed_submission_continues_current_card_without_returning_to_shop(self):
        action = Custom()
        action.custom_cards = {"目标卡": {"menu": []}}
        action._load_custom_cards = Mock()
        action._customization_enabled = Mock(return_value=True)
        action._heal_before_customization = Mock(return_value=True)
        events = []
        action._handle_main = Mock(side_effect=lambda *_a: events.append("main") or ("enter" if len(events) == 1 else "exit"))
        action._handle_select = Mock(side_effect=["detail", "back"])
        action._handle_detail_execute = Mock(side_effect=lambda *_a: events.append("execute") or ("confirm" if events.count("execute") == 1 else "list"))
        action._handle_execution_confirmation = Mock(return_value="execute")
        action._wait_custom_page = Mock(return_value=True)
        action._click = Mock()
        with hif_patch("_hif_nonbattle_screencap", return_value=object()), \
                patch.object(PRODUCE.ProduceHIF__ProduceCardsAuto, "_load_hif_profession", return_value="集中"), \
                patch.object(Consult, "_wait_shop_points") as read:
            self.assertTrue(action._run_inner(self.context, None))
        self.assertEqual(events, ["main", "execute", "execute", "main"])
        action._click.assert_not_called()
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
