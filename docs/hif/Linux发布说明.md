

## Linux x64（实验性）

新增 `MaaGakumasu-HIF-linux-x64-v261008.2.tar.gz` 和同名 SHA-256 校验文件。包内置 .NET 10、Python 3.12.9 和 MaaFramework 5.12.3，解压到新的可写目录后运行 `./start.sh`，再配置 ADB 设备。

已在 Debian 13 x86_64 验证 206 项测试、原生资源加载、五个配置面板保存/重开、Agent 生命周期、首次启动配置、更新回滚以及解压后的图形启动；发布工作流还会在 Ubuntu 24.04 x64 重新构建并运行完整验证。Linux 下的真实模拟器和游戏长流程尚未验证。

Linux 与 Windows 的运行库分别打包。升级请解压对应平台的完整包到新目录，并复制自己的 `config/`；需要保留布局时一并复制 `resource/mfa_layout.json`。手动更新按平台选择发行包并校验 SHA-256，拒绝跨平台安装。
