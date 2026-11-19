"""启动本地同步安装器，独立保存日志并返回安装器的退出码。"""
import argparse
from pathlib import Path
import subprocess


def run_installer(powershell, operation):
    operation = operation.resolve(strict=True)
    stage = operation.parent
    command = [str(powershell), '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', str(stage / 'hif_update_runner.ps1'), '-OperationPath', str(operation)]
    # 新版前台会继续运行。直接传入文件句柄，等待安装器退出时无需等前台关闭输出管道。
    with (stage / 'install.stdout.log').open('wb') as stdout, (stage / 'install.stderr.log').open('wb') as stderr:
        return subprocess.run(command, cwd=stage, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                              creationflags=subprocess.CREATE_NO_WINDOW).returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--powershell', required=True, type=Path)
    parser.add_argument('--operation', required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run_installer(args.powershell, args.operation))
