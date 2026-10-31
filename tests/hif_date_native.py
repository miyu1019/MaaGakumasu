"""Offline MaaFramework checks; uses a custom controller and never connects to the game."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'ProduceHIF__'


class Screen(CustomController):
    phase = 'date'
    date_frame = None
    allow_clicks = False
    def connect(self): return True
    def request_uuid(self): return 'hif-date-offline'
    def get_features(self): return 0
    def screencap(self):
        return self.date_frame if self.phase == 'date' and self.date_frame is not None else np.zeros((1280, 720, 3), dtype=np.uint8)
    def click(self, x, y):
        assert self.allow_clicks, 'Unexpected fixture click'
        self.phase = {'date': 'title', 'title': 'bonus', 'bonus': 'home', 'home': 'choose', 'choose': 'battle'}[self.phase]
        self.steps.append(self.phase)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--date-image', type=Path, required=True)
    parser.add_argument('--title-image', type=Path, required=True)
    args = parser.parse_args()
    Library.open(args.library)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'agent'))
    sys.path.insert(0, str(ROOT / 'extensions/hif/agent'))
    with patch.object(AgentServer, 'custom_action', side_effect=lambda _name: lambda cls: cls), \
            patch.object(AgentServer, 'custom_recognition', side_effect=lambda _name: lambda cls: cls):
        from hif.action import produce as action
        from hif.reco.produce import ProduceHIF__DateRequestedAuto

    state = action._hif_recovery
    nodes = json.loads((ROOT / 'extensions/hif/resource/base/pipeline/HIF.json').read_text(encoding='utf-8'))

    class Inspect(CustomAction):
        def run(self, context, argv):
            assert context.get_node_data('StartUpTestScreen') is None
            print('Isolated startup next definition:', context.get_node_data(PREFIX + 'StartUpTestScreen')['next'])
            for path, expected in ((args.date_image, 'DateChanged'), (args.title_image, 'DateTitle')):
                frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
                assert frame is not None and frame.shape == (1280, 720, 3)
                if expected == 'DateChanged':
                    assert action._hif_network_dialog_possible(frame)
                else:
                    assert action._hif_title_possible(frame)
                for name in ('DateChanged', 'DateTitle', 'ProduceNetworkRetry', 'DateRecover'):
                    result = context.run_recognition(PREFIX + name, frame)
                    matched = bool(result and result.hit)
                    assert matched == (name in (expected, 'DateRecover')), (path.name, name, result)
                    print(path.name, name, matched, result.box if result else None)
            return True

    @action._hif_date_interrupt
    class Interrupted(CustomAction):
        def run(self, context, argv):
            try:
                state.pending = 'fixture:unconfirmed'
                raise action._HifDateChanged()
            except Exception:
                raise AssertionError('Date interrupt swallowed by old action')

    class Recovered(CustomAction):
        count = 0
        def run(self, context, argv):
            assert state.date_requested and state.pending == 'fixture:unconfirmed'
            self.count += 1
            state.reset(state.task_id)
            return True

    class MustNotRun(CustomAction):
        def run(self, context, argv):
            raise AssertionError('Old action continuation was replayed')

    steps = []

    class Stage(CustomRecognition):
        def analyze(self, context, argv):
            phase = json.loads(argv.custom_recognition_param)
            return self.AnalyzeResult(box=[0, 0, 1, 1] if screen.phase == phase else None, detail='offline phase')

    with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='hif-date-native-', ignore_cleanup_errors=True) as folder:
        Toolkit.init_option(folder)
        resource = Resource()
        assert resource.post_bundle(ROOT / 'extensions/hif/resource/base').wait().succeeded
        recovered = Recovered()
        for name, callback in {
            'FixtureInspect': Inspect(), 'FixtureInterrupted': Interrupted(),
            'FixtureRecovered': recovered, 'FixtureMustNotRun': MustNotRun(),
            PREFIX + 'DateRecoverAuto': action.ProduceHIF__DateRecoverAuto(),
        }.items():
            assert resource.register_custom_action(name, callback)
        assert resource.register_custom_recognition(PREFIX + 'DateRequestedAuto', ProduceHIF__DateRequestedAuto())
        assert resource.register_custom_recognition('FixtureStage', Stage())
        screen, tasker = Screen(), Tasker()
        assert screen.post_connection().wait().succeeded
        assert tasker.bind(resource, screen)

        def run(entry, override):
            job = tasker.post_task(entry, override)
            deadline = time.monotonic() + 60
            while not job.done and time.monotonic() < deadline:
                time.sleep(.01)
            if not job.done:
                tasker.post_stop().wait()
                raise AssertionError('Native date fixture timed out')
            assert job.succeeded, job.status

        run('FixtureInspect', {'FixtureInspect': {
            'recognition': 'DirectHit', 'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureInspect'}}}})
        fixture = {
            'FixtureInterrupted': {'recognition': 'DirectHit',
                'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureInterrupted'}},
                'on_error': [PREFIX + 'DateRequested'], 'next': ['FixtureOldContinuation']},
            PREFIX + 'DateRequested': {**nodes[PREFIX + 'DateRequested'],
                'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureRecovered'}}},
            PREFIX + 'ProduceEntryHIF': {'recognition': 'DirectHit', 'action': 'StopTask', 'next': []},
            'FixtureOldContinuation': {'recognition': 'DirectHit',
                'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureMustNotRun'}}},
        }
        run('FixtureInterrupted', fixture)
        assert recovered.count == 1
        # Same failure inside a JumpBack action must replace its stale continuation, too.
        fixture['FixtureParent'] = {'recognition': 'DirectHit', 'action': 'DoNothing',
                                    'next': ['[JumpBack]FixtureInterrupted', 'FixtureOldContinuation']}
        run('FixtureParent', fixture)
        assert recovered.count == 2
        # Exercise the production recovery loop against real Context data/actions with scripted game screens.
        flow = {PREFIX + 'ProduceEntryHIF': fixture[PREFIX + 'ProduceEntryHIF']}
        def stage(node, phase, following=None):
            flow[node] = {
                'recognition': {'type': 'Custom', 'param': {
                    'custom_recognition': 'FixtureStage', 'custom_recognition_param': phase}},
                'action': 'DoNothing' if following is None else {'type': 'Click', 'param': {'target': [0, 0, 1, 1]}},
            }
        stage(PREFIX + 'DateChanged', 'date', 'title')
        stage(PREFIX + 'DateTitle', 'title')
        stage(PREFIX + 'ProduceNetworkRetry', 'never')
        stage(PREFIX + 'ClickCenter', 'title', 'bonus')
        stage(PREFIX + 'StartUpLoginBonus', 'bonus', 'home')
        stage(PREFIX + 'ProduceHomeFlag', 'home')
        stage(PREFIX + 'ProduceContinue', 'home', 'choose')
        stage(PREFIX + 'ProduceChooseContinue', 'choose', 'battle')
        for suffix in action._HIF_SAFE_SCENES:
            flow[PREFIX + suffix] = {'enabled': False}
        stage(PREFIX + 'ProduceHIFCardsFlag', 'battle')
        flow[PREFIX + 'ProduceHIFCardsFlag']['enabled'] = True
        screen.date_frame = cv2.imdecode(np.fromfile(args.date_image, dtype=np.uint8), cv2.IMREAD_COLOR)
        screen.allow_clicks = True
        screen.steps = steps
        for skip in (False, True):
            screen.phase = 'date'
            steps.clear()
            flow[PREFIX + 'ProduceSkipPreparation'] = {'enabled': skip}
            run(PREFIX + 'DateRecover', flow)
            assert steps == ['title', 'bonus', 'home', 'choose', 'battle'], steps
            assert state.pending is None and not state.date_requested
        # A recovery action failure must terminate even beneath a stale JumpBack frame.
        fixture[PREFIX + 'DateRequested'] = {**nodes[PREFIX + 'DateRequested'],
            'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureFailedRecovery'}}}
        class FailedRecovery(CustomAction):
            def run(self, context, argv): return False
        assert resource.register_custom_action('FixtureFailedRecovery', FailedRecovery())
        run('FixtureParent', fixture)
    print('PASS: screenshot OCR, real Context recovery with both skip settings, on_error/JumpBack routing and failure stop')


if __name__ == '__main__':
    main()
