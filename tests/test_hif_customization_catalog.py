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


if __name__ == "__main__":
    unittest.main()
