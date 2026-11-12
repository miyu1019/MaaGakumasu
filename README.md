<!-- markdownlint-disable MD033 MD041 -->

<p align="center">
  <img alt="LOGO" src="./logo.png" width="160" height="160" />
</p>

<div align="center">

# MaaGakumasu-HIF

**学園アイドルマスター** HIF 培育自动化助手，基于 [MaaGakumasu](https://github.com/SuperWaterGod/MaaGakumasu) 与 [MaaFramework](https://github.com/MaaXYZ/MaaFramework)。

[下载正式版](https://github.com/miyu1019/MaaGakumasu/releases/latest) · [AutoBuild 测试版](https://github.com/miyu1019/MaaGakumasu/releases/tag/autobuild) · [使用与构建](docs/hif/使用与构建.md) · [当前版本说明](docs/hif/发布说明.md)

</div>

> [!IMPORTANT]
> 本 Fork 只维护 **HIF 培育路线**。自动培育仍处于测试阶段，可能因弹窗、识别失败或游戏界面更新而卡住，请在可随时接管的情况下使用。

## 功能

- **HIF培育任务**：固定本战，沿用游戏中已选偶像，无需选择偶像、剧本或难度；支持培育次数设置、体力药，以及培育中途通过「跳过准备阶段」续跑。
- **HIF 策略页**（左侧导航）：
  - **出牌优先级**：按职业设置战斗出牌顺序、条件优先级和使用限制，以及未配置卡偏好和保留卡（全力、強気）；
  - **获取与定制**：授业换入、支给领卡时优先获取的卡，以及技能卡定制项目与目标次数；
  - **授业换出卡**：授业换卡时优先换出的卡；
  - **出牌编排**：起始卡打出前后用饮，并按顺序衔接后续卡；
  - **卡库**：按职业筛选全部卡牌，点卡加入或移出各名单；
  - **饮料**：领取顺序、购买范围与使用时机。
- **职业配置分享**：以 JSON 导出、导入单个职业的出牌配置，详见[按职业分享配置](extensions/hif/docs/hif_native_priority_panel.md#按职业分享配置)。
- **更新与备份**：应用内检查本 Fork 的 Releases 并安装完整包，失败自动回滚；附带一键导出、导入设置脚本。

战斗职业：好调、集中、好印象、元気、全力、強気。发行包只预置「集中」策略，其他职业为空白模板，可在策略页中自行配置。

## 运行环境

| 项目 | 说明 |
| --- | --- |
| 系统 | Windows x64 |
| 模拟器 | MuMu 模拟器，分辨率 `1280x720 (240DPI)` |
| 游戏版本 | 官服已在 MuMu 完成育成测试；DMM 尚未完整验证；不支持插件版汉化 |
| 运行库 | 若无法启动，请安装 [VC++ 可再发行程序包](https://aka.ms/vs/17/release/vc_redist.x64.exe) 与 [.NET 桌面运行时 10](https://dotnet.microsoft.com/zh-cn/download/dotnet/10.0) |

## 快速开始

1. 从 [Releases](https://github.com/miyu1019/MaaGakumasu/releases/latest) 下载 `MaaGakumasu-HIF-win-x64-<版本>.zip`（可用同名 `.sha256` 校验），解压到新目录。
2. 运行 `MaaGakumasu.exe`，连接模拟器。首次启动只有「HIF培育」任务，默认职业为「集中」、培育一次、不使用体力药。
3. 在游戏中选好偶像，并开启剧情、弹窗相关的跳过选项。
4. 按需在「HIF 策略」页调整策略并保存，检查任务选项后，让游戏停在主界面（ホーム）再启动；任务会从主界面进入培育。培育中途卡住、需要接着跑时，打开「跳过准备阶段」，在培育画面直接启动。

> [!TIP]
> 建议在自动培育时**关闭实时预览**，以减少演出横竖屏切换时截图失败对任务的干扰。

## 版本与更新

- **正式版**：标签为 `vYYMMDD.序号`，同日递增序号。
- **测试版（AutoBuild）**：每天北京时间 04:00 从 `hif-dev` 分支自动构建，仅保留最新一次通过验证的 Windows x64 包；请解压到独立目录测试。
- **应用内更新**：「设置 → 版本更新」可选择正式版或测试版渠道。更新会等待全部任务结束，备份后安装并重启；个人配置、连接、任务选项与布局保留。
- **手动升级**：退出旧版，运行 `export_settings_导出设置.bat` 备份；解压新版后运行 `import_settings_导入设置.bat` 导入。详见[一键备份与导入](docs/hif/使用与构建.md#一键备份与导入设置和个人策略)。

本客户端只从本 Fork 的 Releases 更新，不提供 Mirror 酱下载源，也不跟随原版 MaaGakumasu 更新。

## 配置文件

- 个人策略：`config/hif/`（如 `cards_priority.json`），策略页与脚本共用；
- 实例、任务与连接：`config/instances/`。

默认模板只在文件不存在时复制，不会覆盖已有设置。仅兼容 v261015.1 及之后版本的配置；更早的旧格式内容会被忽略，并在策略页顶部提示所属模块。

## 问题反馈

请在 [Issues](https://github.com/miyu1019/MaaGakumasu/issues) 中附上：

- `debug/` 下的日志（如 `maa.log`）及出问题时的截图；
- `hif-build-info.json` 中的 HIF 版本；
- 模拟器类型、分辨率与游戏版本（官服 / DMM）。

日志可用 `clear_logs_清除日志.bat` 清理。

## 从源码构建

需要 Windows x64、Git、Python 3.12.9 与 .NET SDK 10。

```powershell
python tools/hif_build.py build      # 生成运行目录 build/MaaGakumasu-HIF
python tools/hif_verify.py           # 完整验证
python tools/hif_build.py package    # 打包到 dist/
```

固定版本：MaaFramework 5.14.2、MFAAvalonia v2.16.4（定制源码位于 `frontend/`）、Python 3.12.9。上游同步、前台升级与发布流程见[使用与构建](docs/hif/使用与构建.md)。

| 目录 | 内容 |
| --- | --- |
| `extensions/hif/` | HIF 动作与识别、流水线、任务定义、卡牌目录、默认策略 |
| `frontend/` | 定制的 MFAAvalonia 前台，含「HIF 策略」页 |
| `agent/`、`assets/` | 原版 MaaGakumasu v1.5.1 脚本与资源，保持与上游一致 |
| `tools/`、`tests/` | 构建、验证、更新与同步工具；回归测试 |

## 文档

- [使用与构建](docs/hif/使用与构建.md)
- [出牌优先级、授业换出卡、出牌编排与卡库](extensions/hif/docs/hif_native_priority_panel.md)
- [获取与定制（技能卡定制）](extensions/hif/docs/hif_custom_card_panel.md)
- [饮料](extensions/hif/docs/hif_drink_panel.md)
- [新卡更新流程](extensions/hif/docs/hif_new_card_update.md)

## 与上游的关系

本项目 Fork 自 [SuperWaterGod/MaaGakumasu](https://github.com/SuperWaterGod/MaaGakumasu)，只开发 HIF 路线。原版的日常、商店、竞赛、初/NIA 培育等功能来自上游、不在本 Fork 维护；如需这些功能请使用原版。原版的下载、交流群与 Mirror 酱渠道均不对应本 Fork。

## 免责声明

本软件开源、免费，仅供学习交流，请勿用于商业用途；本项目只提供自动化脚本，不提供任何游戏资源。若有商家使用本软件代练收费，相关费用、问题及后果与本软件无关。

**使用过程中可能出现意想不到的问题。因软件漏洞、理解歧义或异常操作导致的账号问题，开发者不承担任何责任，请自行尝试运行效果后谨慎使用。**

## 许可证

[AGPL-3.0](LICENSE)

## 鸣谢

- [MaaGakumasu](https://github.com/SuperWaterGod/MaaGakumasu) 原作者及[全体贡献者](https://github.com/SuperWaterGod/MaaGakumasu/graphs/contributors)
- [MaaFramework](https://github.com/MaaXYZ/MaaFramework)：自动化框架
- [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia)：前台界面
- [Gakumas Tools](https://github.com/gakumas-tools)：卡牌、定制数据与卡图来源
