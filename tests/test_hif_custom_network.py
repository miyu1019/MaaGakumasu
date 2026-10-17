"""定制提交后的通信等待：恢复后确认原操作，不重放提交。"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from tests.test_hif_behavior import PRODUCE


class CustomNetworkTest(unittest.TestCase):
    def setUp(self):
        PRODUCE._hif_recovery.reset(None)
        self.clock = [0.0]
        self.context = NS(tasker=NS(stopping=False))
        self.action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        self.action._queue = ["目标卡+"]
        self.action._cur_card = "目标卡+"
        self.action._last_total = 2
        self.action._pending_option = 1
        self.action._option_counts = {1: 0}
        PRODUCE._hif_recovery.pending = "定制:目标卡+:1"
        self.action._confirm_completed = Mock(return_value=False)
        self.action._read_total = Mock(side_effect=lambda _ctx, frame: 1 if frame == "result" else None)
        self.action._confirmation_network_busy = Mock(side_effect=lambda _ctx, frame: frame == "connecting")
        self.action._click = Mock()

    def tearDown(self):
        PRODUCE._hif_recovery.reset(None)

    def run_confirmation(self, first, capture, sleep=None):
        def advance(seconds):
            self.clock[0] += seconds
        with patch.object(PRODUCE.time, "monotonic", side_effect=lambda: self.clock[0]), \
                patch.object(PRODUCE.time, "sleep", side_effect=sleep or advance), \
                patch.object(PRODUCE, "_hif_nonbattle_screencap", side_effect=capture) as frames, \
                patch.object(PRODUCE, "_hif_failure", return_value=False) as failure:
            result = self.action._handle_execution_confirmation(self.context, first)
        self.action._click.assert_not_called()
        return result, frames, failure

    def test_connecting_for_20_seconds_then_confirms_original_submission(self):
        def capture(_context, **_kw):
            return "connecting" if self.clock[0] < 20 else "result"
        result, frames, failure = self.run_confirmation("connecting", capture)
        self.assertEqual(result, "execute")
        self.assertEqual(self.action._option_counts, {1: 1})
        self.assertIsNone(self.action._pending_option)
        self.assertIsNone(PRODUCE._hif_recovery.pending)
        self.assertGreaterEqual(self.clock[0], 21)
        failure.assert_not_called()
        self.assertTrue(all(call.kwargs == {"retry_limit": 1} for call in frames.call_args_list))

    def test_retry_near_old_deadline_gets_fresh_confirmation_time(self):
        retried = [False]
        def capture(_context, **_kw):
            if self.clock[0] >= 9.5 and not retried[0]:
                retried[0] = True
                self.clock[0] += 3
                PRODUCE._hif_recovery.paused += 3
            return "result" if self.clock[0] >= 20 else "unknown"
        result, _frames, failure = self.run_confirmation("unknown", capture)
        self.assertTrue(retried[0])
        self.assertEqual(result, "execute")
        self.assertEqual(self.action._option_counts, {1: 1})
        self.assertGreaterEqual(self.clock[0], 21)
        failure.assert_not_called()

    def test_network_timeout_keeps_pending_submission_and_does_not_navigate(self):
        result, _frames, failure = self.run_confirmation("connecting", lambda *_a, **_kw: "connecting")
        self.assertEqual(result, "failed")
        self.assertEqual(self.clock[0], 30)
        self.assertEqual(self.action._option_counts, {1: 0})
        self.assertIsNotNone(self.action._pending_option)
        self.assertIsNotNone(PRODUCE._hif_recovery.pending)
        failure.assert_called_once()
        self.assertEqual(failure.call_args.args[2], "定制通信等待")

    def test_stop_during_network_wait_cancels_without_capture_or_failure(self):
        def stop(_seconds):
            self.context.tasker.stopping = True
        result, frames, failure = self.run_confirmation("connecting", Mock(), sleep=stop)
        self.assertEqual(result, "stopped")
        frames.assert_not_called()
        failure.assert_not_called()
        self.assertEqual(self.action._option_counts, {1: 0})

    def test_repeated_retries_cannot_extend_confirmation_indefinitely(self):
        def capture(_context, **_kw):
            self.clock[0] += 3
            PRODUCE._hif_recovery.paused += 3
            return "unknown"
        result, _frames, failure = self.run_confirmation("unknown", capture)
        self.assertEqual(result, "failed")
        self.assertGreaterEqual(self.clock[0], 40)
        self.assertLessEqual(self.clock[0], 43)
        self.assertIsNotNone(self.action._pending_option)
        self.assertEqual(self.action._option_counts, {1: 0})
        failure.assert_called_once()

    def test_single_capture_cannot_retry_network_forever(self):
        controller = NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: "network")))
        self.context.tasker.controller = controller
        with patch.object(PRODUCE, "_hif_retry_network", return_value=True) as retry:
            self.assertEqual(PRODUCE._hif_nonbattle_screencap(self.context, retry_limit=1), "network")
        retry.assert_called_once()

    def test_detector_reads_only_bottom_right_connecting_label(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        for text, expected in (("CONNECTING", True), ("connecting", True), ("通信中", False), ("", False)):
            def recognize(name, _image, pipeline_override):
                self.assertEqual(pipeline_override[name]["roi"], [500, 1200, 220, 80])
                return NS(hit=bool(text), filtered_results=[NS(text=text)])
            self.context.run_recognition = recognize
            self.assertEqual(action._confirmation_network_busy(self.context, object()), expected)


if __name__ == "__main__":
    unittest.main()
