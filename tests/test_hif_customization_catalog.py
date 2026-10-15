import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("customization_sync", ROOT / "tools/sync_hif_customization_catalog.py")
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class CustomizationCatalogTests(unittest.TestCase):
    def test_adds_maxima_without_changing_cards_or_menu_order(self):
        catalog = {"sourceRevision": "card-source", "cards": [{"id": 547, "name": "セッティング+",
                   "options": [{"id": 73, "name": "全力値+"}, {"id": 43, "name": "保留追加"}]}]}
        data = [{"id": 43, "max": 1}, {"id": 73, "max": 3}]
        updated = SYNC.build_catalog(copy.deepcopy(catalog), data)
        self.assertEqual(updated["customizationRevision"], SYNC.REVISION)
        self.assertEqual([option["max"] for option in updated["cards"][0]["options"]], [3, 1])
        updated.pop("customizationRevision")
        for option in updated["cards"][0]["options"]:
            option.pop("max")
        self.assertEqual(updated, catalog)
        for maximum in (None, 0, -1, 1.5, True):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                SYNC.build_catalog(copy.deepcopy(catalog), [{"id": 43, "max": 1}, {"id": 73, "max": maximum}])
        with self.assertRaises(KeyError):
            SYNC.build_catalog(copy.deepcopy(catalog), [])

    def test_all_catalog_options_have_positive_integer_maxima(self):
        catalog = json.loads((ROOT / "extensions/hif/catalog/hif_customization_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["customizationRevision"], SYNC.REVISION)
        maxima = {}
        for card in catalog["cards"]:
            self.assertIn(len(card["options"]), (2, 3))
            for option in card["options"]:
                self.assertIs(type(option["max"]), int)
                self.assertGreater(option["max"], 0)
                self.assertEqual(maxima.setdefault(option["id"], option["max"]), option["max"])

    def test_screenshot_options_survive_upstream_maxima_sync(self):
        catalog = {"cards": [{"id": 890, "sourceEvidence": {"type": "mumu-screenshot"},
                              "options": [{"id": -8902, "name": "集中+", "max": 3}]}]}
        updated = SYNC.build_catalog(copy.deepcopy(catalog), [])
        self.assertEqual(updated["cards"], catalog["cards"])

    def test_october_15_screenshot_menu_order_and_branch_lengths(self):
        catalog = json.loads((ROOT / "extensions/hif/catalog/hif_customization_catalog.json").read_text(encoding="utf-8"))
        cards = {card["id"]: card for card in catalog["cards"]}
        expected = {
            890: (3, [("パラメータ追加", 1), ("集中+", 3), ("好調追加", 1)]),
            892: (3, [("パラメータ追加", 1), ("好調+", 3), ("集中追加", 1)]),
            894: (2, [("集中追加", 1), ("好調+", 2), ("パラメータ上昇回数+", 1)]),
            896: (2, [("好調追加", 1), ("集中+", 2), ("パラメータ上昇回数+", 1)]),
            898: (3, [("やる気追加", 1), ("好印象+", 3), ("好印象分パラメータ追加", 1)]),
            900: (3, [("好印象+", 1), ("やる気+", 3), ("元気分パラメータ追加", 1)]),
            902: (2, [("やる気追加", 1), ("好印象+", 2), ("好印象分パラメータ追加", 1)]),
            904: (2, [("やる気追加", 1), ("元気分パラメータ+", 2), ("体力消費コスト値-", 1)]),
            906: (2, [("全力値+", 2), ("成長追加", 1), ("保留追加", 1)]),
            908: (2, [("全力値+", 2), ("成長追加", 2), ("レッスン中1回の制限削除", 1)]),
        }
        option_ids = set()
        for card_id, (limit, menu) in expected.items():
            with self.subTest(card_id=card_id):
                card = cards[card_id]
                self.assertEqual(card["sourceEvidence"]["type"], "mumu-screenshot")
                self.assertEqual(card["customizationLimit"], limit)
                self.assertEqual([(option["name"], option["max"]) for option in card["options"]], menu)
                for option in card["options"]:
                    self.assertLess(option["id"], 0)
                    self.assertNotIn(option["id"], option_ids)
                    option_ids.add(option["id"])


if __name__ == "__main__":
    unittest.main()
