"""定制商店内的超时：只读宽限后交回入口；回血/定制结果待确认时不交回、不重放。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_hif_behavior import COMMON, PRODUCE, hif_patch

CUSTOM = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto


class CustomShopGraceTest(unittest.TestCase):
    def setUp(self):
        PRODUCE._hif_recovery.reset(None)
        CUSTOM._skip_heal_until = 0.0
        self.addCleanup(setattr, CUSTOM, '_skip_heal_until', 0.0)
        self.addCleanup(PRODUCE._hif_recovery.reset, None)

    def context(self):
        return SimpleNamespace(tasker=SimpleNamespace(stopping=False), get_node_data=lambda *_a: {'enabled': True},
                               run_recognition=lambda *_a, **_kw: SimpleNamespace(hit=True))

    def heal(self, closes):
        """点回復する后 closes 决定商店是否恢复；返回结果与点击记录。"""
        action, confirmed, clicks = CUSTOM(), [False], []
        action._heal_dialog_visible = lambda *_a: not confirmed[0]
        action._heal_plus_pos = lambda *_a: action.HEAL_PLUS_POS
        action._heal_button_pos = lambda *_a, cancel=False: action.HEAL_CANCEL_POS if cancel else action.HEAL_CONFIRM_POS
        def click(_context, pos, **_kw):
            clicks.append(pos)
            if pos == action.HEAL_CONFIRM_POS:
                confirmed[0] = True
        action._click = click
        action._wait_heal_shop = lambda *_a: closes
        with hif_patch('_hif_nonbattle_screencap', return_value=object()), patch.object(COMMON.time, 'sleep'):
            return action._heal_before_customization(self.context()), clicks

    def test_heal_submit_is_pending_until_shop_returns(self):
        result, clicks = self.heal(closes=True)
        self.assertTrue(result)
        self.assertIsNone(PRODUCE._hif_recovery.pending)
        result, clicks = self.heal(closes=False)
        self.assertFalse(result)
        self.assertEqual(PRODUCE._hif_recovery.pending, '中场回血')
        self.assertEqual(clicks.count(CUSTOM.HEAL_CONFIRM_POS), 1)

    def test_pending_heal_stops_without_hand_back_or_exit_click(self):
        action = CUSTOM()
        action._click = lambda *_a, **_kw: self.fail('回血结果待确认时不能点击')
        PRODUCE._hif_recovery.pending = '中场回血'
        with hif_patch('_hif_failure', return_value=False) as failure, hif_patch('_hif_timeout') as timeout:
            self.assertFalse(action._skip_customization(self.context(), '中场回血后商店未恢复'))
        failure.assert_called_once()
        timeout.assert_not_called()
        # 真实宽限函数遇到无原页面判定的待确认操作也直接停止。
        with hif_patch('_hif_failure', return_value=False) as failure:
            self.assertFalse(action._hand_back(self.context(), '离店前回血', 'timeout'))
        failure.assert_called_once()
        self.assertEqual(CUSTOM._skip_heal_until, 0.0)

    def run_shop(self, timeout_result, heal=None, **handlers):
        action = CUSTOM()
        action.custom_cards = {'届いて！+': {'menu': []}}
        action._load_custom_cards = lambda *_a: None
        action._customization_enabled = lambda *_a: True
        action._heal_before_customization = heal or (lambda *_a: True)
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        patches = [patch.object(action, name, return_value=value) for name, value in handlers.items()]
        with hif_patch('_hif_nonbattle_screencap', return_value=object()), \
                hif_patch('_hif_timeout', return_value=timeout_result) as timeout:
            for item in patches:
                item.start()
            try:
                result = action._run_inner(context, None)
            finally:
                for item in patches:
                    item.stop()
        return result, timeout

    def test_unconfirmed_return_page_hands_back_and_ends_action(self):
        for returned in (True, False):
            with self.subTest(returned=returned):
                CUSTOM._skip_heal_until = 0.0
                result, timeout = self.run_shop(returned, _handle_main='enter', _handle_select='back',
                                                _wait_custom_page=False)
                self.assertEqual(result, returned)
                timeout.assert_called_once()
                self.assertEqual(timeout.call_args.args[2], '返回商店')
                self.assertEqual(CUSTOM._skip_heal_until > 0, returned)

    def test_reentry_after_hand_back_skips_one_pre_custom_heal(self):
        heals = []
        result, _timeout = self.run_shop(True, heal=lambda *_a: heals.append(True) or True,
                                         _handle_main='enter', _handle_select='back', _wait_custom_page=False)
        self.assertTrue(result)
        self.assertEqual(heals, [True])
        result, _timeout = self.run_shop(True, heal=lambda *_a: self.fail('宽限交回后重新进店不能再回血'),
                                         _handle_main='exit')
        self.assertTrue(result)
        self.assertEqual(CUSTOM._skip_heal_until, 0.0)
        heals.clear()
        result, _timeout = self.run_shop(True, heal=lambda *_a: heals.append(True) or True, _handle_main='exit')
        self.assertEqual(heals, [True])  # 只跳过一次

    def test_heal_before_exit_hands_back_as_handed_back(self):
        action = CUSTOM()
        action._done_all = True
        action._customization_enabled = lambda *_a: True
        action._heal_before_customization = lambda *_a: False
        action._click = lambda *_a, **_kw: self.fail('回血未确认不能点終了')
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False), run_recognition=lambda *_a: SimpleNamespace(hit=True))
        for returned, expected in ((True, 'handed_back'), (False, 'failed')):
            with self.subTest(returned=returned), \
                    patch.object(PRODUCE.ProduceHIF__ProduceHIFConsultAuto, 'buy_custom_end_drinks'), \
                    hif_patch('_hif_timeout', return_value=returned):
                self.assertEqual(action._handle_main(context, object()), expected)


if __name__ == '__main__':
    unittest.main()
