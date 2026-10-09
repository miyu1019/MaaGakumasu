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
from hif_app import digest_tree, python_executable, read, runtime_platform, write
from hif_build import frontend_fingerprint


def run(command, cwd=ROOT, env=None):
    subprocess.run([str(x) for x in command], cwd=cwd, check=True, env=env)


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
    for name in ('hif_build.py', 'sync_upstream.py'):
        shutil.copy2(ROOT / 'tools' / name, fixture / 'tools' / name)
    python = python_executable(fixture)
    run([python, fixture / 'tools/validate.py', '--native'], fixture)
    run([python, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], fixture)
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
    run(['dotnet', assembly, fixture, '--first-run-only'])
    run(['dotnet', assembly, fixture])
    run(['dotnet', assembly, fixture, '--followups-only'])
    run(['dotnet', assembly, fixture / 'temp/update-qa', '--new-scenario-only'])
    run(['dotnet', assembly, fixture, '--agent-cleanup-only'], env=env)
    run(['dotnet', assembly, fixture, '--agent-demand-only'], env=env)
    run(['dotnet', assembly, fixture, '--repair-only'])
    if runtime_platform(runtime) == 'win-x64':
        run(['dotnet', assembly, fixture, '--full-update-policy-only'])
    assert before == {name: digest_tree(runtime / name) for name in before}, 'Verification modified packaged settings'
    run(['git', 'diff', '--check'])
    platform = runtime_platform(runtime)
    frontend_publish = args.frontend_publish or ROOT / 'build' / ('frontend-publish' if platform == 'win-x64' else 'frontend-publish-' + platform)
    frontend_dll = frontend_publish / 'libs/MFAAvalonia.Core.dll'
    installed_dll = runtime / 'libs/MFAAvalonia.Core.dll'
    assert frontend_dll.read_bytes() == installed_dll.read_bytes()
    result = {'frontend_source_identity': True, 'upstream_byte_identity': True, 'native_layers': True, 'python_tests': True,
              'five_panels_save_reopen': True, 'new_scenario_native_parser': True,
              'native_followup_panels_migration_and_save_reopen': True,
              'native_agent_cleanup': True,
              'agent_demand_loading': True,
              'native_finite_count_completion': True,
              'native_first_run_instance_options_layout': True,
              'native_full_update_policy': platform == 'win-x64',
              'manual_update_and_rollback': read(fixture / 'debug/update-qa-result.json'),
              'packaged_user_settings_unchanged': True,
              'frontend_sha256': hashlib.sha256(installed_dll.read_bytes()).hexdigest(),
              'new_build_game_test': False, 'remote_uploaded': False}
    write(ROOT / 'build/verification-result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
