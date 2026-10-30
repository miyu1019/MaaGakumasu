"""Date changes unwind old actions, then resume through the normal home entry."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, call, patch

from test_hif_behavior import PRODUCE, ROOT


PREFIX = 'ProduceHIF__'
NODES = json.loads((ROOT / 'extensions/hif/resource/base/pipeline/HIF.json').read_text(encoding='utf-8'))
LOGIN_NODES = json.loads((ROOT / 'extensions/hif/resource/base/pipeline/HIFDateLogin.json').read_text(encoding='utf-8'))


class DateRecoveryTest(unittest.TestCase):
    def setUp(self):
        PRODUCE._hif_recovery.reset(None)
        self.clock = [0.0]
        self.clicks = []
        self.scene = 'date'
        self.argv = NS(node_name=PREFIX + 'DateRecover', task_detail=NS(task_id=17))
        self.context = NS(
            tasker=NS(stopping=False, controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: self.scene)))),
            run_recognition=self.recognize, run_action=self.act,
            get_node_data=LOGIN_NODES.get,
        )

    def tearDown(self):
        PRODUCE._hif_recovery.reset(None)

    def recognize(self, name, image):
        hits = {
            'date': {PREFIX + 'DateChanged'}, 'title': {PREFIX + 'DateTitle', PREFIX + 'ClickCenter'},
            'bonus': {PREFIX + 'StartUpLoginBonus'},
            'home': {PREFIX + 'ProduceHomeFlag', PREFIX + 'ProduceContinue'},
            'empty_home': {PREFIX + 'ProduceHomeFlag'},
            'choose': {PREFIX + 'ProduceChooseContinue'},
        }
        return NS(hit=name in hits.get(image, set()), box=[230, 1115, 265, 90])

    def act(self, name, box):
        self.clicks.append(name)
        self.scene = {
            PREFIX + 'DateChanged': 'title', PREFIX + 'ClickCenter': 'bonus', PREFIX + 'StartUpLoginBonus': 'home',
            PREFIX + 'ProduceContinue': 'choose', PREFIX + 'ProduceChooseContinue': 'battle',
        }[name]
        return NS(success=True)

    def recover(self):
        def sleep(seconds):
            self.clock[0] += seconds
        with patch.object(PRODUCE.time, 'monotonic', side_effect=lambda: self.clock[0]), \
                patch.object(PRODUCE.time, 'sleep', side_effect=sleep), \
                patch.object(PRODUCE, '_hif_network_dialog_possible', side_effect=lambda image: image == 'date'), \
                patch.object(PRODUCE, '_hif_safe_scene', side_effect=lambda _ctx, image, **_kw: PREFIX + 'ProduceHIFCardsFlag' if image == 'battle' else None), \
                patch.object(PRODUCE, '_hif_failure', return_value=False) as failure:
            result = PRODUCE.ProduceHIF__DateRecoverAuto().run(self.context, self.argv)
        return result, failure

    def test_date_and_title_resume_without_loop_counter_or_skip_option(self):
        for initial in ('date', 'title'):
            for skip in (False, True):
                with self.subTest(initial=initial, saved_skip=skip):
                    self.setUp()
                    self.scene = initial
                    saved_skip = {'enabled': skip, 'max_hit': 1}
                    original_get = self.context.get_node_data
                    self.context.get_node_data = Mock(side_effect=lambda name: saved_skip if name == PREFIX + 'ProduceSkipPreparation' else original_get(name))
                    PRODUCE._hif_recovery.bind(self.argv).pending = '定制:旧操作'
                    PRODUCE._hif_recovery.deadline = -1
                    result, failure = self.recover()
                    self.assertTrue(result)
                    failure.assert_not_called()
                    self.assertEqual(saved_skip, {'enabled': skip, 'max_hit': 1})
                    self.assertEqual(self.context.get_node_data.call_args_list, [call(PREFIX + 'StartUpTestScreen')] * 2)
                    expected = [PREFIX + 'ClickCenter', PREFIX + 'StartUpLoginBonus', PREFIX + 'ProduceContinue', PREFIX + 'ProduceChooseContinue']
                    self.assertEqual(self.clicks, ([PREFIX + 'DateChanged'] if initial == 'date' else []) + expected)
                    self.assertIsNone(PRODUCE._hif_recovery.pending)
                    self.assertIsNone(PRODUCE._hif_recovery.deadline)
                    self.assertEqual(PRODUCE._hif_recovery.task_id, 17)

    def test_missing_login_node_reports_actual_cause_without_waiting_180_seconds(self):
        self.scene = 'title'
        self.context.get_node_data = Mock(return_value=None)
        result, failure = self.recover()
        self.assertFalse(result)
        self.context.get_node_data.assert_called_once_with(PREFIX + 'StartUpTestScreen')
        self.assertIn('StartUpTestScreen 不存在', failure.call_args.args[-1])
        self.assertEqual(self.clock[0], 0)
        self.assertEqual(self.clicks, [])

    def test_unknown_login_screen_never_uses_unconditional_title_click(self):
        self.scene = 'unknown'
        original = self.context.run_recognition
        self.context.run_recognition = lambda name, image: NS(hit=True, box=[0, 0, 1, 1]) if name == PREFIX + 'ClickCenter' else original(name, image)
        result, failure = self.recover()
        self.assertFalse(result)
        self.assertEqual(self.clicks, [])
        failure.assert_called_once()

    def test_isolated_login_resources_have_no_upstream_dependencies(self):
        nodes = {}
        for path in (ROOT / 'extensions/hif/resource/base/pipeline').glob('*.json'):
            nodes.update(json.loads(path.read_text(encoding='utf-8')))
        startup = nodes[PREFIX + 'StartUpTestScreen']
        for name in [*startup['next'], PREFIX + 'StartUpCloseButton']:
            self.assertTrue(name.startswith(PREFIX))
            node = nodes[name]
            self.assertIn(node['action'], ('Click', 'DoNothing'))
            self.assertNotIn('next', node)
            if 'template' in node:
                self.assertTrue((ROOT / 'extensions/hif/resource/base/image' / node['template']).is_file())

    def test_missing_save_fails_without_starting_new_produce(self):
        self.scene = 'empty_home'
        result, failure = self.recover()
        self.assertFalse(result)
        self.assertEqual(self.clicks, [])
        self.assertIn('没有继续育成', failure.call_args.args[-1])

    def test_total_deadline_is_independent_of_network_pause_budget(self):
        self.scene = 'unknown'
        PRODUCE._hif_recovery.paused = 10000
        result, failure = self.recover()
        self.assertFalse(result)
        self.assertEqual(self.clock[0], 180)
        failure.assert_called_once()

    def test_stop_does_not_click(self):
        self.context.tasker.stopping = True
        result, failure = self.recover()
        self.assertFalse(result)
        self.assertEqual(self.clicks, [])
        failure.assert_not_called()

    def test_old_animation_guards_are_cleared_without_resetting_business_counts(self):
        train = PRODUCE.ProduceHIF__ProduceHIFTrainAuto
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        with patch.object(train, '_recognition_blocked_day', 5), \
                patch.object(swap, '_delete_card_exchange_count', 1), \
                patch.object(PRODUCE.ProduceHIF__ProduceHIFLessonAuto, '_recognition_gate_until', 9999999999):
            result, failure = self.recover()
            self.assertTrue(result)
            failure.assert_not_called()
            self.assertIsNone(train._recognition_blocked_day)
            self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFLessonAuto._recognition_gate_until, 0)
            self.assertEqual(swap._delete_card_exchange_count, 1)

    def test_slow_login_uses_independent_180_second_budget(self):
        self.scene = 'loading'
        original_get = self.context.get_node_data
        def get_node(name):
            if self.clock[0] >= 40 and self.scene == 'loading':
                self.scene = 'title'
            return original_get(name)
        self.context.get_node_data = get_node
        PRODUCE._hif_recovery.bind(self.argv).deadline = -1
        result, failure = self.recover()
        self.assertTrue(result)
        self.assertGreaterEqual(self.clock[0], 40)
        failure.assert_not_called()

    def test_actual_battle_and_customization_callbacks_unwind_broad_handlers(self):
        for cls, method in (
            (PRODUCE.ProduceHIF__ProduceCardsAuto, '_run_battle'),
            (PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto, '_run_inner'),
        ):
            with self.subTest(action=cls.__name__):
                action = cls()
                with patch.object(action, method, side_effect=PRODUCE._HifDateChanged), \
                        patch.object(PRODUCE, '_hif_failure') as failure:
                    self.assertFalse(action.run(self.context, self.argv))
                self.assertTrue(PRODUCE._hif_recovery.date_requested)
                self.assertEqual(self.clicks, [])
                failure.assert_not_called()

    def test_network_remains_in_place_and_date_never_clicks_old_action(self):
        self.context.run_recognition = Mock(side_effect=lambda node, _: NS(
            hit=node == PREFIX + 'DateChanged', box=[0, 0, 1, 1]))
        with patch.object(PRODUCE, '_hif_network_dialog_possible', return_value=True):
            with self.assertRaises(PRODUCE._HifDateChanged):
                PRODUCE._hif_retry_network(self.context, 'date')
        self.assertEqual(self.clicks, [])
        self.context.run_recognition.assert_any_call(PREFIX + 'ProduceNetworkRetry', 'date')

    def test_all_network_edges_and_custom_actions_have_date_escape(self):
        for name, node in NODES.items():
            children = node.get('next', [])
            if '[JumpBack]' + PREFIX + 'ProduceNetworkRetry' in children:
                index = children.index('[JumpBack]' + PREFIX + 'ProduceNetworkRetry')
                self.assertEqual(children[index + 1], PREFIX + 'DateRecover', name)
            action = node.get('action', {})
            if isinstance(action, dict):
                callback = action.get('param', {}).get('custom_action', '')
                if callback.startswith(PREFIX) and callback != PREFIX + 'DateRecoverAuto':
                    self.assertEqual(node['on_error'][0], PREFIX + 'DateRequested', name)
        self.assertEqual(NODES[PREFIX + 'DateRecover']['next'], [PREFIX + 'ProduceEntryHIF'])
        self.assertEqual(NODES[PREFIX + 'DateRequested']['next'], [PREFIX + 'ProduceEntryHIF'])


if __name__ == '__main__':
    unittest.main()
