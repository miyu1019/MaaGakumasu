from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from test_hif_behavior import PRODUCE, COMMON, hif_patch


# 2026-11-04 14:30 实测饮料栏圆环模板 all_results：(框左边缘, 分数)。
# 第一槽ジンジャーエール浅色圆环在 0.76~0.89 间跳动，后四帧单帧漏检。
BEFORE_USE = [
    ((27, .851), (31, .608), (111, .932), (118, .769), (120, .625), (197, .943), (204, .774), (208, .630)),
    ((27, .844), (34, .630), (112, .925), (117, .700), (125, .553), (197, .924), (203, .731), (206, .599), (288, .699), (288, .560)),
    ((32, .779), (35, .608), (112, .912), (117, .755), (118, .606), (197, .908), (203, .756), (205, .608), (289, .731), (290, .563)),
    ((31, .639), (32, .781), (112, .914), (117, .754), (120, .618), (197, .934), (203, .739), (206, .606), (290, .651)),
    ((27, .787), (31, .590), (112, .916), (117, .602), (197, .920), (203, .746), (205, .599), (288, .537), (289, .719)),
    ((27, .761), (31, .562), (112, .911), (117, .753), (118, .610), (197, .920), (203, .752), (205, .618), (287, .619), (287, .556)),
]
AFTER_USE = [
    ((26, .523), (27, .848), (112, .939), (290, .523)),
    ((27, .799), (35, .622), (112, .935), (206, .552), (290, .599)),
    ((27, .844), (31, .532), (111, .939), (117, .544), (202, .614), (287, .590)),
    ((26, .509), (27, .840), (111, .945), (118, .575), (202, .612), (287, .596)),
]
CUP_DX = 19


def frame(rings, cups=()):
    return {'rings': [NS(box=[x, 1165, 70, 70], score=s) for x, s in rings],
            'cups': [NS(box=[x + CUP_DX, 1178, 33, 43], score=.75) for x in cups]}


class BattleDrinkSlotTest(unittest.TestCase):
    def setup(self, frames):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        state = {'index': -1}
        def screen():
            state['index'] += 1
            return frames[min(state['index'], len(frames) - 1)]
        def recognize(node, image):
            if node == 'ProduceHIF__ProduceCheckDrinkButton':
                hits = [r for r in image['rings'] if r.score >= .8]
                return NS(hit=bool(hits), all_results=image['rings'], filtered_results=hits)
            if node == 'ProduceHIF__ProduceDrinkEmptySlot':
                return NS(hit=bool(image['cups']), all_results=image['cups'], filtered_results=image['cups'])
            raise AssertionError(node)
        controller = NS(post_screencap=lambda: NS(wait=lambda: NS(get=screen)))
        context = NS(tasker=NS(controller=controller, stopping=False), run_recognition=recognize)
        return action, context, state

    def test_recorded_flicker_before_use_counts_three_bottles(self):
        action, context, _ = self.setup([frame(r, cups=(282,)) for r in BEFORE_USE])
        with patch.object(COMMON.time, 'sleep'):
            boxes = action._battle_drink_boxes(context)
        self.assertEqual([box[0] for box in boxes], [27, 111, 197])

    def test_only_missed_frames_keep_first_slot_uncertain_instead_of_counting_two(self):
        action, context, state = self.setup([frame(r, cups=(282,)) for r in BEFORE_USE[2:]])
        with patch.object(COMMON.time, 'sleep'):
            self.assertIsNone(action._battle_drink_boxes(context))
        self.assertEqual(state['index'] + 1, action.DRINK_SLOT_MAX_FRAMES)

    def test_recorded_frames_after_use_count_two_bottles(self):
        action, context, _ = self.setup([frame(r, cups=(197, 282)) for r in AFTER_USE])
        with patch.object(COMMON.time, 'sleep'):
            boxes = action._battle_drink_boxes(context)
        self.assertEqual([box[0] for box in boxes], [27, 111])

    def test_single_frame_rejects_gap_and_conflicting_evidence(self):
        action, context, _ = self.setup([])
        gap = frame(((27, .9), (197, .9)), cups=(111, 282))
        self.assertIsNone(action._battle_drink_boxes(context, gap))
        missed = frame(AFTER_USE[1], cups=(197, 282))
        self.assertIsNone(action._battle_drink_boxes(context, missed))
        self.assertEqual(action._merge_slot_evidence({'drink', 'cup'}), 'unknown')
        self.assertEqual(action._merge_slot_evidence({'low'}), 'empty')
        self.assertEqual(action._merge_slot_evidence({'low', 'weak'}), 'unknown')

    def test_same_name_use_is_confirmed_by_count_and_left_contiguous_bar(self):
        before = [frame(r, cups=(282,)) for r in BEFORE_USE]
        after = [frame(r, cups=(197, 282)) for r in AFTER_USE]
        action, context, state = self.setup(before)
        bar = {'frames': before}
        state_frames = lambda: bar['frames'][min(state['index'], len(bar['frames']) - 1)]
        def screen():
            state['index'] += 1
            return state_frames()
        context.tasker.controller = NS(post_screencap=lambda: NS(wait=lambda: NS(get=screen)))
        names = ['烏龍茶', '初星スーパーソーダ', '初星スーパーソーダ']
        opened, used = [], []
        def open_detail(_context, pos, _label):
            opened.append(pos)
            return True
        action._open_drink_detail = open_detail
        action._read_battle_drink_identity = lambda _c: (names[len(opened) - 1],) * 2
        action._close_drink_detail = lambda _c: True
        def use(_c):
            used.append(names[len(opened) - 1])
            bar['frames'], state['index'] = after, -1
            return True
        action._use_drink = use
        action._wait_for_battle_drink_return = lambda _c: True
        action._wait_for_drink_list_stable = lambda *_a, **_k: True
        logs = []
        with patch.object(COMMON.time, 'sleep'), hif_patch('logger', NS(info=logs.append, warning=lambda *_: None)):
            self.assertTrue(action._consume_battle_drinks(context, ['初星スーパーソーダ'], 'test', max_uses=1))
        self.assertEqual(opened, [(62, 1200), (146, 1200)])
        self.assertEqual(used, ['初星スーパーソーダ'])
        self.assertTrue(any('3→2' in message for message in logs))

    def test_uncertain_bar_skips_drinks_without_opening_any_bottle(self):
        action, context, _ = self.setup([frame(r, cups=(282,)) for r in BEFORE_USE[2:]])
        action._wait_for_drink_list_stable = lambda *_a, **_k: True
        action._open_drink_detail = lambda *_a: self.fail('瓶数不明时不可点开饮料')
        with patch.object(COMMON.time, 'sleep'):
            self.assertFalse(action._consume_battle_drinks(context, ['初星スーパーソーダ'], 'test'))


if __name__ == '__main__':
    unittest.main()
