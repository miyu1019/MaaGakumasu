"""Lesson identity and option layout guards, independent of button contrast."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Optional, Union
import unittest


source = Path(__file__).resolve().parents[1] / 'extensions/hif/agent/hif/reco/produce.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ProduceHIF__ProduceHIFOptionFlagAuto')
node.decorator_list = []
class Recognition:
    AnalyzeArg = object
    AnalyzeResult = staticmethod(lambda **kwargs: NS(**kwargs))
namespace = dict(CustomRecognition=Recognition, Context=object, RectType=list,
                 Optional=Optional, Union=Union, re=__import__('re'),
                 ProduceHIF__ProduceHIFOptionAuto=NS(_recognition_gate_active=lambda _: False),
                 logger=NS(warning=lambda _: None))
exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
Option = namespace[node.name]


class LessonOptionTests(unittest.TestCase):
    def recognize(self, *, title=True, cap=True, rows=(680, 780, 880), gate=False):
        namespace['ProduceHIF__ProduceHIFOptionAuto'] = NS(_recognition_gate_active=lambda _: gate)
        calls = []
        def read(_name, _image, pipeline_override):
            roi = pipeline_override[_name]['roi']
            calls.append(roi)
            if roi == Option.OPTION_ROI:
                return NS(hit=cap)
            if roi == [30, 35, 150, 115]:
                return NS(hit=title)
            return NS(hit=bool(rows), all_results=[NS(text='選択肢', box=[80, y, 290, 32]) for y in rows])
        result = Option().analyze(NS(run_recognition=read), NS(image=object()))
        return result.box, calls

    def test_lesson_title_cap_and_three_rows_match_without_pixel_contrast(self):
        box, calls = self.recognize()
        self.assertIsNotNone(box)
        self.assertEqual(len(calls), 3)

    def test_cap_and_three_text_rows_without_lesson_title_are_rejected(self):
        self.assertIsNone(self.recognize(title=False)[0])

    def test_partial_or_shifted_option_rows_are_rejected(self):
        for rows in ((680, 780), (580, 680, 780), (680, 685, 690)):
            with self.subTest(rows=rows):
                self.assertIsNone(self.recognize(rows=rows)[0])

    def test_missing_cap_is_rejected_before_further_ocr(self):
        box, calls = self.recognize(cap=False)
        self.assertIsNone(box)
        self.assertEqual(len(calls), 1)

    def test_post_click_gate_prevents_recognition_and_ocr(self):
        box, calls = self.recognize(gate=True)
        self.assertIsNone(box)
        self.assertFalse(calls)


if __name__ == '__main__':
    unittest.main()
