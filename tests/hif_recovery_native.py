"""Explicit real-engine recovery checks with synthetic screens; never touches a game."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
from maa.library import Library
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import ContextEventSink
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from maa.agent.agent_server import AgentServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'extensions/hif/agent'))


class Screen(CustomController):
    def connect(self): return True
    def request_uuid(self): return 'hif-recovery-fixture'
    def get_features(self): return 0
    def screencap(self): return np.zeros((1280, 720, 3), dtype=np.uint8)


class Events(ContextEventSink):
    def __init__(self):
        super().__init__()
        self.nodes = Counter()

    def on_raw_notification(self, _context, message, details):
        if message == 'Node.PipelineNode.Succeeded':
            self.nodes[details['node_details']['name']] += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    args = parser.parse_args()
    Library.open(args.library)
    assert Library.version() == 'v5.14.2', Library.version()
    # Register production callbacks on the resource, not an external Agent server.
    with patch.object(AgentServer, 'custom_action', side_effect=lambda _name: lambda cls: cls), \
            patch.object(AgentServer, 'custom_recognition', side_effect=lambda _name: lambda cls: cls):
        from hif.action import produce as action
        from hif.reco.produce import ProduceHIF__ProduceHIFRecoveryExpiredAuto
    state = action._hif_recovery
    entry = 'ProduceHIF__ProduceEntryHIF'
    recovery = 'ProduceHIF__ProduceHIFEntryRecovery'
    expired = 'ProduceHIF__ProduceHIFRecoveryExpired'
    home = 'ProduceHIF__ProduceHIFMainScreenFlag'
    base = json.loads((ROOT / 'extensions/hif/resource/base/pipeline/HIF.json').read_text(encoding='utf-8'))
    parameters = {
        entry: {**base[entry], 'timeout': 1, 'rate_limit': 0, 'next': ['FixtureReady']},
        recovery: base[recovery], expired: base[expired],
        home: {'recognition': 'DirectHit', 'action': 'DoNothing', 'next': []},
        'FixtureReady': {'recognition': {'type': 'Custom', 'param': {'custom_recognition': 'FixtureReady'}},
                         'action': 'DoNothing', 'next': []},
        'FixtureBudget': {'recognition': 'DirectHit', 'action': {'type': 'Custom', 'param': {'custom_action': 'FixtureBudget'}},
                          'next': [entry]},
    }
    for suffix in action._HIF_SAFE_SCENES:
        if 'ProduceHIF__' + suffix != home:
            parameters['ProduceHIF__' + suffix] = {'enabled': False}

    class Ready(CustomRecognition):
        allow = True
        def analyze(self, context, argv):
            return self.AnalyzeResult(box=[0, 0, 1, 1] if self.allow and state.entry_recovered else None, detail='fixture')

    class Budget(CustomAction):
        pending = False
        def run(self, context, argv):
            state.bind(argv)
            if self.pending:
                state.pending = 'fixture:submitted'
            else:
                state.deadline = state.now() - 1
            return True

    with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='hif-recovery-native-', ignore_cleanup_errors=True) as folder:
        Toolkit.init_option(folder)
        resource, screen, tasker, events = Resource(), Screen(), Tasker(), Events()
        assert resource.post_bundle(ROOT / 'extensions/hif/resource/base').wait().succeeded
        callbacks = {
            'ProduceHIF__ProduceHIFEntryObserveAuto': action.ProduceHIF__ProduceHIFEntryObserveAuto(),
            'ProduceHIF__ProduceHIFEntryRecoveryAuto': action.ProduceHIF__ProduceHIFEntryRecoveryAuto(),
            'ProduceHIF__ProduceHIFRecoveryExpiredAction': action.ProduceHIF__ProduceHIFRecoveryExpiredAction(),
        }
        for name, callback in callbacks.items():
            assert resource.register_custom_action(name, callback)
        ready, budget = Ready(), Budget()
        assert resource.register_custom_recognition('FixtureReady', ready)
        assert resource.register_custom_recognition('ProduceHIF__ProduceHIFRecoveryExpiredAuto', ProduceHIF__ProduceHIFRecoveryExpiredAuto())
        assert resource.register_custom_action('FixtureBudget', budget)
        assert screen.post_connection().wait().succeeded
        assert tasker.bind(resource, screen)
        tasker.add_context_sink(events)

        def run(start=entry, override=None, success=True):
            events.nodes.clear()
            job = tasker.post_task(start, override or parameters)
            deadline = time.monotonic() + 15
            while not job.done and time.monotonic() < deadline:
                time.sleep(.01)
            if not job.done:
                tasker.post_stop().wait()
                raise AssertionError('Recovery did not terminate')
            assert job.succeeded == success, (job.status, events.nodes)
            return events.nodes.copy()

        counts = run()
        assert counts[entry] == 2 and counts[recovery] == 1, counts
        # A new task ID starts a new budget even after the previous task recovered.
        counts = run()
        assert counts[recovery] == 1, counts
        nested = {**parameters,
                  'FixtureParent': {'recognition': 'DirectHit', 'action': 'DoNothing',
                                    'next': ['[JumpBack]' + entry, 'FixtureDone']},
                  'FixtureDone': {'recognition': 'DirectHit', 'action': 'DoNothing', 'next': []},
                  entry: {**parameters[entry], 'max_hit': 2}}
        counts = run('FixtureParent', nested)
        assert counts[recovery] == 1 and counts['FixtureDone'] == 1, counts
        ready.allow = False
        run(success=False)  # Second timeout must fail, not loop indefinitely.
        assert state.entry_recovered
        budget.pending = True
        run('FixtureBudget', success=False)
        assert state.pending == 'fixture:submitted'
        budget.pending = False
        guarded = {**parameters, entry: {**parameters[entry], 'next': ['[JumpBack]' + expired, 'FixtureReady']}}
        run('FixtureBudget', guarded, success=False)
        assert events.nodes['FixtureReady'] == 0
        assert not state.entry_recovered and state.deadline is not None
        assert tasker.post_task('FixtureReady', {**parameters, 'FixtureReady': {'recognition': 'DirectHit'}}).wait().succeeded
    print('MaaFramework 5.14.2 recovery PASS: real on_error, new task reset, repeated timeout, pending transaction, expiry, next task')


if __name__ == '__main__':
    main()
