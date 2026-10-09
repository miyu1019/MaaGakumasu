"""Exercise a compiled frontend and the detached installer in isolated local runtimes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import install_composition, read, write
from hif_update import package_manifest


def wait_for(predicate, seconds=360):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.25)
    raise TimeoutError('Full-update smoke test timed out')


def run(runtime, frontend, output, startup_failure=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    current = output / 'current'
    shutil.copytree(runtime, current, ignore=shutil.ignore_patterns('config', 'backup', 'temp', 'debug', 'logs', '__pycache__'))
    shutil.copytree(frontend, current, dirs_exist_ok=True)
    shutil.copy2(frontend / 'MFAAvalonia.exe', current / 'MaaGakumasu.exe')
    (current / 'MFAAvalonia.exe').unlink()
    shutil.copytree(runtime / 'runtimes', current / 'runtimes', dirs_exist_ok=True)
    for path in (runtime / 'libs').glob('Maa*.dll'):
        shutil.copy2(path, current / 'libs' / path.name)
    shutil.copytree(ROOT / 'extensions/hif/first-run/config', current / 'config')
    for path in (ROOT / 'extensions/hif/defaults').glob('*.json'):
        (current / 'config/hif').mkdir(exist_ok=True)
        shutil.copy2(path, current / 'config/hif' / path.name)
    for name in ('hif_update.py', 'hif_app.py', 'hif_update_runner.ps1', 'export_settings.ps1', 'import_settings.ps1'):
        shutil.copy2(ROOT / 'tools' / name, current / 'tools' / name)
    for name in ('export_settings_导出设置.bat', 'import_settings_导入设置.bat', 'recover_update_恢复更新.bat'):
        shutil.copy2(ROOT / name, current / name)
    write(current / 'appsettings.json', {'NoAutoStart': 'True', 'ShowGui': 'False'})
    write(current / 'config/hif/smoke-personal.json', {'personal': 'preserve exactly'})
    old = read(current / 'hif-release.json')
    package_manifest(current, old['version'])
    candidate = output / 'candidate'
    shutil.copytree(current, candidate, ignore=shutil.ignore_patterns('appsettings.json'))
    write(candidate / 'config/hif/smoke-personal.json', {'personal': 'public default must not replace user'})
    incoming = {**old, 'version': 'v261009.99'}
    write(candidate / 'hif-release.json', incoming)
    info = read(candidate / 'hif-build-info.json')
    info['version'] = incoming['version']
    write(candidate / 'hif-build-info.json', info)
    task_path = candidate / 'extensions/hif/tasks/produce_hif.json'
    tasks = read(task_path)
    tasks['option']['HIF.QA新增选项'] = {'cases': [{'name': '关闭'}, {'name': '开启'}], 'default_case': '开启'}
    tasks['task'][0]['option'].append('HIF.QA新增选项')
    write(task_path, tasks)
    install_composition(candidate)
    if startup_failure:
        (candidate / 'MFAAvalonia.dll').write_bytes(b'injected managed entrypoint startup failure')
    manifest = package_manifest(candidate, incoming['version'])
    package = output / ('MaaGakumasu-HIF-win-x64-' + incoming['version'] + '.zip')
    with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name in sorted(set(manifest['files']) | set(manifest['defaults']) | {'hif-package-manifest.json'}):
            archive.write(candidate / name, 'MaaGakumasu-HIF/' + name)
    package.with_suffix('.zip.sha256').write_text(hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name)
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    start = subprocess.Popen([str(current / 'MaaGakumasu.exe'), '--hif-update-package', str(package)],
                             cwd=current, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    operation_path = None
    try:
        def operation_ready():
            pointer = current / 'temp/hif-update-pending.json'
            if pointer.exists():
                path = Path(read(pointer)['operation'])
                if path.exists():
                    return path
            return None
        operation_path = wait_for(operation_ready)
        def completed():
            op = read(operation_path)
            if startup_failure and op['state'] == 'rolled_back' and op.get('restored_pid'):
                return op
            if op['state'] in ('failed', 'rolled_back', 'rollback_failed'):
                raise RuntimeError(op.get('error', op['state']))
            return op if op['state'] == 'complete' else None
        operation = wait_for(completed)
        assert start.wait(timeout=15) == 0
        assert read(current / 'hif-release.json')['version'] == (old['version'] if startup_failure else incoming['version'])
        assert read(current / 'config/hif/smoke-personal.json') == {'personal': 'preserve exactly'}
        assert read(current / 'config/config.json')['EnableCheckVersion'] is True
        assert read(current / 'config/config.json')['EnableAutoUpdateResource'] is False
        assert Path(operation['settings_backup']).is_file()
        with zipfile.ZipFile(operation['settings_backup']) as backup:
            for name in ('config/hif/cards_priority.json', 'config/hif/smoke-personal.json'):
                assert backup.read(name) == (current / name).read_bytes(), name
        result = {'native_frontend_preparation': True, 'native_idle_countdown_exit': True,
                  'detached_install_and_restart': True, 'new_frontend_acknowledged': True,
                  'personal_strategy_bytes_preserved': True, 'backup_created': True,
                  'old_pid': start.pid, 'new_pid': operation['launched_pid'], 'operation': str(operation_path)}
        if startup_failure:
            result['new_frontend_acknowledged'] = False
            result['startup_failure_rolled_back'] = True
            assert (current / 'MFAAvalonia.dll').read_bytes() == (frontend / 'MFAAvalonia.dll').read_bytes()
            def old_started():
                return any('更新失败，已恢复旧版' in file.read_text(encoding='utf-8-sig', errors='replace')
                           for file in (current / 'logs').glob('*.log'))
            wait_for(old_started, 30)
            result['old_frontend_reopened'] = True
        write(output / 'result.json', result)
        print(json.dumps(result, ensure_ascii=True))
    finally:
        if start.poll() is None:
            start.kill()
            start.wait()
        if operation_path and operation_path.exists():
            op = read(operation_path)
            executable = "'" + str(current / 'MaaGakumasu.exe').replace("'", "''") + "'"
            subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                            'Get-Process -Name MaaGakumasu -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq ' +
                            executable + ' } | Stop-Process'], capture_output=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, default=ROOT / 'build/MaaGakumasu-HIF')
    parser.add_argument('--frontend', type=Path, default=ROOT / 'build/full-update-frontend')
    parser.add_argument('--output', type=Path, default=ROOT / 'build' / ('full-update-smoke-' + str(time.time_ns())))
    parser.add_argument('--startup-failure', action='store_true')
    args = parser.parse_args()
    run(args.runtime, args.frontend, args.output, args.startup_failure)
