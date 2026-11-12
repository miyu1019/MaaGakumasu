"""Verify a built runtime using fixtures; never run the game or touch user installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import digest_tree, python_executable, read, write
from hif_build import frontend_fingerprint


def run(command, cwd=ROOT, env=None):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True, env=env)


# 耗时长的测试模块先启动；同一组在一个进程里依次运行：两个更新测试都会改动运行目录的 temp/，
# 完整包更新测试会在运行目录下临时运行一个名为 MaaGakumasu.exe 的进程，导出设置检测到它会拒绝导出。
SLOW_TEST_GROUPS = [('test_full_update.py', 'test_fork_update.py', 'test_export_settings.py'), ('test_hif_keep_drink.py',),
                    ('test_sync_upstream.py',), ('test_clear_logs.py',), ('test_hif_behavior.py',)]
RUN_TEST_FILES = ('import sys, unittest; sys.path.insert(0, "."); suite = unittest.TestSuite('
                  'unittest.defaultTestLoader.discover("tests", pattern=name) for name in sys.argv[1:]); '
                  'sys.exit(not unittest.TextTestRunner(verbosity=0).run(suite).wasSuccessful())')


PANEL_QA_MODES = ('build-version', 'first-run', 'panels', 'sequences', 'priority-share', 'custom-counts', 'drink-limits', 'panel-help',
                  'strategy-page', 'dashboard-maximize', 'task-description', 'task-list-layout', 'instance-layout', 'live-view-off',
                  'keep-alive-page')
PANEL_QA_AGENT_MODES = ('agent-cleanup', 'agent-demand', 'repair', 'full-update-policy', 'swap-out-options')


def run_python_tests(python, fixture, workers=4):
    """按模块分进程并行运行 tests/，任一组失败即报错并输出该组日志。"""
    from concurrent.futures import ThreadPoolExecutor
    grouped = {name for group in SLOW_TEST_GROUPS for name in group}
    groups = list(SLOW_TEST_GROUPS) + [(p.name,) for p in sorted((fixture / 'tests').glob('test_*.py')) if p.name not in grouped]
    missing = [name for group in SLOW_TEST_GROUPS for name in group if not (fixture / 'tests' / name).is_file()]
    assert not missing, '测试分组里的模块不存在：' + '、'.join(missing)

    def execute(group):
        return group, subprocess.run([str(python), '-c', RUN_TEST_FILES, *group], cwd=fixture, capture_output=True,
                                     text=True, encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONUTF8': '1'})

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(execute, groups))
    ran = 0
    failed = []
    for group, result in results:
        for line in result.stderr.splitlines():
            if line.startswith('Ran ') and ' test' in line:
                ran += int(line.split()[1])
        if result.returncode:
            failed.append(group)
            print(result.stdout + result.stderr, flush=True)
    print(f'Python tests: {ran} tests in {len(groups)} processes', flush=True)
    if failed:
        raise SystemExit('Python 测试失败：' + '、'.join(name for group in failed for name in group))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', default=str(ROOT / 'build/MaaGakumasu-HIF'))
    parser.add_argument('--fixture', default=str(ROOT / 'build/verification-runtime'))
    parser.add_argument('--frontend-publish', type=Path, default=None)
    args = parser.parse_args()
    runtime = Path(args.runtime).resolve()
    assert read(runtime / 'hif-build-info.json').get('frontend_source_sha256') == frontend_fingerprint(), \
        'Runtime frontend source identity differs from repository source'
    before = {name: digest_tree(runtime / name) for name in ('upstream', 'extensions', 'config', 'resource', 'python', 'libs', 'runtimes')}
    assert before['upstream'] == read(runtime / 'upstream-provenance.json')['files']
    fixture = Path(args.fixture).resolve()
    if not fixture.is_relative_to(ROOT / 'build'):
        raise SystemExit('Verification fixture must stay inside this repository build directory.')
    if fixture.exists():
        raise SystemExit('Verification fixture already exists; preserve it and use a fresh build for the next run.')
    fixture.mkdir()
    for name in ('upstream', 'extensions', 'config', 'resource', 'python', 'libs', 'runtimes', 'tools', 'lang', 'data'):
        if (runtime / name).is_dir():
            shutil.copytree(runtime / name, fixture / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('interface.json', 'upstream-provenance.json', 'hif-release.json',
                 'hif-build-info.json', 'requirements-hif.txt', 'README.md', 'clear_logs_清除日志.bat',
                 'export_settings_导出设置.bat', 'import_settings_导入设置.bat', 'recover_update_恢复更新.bat'):
        shutil.copy2(runtime / name, fixture / name)
    shutil.copytree(runtime / 'docs', fixture / 'docs')
    (fixture / 'temp').mkdir()
    (fixture / 'debug').mkdir()
    (fixture / 'build').mkdir()
    shutil.copytree(ROOT / 'tests', fixture / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'frontend', fixture / 'frontend',
                    ignore=shutil.ignore_patterns('bin', 'obj', '.avalonia-build-tasks', '__pycache__'))
    for name in ('hif_build.py', 'sync_upstream.py', 'sync_hif_customization_catalog.py'):
        shutil.copy2(ROOT / 'tools' / name, fixture / 'tools' / name)
    python = python_executable(fixture)
    run([python, fixture / 'tools/validate.py', '--native'], fixture)
    run_python_tests(python, fixture)
    run([python, ROOT / 'tools/verify_hif_completion.py', '--root', fixture], fixture)
    run([python, fixture / 'tools/verify_updates.py'], fixture)
    env = os.environ.copy()
    env['TEMP'] = env['TMP'] = str(ROOT / 'build/temp')
    env['DOTNET_CLI_HOME'] = str(ROOT / 'build/dotnet')
    env['DOTNET_CLI_TELEMETRY_OPTOUT'] = '1'
    project = ROOT / 'tools/PanelQa/PanelQa.csproj'
    run(['dotnet', 'build', project, '--disable-build-servers', '-m:1', '-p:UseSharedCompilation=false',
         '-p:NuGetAudit=false', '-p:RuntimeRoot=' + str(runtime), '--nologo', '-v:quiet'], env=env)
    assembly = ROOT / 'tools/PanelQa/bin/Debug/net10.0/PanelQa.dll'
    # 原生面板检查按原顺序在同一进程里依次运行，省去每项启动 Avalonia 与原生库的时间；
    # 新剧本检查用另一个运行目录，Agent 相关检查需要上面的临时目录环境，各占一个进程。
    run(['dotnet', assembly, fixture, '--modes', ','.join(PANEL_QA_MODES)])
    run(['dotnet', assembly, fixture / 'temp/update-qa', '--new-scenario-only'])
    run(['dotnet', assembly, fixture, '--modes', ','.join(PANEL_QA_AGENT_MODES)], env=env)
    assert before == {name: digest_tree(runtime / name) for name in before}, 'Verification modified packaged settings'
    run(['git', 'diff', '--check'])
    frontend_publish = args.frontend_publish or ROOT / 'build/frontend-publish'
    frontend_dll = frontend_publish / 'libs/MFAAvalonia.Core.dll'
    installed_dll = runtime / 'libs/MFAAvalonia.Core.dll'
    assert frontend_dll.read_bytes() == installed_dll.read_bytes()
    result = {'frontend_source_identity': True, 'upstream_byte_identity': True, 'native_layers': True, 'python_tests': True,
              'five_panels_save_reopen': True, 'new_scenario_native_parser': True,
              'native_play_sequences_and_save_reopen': True,
              'native_drink_limits_and_panel_help': True, 'native_strategy_page': True,
              'native_task_description_settings': True,
              'native_task_list_small_window_layout': True,
              'native_instance_layouts_and_global_panel_switches': True,
              'native_agent_cleanup': True,
              'agent_demand_loading': True,
              'native_finite_count_completion': True,
              'native_first_run_instance_options_layout': True,
              'native_full_update_policy': True,
              'manual_update_and_rollback': read(fixture / 'debug/update-qa-result.json'),
              'packaged_user_settings_unchanged': True,
              'frontend_sha256': hashlib.sha256(installed_dll.read_bytes()).hexdigest(),
              'new_build_game_test': False, 'remote_uploaded': False}
    write(ROOT / 'build/verification-result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
