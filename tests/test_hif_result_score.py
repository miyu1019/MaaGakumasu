import unittest
from types import SimpleNamespace as NS

from test_hif_behavior import PRODUCE


class HifResultScoreTests(unittest.TestCase):
    def test_player_score_follows_log_button_at_every_rank(self):
        action = PRODUCE.ProduceHIF__ProduceHIFReChallengeAuto()
        for rank in range(4):
            with self.subTest(rank=rank):
                rows = []
                for index, score in enumerate(('1,162,450', '482,680', '366,712', '347,516')):
                    rows.extend([NS(text=str(index + 1), box=[35, 259 + 180 * index, 99, 93]),
                                 NS(text=score, box=[225, 296 + 180 * index, 201, 41])])
                rows.append(NS(text='ログ', box=[625, 294 + 180 * rank, 42, 23]))
                context = NS(run_recognition=lambda *_args, **_kwargs: NS(hit=True, filtered_results=rows))
                self.assertEqual(action._read_my_score(context, None), (1162450, 482680, 366712, 347516)[rank])

    def test_missing_or_ambiguous_player_row_does_not_use_opponent_score(self):
        action = PRODUCE.ProduceHIF__ProduceHIFReChallengeAuto()
        score = NS(text='1,162,450', box=[225, 296, 201, 41])
        log = NS(text='ログ', box=[625, 294, 42, 23])
        for rows in ([score], [score, log, log], [log], [log, score, score],
                     [log, NS(text='482,680', box=[225, 475, 165, 43])]):
            context = NS(run_recognition=lambda *_args, **_kwargs: NS(hit=True, filtered_results=rows))
            self.assertIsNone(action._read_my_score(context, None))

    def test_small_scores_and_translated_log_labels(self):
        action = PRODUCE.ProduceHIF__ProduceHIFReChallengeAuto()
        for label, text, expected in [('日志', '9，999', 9999), ('日誌', '0', 0), ('ログ', '1162450', 1162450)]:
            rows = [NS(text=label, box=[625, 294, 42, 23]), NS(text=text, box=[225, 296, 201, 41])]
            context = NS(run_recognition=lambda *_args, **_kwargs: NS(hit=True, filtered_results=rows))
            self.assertEqual(action._read_my_score(context, None), expected)
