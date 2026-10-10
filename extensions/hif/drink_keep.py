"""Vision and selection rules for the HIF checkbox drink-cap page."""
import cv2
import numpy as np
import time
from pathlib import Path

VIEW = (53, 273, 667, 1067)
INFO_GLYPH = cv2.imread(str(Path(__file__).parent / 'resource/base/image/hif/produce/hif_drink_info.png'), cv2.IMREAD_GRAYSCALE)


def page_rows(image):
    """Read only complete rows; a white tick inside a gray box is unselected."""
    x0, y0, x1, y1 = VIEW
    strip = image[y0:y1, 608:650].astype(np.int16)
    b, g, r = strip[..., 0], strip[..., 1], strip[..., 2]
    orange = (r > 230) & (g > 65) & (g < 195) & (b < 90)
    gray = (strip.max(axis=2) - strip.min(axis=2) < 22) & (r > 145) & (r < 230)
    count, _, stats, centers = cv2.connectedComponentsWithStats((orange | gray).astype(np.uint8))
    background = np.median(image[y0:y1, 653:658], axis=1)
    filled = background.min(axis=1) < 251
    rows = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (14 <= w <= 38 and 14 <= h <= 40 and area >= 100):
            continue
        cy = int(round(centers[i][1]))
        top, bottom = cy, cy + 1
        while top > 0 and filled[top - 1]:
            top -= 1
        while bottom < len(filled) and filled[bottom]:
            bottom += 1
        if bottom - top < 70 or top < 2 or bottom >= len(filled) - 2:
            continue
        # Locate the colored square icon inside this row, including tall effect rows.
        icon_band = image[y0 + top:y0 + bottom, 59:144].astype(np.int16)
        # 黄色长行的圆角边缘是白色，不能逐像素当作该行背景。
        row_background = np.median(image[y0 + top:y0 + bottom, 653:658], axis=(0, 1))
        colored = np.abs(icon_band - row_background).max(axis=2) > 30
        ys = np.flatnonzero(colored.sum(axis=1) > 35)
        icon = None
        if len(ys):
            icon_top, icon_bottom = y0 + top + int(ys[0]), y0 + top + int(ys[-1]) + 1
            if 55 <= icon_bottom - icon_top <= 105:
                icon = image[icon_top:icon_bottom, 61:142].copy()
        box = [608 + int(x), y0 + int(y), int(w), int(h)]
        selected = bool(orange[y:y + h, x:x + w].sum() > area / 2)
        info = None
        if INFO_GLYPH is not None:
            badge = cv2.cvtColor(image[y0 + top:y0 + bottom, 120:151], cv2.COLOR_BGR2GRAY)
            scores = cv2.matchTemplate((badge > 235).astype(np.uint8) * 255, INFO_GLYPH, cv2.TM_CCOEFF_NORMED)
            _, peak, _, (bx, by) = cv2.minMaxLoc(scores)
            alternatives = scores.copy()
            alternatives[max(0, by - 5):by + 6, :] = -1
            if peak >= .83 and peak - float(alternatives.max()) >= .06:
                info = (120 + bx + INFO_GLYPH.shape[1] // 2, y0 + top + by + INFO_GLYPH.shape[0] // 2)
        rows.append({'y': y0 + cy, 'box': box, 'selected': selected,
                     'bounds': [x0, y0 + top, x1 - x0, bottom - top],
                     'icon': icon,
                     'info': info})
    return sorted(rows, key=lambda item: item['y'])


def scrollbar(image):
    strip = image[VIEW[1]:VIEW[3], 676:684].astype(np.int16)
    mask = ((strip.max(axis=2) - strip.min(axis=2) < 40)
            & (strip[..., 2] > 65) & (strip[..., 2] < 205))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    bars = [s for s in stats[1:count] if s[2] >= 2 and s[3] >= 25]
    if not bars:
        return None
    x, y, w, h, _ = max(bars, key=lambda s: s[4])
    return int(y + VIEW[1]), int(y + h + VIEW[1])


def view_stable(first, second):
    if first is None:
        return False
    x0, y0, x1, y1 = VIEW
    a, b = first[y0:y1, x0:x1], second[y0:y1, x0:x1]
    return a.shape == b.shape and np.abs(a.astype(np.int16) - b.astype(np.int16)).mean() <= 2.5


def scroll_delta(first, second, direction):
    """Measure content displacement from overlapping edges, rejecting ambiguous matches."""
    _, y0, _, y1 = VIEW
    before = cv2.GaussianBlur(cv2.Canny(cv2.cvtColor(first[y0:y1, 58:605], cv2.COLOR_BGR2GRAY), 40, 110), (5, 5), 0)
    after = cv2.GaussianBlur(cv2.Canny(cv2.cvtColor(second[y0:y1, 58:605], cv2.COLOR_BGR2GRAY), 40, 110), (5, 5), 0)
    height = len(before)
    start, end = (height // 3, height - 25) if direction > 0 else (25, 2 * height // 3)
    template = before[start:end]
    if np.count_nonzero(template) < 150:
        return None
    scores = cv2.matchTemplate(after, template, cv2.TM_CCOEFF_NORMED).ravel()
    shifts = start - np.arange(len(scores))
    scores[(shifts * direction < -3) | (np.abs(shifts) > height / 2)] = -1
    best = int(np.argmax(scores))
    peak = float(scores[best])
    alternatives = scores.copy()
    alternatives[max(0, best - 8):best + 9] = -1
    if peak < 0.80 or peak - float(alternatives.max()) < 0.06:
        return None
    delta = int(shifts[best])
    return 0 if abs(delta) <= 3 else delta


def merge_rows(entries, rows, offset, headers):
    for row in rows:
        position = row['y'] + offset
        group = 'held' if 'held' in headers and position > headers['held'] else 'received'
        found = next((e for e in entries if abs(e['position'] - position) <= 12), None)
        if found:
            if found['group'] != group or found['selected'] != row['selected']:
                raise ValueError('List changed during read-only scan')
            continue
        entries.append({**row, 'position': position, 'group': group, 'name': None})
    entries.sort(key=lambda e: e['position'])


def keep_indices(entries, capacity, priority, disabled=()):
    ranks = {name: i for i, name in enumerate(priority)}
    disabled = set(disabled)
    def key(i):
        entry = entries[i]
        name = entry['name']
        rank = len(ranks) + 1 if name in disabled else ranks.get(name, len(ranks))
        return rank, entry['group'] != 'held', i
    return set(sorted(range(len(entries)), key=key)[:capacity])


def icon_name(icon, catalog, image_root):
    if icon is None or min(icon.shape[:2]) < 40:
        return None
    crop = cv2.resize(icon, (96, 96))[10:88, 10:83]
    scores = []
    for drink in catalog:
        template = cv2.imread(str(image_root / (str(drink['id']) + '.webp')))
        if template is None:
            continue
        template = cv2.resize(template, (96, 96))[20:80, 25:71]
        score = max(float(cv2.minMaxLoc(cv2.matchTemplate(crop,
            cv2.resize(template, None, fx=scale, fy=scale), cv2.TM_CCOEFF_NORMED))[1])
            for scale in np.arange(0.65, 1.06, 0.05))
        scores.append((score, drink['name']))
    scores.sort(reverse=True)
    return scores[0][1] if len(scores) > 1 and scores[0][0] >= 0.82 and scores[0][0] - scores[1][0] >= 0.04 else None


class KeepPageError(RuntimeError):
    pass


class KeepDrinkFlow:
    """One page-local transaction; no identities are needed for basic supplementation."""
    STEP_TIMEOUT = 8.0
    MAX_SWIPES = 12
    MAX_KEEP_ATTEMPTS = 6

    def __init__(self, action, context, priority=None, disabled=()):
        self.action, self.context = action, context
        self.priority, self.disabled = priority, disabled
        self.entries, self.offset, self.headers = [], 0, {}
        self.capacity = None
        self.step = (VIEW[3] - VIEW[1]) // 6
        self.keep_clicks, self.keep_image, self.keep_clicked_at = 0, None, 0

    def log(self, message):
        self.action._log_throttled(message, 'info', 'HIF饮料所持上限页: ' + message)

    def check_stop(self):
        if getattr(self.context.tasker, 'stopping', False):
            raise KeepPageError('任务已停止，取消后续点击')

    def capture(self):
        self.check_stop()
        return self.action._screencap(self.context)

    def stable_image(self):
        deadline, previous, stable = time.monotonic() + self.STEP_TIMEOUT, None, 0
        while time.monotonic() < deadline:
            image = self.capture()
            stable = stable + 1 if view_stable(previous, image) else 1
            previous = image
            if stable >= 3:
                if not self.action._window_open(self.context, image):
                    raise KeepPageError('扫描期间已离开所持上限页')
                return image
            time.sleep(0.2)
        raise KeepPageError('列表未在8秒内稳定')

    def remaining(self, image):
        value = self.action._selection_remaining(self.context, image)
        if value is None:
            raise KeepPageError('剩余选择数无法确认，不操作复选框')
        return value

    def move(self, direction, before, measure=True):
        self.check_stop()
        start = 945 if direction > 0 else 420
        job = self.context.tasker.controller.post_swipe(430, start, 430, start - direction * self.step, 650).wait()
        if getattr(job, 'succeeded', True) is False:
            raise KeepPageError('列表滑动控制器失败')
        after = self.stable_image()
        if not measure:
            return after, None
        delta = scroll_delta(before, after, direction)
        if delta is None:
            raise KeepPageError('滚动位移不唯一，无法关联同一瓶饮料')
        self.offset += delta
        return after, delta

    def top(self):
        image = self.stable_image()
        for _ in range(self.MAX_SWIPES):
            bar = scrollbar(image)
            headers = self.action._page_headers(self.context, image)
            if 'received' in headers and (bar is None or bar[0] <= VIEW[1] + 4):
                self.offset = 0
                return image
            before = image
            image, _ = self.move(-1, image, measure=False)
            if view_stable(before, image):
                break
        raise KeepPageError('无法确认列表顶部')

    def scan(self):
        for attempt in range(2):
            try:
                image = self.top()
                self.entries, self.headers = [], {}
                initial_remaining = self.remaining(image)
                stationary = 0
                for swipe in range(self.MAX_SWIPES + 1):
                    for group, y in self.action._page_headers(self.context, image).items():
                        self.headers[group] = y + self.offset
                    rows = page_rows(image)
                    if not rows:
                        raise KeepPageError('完整条目及复选框未识别')
                    merge_rows(self.entries, rows, self.offset, self.headers)
                    bar = scrollbar(image)
                    bottom = bar is None or bar[1] >= VIEW[3] - 4
                    if stationary >= 2 and bottom and rows[-1]['bounds'][1] + rows[-1]['bounds'][3] < VIEW[3] - 2:
                        if 'received' not in self.headers or 'held' not in self.headers:
                            raise KeepPageError('收到／手持分组未完整识别')
                        if self.remaining(image) != initial_remaining:
                            raise KeepPageError('只读扫描期间选择数发生变化')
                        self.capacity = sum(e['selected'] for e in self.entries) + initial_remaining
                        if not 1 <= self.capacity <= 4 or self.capacity > len(self.entries):
                            raise KeepPageError('完整清单与可保留数量不一致')
                        self.log(f'完整扫描{len(self.entries)}瓶，容量={self.capacity}，待补={initial_remaining}')
                        return
                    if swipe == self.MAX_SWIPES:
                        break
                    image, delta = self.move(1, image)
                    stationary = stationary + 1 if delta == 0 else 0
                raise KeepPageError('12次滚动后仍未确认完整列表')
            except (KeepPageError, ValueError):
                if attempt:
                    raise
                self.step //= 2
                self.log('扫描未完整确认，回顶并缩短步长重扫一次')

    def locate(self, entry):
        image = self.stable_image()
        for _ in range(self.MAX_SWIPES + 1):
            for row in page_rows(image):
                if abs(row['y'] + self.offset - entry['position']) <= 12:
                    return image, row
            direction = 1 if entry['position'] - self.offset > (VIEW[1] + VIEW[3]) / 2 else -1
            image, delta = self.move(direction, image)
            if delta == 0:
                break
        raise KeepPageError('滚动后无法重新定位原条目')

    def toggle(self, entry, selected):
        image, row = self.locate(entry)
        before = self.remaining(image)
        if row['selected'] == selected:
            entry['selected'] = selected
            return
        expected_remaining = before - 1 if selected else before + 1
        if expected_remaining < 0:
            raise KeepPageError('名额不足，不能追加勾选')
        deadline, attempts, stable, previous, clicked = time.monotonic() + self.STEP_TIMEOUT, 0, 0, None, -10
        while time.monotonic() < deadline:
            self.check_stop()
            if row is None:
                # 勾选动画的过渡帧可能暂时没有完整框；不据此重复点击。
                stable, previous = 0, None
                time.sleep(.2)
                image = self.capture()
                row = next((r for r in page_rows(image) if abs(r['y'] + self.offset - entry['position']) <= 12), None)
                continue
            position = row['box']
            key = (row['selected'], self.remaining(image), position[0], position[1])
            stable = stable + 1 if previous and key[:2] == previous[:2] and abs(key[2] - previous[2]) <= 6 and abs(key[3] - previous[3]) <= 6 else 1
            previous = key
            if stable >= 3:
                if row['selected'] == selected and key[1] == expected_remaining:
                    entry['selected'] = selected
                    self.log(f'{entry["group"]}条目{entry["position"]}: 已确认勾选={selected}，剩余={expected_remaining}')
                    return
                if row['selected'] != selected and key[1] == before and attempts < 2 and time.monotonic() - clicked >= 1:
                    self.context.tasker.controller.post_click(position[0] + position[2] // 2, position[1] + position[3] // 2).wait()
                    attempts, clicked, stable = attempts + 1, time.monotonic(), 0
            time.sleep(0.2)
            image = self.capture()
            row = next((r for r in page_rows(image) if abs(r['y'] + self.offset - entry['position']) <= 12), None)
        raise KeepPageError('勾选状态或选择数未确认，停止重复切换')

    def basic(self):
        for entry in sorted(self.entries, key=lambda e: (e['group'] != 'held', e['position'])):
            image = self.stable_image()
            if self.remaining(image) == 0:
                return
            if not entry['selected']:
                self.toggle(entry, True)
        if self.remaining(self.stable_image()) != 0:
            raise KeepPageError('手持与收到候选均检查后仍未补足')

    def optimize(self):
        snapshot = [e['selected'] for e in self.entries]
        changed = False
        try:
            for entry in self.entries:
                _, row = self.locate(entry)
                entry['name'] = self.action._resolve_keep_name(self.context, row)
                self.top()  # 详情关闭后可能重置列表位置，重新建立坐标起点。
                if not entry['name']:
                    self.log('身份未全部确认，保留初始选择并转基础补选')
                    return False
            keep = keep_indices(self.entries, self.capacity, self.priority, self.disabled)
            for selected in (False, True):
                for i, entry in enumerate(self.entries):
                    desired = i in keep
                    if desired == selected and entry['selected'] != desired:
                        changed = True
                        self.toggle(entry, desired)
            return True
        except KeepPageError:
            if not changed:
                raise
            self.log('优先级调整失败，恢复初始选择后转基础补选')
            for selected in (False, True):
                for entry, desired in zip(self.entries, snapshot):
                    if desired == selected:
                        self.toggle(entry, desired)
            return False

    def verify(self):
        expected = {e['position']: e['selected'] for e in self.entries}
        self.scan()
        if len(expected) != len(self.entries) or any(not any(abs(position - e['position']) <= 12 and selected == e['selected']
                for position, selected in expected.items()) for e in self.entries):
            raise KeepPageError('提交前重新遍历的保留集合不一致')
        if sum(e['selected'] for e in self.entries) != self.capacity:
            raise KeepPageError('已选总数与容量不一致')

    def click_keep(self):
        deadline, stable, previous_image = time.monotonic() + self.STEP_TIMEOUT, 0, None
        while time.monotonic() < deadline:
            image = self.capture()
            ready = (self.remaining(image) == 0 and self.action._keep_button_enabled(image)
                     and self.action._window_open(self.context, image))
            stable = stable + 1 if ready and view_stable(previous_image, image) else (1 if ready else 0)
            previous_image = image
            if stable >= 3:
                self.check_stop()
                self.keep_image = image.copy()
                self.context.tasker.controller.post_click(*self.action.KEEP_POS).wait()
                self.keep_clicks += 1
                self.keep_clicked_at = time.monotonic()
                self.log(f'点击残す，第{self.keep_clicks}/{self.MAX_KEEP_ATTEMPTS}次；等待页面结果')
                break
            time.sleep(0.2)
        else:
            raise KeepPageError('剩余数为0及残す可用未同时确认')
    def submit(self):
        try:
            self.click_keep()
        except KeepPageError:
            self.check_stop()
            if not self.keep_clicks or self.action._window_open(self.context, self.capture()):
                raise
            self.log('补点前原提交已延迟生效，停止补点并继续处理动画或下一页')
        return self.wait_after_submit()

    def wait_after_submit(self):
        deadline, limit = time.monotonic() + self.STEP_TIMEOUT, time.monotonic() + 30
        stable, previous, previous_image, departed, gone, taps, last_tap = 0, None, None, False, 0, 0, -10
        while time.monotonic() < min(deadline, limit):
            image = self.capture()
            state = self.action._keep_after_submit_state(self.context, image)
            if state != previous:
                stable, deadline = 0, time.monotonic() + self.STEP_TIMEOUT
            steady = state[0] == 'reward' or view_stable(previous_image, image)
            stable = stable + 1 if steady else 1
            gone = gone + 1 if state[0] != 'cap' and not page_rows(image) else 0
            departed = departed or gone >= 3
            if stable >= 3:
                if state[0] == 'reward' and time.monotonic() - last_tap >= .6:
                    if taps >= 12:
                        raise KeepPageError('领取动画跳过12次仍未结束，不继续点击')
                    self.check_stop()
                    self.context.tasker.controller.post_click(22, 22).wait()
                    taps, last_tap, stable = taps + 1, time.monotonic(), 0
                    deadline = time.monotonic() + self.STEP_TIMEOUT
                    self.log(f'领取动画「{state[1]}」：点击左上角跳过，第{taps}/12次')
                elif state[0] == 'cap' and departed:
                    # 不把第二个弹窗当作原弹窗提交失败；入口会创建新的页面事务。
                    self.log('上一上限页已退出，出现下一上限页；交回HIF入口重新处理')
                    return True
                elif state[0] == 'cap' and time.monotonic() - self.keep_clicked_at >= 1.5:
                    changed = not view_stable(self.keep_image, image)
                    if changed:
                        self.log('上限页内容已变化；交回HIF入口重新识别当前页，不补点上一页')
                        return True
                    self.log('上限页仍可见，无法断定原页或下一页；重新扫描后决定补点')
                    return False
                elif state[0] == 'page' and departed:
                    self.log('残す提交一次后，领取动画已结束，下一可操作页面已稳定')
                    return True
            previous, previous_image = state, image
            time.sleep(0.2)
        if self.action._window_open(self.context, image):
            self.log('提交后页面分类未确定，但上限页仍可识别；重新扫描当前页')
            return False
        raise KeepPageError('残す提交后页面无法识别，不盲目补点')

    def prepare(self, force_scan=False):
        image = self.stable_image()
        if not force_scan and self.priority is None and self.remaining(image) == 0 and self.action._keep_button_enabled(image):
            return
        try:
            self.scan()
        except (KeepPageError, ValueError):
            # 没有改过选择，只能确认已有足额选择；不凭截断清单盲目补选。
            image = self.stable_image()
            if self.remaining(image) == 0 and self.action._keep_button_enabled(image):
                return
            raise
        self.top()
        if self.priority is None or not self.optimize():
            self.basic()
        self.verify()

    def run(self):
        while self.keep_clicks < self.MAX_KEEP_ATTEMPTS:
            self.check_stop()
            try:
                self.prepare(force_scan=self.keep_clicks > 0)
            except KeepPageError:
                self.check_stop()
                if not self.keep_clicks or self.action._window_open(self.context, self.capture()):
                    raise
                # 上次点击可能延迟生效，重扫时已经出现动画；先推进，不再点击残す。
                if self.wait_after_submit():
                    return True
                continue
            if self.submit():
                return True
        raise KeepPageError('残す累计6次点击后仍未确认页面推进，不再补点')
