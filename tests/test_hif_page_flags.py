"""最终评价/回忆照片页识别：页面停留期间须连续命中，入口恢复的只读连续确认才能成立。"""
import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECO = ROOT / "extensions/hif/agent/hif/reco/produce.py"


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_reco():
    package = "hif_reco_under_test"
    action = types.ModuleType(f"{package}.action.produce")
    for name in ("ProduceHIF__ProduceCardsAuto", "ProduceHIF__ProduceHIFConsultAuto", "ProduceHIF__ProduceHIFLessonAuto",
                 "ProduceHIF__ProduceHIFOptionAuto", "ProduceHIF__ProduceHIFRestAuto", "ProduceHIF__ProduceHIFTrainAuto"):
        setattr(action, name, MagicMock(name=name))
    recognition = type("CustomRecognition", (), {
        "AnalyzeArg": object,
        "AnalyzeResult": staticmethod(lambda box=None, detail=None: SimpleNamespace(box=box, detail=detail)),
    })
    server = type("AgentServer", (), {"custom_recognition": staticmethod(lambda _name: lambda cls: cls)})
    stubs = {
        "utils": SimpleNamespace(logger=_Logger()),
        "maa": types.ModuleType("maa"),
        "maa.define": SimpleNamespace(RectType=list),
        "maa.context": SimpleNamespace(Context=object),
        "maa.agent": types.ModuleType("maa.agent"),
        "maa.agent.agent_server": SimpleNamespace(AgentServer=server),
        "maa.custom_recognition": SimpleNamespace(CustomRecognition=recognition),
        "extensions.hif.recovery": SimpleNamespace(state=None),
        package: types.ModuleType(package),
        f"{package}.action": types.ModuleType(f"{package}.action"),
        f"{package}.action.produce": action,
        f"{package}.reco": types.ModuleType(f"{package}.reco"),
    }
    spec = importlib.util.spec_from_file_location(f"{package}.reco.produce", RECO)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


RECO_MODULE = _load_reco()


class _Image:
    shape = (1280, 720, 3)


def _context(hits):
    frames = iter(hits)

    def run_recognition(*_args, **_kwargs):
        hit = next(frames)
        box = SimpleNamespace(box=[300, 1120, 120, 60])
        return SimpleNamespace(hit=hit, filtered_results=[box] if hit else [])
    return SimpleNamespace(run_recognition=run_recognition)


class PageFlagTests(unittest.TestCase):
    def check(self, cls):
        reco = cls()
        hits = [True] * 6 + [False, True, True]
        context = _context(hits)
        arg = SimpleNamespace(image=_Image())
        results = [reco.analyze(context, arg).box is not None for _ in hits]
        # 首次出现需连续2帧；之后页面停留期间每帧都命中（实测入口恢复需连续3帧同一页面）；漏一帧后重新计数。
        self.assertEqual(results, [False, True, True, True, True, True, False, False, True])

    def test_final_rating_keeps_hitting_while_page_stays(self):
        self.check(RECO_MODULE.ProduceHIF__ProduceHIFFinalRatingFlagAuto)

    def test_photo_memory_keeps_hitting_while_page_stays(self):
        self.check(RECO_MODULE.ProduceHIF__ProduceHIFPhotoMemoryFlagAuto)


if __name__ == "__main__":
    unittest.main()
