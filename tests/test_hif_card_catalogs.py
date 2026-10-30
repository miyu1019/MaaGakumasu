import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "extensions/hif/catalog"
ICONS = ROOT / "extensions/hif/resource/base/image/hif/hif_card_icons"


def load(name):
    return json.loads((CATALOG / name).read_text(encoding="utf-8"))


class CardCatalogTests(unittest.TestCase):
    def test_shared_catalogs_and_icons_stay_in_sync(self):
        target = load("hif_target_card_catalog.json")
        priority = load("hif_priority_card_catalog.json")
        images = load("hif_priority_card_images.json")
        sources = load("hif_card_icon_sources.json")["icons"]
        self.assertEqual(len({card["id"] for card in target}), len(target))
        self.assertEqual(len({card["name"] for card in target}), len(target))
        self.assertEqual(set(priority), set(images))
        self.assertEqual({card["name"] for card in target} | {"眠気"}, set(images))
        self.assertEqual(len(set(images.values())), len(images))
        self.assertEqual({str(card_id) for card_id in images.values()}, set(sources))
        for card in target:
            self.assertEqual(images[card["name"]], card["id"])
        for card_id, source in sources.items():
            with self.subTest(card_id=card_id):
                self.assertIn(source["source"], (f"{card_id}.webp", f"{card_id}_8.webp"))
                self.assertEqual(hashlib.sha256((ICONS / f"{card_id}.webp").read_bytes()).hexdigest(), source["sha256"])

    def test_roar_and_garakuta_road_cards_use_full_name_ocr(self):
        cards = {card["id"]: card for card in load("hif_target_card_catalog.json")}
        priority = load("hif_priority_card_catalog.json")
        expected = {
            910: ("獲物はあなた+", "sense", "active"),
            912: ("結果としての今+", "anomaly", "active"),
            914: ("ファンが望むこと+", "anomaly", "mental"),
        }
        for card_id, (name, plan, card_type) in expected.items():
            with self.subTest(card_id=card_id):
                self.assertEqual(cards[card_id], {"id": card_id, "name": name, "rarity": "SSR", "plan": plan,
                                                  "type": card_type, "sourceType": "pIdol", "upgraded": True})
                self.assertEqual(priority[name], {"name": name[:-1], "template": ""})


if __name__ == "__main__":
    unittest.main()
