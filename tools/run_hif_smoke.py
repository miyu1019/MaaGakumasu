"""Explicit device test; never called by startup or the updater."""
import argparse
import copy
import json
from pathlib import Path
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import read, write
from maa.agent_client import AgentClient
from maa.controller import AdbController
from maa.context import ContextEventSink
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit


def merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def selected_parameters(task, definitions):
    result = copy.deepcopy(task['pipeline_override'])
    saved = {o['name']: o for o in task.get('option', [])}
    def apply(name):
        definition = definitions[name]
        stored = saved.get(name, {})
        if definition['type'] == 'input':
            values = {}
            for field in definition['inputs']:
                value = stored.get('data', {}).get(field['name'], field['default'])
                kind = field.get('pipeline_type', 'string')
                values[field['name']] = int(value) if kind == 'int' else float(value) if kind == 'float' else value
            def substitute(value):
                if isinstance(value, str) and value.startswith('{') and value.endswith('}'):
                    return values[value[1:-1]]
                if isinstance(value, dict):
                    return {k: substitute(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [substitute(v) for v in value]
                return value
            merge(result, substitute(definition.get('pipeline_override', {})))
            return
        cases = definition.get('cases', [])
        default = definition.get('default_case')
        if definition['type'] == 'checkbox':
            selected = stored.get('selected_cases', default or [])
            picked = [case for case in cases if case['name'] in selected]
        else:
            index = stored.get('index')
            if index is None:
                index = next((i for i, case in enumerate(cases) if case['name'] == default), 0)
            picked = [cases[index]] if cases and 0 <= index < len(cases) else []
        for case in picked:
            merge(result, case.get('pipeline_override', {}))
            for child in case.get('option', []):
                for option in stored.get('sub_options', []):
                    saved[option['name']] = option
                apply(child)
    for name in definitions:
        if name in saved:
            apply(name)
    return result


class Evidence(ContextEventSink):
    def __init__(self, path):
        super().__init__()
        self.file = path.open('w', encoding='utf-8')
        self.last = None

    def on_raw_notification(self, _context, message, details):
        if message == 'Node.PipelineNode.Succeeded':
            name = details.get('name')
            self.file.write(json.dumps({'time': time.time(), 'name': name, 'details': details}, ensure_ascii=False) + '\n')
            self.file.flush()
            if name != self.last:
                print('NODE', name, flush=True)
                self.last = name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--timeout', type=int, default=2400)
    parser.add_argument('--instance', default='0ff44ccf')
    parser.add_argument('--use-ap', action='store_true', help='Explicitly authorized test-only AP restoration')
    parser.add_argument('--resume', action='store_true', help='Resume the cultivation currently on screen')
    parser.add_argument('--finish-only', action='store_true', help='Validate finite-count completion on the home screen without starting cultivation')
    args = parser.parse_args()
    config = read(ROOT / 'config/instances' / (args.instance + '.json'))
    saved = next(t for t in config['TaskItems'] if t['entry'] == 'ProduceHIF')
    document = read(ROOT / 'extensions/hif/tasks/produce_hif.json')
    spec = next(t for t in document['task'] if t['name'] == saved['name'])
    task = {**spec, 'option': saved['option']}
    parameters = selected_parameters(task, document['option'])
    parameters['ProduceHIF__ProduceLoop'] = {'max_hit': args.rounds}
    if args.finish_only:
        parameters['ProduceHIF__ProduceLoop'] = {'max_hit': 0}
    if args.use_ap:
        parameters['ProduceHIF__ProduceLackAP'] = {'next': 'ProduceHIF__ProduceLackAPRestore'}
    if args.resume:
        parameters['ProduceHIF__ProduceSkipPreparation'] = {'enabled': True}
    Toolkit.init_option(str(ROOT))
    resource = Resource()
    paths = [ROOT / 'extensions/hif/resource/base']
    if saved['name'].endswith('-zh_CN'):
        paths.append(ROOT / 'extensions/hif/resource/zh_CN')
    for path in paths:
        assert resource.post_bundle(path).wait().succeeded, path
    device = config['AdbDevice']
    controller = AdbController(device['AdbPath'], device['AdbSerial'],
                               screencap_methods=device['ScreencapMethods'],
                               input_methods=device['InputMethods'], config=json.loads(device['Config']))
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    stamp = str(time.time_ns())
    client = AgentClient()
    assert client.bind(resource)
    log = (ROOT / 'debug' / ('agent-smoke-' + stamp + '.log')).open('w', encoding='utf-8')
    process = subprocess.Popen([str(ROOT / 'python/python.exe'), '-u', str(ROOT / 'tools/agent_entry.py'), '--hif-only', client.identifier],
                               cwd=ROOT, stdout=log, stderr=log, creationflags=0x08000000)
    if not client.connect():
        process.terminate()
        raise RuntimeError('Agent connect failed; see ' + log.name)
    evidence = Evidence(ROOT / 'debug' / ('hif-smoke-' + stamp + '.jsonl'))
    tasker.add_context_sink(evidence)
    job = tasker.post_task('ProduceHIF__ProduceBackHome' if args.finish_only else 'ProduceHIF', parameters)
    deadline = time.monotonic() + args.timeout
    while not job.done and time.monotonic() < deadline:
        time.sleep(1)
    timed_out = not job.done
    if timed_out:
        tasker.post_stop().wait()
    import cv2
    controller.post_screencap().wait()
    cv2.imwrite(str(ROOT / 'debug' / ('hif-smoke-' + stamp + '.png')), controller.cached_image)
    print('RESULT', job.status._status, flush=True)
    print('EVIDENCE', stamp, flush=True)
    write(ROOT / 'debug' / ('hif-smoke-' + stamp + '-result.json'),
          {'status': int(job.status._status), 'timed_out': timed_out, 'round_limit': args.rounds,
           'resume': args.resume, 'test_only_ap_restore': args.use_ap, 'finish_only': args.finish_only})
    client.disconnect()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
    log.close()
    return 0 if job.succeeded and not timed_out else 1


if __name__ == '__main__':
    raise SystemExit(main())
