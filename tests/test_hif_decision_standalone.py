"""出牌决策与共用模块不得依赖 maa，便于脱离 MaaFramework 单独测试决策逻辑。"""
import sys
import types
import pathlib
import unittest
import importlib.util
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "extensions/hif/agent/hif/action"


class _BlockMaa:
    def find_spec(self, name, path=None, target=None):
        if name == "maa" or name.startswith("maa."):
            raise ImportError(f"maa must not be imported: {name}")
        return None


class DecisionStandaloneTest(unittest.TestCase):
    def load(self):
        utils = types.ModuleType("utils")
        utils.logger = types.SimpleNamespace(**{level: lambda *_a, **_k: None for level in ("debug", "info", "warning", "error")})
        name = "hif_action_without_maa"
        spec = importlib.util.spec_from_file_location(
            name, ACTION_DIR / "__init__.py", submodule_search_locations=[str(ACTION_DIR)])
        package = importlib.util.module_from_spec(spec)
        blocked = {key: None for key in list(sys.modules) if key == "maa" or key.startswith("maa.")}
        with patch.dict(sys.modules, {"utils": utils, name: package, **blocked}), \
                patch.object(sys, "path", [str(ROOT), *sys.path]), \
                patch.object(sys, "meta_path", [_BlockMaa(), *sys.meta_path]):
            # Only the package object is registered; __init__ (which registers every action) is not executed.
            return importlib.import_module(f"{name}.battle_decision")

    def test_decision_imports_and_decides_without_maa(self):
        decision = self.load()

        class Probe(decision.BattleDecisionMixin):
            _last_turn_count = 5
            _active_pointer_state = None
            _full_power_value = None
            _extra_turn_read_confirmed = True
            _pending_extra_turns = 0
            _hif_card_types = {}
            _followups = None
            _paired_card = None
            unconfigured_card_preference = {}
            no_extra_turn_keys = set()
            use_conditions = {}
            conditional_priority_rules = {}
            priority_index = {"A": 2, "B": 1}
            unknown_priority = 11
            profession = "集中"

        hand = [{"key": "A", "box": [0, 884, 180, 250], "conf": 0.9},
                {"key": "B", "box": [190, 884, 180, 250], "conf": 0.8},
                {"key": None, "box": [380, 884, 180, 250], "conf": 0.99}]
        self.assertEqual(Probe()._decide(None, hand), {"type": "play", "box": [190, 884, 180, 250], "combo": None, "key": "B"})


if __name__ == "__main__":
    unittest.main()
