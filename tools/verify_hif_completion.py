"""Exercise HIF count/JumpBack termination with the pinned engine and a fake screen."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time

import numpy as np
from maa.context import ContextEventSink
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit


class Screen(CustomController):
    def connect(self):
        return True

    def request_uuid(self):
        return 'hif-completion-fixture'

    def get_features(self):
        return 0

    def screencap(self):
        return np.zeros((1280, 720, 3), dtype=np.uint8)


class Events(ContextEventSink):
    def __init__(self):
        super().__init__()
        self.nodes = Counter()

    def on_raw_notification(self, _context, message, details):
        if message == 'Node.PipelineNode.Succeeded':
            self.nodes[details['node_details']['name']] += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    Toolkit.init_option(str(root))
    resource = Resource()
    assert resource.post_bundle(root / 'extensions/hif/resource/base').wait().succeeded
    controller = Screen()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    events = Events()
    tasker.add_context_sink(events)
    loop, end = 'ProduceHIF__ProduceLoop', 'ProduceHIF__TaskFinished'
    entry, branch, home = ('ProduceHIF__ProduceEntryHIF',
                           'ProduceHIF__ProduceHIFEndFlag',
                           'ProduceHIF__ProduceHIFMainScreenFlag')
    nodes = json.loads((root / 'extensions/hif/resource/base/pipeline/HIF.json').read_text(encoding='utf-8'))
    # Only replace game recognition/actions; use the real count and terminal nodes.
    override = {name: {'recognition': 'DirectHit', 'action': 'DoNothing',
                       'pre_delay': 0, 'post_delay': 0, 'rate_limit': 0,
                       'pre_wait_freezes': 0, 'post_wait_freezes': 0}
                for name in (loop, entry, branch, home, end, 'ProduceHIF')}
    override[end]['action'] = nodes[end]['action']
    override[loop]['next'] = [entry]
    override[entry]['next'] = ['[JumpBack]' + branch]
    override[branch]['next'] = [home]

    def run(start, parameters, limit=6):
        events.nodes.clear()
        job = tasker.post_task(start, parameters)
        deadline = time.monotonic() + limit
        while not job.done and time.monotonic() < deadline:
            time.sleep(.01)
        if not job.done:
            tasker.post_stop().wait()
            raise AssertionError('HIF task did not terminate: ' + str(events.nodes))
        assert job.succeeded, job.status
        return events.nodes.copy()

    # Reproduce the former empty-next return before checking the corrected node.
    old = {**override, loop: {**override[loop], 'max_hit': 1},
           end: {**override[end], 'action': 'DoNothing'}}
    job = tasker.post_task('ProduceHIF', old)
    deadline = time.monotonic() + 6
    while events.nodes[end] < 2 and not job.done and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        assert not job.done and events.nodes[end] >= 2, events.nodes
    finally:
        tasker.post_stop().wait()

    for rounds in (0, 1, 2, 6, 9):
        counts = run('ProduceHIF', {**override, loop: {**override[loop], 'max_hit': rounds}})
        assert counts[loop] == rounds and counts[end] == 1, counts
        # StopTask must finish just this chain, leaving the queue usable.
        assert run(end, override)[end] == 1
    for start in ('ProduceHIF__ProduceBackHome', home):
        parameters = {**override, start: {**override[home], 'next': nodes[start]['next']},
                      loop: {**override[loop], 'max_hit': 0}}
        assert run(start, parameters)[end] == 1

    # Unlimited mode must remain running until explicitly canceled.
    events.nodes.clear()
    job = tasker.post_task('ProduceHIF', override)
    deadline = time.monotonic() + 6
    while events.nodes[loop] < 3 and not job.done and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        assert not job.done and events.nodes[loop] >= 3 and events.nodes[end] == 0, events.nodes
    finally:
        tasker.post_stop().wait()
    print('HIF completion passed: finite 0/1/2/6/9, nested JumpBack, both home exits, next task and unlimited mode.')


if __name__ == '__main__':
    main()
