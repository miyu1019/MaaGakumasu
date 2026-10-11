import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_hif_behavior import PRODUCE


class SupplySkipTests(unittest.TestCase):
    def replay(self, outcome, after_clicks):
        action = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto()
        clicks, sleeps = [], []
        context = NS(tasker=NS(stopping=False), run_action=clicks.append)
        def screenshot(_):
            if outcome == 'stopped' and len(clicks) >= after_clicks:
                context.tasker.stopping = True
            return outcome if len(clicks) >= after_clicks else 'scene'
        action._is_card_screen = lambda _, image: image == 'card'
        action._is_supply_scene = lambda _, image: image in ('scene', 'stopped')
        with patch.object(PRODUCE, '_hif_nonbattle_screencap', side_effect=screenshot), \
                patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, '_is_receive_drink_screen', side_effect=lambda _, image: image == 'drink'), \
                patch.object(PRODUCE.time, 'sleep', side_effect=sleeps.append):
            result = action.run(context, None)
        return result, clicks, sleeps

    def test_checks_each_click_and_stops_immediately_on_either_reward_page(self):
        for outcome in ('card', 'drink'):
            with self.subTest(outcome=outcome):
                result, clicks, sleeps = self.replay(outcome, 3)
                self.assertTrue(result)
                self.assertEqual(clicks, ['ProduceHIF__Click_1'] * 3)
                self.assertEqual(sleeps, [0.4] * 3)

    def test_no_click_after_leaving_supply_or_manual_stop(self):
        for outcome in ('lesson', 'stopped'):
            with self.subTest(outcome=outcome):
                result, clicks, _ = self.replay(outcome, 1)
                self.assertTrue(result)
                self.assertEqual(clicks, ['ProduceHIF__Click_1'])

    def test_checks_last_click_before_reporting_limit(self):
        result, clicks, _ = self.replay('drink', 20)
        self.assertTrue(result)
        self.assertEqual(len(clicks), 20)
        result, clicks, sleeps = self.replay('scene', 100)
        self.assertFalse(result)
        self.assertEqual(len(clicks), 20)
        self.assertEqual(sleeps, [0.4] * 20)


if __name__ == '__main__':
    unittest.main()
