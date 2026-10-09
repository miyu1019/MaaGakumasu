"""Validate the isolated task graph and register callbacks without a device."""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validate(root, upstream=None, native=False):
    root = Path(root)
    upstream = Path(upstream) if upstream else root / 'upstream'
    sys.path.insert(0, str(root / 'tools'))
    from hif_app import compose
    compose(root, upstream)
    callbacks = set()
    callback_sets = []
    for folder in (root / 'extensions/hif/agent', upstream / 'agent'):
        local = set()
        for path in folder.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('custom_action', 'custom_recognition') and node.args and isinstance(node.args[0], ast.Constant):
                    local.add(node.args[0].value)
        callback_sets.append(local)
        callbacks.update(local)
    if callback_sets[0] & callback_sets[1]:
        raise ValueError('Upstream and HIF callback names conflict.')
    layers = []
    for layer in ('base', 'zh_CN', 'DMM'):
        nodes = {}
        for path in (root / 'extensions/hif/resource' / layer / 'pipeline').glob('*.json'):
            nodes.update(read(path))
        layers.append(nodes)
    nodes = layers[0]
    tasks = read(root / 'extensions/hif/tasks/produce_hif.json')
    errors = []
    def check(value, owner=''):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('next', 'on_error', 'interrupt'):
                    for target in item if isinstance(item, list) else [item]:
                        target = re.sub(r'^\[[^]]+\]', '', target)
                        if target not in nodes:
                            errors.append(f'{owner}: missing node {target}')
                if key in ('custom_action', 'custom_recognition') and item not in callbacks:
                    errors.append(f'{owner}: missing callback {item}')
                if key == 'template':
                    for image in item if isinstance(item, list) else [item]:
                        if not isinstance(image, str):
                            continue
                        if not any((root / 'extensions/hif/resource' / layer / 'image' / image).is_file()
                                   for layer in ('base', 'zh_CN', 'DMM')):
                            errors.append(f'{owner}: missing image {image}')
                check(item, owner)
        elif isinstance(value, list):
            for item in value:
                check(item, owner)
    for name, node in nodes.items():
        if not name.startswith('ProduceHIF'):
            errors.append('Non-HIF node in extension: ' + name)
        check(node, name)
    for layer in layers[1:]:
        check(layer, 'overlay')
    check(tasks, 'task')
    for task in tasks['task']:
        assert task['entry'] == 'ProduceHIF'
        assert all(x.startswith('HIF.') for x in task['option'])
        assert not {'HIF.培育偶像', 'HIF.培育偶像-zh_CN', 'HIF.培育难度', 'HIF.跳过选择偶像'} & set(task['option'])
        for key in task['option']:
            assert key in tasks['option'], key
    if errors:
        raise ValueError('\n'.join(sorted(set(errors))))
    for path in (upstream / 'resource').rglob('pipeline/*.json'):
        if set(read(path)) & nodes.keys():
            raise ValueError('Upstream and HIF pipeline names conflict: ' + str(path))
    if native:
        # Use the pinned runtime to reject missing models, invalid node fields and import errors.
        # AgentServer and MaaFramework use different native API modes and must live
        # in different processes (the same split used by the actual front end).
        for directory, module in ((upstream / 'agent', 'custom'), (root / 'extensions/hif/agent', 'hif')):
            code = ('import sys; sys.path.insert(0, ' + repr(str(root))
                    + '); sys.path.insert(0, ' + repr(str(directory)) + '); import ' + module)
            subprocess.run([sys.executable, '-c', code], check=True, cwd=root)
        from maa.resource import Resource
        from maa.toolkit import Toolkit
        Toolkit.init_option(str(root))
        for resource in read(upstream / 'interface.json')['resource']:
            bundle = Resource()
            for path in resource['path']:
                job = bundle.post_bundle(upstream / path).wait()
                if not job.succeeded:
                    raise ValueError('Native resource load failed: ' + path)
            bundle = Resource()
            variants = read(root / 'extensions/hif/resource-variants.json')
            paths = [root / 'extensions/hif/resource' / layer for layer in variants.get(resource['name'], ['base'])]
            for path in paths:
                if not bundle.post_bundle(path).wait().succeeded:
                    raise ValueError('Native HIF resource load failed: ' + str(path))
    return {'nodes': len(nodes), 'callbacks': len(callbacks), 'tasks': len(tasks['task'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--upstream')
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    print(json.dumps(validate(args.root, args.upstream, native=args.native), ensure_ascii=False))


if __name__ == '__main__':
    main()
