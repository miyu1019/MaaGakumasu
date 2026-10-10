import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import mock_open, patch


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


utils = types.ModuleType("utils")
utils.logger = _Logger()
numpy = types.ModuleType("numpy")
numpy.arange = lambda start, stop, step: [
    start + index * step for index in range(int((stop - start) / step) + 1)
]
cv2 = types.ModuleType("cv2")
context_module = types.ModuleType("maa.context")
context_module.Context = object
action_module = types.ModuleType("maa.custom_action")
action_module.CustomAction = type("CustomAction", (), {"RunArg": object})
server_module = types.ModuleType("maa.agent.agent_server")
server_module.AgentServer = type(
    "AgentServer",
    (),
    {"custom_action": staticmethod(lambda _name: lambda cls: cls)},
)
STUB_MODULES = {
    "utils": utils,
    "numpy": numpy,
    "cv2": cv2,
    "maa": types.ModuleType("maa"),
    "maa.context": context_module,
    "maa.custom_action": action_module,
    "maa.agent": types.ModuleType("maa.agent"),
    "maa.agent.agent_server": server_module,
}

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "produce_under_test", ROOT / "extensions/hif/agent/hif/action/produce.py"
)
PRODUCE = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, STUB_MODULES):
    SPEC.loader.exec_module(PRODUCE)

# Regression fixtures must not depend on private runtime strategies.
PRODUCE.CARDS_PRIORITY_CONFIG_PATH = str(ROOT / 'extensions/hif/defaults/cards_priority.json')


class _Context:
    def __init__(self, nodes):
        self.nodes = nodes

    def get_node_data(self, name):
        return self.nodes.get(name, {})


class WantedCardSwapTest(unittest.TestCase):
    def test_hif_move_reads_only_cropped_title_and_requires_full_name(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        title_image = object()
        crops = []

        class Image:
            def __getitem__(self, region):
                crops.append(region)
                return SimpleNamespace(copy=lambda: title_image)

        for text, expected in [
            ("脚光", "脚光"), ("脚光+", "脚光"), (" 腳 光 ＋ ", "脚光"),
            ("パフォーマンスの基本", None), ("国民的アイドル+", None),
            ("の基本好調2ターン集中+2", None), ("好調消費2ターン", None),
            ("脚", None), ("光+", None), ("脚光+集中+9", None), ("", None),
        ]:
            with self.subTest(text=text):
                def recognize(name, image, pipeline_override):
                    self.assertEqual(name, "ProduceHIF__ProduceRecognitionScore")
                    self.assertIs(image, title_image)
                    node = pipeline_override[name]
                    self.assertEqual(node["roi"], [0, 0, 410, 40])
                    self.assertEqual(node["expected"], action.MOVE_OCR_ALL)
                    return SimpleNamespace(hit=bool(text), filtered_results=[
                        SimpleNamespace(text=text, box=[0, 0, 100, 30])
                    ])

                context = SimpleNamespace(run_recognition=recognize)
                self.assertEqual(action._match_move_card_name(context, Image()), expected)
                self.assertEqual(crops[-1], (slice(105, 145), slice(190, 600)))

    def test_hif_move_joins_split_title_in_reading_order(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        image = SimpleNamespace(copy=lambda: object())

        class Image:
            def __getitem__(self, _region):
                return image

        context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
            hit=True, filtered_results=[
                SimpleNamespace(text="光+", box=[30, 0, 50, 30]),
                SimpleNamespace(text="脚", box=[0, 0, 30, 30]),
            ],
        ))
        self.assertEqual(action._match_move_card_name(context, Image()), "脚光")

    def test_non_hif_move_keeps_existing_recognition_path(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        image = object()

        def recognize(name, received, pipeline_override):
            self.assertIs(received, image)
            self.assertEqual(pipeline_override[name]["roi"], action.MOVE_NAME_ROI)
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="脚光+")])

        self.assertEqual(action._match_move_card_name(
            SimpleNamespace(run_recognition=recognize), image,
        ), "脚光")

    def test_hif_move_retries_empty_titles_but_skips_read_non_targets(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action.profession = "集中"
        action.MOVE_GRID = action.MOVE_GRID[:3]
        replies = {
            action.MOVE_GRID[0]: ["パフォーマンスの基本"],
            action.MOVE_GRID[1]: [None, "国民的アイドル+"],
            action.MOVE_GRID[2]: ["脚光+"],
        }
        reads = {pos: 0 for pos in replies}
        clicks, tasks = [], []

        class Image:
            def __getitem__(self, _region):
                return SimpleNamespace(copy=lambda: object())

        image = Image()

        def recognize(name, _image, **_kwargs):
            if name == "ProduceHIF__ProduceRecognitionChooseMoveCards":
                return SimpleNamespace(hit=True)
            pos = clicks[-1]
            text = replies[pos][reads[pos]]
            reads[pos] += 1
            return SimpleNamespace(hit=bool(text), filtered_results=[
                SimpleNamespace(text=text, box=[0, 0, 100, 30])
            ])

        context = SimpleNamespace(
            run_recognition=recognize,
            run_task=lambda name: tasks.append(name),
            tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
                post_click=lambda *pos: clicks.append(pos) or SimpleNamespace(wait=lambda: None),
                post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(get=lambda: image)
                ),
            )),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None), \
                patch.object(PRODUCE.logger, "info") as log:
            self.assertTrue(action._handle_move_cards(context, image))
        self.assertEqual(list(reads.values()), [1, 2, 1])
        self.assertEqual(clicks, action.MOVE_GRID)
        self.assertEqual(tasks, ["ProduceHIF__ProduceMoveCards"])
        messages = [call.args[0] for call in log.call_args_list]
        self.assertTrue(any("卡名OCR尝试第1次=[パフォーマンスの基本]" in m for m in messages))
        self.assertTrue(any("已识别卡名=[国民的アイドル+]，非脚光" in m for m in messages))
        self.assertTrue(action._move_done)

    def test_interval_heal_uses_ten_plus_clicks_then_waits_for_shop(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        clicks = []
        recognized = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                stopping=False,
                controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(get=lambda: object())
                )),
            ),
            get_node_data=lambda _name: {"enabled": True},
            run_recognition=lambda name, _image, **_kwargs: (
                recognized.append(name) or SimpleNamespace(hit=True)
            ),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action._heal_before_customization(context))
        self.assertEqual(clicks, [action.HEAL_OPEN_POS] + [action.HEAL_PLUS_POS] * 10 + [action.HEAL_CONFIRM_POS])
        self.assertEqual(recognized, ["ProduceHIF__ProduceRecognitionScore"] + ["ProduceHIF__ProduceHIFCardCustomFlag"] * 4)

    def test_custom_entry_waits_for_select_page_before_scanning_cards(self):
        for scenes, succeeds, entries in (
            (["shop", "shop", "shop", "select", "select", "shop"], True, 2),
            (["shop", "transition", "select", "select", "shop"], True, 1),
            (["shop"], False, 3),
            (["transition"], False, 0),
        ):
            with self.subTest(scenes=scenes):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                action.BETWEEN_LIMIT = 3
                action._heal_before_customization = lambda _context: True
                action.custom_cards = {"目标卡": {"menu": ["已选项目"]}}
                action._load_custom_cards = lambda _profession: None
                action._read_left = lambda _context, image, roi: (
                    2 if (image == "shop" and roi == action.MAIN_LEFT_ROI)
                    or (image == "select" and roi == action.SELECT_LEFT_ROI) else None
                )
                clicks, scanned = [], []
                action._click = lambda _context, pos, delay=None: clicks.append(pos)

                def select(_context, image):
                    self.assertEqual(image, "select")
                    scanned.append(image)
                    action._done_all = True
                    return "back"

                action._handle_select = select
                frames = iter(scenes)
                context = SimpleNamespace(
                    tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
                        post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(
                            get=lambda: next(frames, scenes[-1])
                        )),
                    )),
                    get_node_data=lambda _name: {},
                    run_recognition=lambda _name, image: SimpleNamespace(hit=image == "shop"),
                )
                with patch.object(PRODUCE.time, "sleep", return_value=None), \
                        patch.object(PRODUCE.ProduceHIF__ProduceHIFConsultAuto, "buy_custom_end_drinks"):
                    self.assertEqual(action._run_inner(context, None), succeeds)
                self.assertEqual(clicks.count(action.CUSTOMIZE_POS), entries)
                self.assertEqual(scanned, ["select"] if succeeds else [])
                self.assertEqual(clicks.count(action.TERMINATE_POS), int(succeeds))

    def test_interval_heal_cancels_if_confirmation_does_not_close_dialog(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        action._wait_heal_shop = unittest.mock.Mock(side_effect=[False, True])
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                stopping=False,
                controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(get=lambda: object())
                )),
            ),
            get_node_data=lambda _name: {"enabled": True},
            run_recognition=lambda _name, _image, **_kwargs: SimpleNamespace(hit=True),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action._heal_before_customization(context))
        self.assertEqual(clicks[-2:], [action.HEAL_CONFIRM_POS, action.HEAL_CANCEL_POS])

    def test_interval_heal_retries_open_once_then_skips_if_still_closed(self):
        for opens_on_retry in (True, False):
            with self.subTest(opens_on_retry=opens_on_retry):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                clicks = []
                ocr_calls = 0
                action._click = lambda _context, pos, delay=None: clicks.append(pos)

                def recognize(name, _image, **_kwargs):
                    nonlocal ocr_calls
                    if name == "ProduceHIF__ProduceRecognitionScore":
                        ocr_calls += 1
                        return SimpleNamespace(hit=opens_on_retry and ocr_calls > 6)
                    return SimpleNamespace(hit=True)

                context = SimpleNamespace(
                    tasker=SimpleNamespace(
                        stopping=False,
                        controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                            wait=lambda: SimpleNamespace(get=lambda: object())
                        )),
                    ),
                    get_node_data=lambda _name: {"enabled": True},
                    run_recognition=recognize,
                )
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertTrue(action._heal_before_customization(context))
                self.assertEqual(clicks[:2], [action.HEAL_OPEN_POS] * 2)
                self.assertEqual(clicks.count(action.HEAL_PLUS_POS), 10 if opens_on_retry else 0)
                self.assertEqual(ocr_calls, 7 if opens_on_retry else 12)

    def test_card_custom_switch_skips_saved_cards_but_keeps_shop_finish(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        saved_cards = {"目标卡": {"menu": ["已选项目"]}}
        action.custom_cards = saved_cards
        action._load_custom_cards = lambda _profession: None
        healed = []
        action._heal_before_customization = lambda _context: healed.append(True) or True
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        action._handle_select = lambda *_args: self.fail("关闭时不应进入选卡")
        clicks = []
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                stopping=False,
                controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(get=lambda: object())
                )),
            ),
            get_node_data=lambda _name: {"enabled": False},
            run_recognition=lambda *_args, **_kwargs: self.fail("关闭时不应检索卡牌"),
        )
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFConsultAuto, "buy_custom_end_drinks") as buy:
            self.assertTrue(action._run_inner(context, None))
            buy.assert_called_once_with(context)
        self.assertEqual(healed, [True])
        self.assertEqual(action.custom_cards, saved_cards)
        self.assertEqual(action._queue, [])
        self.assertEqual(clicks, [action.TERMINATE_POS])

    def test_card_custom_switch_defaults_on_and_uses_saved_queue(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action.custom_cards = {"目标卡": {"menu": ["已选项目"]}}
        action._load_custom_cards = lambda _profession: None
        action._heal_before_customization = lambda _context: True
        observed = []
        action._handle_main = lambda _context, _image: observed.append((action._queue[:], action._done_all)) or "exit"
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                stopping=False,
                controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(get=lambda: object())
                )),
            ),
            get_node_data=lambda _name: {},
        )
        self.assertTrue(action._run_inner(context, None))
        self.assertEqual(observed, [(["目标卡"], False)])
        self.assertTrue(action._customization_enabled(SimpleNamespace(
            get_node_data=lambda _name: {"enabled": True}
        )))
        self.assertTrue(action._customization_enabled(SimpleNamespace(
            get_node_data=lambda _name: (_ for _ in ()).throw(RuntimeError("旧配置"))
        )))

    def test_custom_card_panel_ids_use_common_menu_columns(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            config = pathlib.Path(directory) / "config" / "hif"
            config.mkdir(parents=True)
            catalog = pathlib.Path(directory) / "catalog"
            catalog.mkdir()
            (catalog / "hif_customization_catalog.json").write_text(
                (ROOT / "extensions/hif/catalog/hif_customization_catalog.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (config / "hif_custom_card_selection.json").write_text(
                json.dumps({"cards": {"381": [65, 77], "719": [28]}}), encoding="utf-8"
            )
            (catalog / "cards_customize.json").write_text("{}", encoding="utf-8")
            (catalog / "custom_card_images.json").write_text("[]", encoding="utf-8")
            with patch.object(PRODUCE, "BASE_DIR", directory), patch.object(PRODUCE, "EXT_DIR", directory):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()

        self.assertEqual(list(action.custom_cards), ["ジャストアピール+", "リスキーチャンス+"])
        self.assertEqual(action.FACE_TEMPLATES["ジャストアピール+"][0], "hif/hif_card_icons/381.webp")
        action._cur_card = "ジャストアピール+"
        self.assertEqual(action._current_menu_items(_Context({})), [(140, 929), (580, 929)])
        action._cur_card = "リスキーチャンス+"
        self.assertEqual(action._current_menu_items(_Context({})), [(360, 929)])


    def test_custom_card_stop_skips_exit_clicks(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action.custom_cards = {}
        action._load_custom_cards = lambda _profession: None
        action.STEP_LIMIT = 0  # Exercise the exit path without UI recognition.
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)

        self.assertTrue(action._run_inner(context, None))
        self.assertEqual(clicks, [])

        context.tasker.stopping = False

        def stop_after_back(_context, pos, delay=None):
            clicks.append(pos)
            context.tasker.stopping = True

        action._click = stop_after_back
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action._run_inner(context, None))
        self.assertEqual(clicks, [action.BACK_POS])

        context.tasker.stopping = False
        clicks.clear()
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action._run_inner(context, None))
        self.assertEqual(clicks, [action.BACK_POS, action.TERMINATE_POS])

    def test_custom_card_reload_selection_saved_after_action_creation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            config = pathlib.Path(directory) / "config" / "hif"
            config.mkdir(parents=True)
            catalog = pathlib.Path(directory) / "catalog"
            catalog.mkdir()
            (catalog / "hif_customization_catalog.json").write_text(
                (ROOT / "extensions/hif/catalog/hif_customization_catalog.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            selection = config / "hif_custom_card_selection.json"
            selection.write_text(json.dumps({"cards": {"381": [65]}}), encoding="utf-8")
            (catalog / "cards_customize.json").write_text("{}", encoding="utf-8")
            (catalog / "custom_card_images.json").write_text("[]", encoding="utf-8")
            with patch.object(PRODUCE, "BASE_DIR", directory), patch.object(PRODUCE, "EXT_DIR", directory):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                self.assertEqual(list(action.custom_cards), ["ジャストアピール+"])
                selection.write_text(json.dumps({"cards": {"719": [28]}}), encoding="utf-8")
                action.STEP_LIMIT = 0
                context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
                self.assertTrue(action._run_inner(context, None))
                self.assertEqual(action._queue, ["リスキーチャンス+"])


    def test_custom_card_selection_and_queue_are_scoped_to_task_profession(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            config = pathlib.Path(directory) / "config" / "hif"
            config.mkdir(parents=True)
            catalog = pathlib.Path(directory) / "catalog"
            catalog.mkdir()
            (catalog / "hif_customization_catalog.json").write_text(
                (ROOT / "extensions/hif/catalog/hif_customization_catalog.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (catalog / "cards_customize.json").write_text("{}", encoding="utf-8")
            (catalog / "custom_card_images.json").write_text("[]", encoding="utf-8")
            selection = config / "hif_custom_card_selection.json"
            saved = {
                "cards": {"719": [28]},  # 职业配置存在时不能回退到旧全局名单。
                "profiles": {
                    "集中": {"381": [65]},
                    "全力": {"381": [77]},
                    "好调": {"719": [28]},
                    "元気": {},
                },
            }
            selection.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(PRODUCE, "BASE_DIR", directory), patch.object(PRODUCE, "EXT_DIR", directory):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                action.STEP_LIMIT = 0
                action._heal_before_customization = lambda _context: True
                for index, profession in PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_BY_MAX_HIT.items():
                    with self.subTest(profession=profession):
                        context = _Context({"ProduceHIF__ProduceHIFProfessionFlag": {"max_hit": index}})
                        context.tasker = SimpleNamespace(stopping=True)
                        self.assertTrue(action._run_inner(context, None))
                        expected = saved["profiles"].get(profession, {})
                        self.assertEqual(
                            {str(card["card_id"]): [item["id"] for item in card["menu"]]
                             for card in action.custom_cards.values()}, expected,
                        )
                        self.assertEqual(action._queue, list(action.custom_cards))
                self.assertEqual(json.loads(selection.read_text(encoding="utf-8")), saved)


    def test_custom_card_uses_visible_pending_card_once_then_rescans(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._queue = ["全身全霊", "セッティング", "出演"]
        action._cur_card = "全身全霊"
        action._card_selected = True
        action._try_idx = 6
        action._match_card_name = lambda *_args: "セッティング"
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)

        self.assertEqual(action._handle_select(None, object()), "detail")
        self.assertEqual(action._cur_card, "セッティング")
        self.assertEqual(clicks, [action.DO_CUSTOMIZE_POS])
        self.assertEqual(action.done_cards, set())

        action._current_menu_items = lambda _context: [(100, 900)]
        action._read_total = lambda *_args: 0
        action._confirm_completed = lambda *_args: False
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        self.assertEqual(action._handle_detail_execute(context, object()), "confirm")
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertEqual(action._handle_execution_confirmation(context, object()), "list")
        self.assertEqual(action.done_cards, {"セッティング"})
        self.assertEqual(action._cur_card, "全身全霊")

        action._card_selected = True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertEqual(action._handle_select(None, object()), "pick")
        self.assertEqual(clicks[-1], action.CARD_GRID[1])
        self.assertEqual(action._queue_idx, 1)

        action._match_card_name = lambda *_args: "出演"
        self.assertEqual(action._handle_select(None, object()), "detail")
        self.assertEqual(action._cur_card, "出演")
        self.assertEqual(action._queue, ["セッティング", "出演", "全身全霊"])

    def test_custom_card_missing_targets_stop_after_one_scan(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._queue = ["全身全霊", "セッティング"]
        action._cur_card = "全身全霊"
        action._match_card_name = lambda *_args: "非目标卡"
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)

        action._card_selected = True
        action._try_idx = len(action.CARD_GRID) - 1
        self.assertEqual(action._handle_select(None, object()), "back")
        self.assertTrue(action._done_all)
        self.assertEqual(action.done_cards, set())
        self.assertEqual(set(action.skipped_cards), {"全身全霊", "セッティング"})
        self.assertEqual(set(action.skipped_cards.values()), {"当前列表未找到可定制目标"})
        self.assertEqual(clicks, [action.BACK_POS])

    def test_custom_card_stops_before_greyed_out_tail(self):
        class CardRegion:
            size = 1

            def __init__(self, enabled):
                self.enabled = enabled

            def max(self, axis):
                assert axis == 2
                return self

            def __gt__(self, threshold):
                assert threshold == 180
                return SimpleNamespace(mean=lambda: 0.95 if self.enabled else 0.0)

            def mean(self):
                return 210 if self.enabled else 90

        class CardImage:
            shape = (1280, 720, 3)

            def __getitem__(self, slices):
                x = slices[1].start + 32
                y = slices[0].start + 32
                return CardRegion(y == 624)

        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._queue = ["積み重ね+"]
        action._card_selected = True
        action._try_idx = 3
        action._match_card_name = lambda *_args: "非目标卡"
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)

        self.assertEqual(action._next_selectable_card(CardImage(), 0), 0)
        self.assertEqual(action._next_selectable_card(CardImage(), 4), len(action.CARD_GRID))
        self.assertEqual(action._handle_select(None, CardImage()), "back")
        self.assertEqual(clicks, [action.BACK_POS])
        self.assertTrue(action._done_all)

    def test_custom_card_logs_non_target_name_separately_from_unreadable(self):
        for raw, expected in (("プライド+", "识别到非目标卡「プライド+」"),
                              ("3908", "该格卡名未能识别")):
            with self.subTest(raw=raw):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                action._queue = ["スポットライト+"]
                action._card_selected = True
                def read_name(*_args):
                    action._last_card_name_ocr = raw
                    return None
                action._match_card_name = read_name
                action._click = lambda *_args, **_kwargs: None
                logs = []
                with patch.object(PRODUCE.logger, "info", side_effect=logs.append), \
                     patch.object(PRODUCE.time, "sleep", return_value=None):
                    for _ in range(3):
                        self.assertEqual(action._handle_select(None, object()), "pick")
                self.assertTrue(any(expected in message for message in logs))

    def test_custom_card_does_not_guess_unrelated_name_by_weak_similarity(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action.custom_cards = {"アイドルになります+": {"ocr_markers": []}}
        action._match_card_face = lambda *_args: None
        context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
            hit=True, filtered_results=[SimpleNamespace(text="アイドル魂+")]
        ))
        self.assertIsNone(action._match_card_name(context, object()))

    def test_custom_execution_waits_without_resubmitting(self):
        for frames, result, completed, reason in (
            ([(None, True, False), (None, True, False),
              (None, False, False), (None, False, False)], "list", True, ""),
            ([(None, True, False), (None, True, False),
              (None, False, False)], "list", False, "执行结果未确认"),
            ([(None, False, False), (None, True, False), (None, True, False),
              (None, False, True), (None, False, True)], "list", True, ""),
            ([(None, False, False), (0, False, False), (0, False, False)], "list", True, ""),
            ([(None, False, False), (1, False, False), (1, False, False)], "execute", False, ""),
            ([(None, False, False)], "list", False, "执行结果未确认"),
            ([(2, False, False)], "list", False, "连续不减少"),
        ):
            with self.subTest(frames=frames):
                action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
                action._queue = ["目标卡+"]
                action._cur_card = "目标卡+"
                action.custom_cards = {"目标卡+": {"menu": [{"pos": (360, 929), "id": 24, "name": "项目"}]}}
                clicks = []
                action._click = lambda _context, pos, delay=None: clicks.append(pos)
                action._read_total = lambda _context, image: image[0]
                action._confirm_completed = lambda _context, image: image[1]
                action._at_custom_limit = lambda _context, image: image[2]
                clock = [0.0]
                remaining = iter(frames[1:])
                context = SimpleNamespace(tasker=SimpleNamespace(
                    stopping=False, controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                        wait=lambda: SimpleNamespace(get=lambda: next(remaining, frames[-1]))
                    )),
                ))
                initial = 1 if completed else 2
                with patch.object(PRODUCE.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(PRODUCE.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    self.assertEqual(action._handle_detail_execute(context, (initial, False, False)), "confirm")
                    self.assertEqual(action._handle_execution_confirmation(context, frames[0]), result)
                self.assertEqual(clicks.count(action.EXEC_POS), 1)
                self.assertEqual(clicks.count((360, 929)), 1)
                self.assertEqual(action.done_cards, {"目标卡+"} if completed else set())
                if reason:
                    self.assertIn(reason, action.skipped_cards["目标卡+"])
                self.assertLessEqual(clock[0], 5.0)

    def test_custom_multiple_operations_require_confirmed_decrease(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._queue = ["目标卡+"]
        action._cur_card = "目标卡+"
        action.custom_cards = {"目标卡+": {"menu": [
            {"pos": (140, 929), "id": 1, "name": "项目1"},
            {"pos": (360, 929), "id": 2, "name": "项目2"},
        ]}}
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        action._read_total = lambda _context, image: image
        action._confirm_completed = lambda *_args: False
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: next(images)))
        )))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertEqual(action._handle_detail_execute(context, 2), "confirm")
            images = iter([1])
            self.assertEqual(action._handle_execution_confirmation(context, 1), "execute")
            self.assertEqual(action._handle_detail_execute(context, 1), "confirm")
            images = iter([0])
            self.assertEqual(action._handle_execution_confirmation(context, 0), "list")
        self.assertEqual(clicks, [(140, 929), action.EXEC_POS, (360, 929), action.EXEC_POS, action.BACK_TO_LIST_POS])
        self.assertEqual(action.done_cards, {"目标卡+"})

    def test_custom_unreadable_initial_total_waits_before_first_submission(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._cur_card = "目标卡+"
        action.custom_cards = {"目标卡+": {"menu": [{"pos": (360, 929), "id": 24, "name": "项目"}]}}
        clicks = []
        action._click = lambda _context, pos, delay=None: clicks.append(pos)
        action._read_total = lambda _context, image: image
        action._confirm_completed = lambda *_args: False
        frames = iter([1, 1])
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: next(frames)))
        )))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertEqual(action._handle_detail_execute(context, None), "confirm")
            self.assertEqual(action._handle_execution_confirmation(context, None), "execute")
            self.assertEqual(clicks, [])
            self.assertEqual(action._handle_detail_execute(context, 1), "confirm")
        self.assertEqual(clicks, [(360, 929), action.EXEC_POS])

    def test_custom_confirmation_stop_prevents_cleanup_clicks(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        action._confirm_completed = lambda *_args: False
        action._read_total = lambda *_args: None
        action._at_custom_limit = lambda *_args: False
        clicks = []
        action._click = lambda *_args: clicks.append(True)
        with patch.object(PRODUCE.time, "sleep", side_effect=lambda _seconds: setattr(context.tasker, "stopping", True)):
            self.assertEqual(action._handle_execution_confirmation(context, object()), "stopped")
        self.assertEqual(clicks, [])
        self.assertEqual(action.done_cards, set())
        self.assertEqual(action.skipped_cards, {})

    def test_custom_success_requires_current_card_text_not_just_white(self):
        # Lightweight array stand-in keeps this test independent of installed numpy.
        class Pixels:
            def __getitem__(self, _key): return self
            def astype(self, _kind): return self
            def __sub__(self, _other): return self
            def __gt__(self, _other): return self
            def __lt__(self, _other): return self
            def __and__(self, _other): return self
            def mean(self): return 1.0
        action = PRODUCE.ProduceHIF__ProduceHIFCardCustomAuto()
        action._cur_card = "仕切り直し+"
        for text, expected in (("", False), ("通常の白い画面", False),
                               ("びしっとキメ顔+をカスタマイズしました", False),
                               ("仕切り直し+をカスタマイズしました", True)):
            context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=[SimpleNamespace(text=text)]
            ))
            with patch.object(PRODUCE.np, "maximum", side_effect=lambda left, _right: left, create=True), \
                    patch.object(PRODUCE.np, "minimum", side_effect=lambda left, _right: left, create=True):
                self.assertEqual(action._confirm_completed(context, Pixels()), expected)

    def test_full_power_zenshin_zenrei_template_and_priority(self):
        config = json.loads(
            (ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8")
        )
        # Exercise editable full-power support with synthetic data. Public defaults
        # intentionally contain only the concentration profession.
        zenshin = {"key": "全身全霊+", "priority": 10, "template": "hif/cards/zenshin_zenrei_hand.png"}
        config["priority_profiles"]["全力"] = [zenshin]
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / 'cards_priority.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(PRODUCE, 'CARDS_PRIORITY_CONFIG_PATH', str(path)):
                action._load_config("全力")
        self.assertEqual(action.priority_index["全身全霊+"], zenshin["priority"])
        self.assertEqual(zenshin["template"], "hif/cards/zenshin_zenrei_hand.png")
        self.assertTrue(
            (ROOT / "extensions/hif/resource/base/image/hif/cards/zenshin_zenrei_hand.png").exists()
        )

    def test_hif_card_templates_follow_current_profession(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        config = json.loads((ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8"))
        profiles = config["priority_profiles"]
        for profession in cards.PROFESSION_BY_MAX_HIT.values():
            cards._load_config(profession)
            priority_keys = [entry["key"] for entry in profiles[profession]]
            recognition_keys = config["recognition_profiles"][profession]
            self.assertEqual(
                [entry["key"] for entry in cards.cards_list],
                list(dict.fromkeys([*recognition_keys, *priority_keys])),
            )
            self.assertEqual(set(cards.priority_index), set(priority_keys))

        cards._load_config("集中")
        self.assertNotIn("ジャストアピール+", {entry["key"] for entry in cards.cards_list})
        self.assertNotIn("眠気", {entry["key"] for entry in cards.cards_list})
        box = [265, 884, 180, 250]
        memory_context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
            hit=True, filtered_results=[SimpleNamespace(text="わたし")]
        ))
        self.assertEqual(cards._identify_by_name(memory_context, object(), box), "わたしだけの思い出+")

        config['priority_profiles']['全力'] = [
            {'key': 'ジャストアピール+', 'priority': 7, 'template': 'hif/cards/just_appeal_hand.png'},
            {'key': '眠気', 'priority': 99, 'template': 'hif/cards/nemuke_hand.png'},
        ]
        config['recognition_profiles']['全力'] = ['ジャストアピール+', '眠気']
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / 'cards_priority.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(PRODUCE, 'CARDS_PRIORITY_CONFIG_PATH', str(path)):
                cards._load_config("全力")
        catalog = {entry["key"]: entry for entry in cards.cards_list}
        self.assertNotIn("わたしだけの思い出+", catalog)
        self.assertEqual(catalog["ジャストアピール+"]["template"], "hif/cards/just_appeal_hand.png")
        self.assertEqual(catalog["眠気"]["template"], "hif/cards/nemuke_hand.png")
        configured_rank = next(
            item["priority"] for item in profiles["全力"] if item["key"] == "ジャストアピール+"
        )
        self.assertEqual(cards.priority_index["ジャストアピール+"], configured_rank)
        context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
            hit=True, filtered_results=[SimpleNamespace(text="ジャストアピール+")]
        ))
        self.assertEqual(cards._identify_card(context, object(), box)[0], "ジャストアピール+")

    def test_removing_priority_keeps_card_recognition(self):
        config = json.loads((ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8"))
        key = config["priority_profiles"]["集中"][0]["key"]
        config["priority_profiles"]["集中"] = [
            item for item in config["priority_profiles"]["集中"] if item["key"] != key
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as folder:
            path = pathlib.Path(folder) / "cards_priority.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
                cards._load_config("集中")
        self.assertNotIn(key, cards.priority_index)
        self.assertIn(key, {item["key"] for item in cards.cards_list})

    def test_same_ocr_prefix_after_import_uses_template_fallback(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards.cards_list = [
            {"key": "summer-a", "name": "夏夜", "template": "cards/a.png"},
            {"key": "summer-b", "name": "夏夜", "template": "cards/b.png"},
        ]
        context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: self.fail("ambiguous OCR called"))
        self.assertIsNone(cards._identify_by_name(context, object(), [265, 884, 180, 250]))

    def test_short_ocr_prefix_does_not_capture_longer_name(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards.cards_list = [
            {"key": "short", "name": "わたし", "template": "cards/short.png"},
            {"key": "long", "name": "わたしは", "template": ""},
        ]
        def recognize(*_args, **kwargs):
            self.assertEqual(kwargs["pipeline_override"]["ProduceHIF__ProduceIdentityName"]["expected"], "わたしは")
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="わたしは")])
        context = SimpleNamespace(run_recognition=recognize)
        self.assertEqual(cards._identify_by_name(context, object(), [265, 884, 180, 250]), "long")

    def test_strong_profession_uses_its_pointer_template(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards.profession = "強気"
        overrides = []
        def recognize(*_args, **kwargs):
            override = kwargs["pipeline_override"]["ProduceHIF__ProduceIdentityMatch"]
            overrides.append(override)
            return SimpleNamespace(hit=override["template"] == "hif/produce/hif_strong_pointer.png")

        context = SimpleNamespace(
            run_recognition=recognize
        )

        self.assertEqual(cards._read_active_pointer_state(context, object()), "強気")
        self.assertEqual(
            overrides[-1]["template"],
            "hif/produce/hif_strong_pointer.png",
        )
        self.assertTrue(
            (ROOT / "extensions/hif/resource/base/image/hif/produce/hif_strong_pointer.png").exists()
        )

    def test_conserve_pointer_uses_current_state_icon(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        templates = []
        def recognize(*_args, **kwargs):
            override = kwargs["pipeline_override"]["ProduceHIF__ProduceIdentityMatch"]
            templates.append(override["template"])
            self.assertEqual(override["roi"], [0, 220, 95, 85])
            return SimpleNamespace(hit=override["template"] == "hif/produce/hif_conserve_pointer.png")
        self.assertEqual(cards._read_active_pointer_state(SimpleNamespace(run_recognition=recognize), object()), "温存")
        self.assertEqual(templates[-1], "hif/produce/hif_conserve_pointer.png")
        self.assertTrue((ROOT / "extensions/hif/resource/base/image/hif/produce/hif_conserve_pointer.png").exists())

    def test_wind_card_is_allowed_on_strong_pointer_only_in_last_turn(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._load_config("全力")
        cards.use_conditions["わたしは、風！+"] = [
            {"state": "全力", "remaining_turns_lte": 3},
            {"state": "強気", "remaining_turns_lte": 1},
        ]
        wind = [{
            "key": "わたしは、風！+",
            "box": [100, 884, 180, 248],
            "score": 1.0,
            "conf": 0.9,
        }]

        cards._active_pointer_state = "強気"
        cards._last_turn_count = 2
        self.assertEqual(cards._decide_full_power(wind)["type"], "skip")
        cards._last_turn_count = 1
        self.assertEqual(cards._decide_full_power(wind)["key"], "わたしは、風！+")

        cards._active_pointer_state = "全力"
        cards._last_turn_count = 3
        self.assertEqual(cards._decide_full_power(wind)["key"], "わたしは、風！+")
        cards._last_turn_count = 4
        self.assertEqual(cards._decide_full_power(wind)["type"], "skip")
        cards._active_pointer_state = "温存"
        cards._last_turn_count = 1
        self.assertEqual(cards._decide_full_power(wind)["type"], "skip")

    def test_three_use_condition_groups_match_any_complete_group(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.profession = "強気"
        action.use_conditions = {"受限卡+": [
            {"state": "全力", "remaining_turns_lte": 3},
            {"state": "強気", "remaining_turns_lte": 1},
            {"state": "温存", "remaining_turns_lte": 2},
        ]}
        for state, turns, expected in [
            ("全力", 4, False), ("全力", 3, True),
            ("強気", 2, False), ("強気", 1, True),
            ("温存", 2, True), ("温存", 3, False),
            (None, 1, False), ("温存", None, False),
        ]:
            with self.subTest(state=state, turns=turns):
                action._active_pointer_state = state
                action._last_turn_count = turns
                self.assertEqual(action._allowed_by_use_conditions("受限卡+"), expected)

    def test_use_turn_limit_filters_cards_in_every_profession(self):
        card = {"key": "受限卡+", "box": [100, 884, 180, 248], "score": 1.0, "conf": 0.9}
        for profession in PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_BY_MAX_HIT.values():
            with self.subTest(profession=profession):
                action = PRODUCE.ProduceHIF__ProduceCardsAuto()
                action.profession = profession
                action.combo_first = action.combo_second = None
                action.priority_index[card["key"]] = 0
                action.use_conditions = {card["key"]: [{"remaining_turns_lte": 3}]}
                action._last_turn_count = 4
                self.assertEqual(action._decide(None, [card])["type"], "skip")
                action._last_turn_count = 3
                self.assertEqual(action._decide(None, [card])["key"], card["key"])
                action._last_turn_count = None
                self.assertFalse(action._allowed_by_use_conditions(card["key"]))

    def test_full_power_has_no_implicit_use_conditions(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            config = json.loads((ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8"))
            config["use_condition_profiles"] = {}
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                action._load_config("全力")
        self.assertEqual(action.use_conditions, {})
        action._active_pointer_state = "全力"
        wind = [{"key": "わたしは、風！+", "box": [100, 884, 180, 248], "conf": 0.9}]
        action._last_turn_count = 4
        self.assertEqual(action._decide_full_power(wind)["key"], "わたしは、風！+")
        action.use_conditions["わたしは、風！+"] = [{"state": "全力", "remaining_turns_lte": 5}]
        self.assertEqual(action._decide_full_power(wind)["key"], "わたしは、風！+")

        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            config = json.loads((ROOT / "extensions/hif/defaults/cards_priority.json").read_text(encoding="utf-8"))
            config["use_condition_profiles"]["全力"]["わたしは、風！+"] = []
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                action._load_config("全力")
        self.assertEqual(action.use_conditions["わたしは、風！+"], [])

    def test_habatake_state_rule_can_be_changed_or_cleared(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._load_config("全力")
        card = [{"key": "羽ばたけ！+", "box": [100, 884, 180, 248], "conf": 0.9}]
        action._active_pointer_state = "強気"
        action.use_conditions["羽ばたけ！+"] = [{"state": "全力"}]
        self.assertEqual(action._decide_full_power(card)["type"], "skip")
        action.use_conditions["羽ばたけ！+"] = [{"state": "強気"}]
        self.assertEqual(action._decide_full_power(card)["key"], "羽ばたけ！+")
        action._active_pointer_state = "温存"
        action.use_conditions["羽ばたけ！+"] = []
        self.assertEqual(action._decide_full_power(card)["key"], "羽ばたけ！+")

    def test_fallback_does_not_play_identified_turn_limited_card(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action.use_conditions = {"受限卡+": [{"remaining_turns_lte": 3}]}
        action._last_turn_count = 4
        action._get_card_info = lambda _results: (0, 0, 1, None, [100, 884, 180, 248])
        action._identify_card = lambda *_args: ("受限卡+", 1.0)
        action._play_a_card = lambda *_args: self.fail("restricted card must not be played")
        skipped = []
        action._skip_round = lambda _context: skipped.append(True)
        action._play_fallback(None, object(), [])
        self.assertEqual(skipped, [True])

    def test_all_professions_require_two_stable_full_hand_snapshots(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        hand = [
            {"key": "存在感+", "box": [100, 884, 180, 248]},
            {"key": "達成感+", "box": [320, 884, 180, 248]},
        ]
        shifted_hand = [
            {"key": "存在感+", "box": [112, 884, 180, 248]},
            {"key": "達成感+", "box": [330, 884, 180, 248]},
        ]
        self.assertEqual(cards._confirm_card_hand(hand), "wait")
        self.assertEqual(cards._confirm_card_hand(shifted_hand), "stable")

        changed_hand = [
            {"key": "存在感+", "box": [100, 884, 180, 248]},
            {"key": "覚悟+", "box": [320, 884, 180, 248]},
        ]
        self.assertEqual(cards._confirm_card_hand(hand), "wait")
        self.assertEqual(cards._confirm_card_hand(changed_hand), "wait")

    def test_unstable_card_timeout_accepts_last_recognition(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        first_hand = [{"key": "存在感+", "box": [100, 884, 180, 248]}]
        last_hand = [{"key": "達成感+", "box": [320, 884, 180, 248]}]

        with patch.object(PRODUCE.time, "time", side_effect=[100.0, 115.0]):
            self.assertEqual(cards._confirm_card_hand(first_hand), "wait")
            self.assertEqual(cards._confirm_card_hand(last_hand), "stable")

    def test_full_power_accherellando_uses_configured_priority(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._load_config("全力")
        action.priority_index["アッチェレランド+"] = 17
        action.priority_index["レスポンスの基本+"] = 16
        action.conditional_priority_rules = {}
        cards = [
            {"key": "アッチェレランド+", "box": [1, 900, 100, 100], "conf": 0.1},
            {"key": "レスポンスの基本+", "box": [2, 900, 100, 100], "conf": 0.9},
        ]

        for turns, full_power in ((5, 10), (4, 10), (1, 9)):
            action._last_turn_count = turns
            action._full_power_value = full_power
            self.assertEqual(action._decide_full_power(cards)["key"], "レスポンスの基本+")

    def test_full_power_conditional_priority_any_and_all(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._load_config("全力")
        action.priority_index["アッチェレランド+"] = 17
        card = {"key": "アッチェレランド+"}
        rule = {"priority": 7, "remaining_turns_lte": 4, "full_power_lt": 10, "mode": "any"}
        action.conditional_priority_rules = {card["key"]: rule}
        action._full_power_value = 11
        self.assertEqual(action._full_power_priority(card, 5)[:2], (17, 17))
        triggered = action._full_power_priority(card, 4)
        self.assertEqual(triggered[:2], (17, 7))
        self.assertIn("命中=剩余回合≤4", triggered[2])
        action._full_power_value = 9
        self.assertEqual(action._full_power_priority(card, 5)[:2], (17, 7))
        rule["mode"] = "all"
        self.assertEqual(action._full_power_priority(card, 5)[:2], (17, 17))
        self.assertEqual(action._full_power_priority(card, 4)[:2], (17, 7))

    def test_unknown_card_uses_profession_setting(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._load_config("全力")
        action.priority_index["眠気"] = 15
        cards = [
            {"key": None, "box": [1, 900, 100, 100], "conf": 0.9},
            {"key": "眠気", "box": [2, 900, 100, 100], "conf": 0.1},
        ]
        action.unknown_priority = 20
        self.assertEqual(action._decide_full_power(cards)["key"], "眠気")
        action.unknown_priority = 5
        self.assertIsNone(action._decide_full_power(cards)["key"])

        action._load_config("集中")
        action.combo_first = action.combo_second = None
        cards[1]["key"] = "存在感+"
        action.priority_index["存在感+"] = 5
        action.unknown_priority = 0
        self.assertIsNone(action._decide(None, cards)["key"])
        action.unknown_priority = 30
        self.assertEqual(action._decide(None, cards)["key"], "存在感+")

    def test_hif_full_drink_default_off_does_not_read_priority_profiles(self):
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        action._screencap = lambda _: object()
        action._window_open = lambda *_: True
        context = SimpleNamespace(get_node_data=lambda _: None)
        with patch('extensions.hif.drink_keep.KeepDrinkFlow') as flow, \
             patch.object(PRODUCE, '_hif_drink_priority_names', side_effect=AssertionError('Priority read while off')):
            flow.return_value.run.return_value = True
            self.assertTrue(action.run(context, None))
            self.assertIsNone(flow.call_args.args[2])

    def test_hif_full_drink_retry_logs_are_throttled(self):
        action = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto
        action._log_times.clear()
        messages = []
        logger = SimpleNamespace(warning=lambda message: messages.append(message))
        with patch.object(PRODUCE, "logger", logger), \
             patch.object(PRODUCE.time, "time", side_effect=[100.0, 105.0, 131.0]):
            action._log_throttled("timeout", "warning", "first")
            action._log_throttled("timeout", "warning", "suppressed")
            action._log_throttled("timeout", "warning", "later")
        self.assertEqual(messages, ["first", "later"])
        action._log_times.clear()

    def test_drink_actions_do_not_run_on_the_other_event_screen(self):
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            ),
        )
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))

        receive = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        receive._is_receive_drink_screen = lambda *_args: False
        receive._click = lambda *_args: self.fail("非P饮料领取页不应点击")
        self.assertFalse(receive.run(context, None))

        limit = PRODUCE.ProduceHIF__ProduceHIFKeepDrinkAuto()
        limit._window_open = lambda *_args: False
        self.assertFalse(limit.run(context, None))

        support_card = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto()
        support_card._is_card_screen = lambda *_args: False
        support_card._is_supply_scene = lambda *_args: True
        support_card._wait_scene_stable = lambda *_args: False
        support_card._click = lambda *_args: self.fail("非技能卡领取页不应点击")
        self.assertFalse(support_card.run(context, None))

        pipeline = json.loads(
            (ROOT / "extensions/hif/resource/base/pipeline/HIF.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            pipeline["ProduceHIF__ProduceHIFDrinkFlag"]["recognition"]["param"]["expected"],
            "受け取るPドリンク",
        )
        self.assertEqual(
            pipeline["ProduceHIF__ProduceHIFDrinkFullKeepFlag"]["recognition"]["param"]["expected"],
            "Pドリンク所持上限",
        )

    def test_supply_transition_uses_click_1_twice_before_each_reward_page(self):
        actions = []
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )
        )
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller),
            run_action=actions.append,
        )
        supply = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto()
        supply._is_card_screen = lambda *_args: False
        supply._is_supply_scene = lambda *_args: True
        supply._wait_scene_stable = lambda *_args: True
        supply._wait_for_reward_prompt = lambda *_args: True
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_is_receive_drink_screen", return_value=False), \
                patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(supply.run(context, None))  # 支给后、饮料前
            self.assertTrue(supply.run(context, None))  # 饮料后、领卡前
        self.assertEqual(actions, ["ProduceHIF__Click_1"] * 4)

        with patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_is_receive_drink_screen", return_value=True):
            self.assertTrue(supply.run(context, None))
        self.assertEqual(actions, ["ProduceHIF__Click_1"] * 4)

    def test_supply_transition_retries_when_two_clicks_do_not_open_reward_page(self):
        actions = []
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )
        )
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller),
            run_action=actions.append,
        )
        supply = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto()
        supply._is_card_screen = lambda *_args: False
        supply._is_supply_scene = lambda *_args: True
        supply._wait_scene_stable = lambda *_args: True
        prompts = iter((False, True))
        supply._wait_for_reward_prompt = lambda *_args: next(prompts)
        with patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_is_receive_drink_screen", return_value=False), \
                patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(supply.run(context, None))
        self.assertEqual(actions, ["ProduceHIF__Click_1"] * 4)

    def test_supply_card_receipt_does_not_retry_transition_on_next_day(self):
        actions = []
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )
        )
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller),
            run_action=actions.append,
        )
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=321))
        supply = PRODUCE.ProduceHIF__ProduceHIFSupplyCardAuto()
        on_card_page = [True]
        supply._is_card_screen = lambda *_args: on_card_page[0]
        supply._select_wanted_card = lambda *_args: True
        supply._click = lambda *_args: None
        supply._wait_scene_stable = lambda *_args: self.fail("领卡后不应再推进过场")
        try:
            self.assertTrue(supply.run(context, argv))
            on_card_page[0] = False
            with patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_is_receive_drink_screen", return_value=False):
                self.assertTrue(supply.run(context, argv))
                supply.__class__._card_received_until = 0
                supply._is_supply_scene = lambda *_args: False
                self.assertTrue(supply.run(context, argv))
            self.assertEqual(actions, [])
        finally:
            supply.__class__._card_received_task_id = None
            supply.__class__._card_received_until = 0

    def test_removed_legacy_drink_discard_helpers_stay_deleted(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto
        for name in (
            "_choose_discard_pos",
            "_find_obtain_pos",
            "_find_slot",
            "_nearest_slot",
            "SUTERU_POS",
            "CONFIRM_HAI_POS",
        ):
            self.assertFalse(hasattr(drink, name), name)

    def test_remaining_drink_waits_for_initial_stable_list(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        waits = []
        cards._wait_for_drink_list_stable = lambda _context, after_use=True: waits.append(after_use) or False
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(cards._drink_buff(context))
        self.assertEqual(waits, [False])

    def test_initial_drink_stability_accepts_an_empty_bar(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()

        class _Controller:
            def post_screencap(self):
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        frames = iter([[], [], []])
        calls = []
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=_Controller(), stopping=False),
            run_recognition=lambda *_args, **_kwargs: calls.append(True) or SimpleNamespace(
                hit=True, filtered_results=next(frames)
            ),
        )
        clock = iter(index * 0.1 for index in range(100))
        with patch.object(PRODUCE.time, "time", side_effect=lambda: next(clock)), \
             patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._wait_for_drink_list_stable(context, after_use=False))
        self.assertEqual(len(calls), cards.DRINK_LIST_INITIAL_STABLE_COUNT)

    def test_drink_detail_retries_the_same_bottle_before_giving_up(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        clicks = []
        controller = SimpleNamespace(
            post_click=lambda x, y: clicks.append((x, y)) or SimpleNamespace(wait=lambda: None)
        )
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
        opened = iter([False, True])
        cards._wait_for_drink_detail = lambda *_args, **_kwargs: next(opened)

        self.assertTrue(cards._open_drink_detail(context, (61, 1200), "测试饮料"))
        self.assertEqual(clicks, [(61, 1200), (61, 1200)])

    def test_turn_six_uses_all_sembri(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()

        class _Controller:
            def post_screencap(self):
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        boxes = iter([
            [SimpleNamespace(box=[10, 1150, 40, 80])],
            [SimpleNamespace(box=[10, 1150, 40, 80])],
            [],
        ])
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=_Controller(), stopping=False),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=next(boxes)
            ),
        )
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._open_drink_detail = lambda *_args, **_kwargs: True
        cards._match_sembri_bottle = lambda *_args, **_kwargs: True
        used = []
        cards._use_drink = lambda *_args, **_kwargs: used.append(True) or True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._drink_sembri(context, use_all=True))
        self.assertEqual(len(used), 2)

    def test_full_power_first_turn_uses_all_chai(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()

        class _Controller:
            def post_screencap(self):
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        boxes = iter([
            [SimpleNamespace(box=[10, 1150, 40, 80])],
            [SimpleNamespace(box=[10, 1150, 40, 80])],
            [],
        ])
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=_Controller(), stopping=False),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=next(boxes)
            ),
        )
        waits = []
        cards._wait_for_drink_list_stable = lambda *_args, **kwargs: waits.append(
            kwargs.get("after_use", True)
        ) or True
        cards._open_drink_detail = lambda *_args, **_kwargs: True
        cards._read_full_power_chai_name = lambda *_args, **_kwargs: cards.FULL_POWER_CHAI_NAME
        used = []
        cards._use_drink = lambda *_args, **_kwargs: used.append(True) or True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._drink_full_power_chai(context))
        self.assertEqual(len(used), 2)
        self.assertEqual(waits, [False, True, True])

    def test_buff_rescans_after_a_drink_detail_fails_to_open(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        bottle = SimpleNamespace(box=[10, 1150, 40, 80])
        boxes = iter([[bottle], [bottle], []])
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=SimpleNamespace(
                    post_screencap=lambda: SimpleNamespace(
                        wait=lambda: SimpleNamespace(get=lambda: object())
                    )
                ),
                stopping=False,
            ),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=next(boxes)
            ),
        )
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        opened = iter([False, True])
        cards._open_drink_detail = lambda *_args, **_kwargs: next(opened)
        cards._read_drink_name = lambda *_args: "ブーストエキス"
        used = []
        cards._use_drink = lambda *_args: used.append(True) or True

        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._drink_buff(context))
        self.assertEqual(used, [True])

    def test_sembri_rescans_after_a_drink_detail_fails_to_open(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        bottle = SimpleNamespace(box=[10, 1150, 40, 80])
        boxes = iter([[bottle], [bottle], []])
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=SimpleNamespace(
                    post_screencap=lambda: SimpleNamespace(
                        wait=lambda: SimpleNamespace(get=lambda: object())
                    )
                ),
                stopping=False,
            ),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=next(boxes)
            ),
        )
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        opened = iter([False, True])
        cards._open_drink_detail = lambda *_args, **_kwargs: next(opened)
        cards._match_sembri_bottle = lambda *_args: True
        used = []
        cards._use_drink = lambda *_args: used.append(True) or True

        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._drink_sembri(context, use_all=True))
        self.assertEqual(used, [True])

    def test_full_power_chai_rescans_after_a_drink_detail_fails_to_open(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        bottle = SimpleNamespace(box=[10, 1150, 40, 80])
        boxes = iter([[bottle], [bottle], []])
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=SimpleNamespace(
                    post_screencap=lambda: SimpleNamespace(
                        wait=lambda: SimpleNamespace(get=lambda: object())
                    )
                ),
                stopping=False,
            ),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=next(boxes)
            ),
        )
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        opened = iter([False, True])
        cards._open_drink_detail = lambda *_args, **_kwargs: next(opened)
        cards._read_full_power_chai_name = lambda *_args: cards.FULL_POWER_CHAI_NAME
        used = []
        cards._use_drink = lambda *_args: used.append(True) or True

        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._drink_full_power_chai(context))
        self.assertEqual(used, [True])

    def test_hand_hold_layout_is_separate_from_motibe_deck_hold(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._full_power_hold_source = cards.FULL_POWER_DECK_HOLD_SOURCE
        self.assertEqual(cards.FULL_POWER_HAND_HOLD_ROI, [50, 390, 340, 150])
        self.assertEqual(cards.FULL_POWER_HAND_HOLD_FIRST_POS, (139, 458))
        self.assertNotEqual(cards.FULL_POWER_HAND_HOLD_ROI, cards.FULL_POWER_HOLD_ROI)
        self.assertNotEqual(cards.FULL_POWER_HAND_HOLD_FIRST_POS, cards.FULL_POWER_HOLD_FIRST_POS)

    def test_hand_hold_position_is_only_used_after_deck_hold_retries(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._full_power_hold_source = "保留手札+"
        cards._full_power_hand_hold_pending = True

        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        controller = _Controller()
        tasks = []
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller, stopping=False),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=False, filtered_results=[]
            ),
            run_task=lambda task: tasks.append(task),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            for _ in range(cards.FULL_POWER_HAND_HOLD_RETRY_LIMIT):
                self.assertTrue(cards._handle_full_power_hold(context, object()))
        self.assertEqual(
            controller.clicks,
            [cards.FULL_POWER_HOLD_FIRST_POS] * (cards.FULL_POWER_HAND_HOLD_RETRY_LIMIT - 1)
            + [cards.FULL_POWER_HAND_HOLD_FIRST_POS],
        )
        self.assertEqual(tasks, ["ProduceHIF__ProduceMoveCards"] * cards.FULL_POWER_HAND_HOLD_RETRY_LIMIT)

    def test_unknown_hold_does_not_scan_motibe_targets(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._full_power_hold_source = cards.FULL_POWER_UNKNOWN_HOLD_SOURCE

        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        controller = _Controller()
        tasks = []
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller, stopping=False),
            run_recognition=lambda *_args, **_kwargs: self.fail("未知卡不应进行四目标模板识别"),
            run_task=lambda task: tasks.append(task),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._handle_full_power_hold(context, object()))
        self.assertEqual(controller.clicks, [cards.FULL_POWER_HOLD_FIRST_POS])
        self.assertEqual(tasks, ["ProduceHIF__ProduceMoveCards"])

    def test_motibe_hold_source_survives_transition_before_hold_page(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._full_power_hold_source = cards.FULL_POWER_DECK_HOLD_SOURCE
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=SimpleNamespace(), stopping=False),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(hit=False),
        )

        self.assertFalse(cards._handle_move_cards(context, object()))
        self.assertEqual(cards._full_power_hold_source, cards.FULL_POWER_DECK_HOLD_SOURCE)

    def test_motibe_hold_waits_for_move_button_before_moving(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._full_power_hold_source = cards.FULL_POWER_DECK_HOLD_SOURCE
        recognitions = []
        moves = []

        class _Controller:
            def post_click(self, *_pos):
                return self

            def post_screencap(self):
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        def recognize(name, *_args, **_kwargs):
            recognitions.append(name)
            if name == "ProduceHIF__ProduceMoveCards":
                return SimpleNamespace(hit=True)
            return SimpleNamespace(
                hit=True, filtered_results=[SimpleNamespace(box=[100, 500, 100, 100])]
            )

        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=_Controller(), stopping=False),
            run_recognition=recognize,
            run_task=lambda task: moves.append(task),
        )
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._handle_full_power_hold(context, object()))
        self.assertEqual(recognitions.count("ProduceHIF__ProduceMoveCards"), 2)
        self.assertEqual(moves, ["ProduceHIF__ProduceMoveCards"])

    def test_main_screen_completion_does_not_click_before_loop(self):
        pipeline = json.loads(
            (ROOT / "extensions/hif/resource/base/pipeline/HIF.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pipeline["ProduceHIF__ProduceHIFMainScreenFlag"]["action"]["type"], "DoNothing")
        self.assertEqual(pipeline["ProduceHIF__ProduceHIFMainScreenFlag"]["next"],
                         ["ProduceHIF__ProduceLoop", "ProduceHIF__TaskFinished"])
        self.assertEqual(pipeline["ProduceHIF__TaskFinished"]["action"]["type"], "StopTask")
        self.assertFalse(pipeline["ProduceHIF__TaskFinished"].get("next"))
        self.assertEqual(pipeline["ProduceHIF__ProduceHIFSupplyFlag"]["action"]["type"], "Click")

    def test_first_lesson_resets_previous_round_swap_state(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 2
        swap._delete_card_a = "a"
        swap._delete_card_b = "b"
        swap._delete_card_delete_attempted = True
        clicks = []
        action = PRODUCE.ProduceHIF__ProduceHIFLessonAuto()
        action._get_screenshot = lambda _context: object()
        action._read_day_counter = lambda _context, _image: 6
        action._read_lesson_attr = lambda _context, _node: "Vo"
        action._lesson_buttons_ready = lambda *_args: True
        action._arm_recognition_gate = lambda _image: None
        action._click_pos = lambda _context, x, y: clicks.append((x, y))

        self.assertTrue(action.run(SimpleNamespace(), None))
        self.assertEqual(swap._delete_card_exchange_count, 0)
        self.assertIsNone(swap._delete_card_a)
        self.assertIsNone(swap._delete_card_b)
        self.assertFalse(swap._delete_card_delete_attempted)
        self.assertEqual(clicks, [action.LESSON_BUTTONS[0]])
        self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index, 1)

    def test_second_lesson_keeps_current_round_swap_state(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 1
        swap._delete_card_a = "a"
        swap._delete_card_b = None
        swap._delete_card_delete_attempted = False
        action = PRODUCE.ProduceHIF__ProduceHIFLessonAuto()
        action._get_screenshot = lambda _context: object()
        action._read_day_counter = lambda _context, _image: 3
        action._read_lesson_attr = lambda _context, _node: "Vi"
        action._lesson_buttons_ready = lambda *_args: True
        action._arm_recognition_gate = lambda _image: None
        action._click_pos = lambda _context, _x, _y: None

        self.assertTrue(action.run(SimpleNamespace(), None))
        self.assertEqual(swap._delete_card_exchange_count, 1)
        self.assertEqual(swap._delete_card_a, "a")
        self.assertIsNone(swap._delete_card_b)
        self.assertFalse(swap._delete_card_delete_attempted)
        self.assertEqual(PRODUCE.ProduceHIF__ProduceHIFOptionAuto._lesson_index, 2)


    def test_candidate_scan_waits_for_stable_name_and_ocr_clicks_next(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        action = swap()
        clicked = []
        action._candidate_slots = lambda _context: [(10, 10)]
        action._click = lambda _context, pos: clicked.append(pos)
        action._wait_stable_card_name = lambda _context: (object(), "頂点へ+")
        action._read_ocr = lambda *_args: ""

        class _Controller:
            def post_screencap(self):
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=_Controller()),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=False, filtered_results=[]
            ),
        )
        self.assertTrue(action._scan(context, [{"name": "頂点へ", "effect": []}], None))
        self.assertEqual(clicked, [(10, 10), swap.NEXT_POS])

    def test_candidate_scan_uses_name_only_with_optional_plus(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto()
        self.assertEqual(action.NAME_ROI, [185, 505, 370, 50])
        action._candidate_slots = lambda _context: [(10, 10)]
        action._click = lambda *_args: None
        action._read_ocr = lambda *_args: self.fail("候选卡不应读取效果文字")
        action._click_next = lambda _context: None
        action._wait_stable_card_name = lambda _context: (object(), "積み重ね")
        specs = [{"name": "積み重ね+", "effect": ["无关效果"], "template": "missing.png"}]
        self.assertTrue(action._scan(_Context({}), specs, None))
        action._wait_stable_card_name = lambda _context: (object(), "")
        action._wait_candidate_page_ready = lambda _context: True
        self.assertFalse(action._scan(_Context({}), specs, None))

    def test_candidate_stable_name_reads_title_roi(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto()
        rois = []
        action._read_ocr = lambda _context, _image, roi, _expected: rois.append(roi) or "覚悟"
        controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(get=lambda: object())
        ))
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            _, name = action._wait_stable_card_name(context)
        self.assertEqual(name, "覚悟")
        self.assertEqual(rois, [action.NAME_ROI] * action.DETAIL_STABLE_COUNT)

    def test_candidate_name_retry_can_recover_and_unread_still_continues(self):
        for replies, expected, clicks in ((["", "目标卡"], True, 2),
                                         (["", "", "目标卡"], True, 3),
                                         (["", "", "其他卡"], False, 3)):
            action = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto()
            names = iter(replies)
            positions = []
            action._candidate_slots = lambda _context: [(10,10),(20,20)] if len(replies)==3 else [(10,10)]
            action._click = lambda _context,pos: positions.append(pos)
            action._wait_stable_card_name = lambda _context: (None,next(names))
            action._wait_candidate_page_ready = lambda _context: True
            action._click_next = lambda _context: None
            self.assertEqual(action._scan(_Context({}),[{"name":"目标卡"}],None),expected)
            self.assertEqual(len(positions),clicks)
            self.assertEqual(positions[:2],[(10,10),(10,10)])

    def test_redraw_waits_for_candidate_page_before_next_scan(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto()
        image = object()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda:SimpleNamespace(get=lambda:image)))),
            run_recognition=lambda *_a,**_k: SimpleNamespace(hit=True,filtered_results=[SimpleNamespace(box=[580,1070,50,40])]))
        events = []
        action._click = lambda *_a: events.append("click")
        action._wait_candidate_page_ready = lambda *_a: events.append("wait") or True
        self.assertTrue(action._click_redraw(context))
        self.assertEqual(events,["click","wait"])

    def test_candidate_ready_waits_for_animation_and_button(self):
        class Region:
            size = 1
            def __init__(self,value):self.value=value
            def copy(self):return self
        class Image:
            shape=(1280,720,3)
            def __init__(self,value):self.region=Region(value)
            def __getitem__(self,_key):return self.region
        action = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto()
        frames=iter([Image(v) for v in [0,10,20,20,20,20]])
        seen=[]
        def capture():
            frame=next(frames);seen.append(frame);return frame
        context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=SimpleNamespace(
            post_screencap=lambda:SimpleNamespace(wait=lambda:SimpleNamespace(get=capture)))))
        action._read_ocr=lambda *_a:"次へ"
        cv=SimpleNamespace(NORM_L1=1,norm=lambda a,b,_type:abs(a.value-b.value))
        with patch.object(PRODUCE,"cv2",cv),patch.object(PRODUCE.time,"sleep",return_value=None):
            self.assertTrue(action._wait_candidate_page_ready(context))
        self.assertEqual(len(seen),6)

    def test_candidate_redraw_exhaustion_selects_first_slot_in_both_layouts(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        for teacher, first_pos in ((False, swap.CARD_SLOTS[0]),
                                   (True, swap.TEACHER_EVENT_CARD_SLOTS[0])):
            with self.subTest(teacher=teacher):
                action = swap()
                context = _Context({swap.TEACHER_EVENT_NODE: {"enabled": teacher}})
                events = []
                action._load_swap_want = lambda _context: [{"name": "目标卡"}]
                action._scan = lambda *_args: events.append("scan") or False
                action._click_redraw = lambda _context: events.append("redraw") or True
                action._click = lambda _context, pos: events.append(("click", pos))
                action._click_next = lambda _context: events.append("next")
                self.assertTrue(action.run(context, None))
                self.assertEqual(events.count("scan"), 1 + action.REDRAW_MAX)
                self.assertEqual(events.count("redraw"), action.REDRAW_MAX)
                self.assertEqual(events[-2:], [("click", first_pos), "next"])

    def test_second_exchange_reselects_a_before_changing(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto

        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        controller = _Controller()
        context = _Context({swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE: {"enabled": True}})
        context.tasker = SimpleNamespace(controller=controller)
        swap._delete_card_exchange_count = 1
        swap._delete_card_a = "脚光"
        action = exchange()
        action.CARD_GRID = [(10, 10)]
        names = iter(["S脚光+D", "S脚光+D"])
        action._wait_stable_name = lambda _context: (object(), next(names))
        action._confirm_exchange = lambda _context, _image: True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action.run(context, None))
        self.assertEqual(controller.clicks, [(10, 10), (10, 10)])

    def test_exchange_name_ocr_reads_only_title_without_requiring_plus(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        self.assertEqual(action.NAME_ROI, [190, 260, 420, 48])
        for name in ("アイドル魂+", "達成感", "わたしは、風！+"):
            with self.subTest(name=name):
                def recognize(_node, _image, **kwargs):
                    self.assertEqual(kwargs["pipeline_override"]["ProduceHIF__ProduceRecognitionScore"]["roi"], action.NAME_ROI)
                    return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text=name)])

                self.assertEqual(action._read_name(SimpleNamespace(run_recognition=recognize), object()), name)

    def test_exchange_basic_fallback_rechecks_name_after_reselection(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action.CARD_GRID = [(10, 10), (20, 20)]
        action._replacement_targets = lambda _context: ["面板卡+", action.TARGET]

        class Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        for confirmed_name, expected in (("自己管理の基本", True), ("達成感", False)):
            with self.subTest(confirmed_name=confirmed_name):
                controller = Controller()
                context = _Context({})
                context.tasker = SimpleNamespace(controller=controller)
                names = iter(["自己管理の基本", "達成感", confirmed_name])
                action._wait_stable_name = lambda _context: (object(), next(names))
                action._confirm_exchange = lambda *_args: True
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertEqual(action.run(context, None), expected)
                self.assertEqual(controller.clicks, [(10, 10), (20, 20), (10, 10)])

    def test_remembered_card_name_strips_known_ui_affixes_only(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        cases = {
            "脚光-2+": "脚光",
            "S脚光+D": "脚光",
            "S成功への道筋+5": "成功への道筋",
            "魅惑の視線M-3+": "魅惑の視線",
            "アドリブ-4X": "アドリブ",
            "7ひと呼吸+": "ひと呼吸",
            "頂点へ+": "頂点へ",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(swap._canonical_remembered_card_name(raw), expected)
                self.assertTrue(swap.remembered_card_name_hit(raw, expected))
        self.assertFalse(swap.remembered_card_name_hit("脚光", "脚本"))
        self.assertFalse(swap.remembered_card_name_hit("国民的アイドル", "アイドル"))

    def test_second_exchange_two_passes_falls_back_to_basic_then_first_card(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        exchange = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto

        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        def run_case(names):
            controller = _Controller()
            context = _Context({swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE: {"enabled": True}})
            context.tasker = SimpleNamespace(controller=controller)
            swap._delete_card_exchange_count = 1
            swap._delete_card_a = "目标卡"
            action = exchange()
            action.CARD_GRID = [(10, 10), (20, 20)]
            names_iter = iter(names)
            action._wait_stable_name = lambda _context: (object(), next(names_iter))
            action._confirm_exchange = lambda _context, _image: True
            with patch.object(PRODUCE.time, "sleep", return_value=None):
                self.assertTrue(action.run(context, None))
            return controller.clicks

        # 两轮均未命中目标时，已识别的基本卡优先于第一格。
        self.assertEqual(
            run_case(["其他", "パフォーマンスの基本+", "其他", "其他", "パフォーマンスの基本+"])[-1],
            (20, 20),
        )
        # 两轮都没有目标或基本卡时，才替换第一格。
        self.assertEqual(
            run_case(["其他", "其他", "其他", "其他", "其他"])[-1],
            (10, 10),
        )

    def test_exchange_panel_order_beats_grid_order_and_basic(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 0
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps({"swap_out_priority_profiles": {
                "集中": ["前のカード+", "後のカード+"]
            }}), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                action.CARD_GRID = [(10, 10), (20, 20), (30, 30)]
                names = {(10, 10): "パフォーマンスの基本+",
                         (20, 20): "後のカード+", (30, 30): "前のカード+"}

                class Controller:
                    def __init__(self):
                        self.clicks = []

                    def post_click(self, x, y):
                        self.clicks.append((x, y))
                        return self

                    def wait(self):
                        return self

                controller = Controller()
                context = _Context({PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 2}})
                context.tasker = SimpleNamespace(controller=controller)
                action._wait_stable_name = lambda _context: (object(), names[controller.clicks[-1]])
                action._confirm_exchange = lambda _context, _image: True
                with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE, "logger") as logs:
                    self.assertTrue(action.run(context, None))
                self.assertEqual(controller.clicks, [(10, 10), (20, 20), (30, 30), (30, 30)])
                self.assertTrue(any("选择面板优先换出卡(顺位1)" in call.args[0]
                                    for call in logs.info.call_args_list))

    def test_exchange_recorded_card_beats_panel_in_both_swap_modes(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps({"swap_out_priority_profiles": {
                "集中": ["面板卡+"]
            }}), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                for mode in (swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE, swap.SECOND_SWAP_CONSULT_DELETE_NODE):
                    with self.subTest(mode=mode):
                        swap._delete_card_exchange_count = 1
                        swap._delete_card_a = "记录卡+"
                        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                        action.CARD_GRID = [(10, 10), (20, 20), (30, 30)]
                        names = {(10, 10): "パフォーマンスの基本+",
                                 (20, 20): "面板卡+", (30, 30): "记录卡+"}

                        class Controller:
                            def __init__(self):
                                self.clicks = []

                            def post_click(self, x, y):
                                self.clicks.append((x, y))
                                return self

                            def wait(self):
                                return self

                        controller = Controller()
                        context = _Context({mode: {"enabled": True},
                                            PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 2}})
                        context.tasker = SimpleNamespace(controller=controller)
                        action._wait_stable_name = lambda _context: (object(), names[controller.clicks[-1]])
                        action._confirm_exchange = lambda _context, _image: True
                        with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE, "logger") as logs:
                            self.assertTrue(action.run(context, None))
                        self.assertEqual(controller.clicks[-1], (30, 30))
                        self.assertTrue(any("选择首次换入卡 a" in call.args[0]
                                            for call in logs.info.call_args_list))

    def test_second_exchange_uses_panel_when_recorded_card_is_absent(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 1
        swap._delete_card_a = "未出现的记录卡+"
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps({"swap_out_priority_profiles": {"集中": ["面板卡+"]}}), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                action.CARD_GRID = [(10, 10), (20, 20)]

                class Controller:
                    def __init__(self):
                        self.clicks = []

                    def post_click(self, x, y):
                        self.clicks.append((x, y))
                        return self

                    def wait(self):
                        return self

                controller = Controller()
                context = _Context({swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE: {"enabled": True},
                                    PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 2}})
                context.tasker = SimpleNamespace(controller=controller)
                names = {(10, 10): "パフォーマンスの基本+", (20, 20): "面板卡+"}
                action._wait_stable_name = lambda _context: (object(), names[controller.clicks[-1]])
                action._confirm_exchange = lambda _context, _image: True
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertTrue(action.run(context, None))
                self.assertEqual(controller.clicks, [(10, 10), (20, 20), (10, 10), (20, 20), (20, 20)])

    def test_exchange_panel_is_profession_scoped_and_missing_key_is_empty(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        context = _Context({PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 2}})
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "cards_priority.json"
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                path.write_text("{}", encoding="utf-8")
                self.assertEqual(action._replacement_targets(context), [action.TARGET])
                path.write_text(json.dumps({"swap_out_priority_profiles": {
                    "集中": ["集中卡+"], "全力": ["全力卡+"]
                }}), encoding="utf-8")
                self.assertEqual(action._replacement_targets(context), ["集中卡+", action.TARGET])
                context.nodes[PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE] = {"max_hit": 5}
                self.assertEqual(action._replacement_targets(context), ["全力卡+", action.TARGET])

    def test_first_exchange_panel_miss_falls_back_to_basic_or_first_card(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 0
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "cards_priority.json"
            path.write_text(json.dumps({"swap_out_priority_profiles": {"集中": ["面板卡+"]}}), encoding="utf-8")
            with patch.object(PRODUCE, "CARDS_PRIORITY_CONFIG_PATH", str(path)):
                for names, expected in ((["其他", "パフォーマンスの基本+"], (20, 20)),
                                        (["其他", "其他"], (10, 10))):
                    with self.subTest(names=names):
                        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
                        action.CARD_GRID = [(10, 10), (20, 20)]

                        class Controller:
                            def __init__(self):
                                self.clicks = []

                            def post_click(self, x, y):
                                self.clicks.append((x, y))
                                return self

                            def wait(self):
                                return self

                        controller = Controller()
                        context = _Context({PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 2}})
                        context.tasker = SimpleNamespace(controller=controller)
                        by_position = dict(zip(action.CARD_GRID, names))
                        action._wait_stable_name = lambda _context: (object(), by_position[controller.clicks[-1]])
                        actions = []
                        action._confirm_exchange = lambda _context, _image: actions.append("change") or True
                        with patch.object(PRODUCE.time, "sleep", return_value=None):
                            self.assertTrue(action.run(context, None))
                        self.assertEqual(actions, ["change"])
                        self.assertEqual(controller.clicks[-1], expected)

    def test_first_exchange_no_match_scans_all_twelve_then_changes_first(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 0
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action._replacement_targets = lambda _context: ["仕切り直し+", action.TARGET]
        clicks = []

        class Controller:
            def post_click(self, x, y):
                clicks.append((x, y))
                return self

            def wait(self):
                return self

        context = _Context({})
        context.tasker = SimpleNamespace(controller=Controller())
        action._wait_stable_name = lambda _context: (object(), "達成感")
        changes = []
        action._confirm_exchange = lambda *_args: changes.append(True) or True
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action.run(context, None))
        self.assertEqual(clicks, action.CARD_GRID + [action.CARD_GRID[0]])
        self.assertEqual(changes, [True])

    def test_first_exchange_does_not_advance_when_change_page_stays_open(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        swap._delete_card_exchange_count = 0
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action.CARD_GRID = [(10, 10)]
        action._replacement_targets = lambda _context: ["目标卡+", action.TARGET]

        class Controller:
            def post_click(self, _x, _y):
                return self

            def wait(self):
                return self

        context = _Context({swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE: {"enabled": True}})
        context.tasker = SimpleNamespace(controller=Controller())
        action._wait_stable_name = lambda _context: (object(), "其他卡")
        action._click_text = lambda *_args: True
        action._wait_exchange_closed = lambda _context: False
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(action.run(context, None))
        self.assertEqual(swap._delete_card_exchange_count, 0)

    def test_exchange_reselection_mismatch_does_not_change_card(self):
        action = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()
        action.CARD_GRID = [(10, 10)]
        action._replacement_targets = lambda _context: ["面板卡+", action.TARGET]

        class Controller:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

        controller = Controller()
        context = _Context({})
        context.tasker = SimpleNamespace(controller=controller)
        names = iter(["面板卡+", "其他卡+"])
        action._wait_stable_name = lambda _context: (object(), next(names))
        action._confirm_exchange = lambda *_args: self.fail("must not exchange an unconfirmed card")
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(action.run(context, None))

    def test_hif_second_battle_delayed_entry_fallback(self):
        self.assertEqual(PRODUCE.ProduceHIF__ProduceCardsAuto.BATTLE_EARLY_TURNS, {11: 2, 10: 2})
        pipeline = json.loads(
            (ROOT / "extensions/hif/resource/base/pipeline/HIF.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            pipeline["ProduceHIF__ProduceHIFCardsFlag"]["recognition"]["param"]["custom_recognition"],
            "ProduceHIF__ProduceHIFCardsFlagAuto",
        )
        self.assertNotIn(
            "[JumpBack]ProduceHIF__ProduceRecognitionSkipRound",
            pipeline["ProduceHIF__ProduceEntryHIF"]["next"],
        )

    def test_custom_end_drink_stops_when_confirmation_does_not_appear(self):
        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_screencap(self):
                return self

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        controller = _Controller()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=False, filtered_results=[]
            ),
        )
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        action.CUSTOM_END_CONFIRM_TIMEOUT = 0

        self.assertFalse(action._confirm_custom_end_exchange(context))
        self.assertEqual(controller.clicks, [])

    def test_custom_end_drink_clicks_recognized_confirmation(self):
        class _Controller:
            def __init__(self):
                self.clicks = []

            def post_screencap(self):
                return self

            def post_click(self, x, y):
                self.clicks.append((x, y))
                return self

            def wait(self):
                return self

            @staticmethod
            def get():
                return object()

        controller = _Controller()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller),
            run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
                hit=True, filtered_results=[SimpleNamespace(box=[400, 1100, 100, 80])]
            ),
        )
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action._confirm_custom_end_exchange(context))
        self.assertEqual(controller.clicks, [(450, 1140)])

    def test_drink_profile_separates_receipt_from_purchase(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "profiles.json"
            path.write_text(json.dumps({"profiles": {
                "全力": [{"id": 27}, {"id": 28, "purchase_enabled": True},
                         {"id": 12, "purchase_enabled": False},
                         {"id": 14, "purchase_enabled": True, "disabled": True}],
                "集中": [{"id": 14, "purchase_enabled": True}],
            }}), encoding="utf-8")
            context = _Context({PRODUCE.ProduceHIF__ProduceCardsAuto.PROFESSION_NODE: {"max_hit": 5}})
            with patch.object(PRODUCE, "HIF_DRINK_PROFILES_PATH", str(path)):
                self.assertEqual(PRODUCE._hif_drink_priority_names(context),
                                 ["厳選初星チャイ", "初星湯", "ブーストエキス"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, purchase_only=True),
                                 ["初星湯"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, purchase_only=True, include_disabled=True),
                                 ["初星湯", "センブリソーダ"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(context, disabled_only=True),
                                 ["センブリソーダ"])
                self.assertEqual(PRODUCE._hif_drink_priority_names(_Context({}), purchase_only=True),
                                 ["センブリソーダ"])

    def test_supply_reads_all_three_before_selecting_highest_priority(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        clicks = []
        names = iter(["センブリソーダ", "初星水", "初星湯", "初星湯"])
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        drink._click = lambda _context, pos, delay=None: clicks.append(pos)
        drink._read_supply_drink_ocr = lambda *_args: next(names)
        self.assertEqual(
            drink._select_priority_supply_drink(context, ["初星湯", "センブリソーダ"]),
            drink.SUPPLY_DRINK_SLOTS[2],
        )
        self.assertEqual(clicks, [*drink.SUPPLY_DRINK_SLOTS, drink.SUPPLY_DRINK_SLOTS[2]])

    def test_supply_name_ignores_small_ocr_symbol_beside_title(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: SimpleNamespace(
            hit=True, filtered_results=[
                SimpleNamespace(text="センブリソーダ", box=[252, 495, 213, 39]),
                SimpleNamespace(text="+", box=[585, 534, 14, 13]),
            ]
        ))
        self.assertEqual(
            drink._read_supply_drink_ocr(context, object(), drink.SUPPLY_DRINK_NAME_ROI),
            "センブリソーダ",
        )

    def test_supply_does_not_accept_a_stale_name_after_reselection(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        names = iter(["初星湯", "初星水", "センブリソーダ", "初星水"])
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        drink._click = lambda *_args, **_kwargs: None
        drink._read_supply_drink_ocr = lambda *_args: next(names)
        self.assertIsNone(drink._select_priority_supply_drink(context, ["初星湯"]))

    def test_disabled_supply_drink_is_not_selected_as_fallback(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        names = iter(["初星湯", "初星水", "センブリソーダ"])
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        drink._click = lambda *_args, **_kwargs: None
        drink._read_supply_drink_ocr = lambda *_args: next(names)
        self.assertIsNone(drink._select_priority_supply_drink(context, [], ["初星湯"]))
        self.assertEqual(drink._supply_fallback_pos, drink.SUPPLY_DRINK_SLOTS[1])

    def test_supply_does_not_receive_when_all_candidates_are_disabled(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        drink._is_receive_drink_screen = lambda *_args: True
        drink._wait_for_bar_stable = lambda _context, image: (image, 0)
        drink._click = lambda *_args, **_kwargs: self.fail("不可盲领禁用饮料")
        drink._try_decline = lambda _context: False

        def select(_context, _priority, _disabled):
            drink._supply_fallback_pos = None
            return None

        drink._select_priority_supply_drink = select
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        with patch.object(PRODUCE, "_hif_drink_priority_names", side_effect=[[], ["初星湯"]]):
            self.assertFalse(drink.run(context, None))

    def test_shop_compares_all_slots_without_price_ocr(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        slots = action.DRINK_SLOTS
        names = ["初星湯", "センブリソーダ", "初星水", "ブーストエキス"]
        checked = []
        action._read_shop_drink = lambda _context, pos, _index: (
            checked.append(pos) or names[slots.index(pos)], pos
        )
        action._read_shop_price = lambda *_args: self.fail("选择饮料时不应读取价格")
        action._read_shop_points = lambda *_args: self.fail("选择饮料时不应读取余额")
        self.assertEqual(
            action._select_priority_shop_drink(None, slots, ["初星湯", "センブリソーダ", "初星水"]),
            "初星湯",
        )
        self.assertEqual(checked, [*slots, slots[0]])

    def test_shop_reads_only_name_after_slot_is_selected(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        image = object()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))
        )))
        action._click = lambda *_args, **_kwargs: None
        action._shop_slot_selected = lambda _image, _pos: True

        def read(_context, _image, roi, _expected):
            self.assertEqual(roi, action.NAME_ROI)
            return "厳選初星チャイ"

        action._read_ocr = read
        self.assertEqual(action._read_shop_drink(context, action.DRINK_SLOTS[0], 0), ("厳選初星チャイ", image))

    def test_shop_does_not_read_stale_name_from_unselected_slot(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object()))
        )))
        action._click = lambda *_args, **_kwargs: None
        action._shop_slot_selected = lambda _image, _pos: False
        action._read_ocr = lambda *_args: self.fail("未选中格位时不应读取上一格名称")
        self.assertEqual(action._read_shop_drink(context, action.DRINK_SLOTS[0], 0), ("", None))

    def test_shop_does_not_buy_old_hardcoded_target_outside_profile(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        slots = action.DRINK_SLOTS
        action._read_shop_drink = lambda _context, pos, _index: ("初星黒酢", pos)
        self.assertIsNone(action._select_priority_shop_drink(None, slots, ["初星湯"]))

    def test_shop_marks_unconfirmed_reselection_as_blocked(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        slots = action.DRINK_SLOTS
        calls = []
        def read(_context, pos, _index):
            calls.append(pos)
            return ("初星湯", pos) if pos == slots[0] and calls.count(pos) == 1 else ("", None)
        action._read_shop_drink = read
        self.assertIs(action._select_priority_shop_drink(None, slots, ["初星湯"]), False)

    def test_custom_end_stops_refresh_when_listed_drink_is_blocked(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        action._buy_custom_end_drinks_enabled = lambda _context: True
        action._read_shop_points = lambda *_args: 50
        selections = []
        action._select_priority_shop_drink = lambda *_args: selections.append(True) or False
        action._click = lambda *_args, **_kwargs: self.fail("购买不可确认时不应刷新")
        context = SimpleNamespace(tasker=SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )),
        ))
        with patch.object(PRODUCE, "_hif_drink_priority_names", return_value=["初星湯"]), \
             patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_bar_full", return_value=False):
            action.buy_custom_end_drinks(context)
        self.assertEqual(selections, [True])

    def test_custom_end_stops_when_points_below_50(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        action._buy_custom_end_drinks_enabled = lambda _context: True
        action._read_shop_points = lambda *_args: 49
        action._select_priority_shop_drink = lambda *_args: self.fail("余额不足50 P时不应扫描饮料")
        context = SimpleNamespace(tasker=SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )),
        ))
        with patch.object(PRODUCE, "_hif_drink_priority_names", return_value=["初星湯"]), \
             patch.object(PRODUCE.ProduceHIF__ProduceHIFDrinkAuto, "_bar_full", return_value=False):
            action.buy_custom_end_drinks(context)

    def test_battle_drink_policy_covers_unlisted_and_unspecified_drinks(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "profiles.json"
            path.write_text(json.dumps({
                "profiles": {"全力": [
                    {"id": 27, "use_timing": {"mode": "first_turn"}},
                    {"id": 28},
                    {"id": 14, "disabled": True},
                    {"id": 22, "use_timing": {"mode": "remaining_turn", "turn": 4}},
                ]},
                "default_use_timing_profiles": {
                    "全力": {"mode": "remaining_turn", "turn": 4}
                },
            }), encoding="utf-8")
            cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
            cards.profession = "全力"
            cards.combo_first, cards.combo_second = "国民", "脚光"
            with patch.object(PRODUCE, "HIF_DRINK_PROFILES_PATH", str(path)):
                cards._load_hif_battle_drink_policy()
            self.assertEqual(cards._due_battle_drink_names(9, first_turn=True), ["厳選初星チャイ"])
            due = cards._due_battle_drink_names(4)
            self.assertIn("初星湯", due)  # 已导入、单项时机未指定
            self.assertIn("初星水", due)  # 未导入
            self.assertNotIn("センブリソーダ", due)  # 勾选不使用
            self.assertNotIn("厳選初星チャイ", due)  # 单项开场时机覆盖统一时机
            self.assertNotIn("初星黒酢", due)  # 组合技尚未结束，先保留
            cards.combo_done = True
            self.assertIn("初星黒酢", cards._due_battle_drink_names(4))

    def test_turn_count_direct_ocr_recovers_four_and_triggers_drinks_once(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._last_turn_count = 5
        cards._drink_catalog_by_key = {"ブーストエキス": "ブーストエキス"}
        cards._drink_default_timing = {"mode": "remaining_turn", "turn": 4}
        reads = []
        def recognize(_node, _image, pipeline_override):
            rule = pipeline_override["ProduceHIF__ProduceRecognitionScore"]
            reads.append(rule)
            if len(reads) == 1:
                return SimpleNamespace(hit=False, filtered_results=[], all_results=[SimpleNamespace(text="X")])
            self.assertTrue(rule["only_rec"])
            self.assertEqual(rule["roi"], cards.TURN_COUNT_ROI)
            self.assertEqual(rule["threshold"], 0.9)
            self.assertEqual(rule["expected"], r"^[1-9]\d?$")
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="4", box=[34, 68, 56, 44])])
        context = SimpleNamespace(run_recognition=recognize)
        turn = cards._read_turn_count(context, object())
        self.assertEqual(turn, 4)
        self.assertEqual(cards._last_turn_count, 4)
        used = []
        cards._consume_battle_drinks = lambda _context, names, _label: used.extend(names) or True
        self.assertTrue(cards._process_battle_drink_timing(context, turn))
        self.assertFalse(cards._process_battle_drink_timing(context, turn))
        self.assertEqual(used, ["ブーストエキス"])

    def test_turn_count_direct_ocr_failure_and_jump_do_not_guess_four(self):
        for previous, fallback in ((4, None), (7, "4"), (5, "2")):
            with self.subTest(previous=previous, fallback=fallback):
                cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
                cards._last_turn_count = previous
                results = iter([
                    SimpleNamespace(hit=False, filtered_results=[]),
                    SimpleNamespace(hit=fallback is not None, filtered_results=(
                        [SimpleNamespace(text=fallback, box=[34, 68, 56, 44])] if fallback else [])),
                ])
                context = SimpleNamespace(run_recognition=lambda *_args, **_kwargs: next(results))
                self.assertIsNone(cards._read_turn_count(context, object()))
                self.assertEqual(cards._last_turn_count, previous)

    def test_turn_count_primary_success_does_not_run_direct_ocr(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._last_turn_count = 5
        reads = []
        def recognize(_node, _image, pipeline_override):
            reads.append(pipeline_override)
            self.assertNotIn("only_rec", pipeline_override["ProduceHIF__ProduceRecognitionScore"])
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="4", box=[47, 69, 32, 42])])
        self.assertEqual(cards._read_turn_count(SimpleNamespace(run_recognition=recognize), object()), 4)
        self.assertEqual(len(reads), 1)

    def test_battle_drink_detail_uses_title_relative_name_line(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        catalog = {PRODUCE._hif_drink_name_key("初星黒酢"): "初星黒酢"}
        results = [
            SimpleNamespace(text="Pドリンク詳細", box=[54, 723, 217, 34]),
            SimpleNamespace(text="初星黒酢", box=[187, 803, 123, 36]),
            SimpleNamespace(text="山札から選択", box=[191, 862, 480, 24]),
            SimpleNamespace(text="捨てる", box=[572, 802, 67, 26]),
        ]
        self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                         ("初星黒酢", "初星黒酢"))
        results[1].text = "読めない飲料"
        self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                         (None, "読めない飲料"))

    def test_battle_drink_detail_accepts_split_title_from_logs(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        name = "パワフル漢方ドリンク"
        catalog = {PRODUCE._hif_drink_name_key(name): name}
        for title_box, detail, detail_box, name_box in (
            ([53, 829, 159, 35], "詳細", [197, 830, 74, 34], [188, 911, 294, 35]),
            ([52, 829, 160, 35], "7詳細", [195, 830, 75, 35], [188, 912, 293, 32]),
        ):
            with self.subTest(detail=detail):
                results = [
                    SimpleNamespace(text="Pドリンク", box=title_box),
                    SimpleNamespace(text=detail, box=detail_box),
                    SimpleNamespace(text=name, box=name_box),
                    SimpleNamespace(text="捨てる", box=[572, 910, 66, 27]),
                ]
                self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                                 (name, name))
                results[2].text = name + "青汁"
                self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                                 (None, name + "青汁"))
                results[2].text = name
                results[2].box = [188, 967, 294, 35]
                self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                                 (None, ""))
                results[2].box = name_box
                results[0].text = "スキルカード詳細"
                self.assertEqual(cards._battle_drink_identity_from_results(results, catalog),
                                 (None, ""))

    def test_battle_drink_skips_unknown_and_not_due_names(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._drink_imported_names = ["センブリソーダ"]
        cards._battle_drink_boxes = lambda _context: [[0, 0, 20, 20], [30, 0, 20, 20]]
        cards._open_drink_detail = lambda *_args: True
        names = iter([(None, "読めない飲料"), ("初星水", "初星水")])
        cards._read_battle_drink_identity = lambda _context: next(names)
        closed = []
        cards._close_drink_detail = lambda _context: closed.append(True) or True
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._use_drink = lambda _context: self.fail("未知或未到期饮料不可使用")
        logs = []
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE, "logger", SimpleNamespace(info=logs.append)), \
             patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(cards._consume_battle_drinks(context, ["センブリソーダ"], "test"))
        self.assertEqual(closed, [True, True])
        self.assertTrue(any("初星水" in message and "未导入" in message for message in logs))
        self.assertTrue(any("読めない飲料" in message and "无法识别" in message for message in logs))

    def test_battle_drink_uses_first_due_slot_without_cancel_or_reopen(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._wait_for_battle_drink_return = lambda _context: True
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        counts = iter([[[0, 0, 20, 20], [40, 0, 20, 20]], [[0, 0, 20, 20]]])
        cards._battle_drink_boxes = lambda _context: next(counts)
        opened = []
        cards._open_drink_detail = lambda _context, pos, _label: opened.append(pos) or True
        names = iter([("初星水", "初星水")])
        cards._read_battle_drink_identity = lambda _context: next(names)
        cards._use_drink = lambda _context: True
        cards._close_drink_detail = lambda _context: self.fail("到期目标应在当前详情直接使用")
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._consume_battle_drinks(
                context, ["初星湯", "初星水"], "test", max_uses=1
            ))
        self.assertEqual(opened, [(10, 10)])

    def test_battle_drink_uses_all_matching_copies_after_repositioning(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._wait_for_battle_drink_return = lambda _context: True
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        opened = []
        cards._open_drink_detail = lambda _context, pos, _label: opened.append(pos) or True
        cards._read_battle_drink_identity = lambda _context: ("初星湯", "初星湯")
        cards._use_drink = lambda _context: True
        cards._close_drink_detail = lambda _context: self.fail("无需取消并复选同一瓶")
        counts = iter([
            [[0, 0, 20, 20], [40, 0, 20, 20]], [[0, 0, 20, 20]],
            [[0, 0, 20, 20]], [],
        ])
        cards._battle_drink_boxes = lambda _context: next(counts)
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._consume_battle_drinks(
                context, ["初星湯"], "test", max_uses=2
            ))
        self.assertEqual(opened, [(10, 10), (10, 10)])

    def test_battle_drink_unconfirmed_bar_change_stops_reuse(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._wait_for_battle_drink_return = lambda _context: True
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._open_drink_detail = lambda *_args: True
        cards._read_battle_drink_identity = lambda _context: ("初星湯", "初星湯")
        uses = []
        cards._use_drink = lambda _context: uses.append(True) or True
        counts = iter([[[0, 0, 20, 20], [40, 0, 20, 20]], []])  # 2→0 不可能由一瓶消耗造成
        cards._battle_drink_boxes = lambda _context: next(counts)
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            # 数量未可靠减少一瓶时停止饮料链，不继续使用下一瓶。
            with self.assertRaises(PRODUCE.HifDrinkFlowError):
                cards._consume_battle_drinks(context, ["初星湯"], "test")
        self.assertEqual(uses, [True])

    def test_battle_drink_batch_continues_after_move_page(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._last_turn_count = 4
        cards.combo_first = cards.combo_second = None
        cards._drink_imported_names = ["初星黒酢", "ブーストエキス"]
        cards._drink_default_timing = {"mode": "remaining_turn", "turn": 4}
        remaining = ["初星黒酢", "烏龍茶", "初星ホエイプロテイン", "ブーストエキス"]
        cards._drink_catalog_by_key = {name: name for name in remaining}
        state = {"page": "battle", "move_count": 0}
        controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(get=lambda: state["page"])))
        context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False, controller=controller),
            run_recognition=lambda node, image: SimpleNamespace(hit=(
                image == "move" if node == "ProduceHIF__ProduceRecognitionChooseMoveCards" else image == "battle"
            )),
        )
        def stable_bar(*_args, **_kwargs):
            self.assertEqual(state["page"], "battle")
            return True
        cards._wait_for_drink_list_stable = stable_bar
        selected = []
        cards._open_drink_detail = lambda _context, pos, _label: selected.append(remaining[pos[0]]) or True
        cards._read_battle_drink_identity = lambda _context: (selected[-1], selected[-1])
        def use_drink(_context):
            remaining.remove(selected[-1])
            state["page"] = "move" if selected[-1] == "初星黒酢" else "battle"
            return True
        cards._use_drink = use_drink
        def move_cards(_context, _image):
            state["move_count"] += 1
            state["page"] = "battle"
            return True
        cards._handle_move_cards = move_cards
        def boxes(_context):
            self.assertEqual(state["page"], "battle", "选卡页不能用于瓶数校验")
            return [[index, 0, 1, 1] for index in range(len(remaining))]
        cards._battle_drink_boxes = boxes
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._process_battle_drink_timing(context, 4))
            self.assertFalse(cards._process_battle_drink_timing(context, 4))
        self.assertEqual(selected, ["初星黒酢", "烏龍茶", "初星ホエイプロテイン", "ブーストエキス"])
        self.assertEqual(remaining, [])
        self.assertEqual(state["move_count"], 1)

    def test_battle_drink_disabled_slot_is_cancelled_before_using_next_slot(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._drink_disabled_names = {"初星水"}
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._wait_for_battle_drink_return = lambda _context: True
        counts = iter([[[0, 0, 20, 20], [40, 0, 20, 20]], [[0, 0, 20, 20]]])
        cards._battle_drink_boxes = lambda _context: next(counts)
        names = iter([("初星水", "初星水"), ("初星湯", "初星湯")])
        cards._read_battle_drink_identity = lambda _context: next(names)
        events = []
        cards._open_drink_detail = lambda _context, pos, _label: events.append(("open", pos)) or True
        cards._close_drink_detail = lambda _context: events.append("cancel") or True
        cards._use_drink = lambda _context: events.append("use") or True
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._consume_battle_drinks(
                context, ["初星水", "初星湯"], "test", max_uses=1))
        self.assertEqual(events, [("open", (10, 10)), "cancel", ("open", (50, 10)), "use"])

    def test_battle_drink_failed_open_rescans_once_before_direct_use(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._wait_for_battle_drink_return = lambda _context: True
        counts = iter([[[0, 0, 20, 20]], [[0, 0, 20, 20]], []])
        cards._battle_drink_boxes = lambda _context: next(counts)
        attempts = iter([False, True])
        opens = []
        def open_detail(_context, pos, _label):
            opens.append(pos)
            return next(attempts)
        cards._open_drink_detail = open_detail
        cards._is_drink_detail_open = lambda *_args: False
        cards._read_battle_drink_identity = lambda _context: ("初星湯", "初星湯")
        cards._close_drink_detail = lambda _context: self.fail("目标已展开，应直接使用")
        cards._use_drink = lambda _context: True
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object())))))
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(cards._consume_battle_drinks(context, ["初星湯"], "test", max_uses=1))
        self.assertEqual(opens, [(10, 10), (10, 10)])

    def test_battle_drink_cancellation_after_identity_does_not_use_drink(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._wait_for_drink_list_stable = lambda *_args, **_kwargs: True
        cards._battle_drink_boxes = lambda _context: [[0, 0, 20, 20]]
        cards._open_drink_detail = lambda *_args: True
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        def read_identity(_context):
            context.tasker.stopping = True
            return "初星湯", "初星湯"
        cards._read_battle_drink_identity = read_identity
        cards._use_drink = lambda _context: self.fail("停止后不可使用饮料")
        cards._close_drink_detail = lambda _context: self.fail("停止后不可点击取消")
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(cards._consume_battle_drinks(context, ["初星湯"], "test"))

    def test_battle_drink_move_failure_stops_without_bar_check(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace(
                post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object())))),
            run_recognition=lambda *_args: SimpleNamespace(hit=True),
        )
        cards._handle_move_cards = lambda *_args: False
        cards._battle_drink_boxes = lambda _context: self.fail("选卡失败不可检查饮料栏")
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(cards._wait_for_battle_drink_return(context))
        context.tasker.stopping = True
        context.tasker.controller.post_screencap = lambda: self.fail("停止后不可截图或处理选卡")
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertFalse(cards._wait_for_battle_drink_return(context))

    def test_black_vinegar_combo_only_excludes_all_normal_timings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'temp') as directory:
            path = pathlib.Path(directory) / "profiles.json"
            path.write_text(json.dumps({
                "profiles": {"集中": [{"id": 22, "use_timing": {"mode": "combo_only"}}]},
                "default_use_timing_profiles": {"集中": {"mode": "remaining_turn", "turn": 4}},
            }), encoding="utf-8")
            cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
            cards.profession = "集中"
            with patch.object(PRODUCE, "HIF_DRINK_PROFILES_PATH", str(path)):
                cards._load_hif_battle_drink_policy()
            self.assertNotIn("初星黒酢", cards._drink_disabled_names)
            for done in (False, True):
                cards.combo_done = done
                self.assertNotIn("初星黒酢", cards._due_battle_drink_names(4))
                self.assertNotIn("初星黒酢", cards._due_battle_drink_names(9, first_turn=True))
            used = []
            cards._consume_battle_drinks = lambda _context, names, *_args, **_kwargs: used.extend(names) or True
            self.assertTrue(cards._drink_brown_bottle(None))
            self.assertEqual(used, ["初星黒酢"])
            self.assertFalse(cards._valid_battle_drink_timing({"mode": "combo_only"}))
            self.assertFalse(cards._valid_battle_drink_timing(
                {"mode": "combo_only", "turn": 4}, allow_combo_only=True))

    def test_disabled_black_vinegar_blocks_combo_drink(self):
        cards = PRODUCE.ProduceHIF__ProduceCardsAuto()
        cards._drink_disabled_names = {"初星黒酢"}
        cards._consume_battle_drinks = lambda *_args, **_kwargs: self.fail("不使用不可进入喝饮流程")
        self.assertFalse(cards._drink_brown_bottle(None))
        self.assertTrue(cards._drink_brown_tried)

    def test_consult_purchase_switch_with_no_checked_drinks_finishes_without_buying(self):
        action = PRODUCE.ProduceHIF__ProduceHIFConsultAuto()
        action._buy_drinks_enabled = lambda _context: True
        action._delete_card_enabled = lambda _context: False
        action._select_priority_shop_drink = lambda *_args: self.fail("未勾选饮料不得购买")
        finished = []
        action._finish_consult = lambda _context: finished.append(True)
        with patch.object(PRODUCE, "_hif_drink_priority_names", return_value=[]), \
             patch.object(action, "_shop_finish_guard_active", return_value=False):
            self.assertTrue(action.run_shop(_Context({})))
        self.assertEqual(finished, [True])

    def test_supply_bar_waits_for_two_equal_slot_counts(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        screens = iter([object(), object()])
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: next(screens))
            )
        )
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
        counts = iter([3, 4, 4])
        drink._bar_filled_count = lambda _image: next(counts)
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            _image, filled = drink._wait_for_bar_stable(context, object())
        self.assertEqual(filled, 4)

    def test_receive_button_waits_for_two_matching_ocr_frames(self):
        drink = PRODUCE.ProduceHIF__ProduceHIFDrinkAuto()
        controller = SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: object())
            )
        )
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
        boxes = iter([None, [300, 1050, 100, 80], [300, 1050, 100, 80]])
        drink._ocr_receive_button = lambda *_args: next(boxes)
        with patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertEqual(
                drink._wait_for_receive_button(context), [300, 1050, 100, 80]
            )

    def test_teacher_event_direct_swap_uses_fourth_card(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        cases = (
            ({swap.SECOND_SWAP_PREFER_FIRST_ACQUIRED_OUT_NODE: {"enabled": True}}, 0, "_delete_card_a"),
            ({swap.SECOND_SWAP_CONSULT_DELETE_NODE: {"enabled": True}}, 1, "_delete_card_b"),
        )
        for nodes, exchange_count, remembered_attr in cases:
            with self.subTest(exchange_count=exchange_count):
                nodes[swap.TEACHER_EVENT_NODE] = {"enabled": True}
                context = _Context(nodes)
                swap.reset_delete_card_state()
                swap._delete_card_exchange_count = exchange_count
                clicked = []
                action = swap()
                action._click = lambda _context, pos: clicked.append(pos)
                action._wait_stable_card_name = lambda _context: (None, "第4张卡")

                self.assertTrue(action.run(context, None))
                self.assertEqual(
                    clicked,
                    [swap.TEACHER_EVENT_CARD_SLOTS[3], swap.NEXT_POS],
                )
                self.assertEqual(getattr(swap, remembered_attr), "第4张卡")




class HifBattleCardRecognitionTest(unittest.TestCase):
    def action(self, profession="集中"):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        action._hif_recognition = True
        action._load_config(profession)
        return action

    def test_playable_wait_exit_misses_are_consecutive_only_in_hif(self):
        for hif, expected, count in ((True,True,6),(False,False,3)):
            action=self.action();action._hif_recognition=hif
            frames=[]
            def capture():frames.append(1);return object()
            def recognize(name,*_a):
                hit=(len(frames)>=4 if name=="ProduceHIF__ProduceRecognitionSkipRound" else
                     len(frames) not in (1,3) if name=="ProduceHIF__ProduceRecognitionHealthFlag" else False)
                return SimpleNamespace(hit=hit)
            context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=SimpleNamespace(
                post_screencap=lambda:SimpleNamespace(wait=lambda:SimpleNamespace(get=capture)))),run_recognition=recognize)
            action._handle_move_cards=lambda *_a:False
            with patch.object(PRODUCE.time,"sleep",return_value=None):
                self.assertEqual(action._wait_until_playable(context,confirmation_count=3),expected)
            self.assertEqual(len(frames),count)

    def test_hif_playable_wait_times_out_and_stops_before_screencap(self):
        action=self.action()
        context,_,_=self.controller_context()
        context.run_recognition=lambda name,*_a:SimpleNamespace(hit=name=="ProduceHIF__ProduceRecognitionHealthFlag")
        action._handle_move_cards=lambda *_a:False
        with patch.object(PRODUCE.time,"monotonic",side_effect=[0,0,1,2,16]), \
                patch.object(PRODUCE.time,"sleep",return_value=None):
            self.assertFalse(action._wait_until_playable(context))
        context.tasker.stopping=True
        context.tasker.controller.post_screencap=lambda:self.fail("停止后不能先截图")
        self.assertFalse(action._wait_until_playable(context))

    def test_debug_selection_pair_shares_prefix_and_saves_correct_images(self):
        action=self.action();action._hif_debug_enabled=True
        before,after=object(),object();writes=[]
        cv=SimpleNamespace(imwrite=lambda path,image:writes.append((path,image)) or True)
        with patch.object(PRODUCE,"cv2",cv),patch.object(PRODUCE.os,"makedirs"), \
                patch.object(PRODUCE.time,"time_ns",return_value=123):
            action._hif_log_selection_pair(before,after,1)
            action._hif_log_selection_pair(before,after,1)
        self.assertEqual([pathlib.Path(path).name for path,_ in writes],
                         ["123-select-2-before.png","123-select-2-after.png"])
        self.assertEqual([image for _,image in writes],[before,after])


    def test_debug_missing_or_unreadable_switch_does_not_enable_saving(self):
        action = self.action()
        self.assertFalse(action._hif_debug_enabled)
        for node in (None, {}, {"enabled": False}, {"enabled": "true"}):
            self.assertFalse(action._load_hif_debug(SimpleNamespace(get_node_data=lambda _name: node)))
        def fail(_name):
            raise RuntimeError("node unavailable")
        self.assertFalse(action._load_hif_debug(SimpleNamespace(get_node_data=fail)))

    def test_debug_off_does_not_write_and_on_saves_once_per_hand(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                action = self.action()
                action._hif_debug_enabled = enabled
                writes, logs = [], []
                image = object()
                cv2 = SimpleNamespace(imwrite=lambda path, value: writes.append((path, value)) or True)
                logger = SimpleNamespace(warning=logs.append, info=lambda _message: None)
                with patch.object(PRODUCE, "cv2", cv2), patch.object(PRODUCE, "logger", logger), \
                        patch.object(PRODUCE.os, "makedirs") as mkdir:
                    action._hif_log_detection(image, [], "unknown")
                    action._hif_log_detection(image, [], "unknown")
                    self.assertEqual(len(logs), 1)
                    self.assertEqual(len(writes), int(enabled))
                    self.assertEqual(mkdir.call_count, int(enabled))
                    if enabled:
                        self.assertEqual(pathlib.Path(writes[0][0]).parent, ROOT / "debug/custom/hif_cards")
                        self.assertIs(writes[0][1], image)
                    action._reset_hif_recognition()
                    action._hif_log_detection(image, [], "new hand")
                    self.assertEqual(len(writes), 2 * int(enabled))

    @staticmethod
    def controller_context(image=None):
        clicks = []
        image = object() if image is None else image
        controller = SimpleNamespace(
            post_click=lambda x, y: clicks.append((x, y)) or SimpleNamespace(
                wait=lambda: None, job_id=len(clicks), succeeded=True),
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image)),
        )
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False),
                                  run_recognition=lambda *_args, **_kwargs: SimpleNamespace(hit=True))
        return context, clicks, image

    def test_basic_cards_use_global_catalog_without_adding_priority(self):
        action = self.action()
        for text in ("落ち着きの基本", "落ち着きの基本+", "落ち着きの基本＋＋"):
            self.assertEqual(action._hif_name_key(text), "落ち着きの基本+")
        self.assertNotIn("落ち着きの基本+", action.priority_index)
        self.assertEqual(action._hif_name_key("思考の基本"), "思考の基本+")
        self.assertEqual(action._hif_name_key("リアクションの", partial=True), "リアクションの基本+")
        self.assertEqual(action._hif_name_key("羽ばたけ!++"), "羽ばたけ！+")

    def test_shared_prefix_is_ambiguous_and_detail_requires_full_name(self):
        action = self.action()
        action._hif_name_index = {"夏夜に咲く思い出": ["summer-a"], "夏夜に響く歌": ["summer-b"]}
        self.assertIsNone(action._hif_name_key("夏夜に", partial=True))
        self.assertIsNone(action._hif_name_key("夏夜に咲く", partial=False))
        self.assertEqual(action._hif_name_key("夏夜に咲く思い出++"), "summer-a")
        action._hif_name_index = {"夏夜": ["short"], "夏夜の歌": ["long"]}
        self.assertIsNone(action._hif_name_key("夏夜", partial=True))
        self.assertEqual(action._hif_name_key("夏夜"), "short")

    def test_shared_prefix_with_explicit_title_end_matches_exact_family(self):
        action = self.action()
        for text in ("アドリブ+", "アドリブ++", "アドリブ＋＋"):
            self.assertEqual(action._hif_name_key(text, partial=True), "アドリブ+")
        self.assertIsNone(action._hif_name_key("アドリブ", partial=True))
        self.assertIsNone(action._hif_name_key("アドリ+", partial=True))
        self.assertIsNone(action._hif_name_key("タイミングの+", partial=True))
        self.assertEqual(action._hif_name_key("アドリブの基本++", partial=True), "アドリブの基本+")

    def test_name_strip_uses_visible_width_without_attribute_icons(self):
        action = self.action()
        reads = []
        def recognize(_name, _image, pipeline_override):
            roi = pipeline_override["ProduceHIF__ProduceIdentityName"]["roi"]
            reads.append(roi)
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="アドリブ+")])
        self.assertEqual(action._identify_by_name(SimpleNamespace(run_recognition=recognize),
                                                 object(), [184, 884, 175, 248]), "アドリブ+")
        self.assertEqual(reads, [[184, 1100, 175, 32]])

    def test_plain_shared_prefix_needs_centered_title_and_visible_blank_end(self):
        class Image:
            shape = (1280, 720, 3)
            def __getitem__(self, _region):
                return SimpleNamespace(size=240)
        image = Image()
        cases = (
            ([349, 884, 174, 248], [397, 1100, 97, 28], 239, "アドリブ+"),
            ([513, 884, 169, 250], [562, 1102, 87, 28], 239, "アドリブ+"),
            ([266, 884, 139, 248], [314, 1100, 87, 28], 239, None),  # 后端被裁断
            ([349, 884, 174, 248], [368, 1100, 87, 28], 239, None),  # 长名的左端前缀不居中
            ([349, 884, 174, 248], [397, 1100, 97, 28], 180, None),  # 后面仍有笔画
        )
        for box, text_box, blank, expected in cases:
            with self.subTest(box=box, text_box=text_box, blank=blank):
                action = self.action()
                result = SimpleNamespace(text="アドリブ", box=text_box)
                context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(
                    hit=True, filtered_results=[result]))
                with patch.object(PRODUCE, "cv2", SimpleNamespace(mean=lambda _region: (blank,) * 4)):
                    self.assertEqual(action._identify_by_name(context, image, box), expected)

    def test_hif_transition_without_skip_does_not_read_turn_count(self):
        for node, expected_reads in (("ProduceHIF__ProduceHIFCardsFlag", 0), ("ProduceHIF__ProduceCardsFlag", 2)):
            with self.subTest(node=node):
                action = PRODUCE.ProduceHIF__ProduceCardsAuto()
                context, _, _ = self.controller_context()
                context.get_node_data = lambda *_a: {}
                action._load_hif_battle_drink_policy = lambda: None
                action._wait_until_playable = lambda *_a, **_k: True
                action._detect_battle_number = lambda *_a: None
                action._is_battle_end = lambda *_a, **_k: False
                action._handle_move_cards = lambda *_a: False
                action._handle_star_get = lambda *_a: False
                action._skip_battle_end_animation = lambda *_a: None
                reads, frames = [], []
                action._read_turn_count = lambda *_a: reads.append(_a) or 1
                def recognize(name, *_a, **_k):
                    self.assertEqual(name, "ProduceHIF__ProduceRecognitionSkipRound")
                    frames.append(name)
                    if len(frames) == 2:
                        context.tasker.stopping = True
                    return SimpleNamespace(hit=False)
                context.run_recognition = recognize
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertTrue(action.run(context, SimpleNamespace(node_name=node)))
                self.assertEqual(len(reads), expected_reads)

    def test_suggestion_glow_name_strip_keeps_legacy_fallback(self):
        action = self.action()
        reads = []
        def recognize(_name, _image, pipeline_override):
            reads.append(pipeline_override["ProduceHIF__ProduceIdentityName"]["roi"])
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(
                text="読めない" if len(reads) == 1 else "わたしだけの思")])
        self.assertEqual(action._identify_by_name(SimpleNamespace(run_recognition=recognize),
                                                 object(), [259, 870, 198, 282]), "わたしだけの思い出+")
        self.assertEqual(reads, [[259, 1102, 198, 32], [259, 1100, 148, 58]])

    def test_latest_rightmost_split_card_is_one_slot(self):
        action = self.action()
        raw = [[48, 884, 144, 250], [259, 870, 198, 282],
               [482, 884, 148, 250], [580, 884, 91, 248]]
        boxes = action._hif_card_boxes([SimpleNamespace(label="cards", box=box, score=0.9) for box in raw])
        self.assertEqual([box for box, _ in boxes], raw[:3])

    def test_four_cards_with_low_confidence_right_strip_do_not_become_five(self):
        action = self.action()
        raw = [[18,884,177,250], [184,886,175,250], [349,884,136,248],
               [511,886,141,248], [610,884,90,248]]
        scores = [.915, .945, .957, .954, .333]
        boxes = action._hif_card_boxes([SimpleNamespace(label="cards", box=box, score=score)
                                       for box, score in zip(raw, scores)])
        self.assertEqual([box for box, _ in boxes], raw[:4])


    def test_hif_turn_direct_primary_avoids_repeated_detection_review(self):
        action = self.action()
        action._last_turn_count = 5
        reads = []
        def recognize(_name, _image, pipeline_override):
            rule = pipeline_override["ProduceHIF__ProduceRecognitionScore"]
            reads.append(rule)
            self.assertTrue(rule["only_rec"])
            self.assertEqual(rule["threshold"], 0.9)
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="4", box=[34,68,56,44])])
        self.assertEqual(action._read_turn_count(SimpleNamespace(run_recognition=recognize), object()), 4)
        self.assertEqual(len(reads), 1)

    def test_hif_turn_failed_direct_read_falls_back_without_accepting_jump(self):
        for previous, expected in ((5, 4), (7, None)):
            action = self.action()
            action._last_turn_count = previous
            reads = []
            def recognize(_name, _image, pipeline_override):
                rule = pipeline_override["ProduceHIF__ProduceRecognitionScore"]
                reads.append(rule)
                if rule["only_rec"]:
                    return SimpleNamespace(hit=False, filtered_results=[])
                self.assertEqual(rule["threshold"], 0.3)
                return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="4", box=[34,68,56,44])])
            self.assertEqual(action._read_turn_count(SimpleNamespace(run_recognition=recognize), object()), expected)
            self.assertEqual(len(reads), 2)

    def test_name_ocr_reads_catalog_card_and_ignores_attribute_line(self):
        action = self.action()
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(
            hit=True, filtered_results=[SimpleNamespace(text="落ち着きの基"), SimpleNamespace(text="M")]))
        self.assertEqual(action._identify_by_name(context, object(), [184, 884, 176, 248]), "落ち着きの基本+")

    def test_suggestion_label_keeps_same_hand_and_offhand_is_rejected(self):
        action = self.action()
        result = lambda label, box: SimpleNamespace(label=label, box=box, score=0.95)
        ordinary = action._hif_card_boxes([
            result("cards", [49, 884, 148, 250]), result("cards", [266, 884, 162, 250])])
        action._hif_bind_hand(ordinary)
        suggested = action._hif_card_boxes([
            result("cards", [49, 884, 147, 250]), result("cards", [154, 884, 83, 250]),
            result("suggestions", [261, 870, 198, 282]), result("cards", [266, 884, 162, 250]),
            result("suggestions", [628, 188, 90, 350]), result("useless", [482, 884, 180, 250])])
        self.assertEqual(len(suggested), 2)
        self.assertTrue(action._hif_same_geometry(suggested))

    def test_template_tie_does_not_repeat_ocr_on_same_image(self):
        action = self.action()
        action.cards_list = [{"key": "a", "template": "a.png"}, {"key": "b", "template": "b.png"}]
        hits = iter([0.85, 0.83])
        context = SimpleNamespace(run_recognition=lambda *_a, **_k: SimpleNamespace(
            hit=True, best_result=SimpleNamespace(box=[110, 900, 100, 120], score=next(hits))))
        with patch.object(action, "_identify_by_name", return_value=None) as ocr:
            self.assertIsNone(action._identify_card(context, object(), [100, 884, 180, 250])[0])
            self.assertEqual(ocr.call_count, 1)

    def test_raised_selected_card_does_not_delete_real_neighbor(self):
        action = self.action()
        results = [SimpleNamespace(label="cards", box=box, score=0.9) for box in (
            [20, 860, 187, 246], [160, 882, 123, 250], [266, 884, 138, 250])]
        self.assertEqual(len(action._hif_card_boxes(results)), 3)

    def test_preview_keeps_target_identity_when_neighbor_temporarily_missing(self):
        action = self.action()
        full = [([100, 884, 180, 250], 0.9), ([320, 884, 180, 250], 0.9)]
        action._hif_bind_hand(full)
        action._hif_detail_keys[0] = "落ち着きの基本+"
        action._hif_detail_attempted.add(0)
        partial = [([110, 858, 180, 250], 0.9)]
        self.assertEqual(action._hif_preview_box(partial, 0), partial[0][0])
        action._hif_bind_hand(partial)
        self.assertEqual(action._hif_detail_keys, {0: "落ち着きの基本+"})

    def test_right_edge_strip_preserves_slot_and_probe_progress(self):
        action = self.action()
        ordinary = [([x, 884, 139, 250], 0.9) for x in (18, 142, 266, 389, 513)]
        action._hif_bind_hand(ordinary)
        action._hif_detail_keys[4] = "シュプレヒコール+"
        action._hif_detail_attempted.add(4)
        action._hif_probe_started_at = 100.0
        narrow = ordinary[:4] + [([595, 884, 104, 248], 0.8)]
        self.assertEqual(action._hif_preview_box(narrow, 4), narrow[4][0])
        for _ in range(3):
            action._hif_bind_hand(narrow)
            raised = ordinary[:4] + [([511, 860, 147, 246], 0.9)]
            self.assertEqual(action._hif_preview_box(raised, 4), raised[4][0])
            action._hif_bind_hand(raised)
            action._hif_bind_hand(ordinary)
            self.assertEqual(action._hif_detail_keys, {4: "シュプレヒコール+"})
            self.assertEqual(action._hif_detail_attempted, {4})
            self.assertEqual(action._hif_probe_started_at, 100.0)

    def test_ambiguous_overlap_does_not_assign_identity_or_click_target(self):
        action = self.action()
        action._hif_bind_hand([([513, 884, 139, 250], 0.9)])
        action._hif_detail_keys[0] = "シュプレヒコール+"
        partials = [([595, 884, 104, 248], 0.8), ([605, 884, 94, 248], 0.8)]
        self.assertIsNone(action._hif_preview_box(partials, 0))
        action._hif_bind_hand(partials)
        self.assertEqual(action._hif_detail_keys, {})

    def test_only_two_matching_hand_observations_cache_identity(self):
        action = self.action()
        first = [{"box": [18, 884, 139, 250], "key": "存在感+"}]
        changed = [{"box": [18, 884, 139, 250], "key": "精神統一+"}]
        self.assertEqual(action._confirm_card_hand(first), "wait")
        self.assertEqual(action._hif_detail_keys, {})
        self.assertEqual(action._confirm_card_hand(first), "stable")
        self.assertEqual(action._hif_detail_keys, {0: "存在感+"})
        action._reset_hif_recognition()
        self.assertEqual(action._hif_detail_keys, {})
        with patch.object(PRODUCE.time, "time", side_effect=[100.0, 116.0]):
            self.assertEqual(action._confirm_card_hand(first), "wait")
            self.assertEqual(action._confirm_card_hand(changed), "stable")
        self.assertEqual(action._hif_detail_keys, {})  # 超时的最后一帧不计为两帧确认。

    def test_temporarily_missing_neighbor_restores_cached_identity_and_attempt(self):
        action = self.action()
        full = [([x, 884, 139, 250], 0.9) for x in (18, 142, 266, 389, 513)]
        action._hif_bind_hand(full)
        keys = ["始まりの合図+", "シュプレヒコール+", "あなたがくれた夢+", "精神統一+", "存在感+"]
        action._hif_detail_keys = dict(enumerate(keys))
        action._hif_detail_attempted.add(2)
        partial = [full[0], ([142, 858, 142, 248], 0.9), ([382, 870, 148, 278], 0.9), full[4]]
        for _ in range(3):
            action._hif_bind_hand(partial)
            self.assertEqual([action._hif_detail_keys[i] for i in range(4)], [keys[0], keys[1], keys[3], keys[4]])
            self.assertEqual(len(action._hif_missing_cards), 1)
            action._hif_bind_hand(full)
            self.assertEqual(action._hif_detail_keys, dict(enumerate(keys)))
            self.assertEqual(action._hif_detail_attempted, {2})
        action._reset_hif_recognition()
        self.assertEqual(action._hif_missing_cards, [])

    def test_loop_log_extra_strip_is_removed_without_removing_neighbor(self):
        action = self.action()
        raw = [[19, 886, 139, 248], [141, 886, 141, 248], [266, 884, 138, 250],
               [389, 884, 139, 250], [511, 884, 148, 248], [597, 888, 102, 246]]
        boxes = action._hif_card_boxes([SimpleNamespace(label="cards", box=box, score=0.95) for box in raw])
        self.assertEqual(len(boxes), 5)
        self.assertEqual(boxes[-1][0], raw[-2])

    def test_extra_box_does_not_clear_other_cards_probe_progress(self):
        action = self.action()
        ordinary = [([x, 884, w, 250], 0.9) for x, w in ((19, 139), (141, 141), (266, 138), (389, 139), (511, 148))]
        raised = [([x, y, w, h], 0.9) for x, y, w, h in (
            (19, 858, 187, 246), (159, 882, 123, 250), (266, 884, 138, 250),
            (389, 884, 139, 250), (509, 870, 198, 282))]
        action._hif_bind_hand(ordinary)
        action._hif_detail_keys[1] = "落ち着きの基本+"
        action._hif_detail_attempted.update((0, 1))
        for _ in range(3):
            action._hif_bind_hand(ordinary + [([597, 888, 102, 246], 0.8)])
            action._hif_bind_hand(raised)
            action._hif_bind_hand(ordinary)
            self.assertEqual(action._hif_detail_keys[1], "落ち着きの基本+")
            self.assertTrue({0, 1}.issubset(action._hif_detail_attempted))

    def test_total_detail_timeout_enters_decision_and_survives_box_fluctuation(self):
        action = self.action()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_probe_started_at = 100.0
        cards = [{"key": None, "box": boxes[0][0], "conf": 0.9}]
        with patch.object(PRODUCE.time, "time", return_value=116.0):
            self.assertFalse(action._hif_probe_unknowns(None, None, cards))
        self.assertTrue(action._hif_probe_expired)
        action._hif_bind_hand(boxes + [([320, 884, 100, 250], 0.8)])
        self.assertFalse(action._hif_probe_unknowns(None, None, cards))
        self.assertEqual(action._decide(None, cards)["type"], "play")

    def test_unconfigured_identity_uses_unknown_priority(self):
        action = self.action("全力")
        action.priority_index = {"レスポンスの基本+": 15}
        action.unknown_priority = 15.5
        action.conditional_priority_rules = {}
        self.assertEqual(action._full_power_priority({"key": "スピーチの基本+"}, 5)[:2], (15.5, 15.5))
        action = self.action()
        action.unknown_priority = 20
        cards = [{"key": "落ち着きの基本+", "box": [100, 884, 180, 250], "conf": 0.9},
                 {"key": "精神統一+", "box": [320, 884, 180, 250], "conf": 0.9}]
        self.assertEqual(action._decide(None, cards)["key"], "精神統一+")

    def test_hif_decision_log_distinguishes_identity_from_priority_source(self):
        for profession in ("集中", "全力"):
            for key, source in ((None, "兜底（暂未识别）"),
                                ("落ち着きの基本+", "兜底（未单独配置）"),
                                ("存在感+", "卡牌配置")):
                with self.subTest(profession=profession, key=key):
                    action = self.action(profession)
                    action.priority_index = {"存在感+": 1}
                    action.unknown_priority = 20
                    action.combo_first = action.combo_second = None
                    action.use_conditions = {}
                    action.conditional_priority_rules = {}
                    logs = []
                    cards = [{"key": key, "box": [100,884,180,250], "conf": 0.9}]
                    with patch.object(PRODUCE, "logger", SimpleNamespace(info=logs.append)):
                        decision = action._decide(None, cards)
                    self.assertEqual(decision["key"], key)
                    self.assertTrue(any(f"优先级来源={source}" in log for log in logs))
                    self.assertFalse(any("未知卡" in log for log in logs))
                    self.assertTrue(any(f"按优先级拟出牌={key or '暂未识别'}" in log for log in logs))

    def test_empty_hand_needs_two_observations_and_timeout_skips(self):
        action = self.action()
        action._hif_log_detection = lambda *_args: None
        context = SimpleNamespace(run_recognition=lambda *_a: SimpleNamespace(hit=True))
        with patch.object(PRODUCE.time, "time", side_effect=[100, 101]):
            self.assertFalse(action._hif_empty_hand(context, None, []))
            self.assertTrue(action._hif_empty_hand(context, None, []))
        action._reset_hif_recognition()
        context.run_recognition = lambda *_a: SimpleNamespace(hit=False)
        with patch.object(PRODUCE.time, "time", side_effect=[100, 115]):
            self.assertFalse(action._hif_empty_hand(context, None, []))
            self.assertTrue(action._hif_empty_hand(context, None, []))

    def test_detail_cache_is_cleared_when_hand_changes(self):
        action = self.action()
        action._hif_bind_hand([([100, 884, 180, 250], 0.9)])
        action._hif_detail_keys[0] = "落ち着きの基本+"
        action._hif_detail_attempted.add(0)
        action._hif_bind_hand([([300, 884, 180, 250], 0.9)])
        self.assertEqual(action._hif_detail_keys, {})
        self.assertEqual(action._hif_detail_attempted, set())

    def test_unknown_detail_is_probed_once_per_hand(self):
        action = self.action()
        context, clicks, image = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_log_detection = lambda *_a: None
        context.run_recognition = lambda *_a: SimpleNamespace(all_results=[])
        action._hif_frame_boxes = lambda *_a: boxes
        action._hif_select_card = lambda *_a: image
        action._hif_read_detail = lambda *_a: ("落ち着きの基本+", "落ち着きの基本++")
        cards = [{"key": None, "box": boxes[0][0]}]
        self.assertTrue(action._hif_probe_unknowns(context, image, cards))
        self.assertEqual(action._hif_detail_keys[0], "落ち着きの基本+")
        self.assertFalse(action._hif_probe_unknowns(context, image, cards))
        self.assertEqual(clicks, [])

    def test_unclear_selection_does_not_click(self):
        action = self.action()
        context, clicks, image = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_frame_boxes = lambda *_a: boxes
        action._hif_selected_index = lambda *_a: -2
        self.assertIsNone(action._hif_select_card(context, image, 0))
        self.assertEqual(clicks, [])

    def test_stale_selection_hint_clears_only_after_two_complete_unselected_frames(self):
        action = self.action()
        context, clicks, image = self.controller_context()
        boxes = [([19, 884, 178, 250], 0.95), ([184, 884, 176, 250], 0.95),
                 ([349, 884, 174, 250], 0.95), ([513, 884, 158, 250], 0.95)]
        action._hif_bind_hand(boxes)
        action._hif_selection_index = 0
        action._hif_has_select = lambda *_a: False
        action._hif_panel_top = lambda *_a: None
        self.assertEqual(action._hif_selected_index(context, image, boxes), -2)
        self.assertEqual(action._hif_selected_index(context, image, boxes), -1)
        self.assertIsNone(action._hif_selection_index)
        self.assertEqual(clicks, [])

    def test_missing_select_ocr_does_not_clear_hint_when_panel_or_raised_card_remains(self):
        for panel, top in ((584, 884), (None, 858)):
            with self.subTest(panel=panel, top=top):
                action = self.action()
                context, _, image = self.controller_context()
                boxes = [([100, top, 180, 250], 0.9)]
                action._hif_bind_hand(boxes)
                action._hif_selection_index = 0
                action._hif_has_select = lambda *_a: False
                action._hif_panel_top = lambda *_a: panel
                for _ in range(3):
                    self.assertEqual(action._hif_selected_index(context, image, boxes), -2)
                self.assertEqual(action._hif_selection_index, 0)

    def test_preplay_transient_missing_box_preserves_detail_progress(self):
        action = self.action()
        context, clicks, _ = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9), ([320, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_detail_keys[0] = "アドリブ+"
        action._hif_detail_attempted.add(0)
        action._hif_probe_started_at = 100.0
        action._hif_frame_boxes = lambda *_a: boxes[:1]
        self.assertFalse(action._play_hif_card(context, boxes[1][0]))
        self.assertEqual(action._hif_detail_keys, {0: "アドリブ+"})
        self.assertEqual(action._hif_detail_attempted, {0})
        self.assertEqual(action._hif_probe_started_at, 100.0)
        self.assertEqual(clicks, [])

    def test_empty_detection_frame_preserves_detail_until_empty_hand_is_confirmed(self):
        action = self.action()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_detail_keys[0] = "アドリブ+"
        action._hif_detail_attempted.add(0)
        action._hif_log_detection = lambda *_a: None
        context = SimpleNamespace(run_recognition=lambda *_a: SimpleNamespace(hit=False))
        self.assertFalse(action._hif_empty_hand(context, None, []))
        self.assertEqual(action._hif_detail_keys, {0: "アドリブ+"})
        self.assertEqual(action._hif_detail_attempted, {0})

    def test_play_selected_card_commits_once_and_unselected_selects_first(self):
        for already_selected, expected_clicks in ((True, 1), (False, 2)):
            with self.subTest(already_selected=already_selected):
                action = self.action()
                context, clicks, _ = self.controller_context()
                boxes = [([100, 858, 180, 250], 0.9)]
                action._hif_bind_hand(boxes)
                action._hif_frame_boxes = lambda *_a: boxes
                action._hif_selected_index = lambda *_a: 0 if already_selected else -1
                selected = lambda: len(clicks) == (0 if already_selected else 1)
                action._hif_has_select = lambda *_a: selected()
                action._hif_panel_top = lambda *_a: 584 if selected() else None
                action._wait_until_playable = lambda *_a, **_k: True
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertTrue(action._play_hif_card(context, boxes[0][0]))
                self.assertEqual(len(clicks), expected_clicks)

    def test_rechecking_selected_card_without_click_does_not_report_missed_click(self):
        action = self.action()
        context, clicks, image = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_frame_boxes = lambda *_a: boxes
        action._hif_selected_index = lambda *_a, pending=True: 0 if pending else -1
        action._hif_has_select = lambda *_a: False
        action._hif_panel_top = lambda *_a: None
        tick, info, warnings = [0.0], [], []
        def clock():
            tick[0] += 0.25
            return tick[0]
        with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE.time, "time", clock), \
                patch.object(PRODUCE, "logger", SimpleNamespace(info=info.append, warning=warnings.append)):
            self.assertIsNone(action._hif_select_card(context, image, 0))
        self.assertEqual(clicks, [])
        self.assertTrue(any("本次未发出点击，原选中状态已消失" in line for line in info))
        self.assertFalse(any("重试" in line or "点击未命中" in line for line in warnings))

    def test_failed_controller_click_does_not_enter_selected_state_wait_or_retry(self):
        action = self.action()
        context, clicks, image = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_frame_boxes = lambda *_a: boxes
        action._hif_selected_index = lambda *_a, **_k: -1
        action._hif_has_select = lambda *_a: self.fail("点击执行失败不进入点击后的画面判断")
        context.tasker.controller.post_click = lambda x,y: clicks.append((x,y)) or SimpleNamespace(
            wait=lambda: None, job_id=333, succeeded=False)
        warnings = []
        with patch.object(PRODUCE, "logger", SimpleNamespace(info=lambda _line: None, warning=warnings.append)):
            self.assertIsNone(action._hif_select_card(context, image, 0))
        self.assertEqual(clicks, [(190, 1009)])
        self.assertTrue(any("点击执行未成功: job_id=333" in line for line in warnings))
        self.assertFalse(any("重试" in line for line in warnings))

    def test_waiting_after_click_does_not_log_stale_preclick_hint(self):
        action = self.action()
        context, _, image = self.controller_context()
        boxes = [([100, 884, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_selection_index = 0
        action._hif_has_select = lambda *_a: False
        action._hif_panel_top = lambda *_a: None
        info = []
        with patch.object(PRODUCE, "cv2", object()), \
                patch.object(PRODUCE, "logger", SimpleNamespace(info=info.append)):
            self.assertEqual(action._hif_selected_index(context, image, boxes, pending=False), -1)
            self.assertEqual(action._hif_selected_index(context, image, boxes, pending=False), -1)
            self.assertEqual(action._hif_selection_index, 0)
            self.assertEqual(info, [])

    def test_missed_selection_diagnostic_respects_debug_switch(self):
        for enabled in (False, True):
            action = self.action()
            context, clicks, image = self.controller_context()
            action._hif_debug_enabled = enabled
            boxes = [([100, 884, 180, 250], 0.9)]
            action._hif_bind_hand(boxes)
            action._hif_frame_boxes = lambda *_a: boxes
            action._hif_selected_index = lambda *_a, **_k: -1
            action._hif_has_select = lambda *_a: len(clicks) >= 2
            action._hif_panel_top = lambda *_a: 584 if len(clicks) >= 2 else None
            diagnostics = []
            action._hif_log_selection_pair = lambda *args: diagnostics.append(args) if action._hif_debug_enabled else None
            context.run_recognition = lambda *_a, **_k: SimpleNamespace(hit=True, all_results=[])
            tick = [0.0]
            def clock():
                tick[0] += 0.25
                return tick[0]
            with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE.time, "time", clock):
                self.assertIsNotNone(action._hif_select_card(context, image, 0))
            self.assertEqual(len(clicks), 2)
            self.assertEqual(len(diagnostics), int(enabled))

    def test_missed_selection_and_missed_commit_retry_only_after_state_confirmation(self):
        for missed_phase, expected_clicks in (("select", 3), ("commit", 2)):
            with self.subTest(missed_phase=missed_phase):
                action = self.action()
                context, clicks, _ = self.controller_context()
                boxes = [([100, 858, 180, 250], 0.9)]
                action._hif_bind_hand(boxes)
                action._hif_frame_boxes = lambda *_a: boxes
                if missed_phase == "select":
                    selected = lambda: len(clicks) == 2
                    action._hif_selected_index = lambda *_a, **_k: -1
                else:
                    selected = lambda: len(clicks) <= 1
                    action._hif_selected_index = lambda *_a, **_k: 0
                action._hif_has_select = lambda *_a: selected()
                action._hif_panel_top = lambda *_a: 584 if selected() else None
                action._wait_until_playable = lambda *_a, **_k: True
                tick = [0.0]
                def clock():
                    tick[0] += 0.25
                    return tick[0]
                with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE.time, "time", clock):
                    self.assertTrue(action._play_hif_card(context, boxes[0][0]))
                self.assertEqual(len(clicks), expected_clicks)

    def test_new_recognition_is_scoped_to_hif_entry(self):
        for node, enabled in (("ProduceHIF__ProduceHIFCardsFlag", True), ("ProduceHIF__ProduceCardsFlag", False), ("ProduceHIF__IdolRoad", False)):
            with self.subTest(node=node):
                action = PRODUCE.ProduceHIF__ProduceCardsAuto()
                context, _, _ = self.controller_context()
                context.tasker.stopping = True
                context.get_node_data = lambda *_a: {}
                action._load_hif_battle_drink_policy = lambda: None
                action._wait_until_playable = lambda *_a, **_k: True
                action._detect_battle_number = lambda *_a: None
                with patch.object(PRODUCE.time, "sleep", return_value=None):
                    self.assertTrue(action.run(context, SimpleNamespace(node_name=node)))
                self.assertEqual(action._hif_recognition, enabled)

    def test_unclear_commit_state_does_not_retry_click(self):
        action = self.action()
        context, clicks, _ = self.controller_context()
        boxes = [([100, 858, 180, 250], 0.9)]
        action._hif_bind_hand(boxes)
        action._hif_frame_boxes = lambda *_a: boxes
        action._hif_selected_index = lambda *_a, **_k: 0
        action._hif_has_select = lambda *_a: not clicks
        action._hif_panel_top = lambda *_a: 584
        tick = [0.0]
        def clock():
            tick[0] += 0.25
            return tick[0]
        with patch.object(PRODUCE.time, "sleep", return_value=None), patch.object(PRODUCE.time, "time", clock):
            self.assertFalse(action._play_hif_card(context, boxes[0][0]))
        self.assertEqual(len(clicks), 1)

    def test_hif_run_probes_suggestion_card_before_decision(self):
        action = PRODUCE.ProduceHIF__ProduceCardsAuto()
        context, _, image = self.controller_context()
        context.get_node_data = lambda name: {"enabled": True} if name == "ProduceHIF__ProduceHIFDebug" else {}
        result = SimpleNamespace(label="suggestions", box=[100, 884, 180, 250], score=0.95)
        neighbor = SimpleNamespace(label="cards", box=[320, 884, 180, 250], score=0.95)
        context.run_recognition = lambda name, *_a, **_k: (
            SimpleNamespace(hit=True, all_results=[result, neighbor]) if name == "ProduceHIF__ProduceRecognitionCards"
            else SimpleNamespace(hit=name == "ProduceHIF__ProduceRecognitionSkipRound"))
        action._wait_until_playable = lambda *_a, **_k: True
        action._load_hif_battle_drink_policy = lambda: None
        action._detect_battle_number = lambda *_a: 1
        action._read_turn_count = lambda *_a: 9
        action._is_battle_end = lambda *_a, **_k: False
        action._handle_move_cards = lambda *_a: False
        action._handle_star_get = lambda *_a: False
        action._process_battle_drink_timing = lambda *_a, **_k: False
        identity_reads = []
        def identify(*args):
            identity_reads.append(args)
            key = "存在感+" if args[-1][0] == 320 and len(identity_reads) <= 4 else None
            return key, 0.0
        action._identify_card = identify
        action._hif_log_detection = lambda *_a: None
        action._hif_select_card = lambda *_a: image
        decisions = []
        decide = action._decide
        action._decide = lambda ctx, cards: decisions.append([c["key"] for c in cards]) or decide(ctx, cards)
        def play(*_args):
            context.tasker.stopping = True
            return True
        action._play_a_card = play
        with patch.object(action, "_hif_read_detail", return_value=("落ち着きの基本+", "落ち着きの基本++")) as detail, \
                patch.object(PRODUCE.time, "sleep", return_value=None):
            self.assertTrue(action.run(context, SimpleNamespace(node_name="ProduceHIF__ProduceHIFCardsFlag")))
        self.assertEqual(detail.call_count, 1)
        self.assertTrue(action._hif_debug_enabled)
        self.assertEqual(len(identity_reads), 4)  # 选中后邻牌漏识别不再引发额外补认。
        self.assertEqual(decisions, [["落ち着きの基本+", "存在感+"]])



class DeleteCardPriorityTest(unittest.TestCase):
    def exercise(self, names, targets, allow_basic=True, reread=None, title_visible=True, confirm_button=True, from_consult=False):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        action.CARD_GRID = [(i, 1) for i in range(len(names))]
        selected, confirmed, cancelled = [], [], []
        def click(x, y):
            selected.append(x)
            return SimpleNamespace(wait=lambda: None)
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(post_click=click)))
        action.wait_for_delete_page = lambda ctx: True
        reads = []
        def read(ctx):
            reads.append(selected[-1])
            name = reread if reread is not None and len(reads) > len(names) else names[selected[-1]]
            return object(), name
        action._wait_stable_card_name = read
        def click_delete(ctx, *args):
            if confirmed and not confirm_button:
                return False
            confirmed.append(names[selected[-1]])
            return True
        action._click_required_ocr = click_delete
        action._wait_for_text = lambda ctx, *args: object() if title_visible else None
        action._wait_for_state = lambda ctx, states: 'consult'
        action._recover_delete = lambda ctx, from_consult: cancelled.append(True) or True
        action._wait_event_closed = lambda ctx: True
        with patch.object(PRODUCE.time, 'sleep', return_value=None):
            result = action._delete_matching_card(context, targets, allow_basic, from_consult=from_consult)
        return result, confirmed, cancelled

    def test_missing_confirmation_title_still_clicks_delete_once(self):
        for from_consult in (False, True):
            with self.subTest(from_consult=from_consult):
                result, confirmed, cancelled = self.exercise(
                    ['目标卡'], ['目标卡'], title_visible=False, from_consult=from_consult)
                self.assertTrue(result)
                self.assertEqual(confirmed, ['目标卡'] * 2)
                self.assertFalse(cancelled)

    def test_missing_confirmation_title_and_button_does_not_blind_click(self):
        result, confirmed, cancelled = self.exercise(
            ['目标卡'], ['目标卡'], title_visible=False, confirm_button=False)
        self.assertFalse(result)
        self.assertEqual(confirmed, ['目标卡'])
        self.assertTrue(cancelled)

    def test_list_order_beats_grid_order_and_basic(self):
        result, confirmed, cancelled = self.exercise(['基本卡+', '低优先卡+', '首选卡+'], ['首选卡', '低优先卡'])
        self.assertTrue(result)
        self.assertEqual(confirmed, ['首选卡+', '首选卡+'])
        self.assertFalse(cancelled)

    def test_basic_is_used_only_when_no_list_card_matches(self):
        self.assertEqual(self.exercise(['其他卡', '表現の基本+'], ['不存在卡'])[1], ['表現の基本+'] * 2)
        self.assertEqual(self.exercise(['表現の基本+'], [])[1], ['表現の基本+'] * 2)

    def test_no_match_deletes_first_card(self):
        result, confirmed, cancelled = self.exercise(['其他卡', '另一张卡'], ['不存在卡'])
        self.assertTrue(result)
        self.assertEqual(confirmed, ['其他卡'] * 2)
        self.assertFalse(cancelled)

    def test_unreadable_first_card_cancels_without_confirmation(self):
        result, confirmed, cancelled = self.exercise([''], ['不存在卡'])
        self.assertFalse(result)
        self.assertFalse(confirmed)
        self.assertTrue(cancelled)

    def test_recorded_b_does_not_fall_back_to_basic(self):
        result, confirmed, cancelled = self.exercise(['表現の基本+'], ['记录卡b'], allow_basic=False)
        self.assertFalse(result)
        self.assertFalse(confirmed)
        self.assertTrue(cancelled)

    def test_changed_selection_cancels_without_confirmation(self):
        result, confirmed, cancelled = self.exercise(['基本卡', '名单卡'], ['名单卡'], reread='其他卡')
        self.assertFalse(result)
        self.assertFalse(confirmed)
        self.assertTrue(cancelled)

    def test_event_entry_ignores_consult_switch_and_recorded_b(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        context = object()
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        with patch.object(swap, '_delete_card_enabled', side_effect=AssertionError('event must not read consult switch')), \
                patch.object(swap, '_delete_card_b', '记录卡b'), \
                patch.object(swap, '_delete_card_delete_attempted', True), \
                patch.object(PRODUCE.ProduceHIF__ProduceHIFExchangeAuto, '_replacement_targets', return_value=['名单卡', '基本']) as targets, \
                patch.object(action, '_delete_matching_card', return_value=True) as delete, \
                patch.object(action, 'wait_for_consult', side_effect=AssertionError('event must not wait for shop')):
            self.assertTrue(action.run(context, None))
        targets.assert_called_once_with(context, include_recorded_a=False)
        delete.assert_called_once_with(context, ['名单卡'], allow_basic=True)

    def test_consult_entry_only_uses_recorded_b(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        context = object()
        with patch.object(action, '_delete_matching_card', return_value=True) as delete:
            self.assertTrue(action.delete_remembered_card(context, '记录卡b'))
        delete.assert_called_once_with(context, ['记录卡b'], from_consult=True)

    def test_event_completion_waits_for_page_disappearance(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: object())))),
            run_recognition=lambda *args: SimpleNamespace(hit=False),
        )
        with patch.object(action, '_detect_state', side_effect=['confirm', None, None]), \
                patch.object(action, 'wait_for_consult', side_effect=AssertionError('event must not wait for shop')), \
                patch.object(PRODUCE.time, 'sleep', return_value=None):
            self.assertTrue(action._wait_event_closed(context))

    def test_event_recovery_cancels_only_confirmation(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        image = object()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(
            post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image)))))
        for state, count in [('confirm', 1), ('delete', 0)]:
            with self.subTest(state=state), patch.object(action, '_detect_state', return_value=state), \
                    patch.object(action, '_click_cancel') as cancel, \
                    patch.object(action, '_cancel_to_consult', side_effect=AssertionError('event must not return to shop')):
                self.assertFalse(action._recover_delete(context, False))
                self.assertEqual(cancel.call_count, count)

    def test_wait_for_consult_passes_context(self):
        action = PRODUCE.ProduceHIF__ProduceHIFCardDeleteAuto()
        context = object()
        with patch.object(action, '_wait_for_state', return_value='consult') as wait:
            self.assertTrue(action.wait_for_consult(context))
        wait.assert_called_once_with(context, ('consult',))

    def test_disabled_mode_excludes_recorded_a_and_uses_profession_list(self):
        swap = PRODUCE.ProduceHIF__ProduceHIFCardSwapAuto
        config = {'swap_out_priority_profiles': {'集中': ['名单卡+', '次选卡']}}
        with patch.object(swap, '_delete_card_exchange_count', 1), patch.object(swap, '_delete_card_a', '旧记录卡'), \
                patch.object(swap, '_tracked_swap_enabled', return_value=True), \
                patch.object(PRODUCE.ProduceHIF__ProduceCardsAuto, '_load_hif_profession', return_value='集中'), \
                patch('builtins.open', mock_open(read_data=json.dumps(config))):
            targets = PRODUCE.ProduceHIF__ProduceHIFExchangeAuto()._replacement_targets(object(), include_recorded_a=False)
        self.assertEqual(targets, ['名单卡+', '次选卡', '基本'])

if __name__ == "__main__":
    unittest.main()
