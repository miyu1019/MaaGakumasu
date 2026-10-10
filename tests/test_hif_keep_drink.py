import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from test_hif_behavior import PRODUCE, ROOT
from extensions.hif import drink_keep as KEEP


class Page:
    def __init__(self, selected=(False, False, True, True), capacity=3, names=('new1', 'new2', 'held1', 'held2')):
        self.capacity, self.selected, self.names = capacity, list(selected), names
        self.offset, self.submitted, self.clicks, self.swipes, self.misses = 0, False, [], [], 0
        self.bounds = [(80, 110), (215, 155), (465, 225), (715, 110)]
        self.height = 860

    def image(self):
        image = np.full((1280, 720, 3), 255, np.uint8)
        if self.submitted:
            return image
        content = np.full((self.height, 720, 3), 255, np.uint8)
        for y, text in ((30, 'Received'), (420, 'Held')):
            cv2.putText(content, text, (60, y), cv2.FONT_HERSHEY_SIMPLEX, .7, (90, 90, 90), 2)
        for i, ((top, height), selected) in enumerate(zip(self.bounds, self.selected)):
            cy = top + height // 2
            color = (185, 237, 255) if selected else (247, 247, 247)
            cv2.rectangle(content, (53, top), (666, top + height), color, -1)
            cv2.rectangle(content, (61, cy - 42), (142, cy + 42), (250, 220, 90), -1)
            cv2.rectangle(content, (82, cy - 23), (113, cy + 24), (30 + i * 30, 65, 90), -1)
            cv2.putText(content, self.names[i], (168, top + 45), cv2.FONT_HERSHEY_SIMPLEX, .6, (40, 40, 40), 2)
            color = (0, 125, 255) if selected else (200, 200, 200)
            cv2.rectangle(content, (616, cy - 13), (642, cy + 13), color, -1)
            cv2.line(content, (621, cy), (626, cy + 5), (255, 255, 255), 3)
            cv2.line(content, (626, cy + 5), (637, cy - 6), (255, 255, 255), 3)
        visible = KEEP.VIEW[3] - KEEP.VIEW[1]
        image[KEEP.VIEW[1]:KEEP.VIEW[3]] = content[self.offset:self.offset + visible]
        thumb = int(visible * visible / self.height)
        bar_top = KEEP.VIEW[1] + int(self.offset * (visible - thumb) / (self.height - visible))
        cv2.rectangle(image, (678, bar_top), (682, bar_top + thumb), (145, 140, 135), -1)
        if self.capacity == sum(self.selected):
            image[1125:1185, 260:345] = (0, 130, 255)
        return image

    def post_screencap(self):
        return SimpleNamespace(wait=lambda: SimpleNamespace(get=self.image))

    def post_swipe(self, x1, y1, x2, y2, duration):
        self.swipes.append((y1, y2))
        self.offset = max(0, min(self.height - (KEEP.VIEW[3] - KEEP.VIEW[1]), self.offset + y1 - y2))
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))

    def post_click(self, x, y):
        self.clicks.append((x, y))
        if y > 1100:
            self.submitted = True
        else:
            index = next(i for i, (top, height) in enumerate(self.bounds)
                         if abs(KEEP.VIEW[1] + top + height // 2 - self.offset - y) < 20)
            if self.misses:
                self.misses -= 1
            else:
                self.selected[index] = not self.selected[index]
        return SimpleNamespace(wait=lambda: None)


def action_for(page):
    def headers(_context, _image):
        return {group: KEEP.VIEW[1] + y - page.offset for group, y in (('received', 30), ('held', 420))
                if KEEP.VIEW[1] <= KEEP.VIEW[1] + y - page.offset < KEEP.VIEW[3]}
    return SimpleNamespace(_screencap=lambda _: page.image(), _window_open=lambda *_: not page.submitted,
        _selection_remaining=lambda *_: page.capacity - sum(page.selected),
        _keep_button_enabled=PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto._keep_button_enabled,
        _page_headers=headers, _log_throttled=lambda *_: None, KEEP_POS=(360, 1158),
        _keep_after_submit_state=lambda *_: ('page', None) if page.submitted else ('cap', None),
        _resolve_keep_name=lambda _c, row: page.names[next(i for i, (top, h) in enumerate(page.bounds)
            if abs(KEEP.VIEW[1] + top + h // 2 - page.offset - row['y']) < 20)])


class KeepDrinkTest(unittest.TestCase):
    def run_flow(self, page, priority=None, disabled=(), modify=None):
        action = action_for(page)
        context = SimpleNamespace(tasker=SimpleNamespace(controller=page, stopping=False))
        flow = KEEP.KeepDrinkFlow(action, context, priority, disabled)
        if modify:
            modify(flow, action)
        clock = [10.0]
        with patch.object(KEEP.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(KEEP.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = flow.run()
        return result, flow

    def test_missing_one_with_all_held_selected_supplements_received_without_identity(self):
        page = Page()
        result, flow = self.run_flow(page, modify=lambda _f, a: setattr(a, '_resolve_keep_name',
            lambda *_: self.fail('Basic mode must not identify names')))
        self.assertTrue(result)
        self.assertEqual(page.selected, [True, False, True, True])
        self.assertEqual(flow.capacity, 3)
        self.assertEqual(page.clicks[-1], (360, 1158))
        self.assertEqual(sum(y > 1100 for _, y in page.clicks), 1)

    def test_missing_two_uses_unselected_held_then_received_and_handles_hidden_row(self):
        page = Page(selected=(False, False, True, False))
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.selected, [True, False, True, True])
        self.assertLess(page.clicks[1][1], page.clicks[0][1])

    def test_ready_basic_page_does_not_scan_or_rank(self):
        page = Page(selected=(True, False, True, True))
        self.assertTrue(self.run_flow(page, modify=lambda f, _a: setattr(f, 'scan', lambda: self.fail('Already ready')))[0])
        self.assertEqual(page.swipes, [])
        self.assertEqual(page.clicks, [(360, 1158)])

    def test_missed_click_retries_same_item_but_submits_once(self):
        page = Page()
        page.misses = 1
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.clicks[0], page.clicks[1])
        self.assertEqual(sum(y > 1100 for _, y in page.clicks), 1)

    def test_transient_missing_row_after_click_waits_without_retoggle(self):
        page, hidden = Page(), [0]
        original = KEEP.page_rows
        def rows(image):
            if page.clicks and hidden[0] < 2:
                hidden[0] += 1
                return []
            return original(image)
        with patch.object(KEEP, 'page_rows', side_effect=rows):
            self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(len(page.clicks), 2)
        self.assertEqual(page.selected, [True, False, True, True])

    def test_priority_replaces_low_held_and_same_rank_preserves_held(self):
        page = Page(selected=(False, False, True, True))
        self.assertTrue(self.run_flow(page, ['new2', 'held1', 'new1', 'held2'])[0])
        self.assertEqual(page.selected, [True, True, True, False])
        entries = [{'name': 'a', 'group': 'received'}, {'name': 'a', 'group': 'held'},
                   {'name': 'b', 'group': 'held'}, {'name': 'bad', 'group': 'held'}]
        self.assertEqual(KEEP.keep_indices(entries, 3, ['a', 'b'], ['bad']), {0, 1, 2})

    def test_unknown_name_falls_back_to_basic(self):
        page = Page()
        self.assertTrue(self.run_flow(page, ['new2'], modify=lambda _f, a: setattr(a, '_resolve_keep_name', lambda *_: None))[0])
        self.assertEqual(page.selected, [True, False, True, True])

    def test_selected_tall_row_with_white_rounded_margin_remains_identifiable(self):
        page = Page(selected=(False, True, True, True), capacity=4)
        image = page.image()
        for top, height in (page.bounds[1], page.bounds[2]):
            y = KEEP.VIEW[1] + top
            image[y:y + 6, 653:658] = 255
            image[y + height - 6:y + height, 653:658] = 255
        rows = KEEP.page_rows(image)
        self.assertEqual([r['selected'] for r in rows], [False, True, True])

    def test_capacity_four_and_duplicate_items_are_not_merged(self):
        page = Page(selected=(False, False, True, True), capacity=4, names=('same', 'same', 'same', 'same'))
        result, flow = self.run_flow(page)
        self.assertTrue(result)
        self.assertEqual(len(flow.entries), 4)
        self.assertEqual(page.selected, [True] * 4)

    def test_fifth_hidden_selected_bottle_counts_toward_capacity_four(self):
        page = Page(selected=(False, False, True, True, True), capacity=4,
                    names=('new1', 'same', 'held1', 'held2', 'same'))
        page.bounds.append((850, 155))
        page.height = 1030
        result, flow = self.run_flow(page)
        self.assertTrue(result)
        self.assertEqual(len(flow.entries), 5)
        self.assertEqual(flow.capacity, 4)
        self.assertEqual(page.selected, [True, False, True, True, True])

    def test_basic_row_inventory_does_not_depend_on_successful_icon_crop(self):
        page = Page()
        image = page.image()
        image[KEEP.VIEW[1]:KEEP.VIEW[3], 59:144] = image[KEEP.VIEW[1]:KEEP.VIEW[3], 655:656]
        rows = KEEP.page_rows(image)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['icon'] is None for row in rows))

    def test_checkbox_changed_without_counter_change_does_not_retry_toggle_or_submit(self):
        page = Page()
        def modify(_f, action):
            action._selection_remaining = lambda *_: 1
        with self.assertRaisesRegex(KEEP.KeepPageError, '选择数未确认'):
            self.run_flow(page, modify=modify)
        self.assertEqual(len(page.clicks), 1)
        self.assertFalse(page.submitted)

    def test_switch_default_off_and_both_task_entries_have_option(self):
        tasks = json.loads((ROOT / 'extensions/hif/tasks/produce_hif.json').read_text(encoding='utf-8'))
        key = 'HIF.所持上限页按优先级保留'
        self.assertEqual(tasks['option'][key]['default_case'], 'No')
        # 前台读取 task.option 字符串列表时默认补 index=0；旧任务必须得到关闭分支。
        cases = tasks['option'][key]['cases']
        self.assertEqual(cases[0]['name'], 'No')
        self.assertFalse(cases[0]['pipeline_override']['ProduceHIF__ProduceHIFKeepDrinkPriority']['enabled'])
        self.assertEqual(cases[1]['name'], 'Yes')
        self.assertTrue(cases[1]['pipeline_override']['ProduceHIF__ProduceHIFKeepDrinkPriority']['enabled'])
        self.assertTrue(all(key in t['option'] for t in tasks['task']))
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        self.assertFalse(action._priority_enabled(SimpleNamespace(get_node_data=lambda _: None)))
        self.assertTrue(action._priority_enabled(SimpleNamespace(get_node_data=lambda _: {'enabled': True})))

    def test_information_name_is_read_above_effect_area_and_detail_closed(self):
        self.check_info_detail('初星スペシャル青汁')

    def test_unknown_information_name_returns_to_list_before_fallback(self):
        self.check_info_detail('未知のドリンク', expected=None)

    def test_information_close_failure_stops_before_next_bottle(self):
        with self.assertRaisesRegex(KEEP.KeepPageError, '未确认关闭'):
            self.check_info_detail('ブーストエキス', stuck=True)

    def check_info_detail(self, name, expected='same', stuck=False):
        state, clicks, clock = ['list'], [], [10.0]
        def click(x, y):
            clicks.append((x, y))
            state[0] = 'detail' if len(clicks) == 1 or stuck else 'list'
            return SimpleNamespace(wait=lambda: None)
        controller = SimpleNamespace(post_click=click)
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False))
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        result = lambda text, box: SimpleNamespace(text=text, box=box)
        detail = [result('Pドリンク詳細', [55, 133, 260, 30]),
                  result(name, [214, 229, 290, 30]), result('閉じる', [337, 1140, 92, 28])]
        action._screencap = lambda _: None
        action._selection_remaining = lambda *_: 1
        action._window_open = lambda *_: state[0] == 'list'
        action._keep_info_results = lambda *_: detail if state[0] == 'detail' else []
        with patch.object(KEEP, 'icon_name', return_value=None), \
             patch.object(KEEP.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(KEEP.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            actual = action._resolve_keep_name(context, {'icon': None, 'info': (132, 388)})
        self.assertEqual(actual, name if expected == 'same' else expected)
        self.assertEqual(state[0], 'list')
        self.assertEqual(clicks, [(132, 388), (383, 1154)])

    def test_ambiguous_scroll_is_bounded_and_never_changes_selection(self):
        page = Page()
        with patch.object(KEEP, 'scroll_delta', return_value=None):
            with self.assertRaisesRegex(KEEP.KeepPageError, '滚动位移'):
                self.run_flow(page)
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.selected, [False, False, True, True])

    def test_priority_failure_restores_initial_selection_then_supplements(self):
        page = Page()
        def modify(flow, _action):
            original = flow.toggle
            failed = [False]
            def toggle(entry, selected):
                if selected and not failed[0]:
                    failed[0] = True
                    raise KEEP.KeepPageError('模拟未生效')
                original(entry, selected)
            flow.toggle = toggle
        self.assertTrue(self.run_flow(page, ['new2', 'held1', 'new1', 'held2'], modify=modify)[0])
        self.assertEqual(page.selected, [True, False, True, True])

    def test_submit_is_not_repeated_when_next_page_cannot_be_confirmed(self):
        page = Page(selected=(True, False, True, True))
        with self.assertRaisesRegex(KEEP.KeepPageError, '不盲目补点'):
            self.run_flow(page, modify=lambda _f, a: setattr(a, '_keep_after_submit_state', lambda *_: ('unknown', None)))
        self.assertEqual(page.clicks, [(360, 1158)])

    def test_reward_animation_taps_several_times_before_confirming_next_page(self):
        page = Page(selected=(True, False, True, True))
        rewards = ['new1', 'new1', 'new2', 'new2']
        original_click = page.post_click
        def click(x, y):
            if (x, y) == (22, 22):
                page.clicks.append((x, y))
                rewards.pop(0)
                return SimpleNamespace(wait=lambda: None)
            return original_click(x, y)
        page.post_click = click
        def modify(_flow, action):
            action._keep_after_submit_state = lambda *_: ('reward', rewards[0]) if rewards else ('page', None)
        self.assertTrue(self.run_flow(page, modify=modify)[0])
        self.assertEqual(page.clicks, [(360, 1158)] + [(22, 22)] * 4)

    def test_second_cap_returns_to_entry_then_uses_a_fresh_flow(self):
        page = Page(selected=(True, False, True, True))
        original_click, stage, keep_count = page.post_click, ['cap1'], [0]
        def click(x, y):
            if y > 1100:
                keep_count[0] += 1
                stage[0] = 'reward' if keep_count[0] == 1 else 'page'
                return original_click(x, y)
            if (x, y) == (22, 22):
                page.clicks.append((x, y))
                stage[0], page.submitted = 'cap2', False
                page.selected = [False, False, True, True]
                page.names = ('second1', 'second2', 'held1', 'held2')
                return SimpleNamespace(wait=lambda: None)
            return original_click(x, y)
        page.post_click = click
        def modify(_flow, action):
            action._keep_after_submit_state = lambda *_: {'cap1': ('cap', None), 'cap2': ('cap', None),
                'reward': ('reward', 'first'), 'page': ('page', None)}[stage[0]]
        first_result, first = self.run_flow(page, modify=modify)
        self.assertTrue(first_result)
        self.assertEqual(stage[0], 'cap2')
        self.assertEqual(keep_count[0], 1)
        second_result, second = self.run_flow(page, modify=modify)
        self.assertTrue(second_result)
        self.assertIsNot(first, second)
        self.assertEqual(second.capacity, 3)
        self.assertEqual(page.selected, [True, False, True, True])
        self.assertEqual(keep_count[0], 2)

    def test_original_cap_not_closed_retries_with_a_bounded_budget(self):
        page = Page(selected=(True, False, True, True))
        def click(x, y):
            page.clicks.append((x, y))
            return SimpleNamespace(wait=lambda: None)
        page.post_click = click
        with self.assertRaisesRegex(KEEP.KeepPageError, '累计6次'):
            self.run_flow(page)
        self.assertEqual(page.clicks, [(360, 1158)] * KEEP.KeepDrinkFlow.MAX_KEEP_ATTEMPTS)

    def test_keep_missed_twice_then_recovers_without_touching_checkboxes(self):
        page = Page(selected=(True, False, True, True))
        original, misses = page.post_click, [2]
        def click(x, y):
            if y > 1100 and misses[0]:
                misses[0] -= 1
                page.clicks.append((x, y))
                return SimpleNamespace(wait=lambda: None)
            return original(x, y)
        page.post_click = click
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.clicks, [(360, 1158)] * 3)
        self.assertEqual(page.selected, [True, False, True, True])

    def test_second_identical_cap_without_observed_exit_does_not_block_progress(self):
        page = Page(selected=(True, False, True, True))
        original, count = page.post_click, [0]
        def click(x, y):
            if y > 1100:
                count[0] += 1
                if count[0] == 1:
                    page.clicks.append((x, y))
                    # 第一个已接受，第二个画面完全相同，未采到退出帧。
                    return SimpleNamespace(wait=lambda: None)
            return original(x, y)
        page.post_click = click
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.clicks, [(360, 1158)] * 2)

    def test_unobserved_second_cap_with_vacancy_reapplies_priority_before_click(self):
        page = Page(selected=(True, False, True, True))
        original, count = page.post_click, [0]
        def click(x, y):
            if y > 1100:
                count[0] += 1
                if count[0] == 1:
                    page.clicks.append((x, y))
                    page.selected = [False, False, True, True]
                    page.names = ('second1', 'second2', 'held1', 'held2')
                    return SimpleNamespace(wait=lambda: None)
            return original(x, y)
        page.post_click = click
        self.assertTrue(self.run_flow(page, ['second2', 'held1', 'held2', 'new1', 'new2'])[0])
        self.assertEqual(count[0], 1)
        self.assertTrue(self.run_flow(page, ['second2', 'held1', 'held2', 'new1', 'new2'])[0])
        self.assertEqual(count[0], 2)
        self.assertEqual(page.selected, [False, True, True, True])
        self.assertEqual(sum(y < 1100 for _, y in page.clicks), 1)

    def test_visibly_changed_cap_hands_back_to_entry_without_retrying_old_page(self):
        page = Page(selected=(True, False, True, True))
        image, click, changed = page.image, page.post_click, [False]
        def changed_image():
            frame = image()
            if changed[0]:
                frame[300:380, 170:600] = 20
            return frame
        def first_click(x, y):
            if y > 1100:
                page.clicks.append((x, y))
                changed[0] = True
                page.selected = [False, False, True, True]
                return SimpleNamespace(wait=lambda: None)
            return click(x, y)
        page.image, page.post_click = changed_image, first_click
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.clicks, [(360, 1158)])
        self.assertFalse(page.submitted)
        # 入口创建新事务后，先补新页名额，再提交。
        page.image, page.post_click = image, click
        self.assertTrue(self.run_flow(page)[0])
        self.assertEqual(page.selected, [True, False, True, True])

    def test_delayed_acceptance_during_rescan_does_not_submit_again(self):
        page = Page(selected=(True, False, True, True))
        page.post_click = lambda x, y: (page.clicks.append((x, y)) or SimpleNamespace(wait=lambda: None))
        def modify(flow, _action):
            def scan():
                page.submitted = True
                raise KEEP.KeepPageError('重扫时原提交延迟生效')
            flow.scan = scan
        self.assertTrue(self.run_flow(page, modify=modify)[0])
        self.assertEqual(page.clicks, [(360, 1158)])

    def test_delayed_acceptance_while_waiting_to_retry_stops_the_retry(self):
        page = Page(selected=(True, False, True, True))
        page.post_click = lambda x, y: (page.clicks.append((x, y)) or SimpleNamespace(wait=lambda: None))
        def modify(flow, _action):
            original = flow.click_keep
            def click_keep():
                if flow.keep_clicks:
                    page.submitted = True
                original()
            flow.click_keep = click_keep
        self.assertTrue(self.run_flow(page, modify=modify)[0])
        self.assertEqual(page.clicks, [(360, 1158)])

    def test_uncertain_cap_classification_rechecks_instead_of_failing(self):
        page = Page(selected=(True, False, True, True))
        original, count = page.post_click, [0]
        def click(x, y):
            count[0] += 1
            if count[0] == 1:
                page.clicks.append((x, y))
                return SimpleNamespace(wait=lambda: None)
            return original(x, y)
        page.post_click = click
        def modify(_flow, action):
            action._keep_after_submit_state = lambda *_: ('page', None) if page.submitted else ('unknown', None)
        self.assertTrue(self.run_flow(page, modify=modify)[0])
        self.assertEqual(page.clicks, [(360, 1158)] * 2)

    def test_stuck_reward_skip_is_bounded(self):
        page = Page(selected=(True, False, True, True))
        original_click = page.post_click
        def click(x, y):
            if (x, y) == (22, 22):
                page.clicks.append((x, y))
                return SimpleNamespace(wait=lambda: None)
            return original_click(x, y)
        page.post_click = click
        with self.assertRaisesRegex(KEEP.KeepPageError, '跳过12次'):
            self.run_flow(page, modify=lambda _f, a: setattr(a, '_keep_after_submit_state', lambda *_: ('reward', 'new1')))
        self.assertEqual(page.clicks.count((22, 22)), 12)

    def test_reward_effect_text_is_not_classified_as_operable_page(self):
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        result = lambda text, box: SimpleNamespace(text=text, box=box)
        results = [result('レッスン終了時に削除される', [50, 970, 510, 28])]
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=True, filtered_results=results))
        self.assertEqual(action._keep_after_submit_state(context, None), ('unknown', None))
        results.extend([result('H.I.F本戦まで', [45, 60, 140, 25]),
                        result('初星スペシャル青汁', [272, 854, 277, 28])])
        self.assertEqual(action._keep_after_submit_state(context, None), ('reward', '初星スペシャル青汁'))
        results.pop()
        self.assertEqual(action._keep_after_submit_state(context, None), ('unknown', None))
        results[:] = [result('H.I.F本戦まで', [45, 60, 140, 25])]
        self.assertEqual(action._keep_after_submit_state(context, None), ('page', None))

    def test_reward_name_tolerates_short_icon_prefix_but_not_effect_text(self):
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        results = [SimpleNamespace(text='', box=[235, 851, 255, 38])]
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=True, filtered_results=results))
        for prefix in ('', '団', '囗★'):
            results[0].text = prefix + '特製ハツボシエキス'
            self.assertEqual(action._keep_after_submit_state(context, None), ('reward', '特製ハツボシエキス'))
        for text in ('獲得した特製ハツボシエキス', '特製ハツボシエキスを獲得', '団特製ハツボシエキ'):
            results[0].text = text
            self.assertEqual(action._keep_after_submit_state(context, None), ('unknown', None))
        results[0].text = '団特製ハツボシエキス'
        results[0].box = [235, 230, 255, 38]
        self.assertEqual(action._keep_after_submit_state(context, None), ('unknown', None))

    def test_receive_prompt_precedes_drink_name_printed_on_bottle(self):
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        result = lambda text, box: SimpleNamespace(text=text, box=box)
        results = [result('受け取るPドリンクを選んでください。', [145, 613, 413, 28]),
                   result('初星水', [190, 876, 61, 38]),
                   result('受け取らない', [285, 993, 147, 25]),
                   result('受け取る', [298, 1076, 119, 35])]
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(hit=True, filtered_results=results))
        self.assertEqual(action._keep_after_submit_state(context, None), ('page', None))
        results.pop()  # 过渡中按钮尚未出现，即使读到瓶身名字，也不能继续跳动画。
        self.assertEqual(action._keep_after_submit_state(context, None), ('unknown', None))
        results[:] = [result('初星水', [295, 854, 129, 28])]
        self.assertEqual(action._keep_after_submit_state(context, None), ('reward', '初星水'))


if __name__ == '__main__':
    unittest.main()
