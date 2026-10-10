<!-- markdownlint-disable MD033 MD041 -->

# MaaGakumasu-HIF

独立 HIF 培育发行版，由 [miyu1019/MaaGakumasu](https://github.com/miyu1019/MaaGakumasu) 维护，基于原作者的 MaaGakumasu，上游脚本固定提交见 `hif-release.json`。支持 **Windows x64**；前台固定为 MFAAvalonia v2.14.0，MaaFramework 固定为 5.12.3，Python 固定为 3.12.9。

运行包可在[本 Fork 的 Releases](https://github.com/miyu1019/MaaGakumasu/releases)下载，当前源码版本的包名为 `MaaGakumasu-HIF-win-x64-v261010.2.zip`，同时提供 SHA-256 校验文件。版本采用 `vYYMMDD.序号`，同一天的新构建递增序号。尚未发布时，可按下文自行构建，本地 ZIP 产物位于 `dist/`。

## HIF 使用

解压到新的目录，运行 `MaaGakumasu.exe`，连接模拟器并检查已添加的 **HIF培育** 任务选项后启动。HIF 固定本战，默认跳过选偶像，沿用游戏中已选偶像；无需选择偶像、剧本或难度。首次默认「集中」、一次培育、不使用体力药、不跳过准备。

五个配置面板为：战斗出牌优先级、优先获取卡、授业优先换出卡、饮料、技能卡定制。发布包只预置「集中」相关策略；其他职业为空白模板，可通过面板自行设置。定制默认选择为空，按需要导入并选择项目。

优先级面板支持「导出职业配置」和「导入职业配置」，以 JSON 分享单个职业的战斗出牌优先级及相关卡牌规则。导入只替换文件对应职业的战斗配置，保留优先获取与换出名单，检查后点击保存生效；详见[按职业分享配置](extensions/hif/docs/hif_native_priority_panel.md#按职业分享配置)。

新解压目录首次打开只显示「一键培育」实例，任务列表只有「HIF培育」，带有维护者已确认的 HIF 任务选项、浅色蓝色主题和界面布局，不包含模拟器连接信息。公开首次启动模板位于 `extensions/hif/first-run/`，随源码维护；更新已有版本时不覆盖用户的实例、任务选项、界面布局和 HIF 策略。

Agent 按任务启动：仅连接模拟器时不启动 Python Agent；HIF 只启动 HIF Agent，上游任务需要执行时才启动主 Agent。同一实例的连续上游任务复用主 Agent，切换到 HIF 或队列结束后释放它；HIF Agent 在 HIF 任务结束后释放。

已有任务列表重新打开或重新加载资源时保持原样，不补入未选择的其他任务；仍可手动添加。实时预览独立刷新，支持横竖屏切换；停止 HIF 时等待当前原生操作结束后释放 Agent。配置保存失败时保留原文件并提示。

正在培育中时，可打开「跳过准备阶段」续跑。自动培育仍处于测试阶段；育成流程已在 MuMu 测试通过，指定次数结束后正常退出。其他平台及汉化/DMM 的完整实机流程尚未验证。

## 设置与更新

个人设置保存在 `config/hif/`，任务顺序和模拟器连接由前台保存在 `config/instances/`。公共默认模板仅在文件不存在时初始化，不覆盖已有设置。升级请解压到新目录，关闭旧前台后保留并复制自己的 `config/`；旧版合并任务的迁移步骤见[使用与构建说明](docs/hif/使用与构建.md)。

更新前可完全退出前台，双击 `export_settings_导出设置.bat`，将配置、全部个人策略、实例列表、定时任务和布局备份到 `backup/` 的时间戳 ZIP。在新版目录双击 `import_settings_导入设置.bat` 并选择 ZIP 即可恢复，也支持拖入 ZIP；导入前自动备份现有设置，失败回滚。新版任务定义保留，新增选项在启动时补默认值。删除旧目录前将 ZIP 另存到外部位置。详见[一键备份与导入说明](docs/hif/使用与构建.md#一键备份与导入设置和个人策略)。

设置 → 版本更新复用 Maa 的「自动检查更新」开关：启动时及每 6 小时检查本 Fork 的稳定 Releases；「自动更新 HIF 完整包」默认关闭，开启后自动下载并等待全部实例任务完成。点击「更新 HIF 完整包」会校验 ZIP 与 SHA-256，等待所有任务和策略编辑窗口结束，显示可取消的 10 秒倒计时，保存并退出前台后自动备份、安装和重启。无限循环任务需自行停止；主动退出时暂不安装，下次启动提示继续。

维护 Fork 可使用 `tools/sync_upstream.py`：先用 `check --ref 官方版本标签` 暂存并检查兼容性，再用 `apply` 同步原版 `agent/`、`assets/` 和上游提交号。HIF 扩展、前台源码及用户配置保持不变；失败保留旧上游，成功留存备份。工具不提交、推送或发布，操作步骤见[维护者同步上游](docs/hif/使用与构建.md#维护者同步上游)。

完整包更新保持原路径，前台、Python、MaaFramework 和脚本一起使用发布包中的版本；个人配置、连接、任务选项、布局、插件和备份保留。准备或保存失败时保留当前版本；安装或启动失败自动回滚，旧文件和配置 ZIP 留存于 `backup/`。中断后可完全退出 Maa，双击 `recover_update_恢复更新.bat` 恢复。首次升级到支持完整包更新的版本须解压新目录并用一键导入迁移配置，之后首次启用自动检查并提示，后续尊重用户设置。本客户端只更新本 Fork，不直接同步原作者脚本或单独更新官方组件，不接入官方 Mirror 酱渠道。

## 本地构建

需要 Windows x64、Git、Python 3.12.9（带 pip）和 .NET SDK 10。构建直接编译仓库 `frontend/` 中的定制源码，按固定版本及 SHA-256 获取 MaaFramework 和 Python，再生成新的运行目录。

```powershell
python tools/hif_build.py build
python tools/hif_verify.py
python tools/hif_build.py package
```

Linux x64 构建和测试方法见[使用与构建说明](docs/hif/使用与构建.md)。既有实验性 Linux 包内置 .NET 与 Python，解压后运行 `./start.sh`；游戏流程仍需自行连接 ADB 设备验证。本次完整包自动安装限定 Windows x64。

源码与公共默认模板存放在 `extensions/hif/`；前台源码存放在 `frontend/`，上游基线固定为 v2.14.0，与 HIF 修改一起提交；按需要单独升级，步骤见[使用与构建说明](docs/hif/使用与构建.md#维护者升级前台)。打包产物在 `dist/`，源码不包含运行时二进制、个人连接配置、其他职业个人策略、日志、截图或备份。

下面保留所基于上游项目的原说明和鸣谢。下文的其他平台、官方发布链接与 Mirror 酱说明属于上游项目；本 HIF 发行版的下载、兼容性和更新方式以上面的说明为准。

---

## 上游原版 README（v1.5.1）

以下内容来自 [SuperWaterGod/MaaGakumasu](https://github.com/SuperWaterGod/MaaGakumasu)，保留原项目介绍与鸣谢；其中官方发布、交流群及 Mirror 酱信息不代表本 HIF Fork 的更新或支持渠道。

<p align="center">
  <img alt="LOGO" src="./logo.png" width="256" height="256" />
</p>

<div align="center">

# MaaGakumasu

基于全新架构的 **学園アイドルマスター(学マス)** 小助手。图像技术 + 模拟控制 + 深度学习，解放双手！  
由 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 强力驱动！

如果 MaaGakumasu 对你有帮助，欢迎在项目右上角点亮 Star 支持。

</div>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blueviolet">
  <img alt="CodeFactor" src="https://www.codefactor.io/repository/github/superwatergod/maagakumasu/badge">
  <img alt="Yolo" src="https://img.shields.io/badge/Yolo-v11-blue">
  <br>
  <img alt="license" src="https://img.shields.io/github/license/SuperWaterGod/MaaGakumasu">
  <img alt="commit" src="https://img.shields.io/github/commit-activity/m/SuperWaterGod/MaaGakumasu">
  <img alt="stars" src="https://img.shields.io/github/stars/SuperWaterGod/MaaGakumasu?style=social">
  <img alt="downloads" src="https://img.shields.io/github/downloads/SuperWaterGod/MaaGakumasu/total?style=social">
  <a href="https://mirrorchyan.com/zh/projects?rid=MaaGakumasu" target="_blank"><img alt="mirrorc" src="https://img.shields.io/badge/Mirror%E9%85%B1-%239af3f6?logo=countingworkspro&logoColor=4f46e5"></a>
</p>

## 功能概览

详细说明请查看 [功能说明](docs/zh_cn/功能说明.md)。当前 README 以 v1.5.1 更新公告与任务配置为准。

| 模块 | 当前支持 |
| --- | --- |
| 启动与日常 | 启动游戏、领取活动费、邮箱礼物、任务奖励、每周免费礼包 |
| 竞赛挑战 | 指定挑战、自动选择分差最小的对手、无编队时自动编队 |
| 社团互动 | 自动请求库存较少的物品，或按配置指定请求 |
| 安排工作 | 领取奖励、自动或指定偶像、自动或指定工作时长 |
| 商店购买 | 扭蛋、金币/AP 兑换、推荐物品与碎片兑换，支持自动免费刷新 |
| 自动培育 | 初 `REGULAR/PRO/MASTER`，NIA `PRO/MASTER`，支持中断继续、失败重试、试镜难度降低、试镜手动接管、老师建议、自动支援卡选择、道具与卡片优先级 |
| 偶像之路 | 自动挑战，支持自动编队、自动战斗与失败重试（暂未适配汉化版，仅支持官服/DMM） |
| 适配能力 | Mirror 酱更新、插件版汉化、DMM 版、支援卡库存识别、繁体 i18n |

### 自动培育

> [!IMPORTANT]
> 自动培育仍处于测试阶段。一次完整培育通常约 `30min`，可能产生大量日志，也可能因弹窗、识别样本不足或游戏 UI 变化而卡住。

当前自动培育支持：

- `初` 难度：`REGULAR`、`PRO`、`MASTER`
- `NIA` 难度：`PRO`、`MASTER`
- 指定偶像卡、自动选择偶像、跳过选择偶像
- 培育次数设置、手动输入次数、无限循环
- 使用体力药、使用道具、自动回忆、关注租借
- 中断后通过“跳过准备阶段”继续培育
- 卡片选择优先级：`建议卡 > 活动卡`、`活动卡 > 建议卡`、`无`
- 考试失败自动重试（初/NIA 均支持）
- 试镜难度降低（NIA）：可选择降低一档或两档
- 跟随老师的建议
- NIA 事件、试镜挑战（自动降档）、指导事件等自动选择逻辑
- 事件保底选择机制：无法匹配优先事件时自动回退到可用选项

### 后续计划

- [ ] 初 `LEGEND` 培育适配
- [ ] 更多语言补充
- [ ] 更多自动培育样本覆盖

## 版本适配

| 环境 | 模拟器 | DMM |
| --- | --- | --- |
| 原版日语 | 已适配，已完全测试 | 已适配，已通过测试 |
| 插件版汉化 | 已适配，已通过测试 | 已适配，未完全测试 |

## 注意事项

> [!NOTE]  
> 项目主要在 Windows 与 MuMu 模拟器 12 上开发和测试。其他系统或模拟器若出现问题，请参考 [提问与反馈指南](docs/zh_cn/提问与反馈指南.md) 并优先保存 `debug\maa.log` 附带截图反馈。

1. 推荐使用 `MuMu 模拟器 12`，模拟器支持情况请查看 [MaaFramework 官方文档](https://maa.plus/docs/zh-cn/manual/device/windows.html)。
2. 推荐分辨率为 `1280x720 (240DPI)`；其他 `16:9` 分辨率和 DMM 端未做完整覆盖测试。
3. 使用 DMM 版时，需要以管理员身份启动程序。
4. 自动培育使用 `YOLOv11` 深度学习模型进行识别，请确保设备支持并启用 GPU 加速。
5. 培育前建议在游戏设置中开启所有跳过对话和弹窗选项，并勾选常见弹窗的“次回以降表示しない”。
6. 本项目仅用于学习交流，请勿用于商业用途。
7. 本项目仅提供自动化脚本，不提供任何游戏资源。

## 使用说明

### Windows

| 架构 | 下载文件 |
| --- | --- |
| 绝大多数 Windows 电脑 | `MaaGakumasu-win-x86_64-vXXX.zip` |
| Windows on ARM | `MaaGakumasu-win-aarch64-vXXX.zip` |

解压后运行 `MaaGakumasu.exe` 即可。压缩包已自带 `Python 3.12.9` 环境，无需额外安装。

首次启动会自动安装相关依赖。如果无法运行，请先安装 [`Visual C++ 可再发行程序包`](https://aka.ms/vs/17/release/vc_redist.x64.exe) 和 [`.NET 桌面运行时 10`](https://dotnet.microsoft.com/zh-cn/download/dotnet/10.0)，然后重启电脑。

Windows 10 或 11 用户也可以使用 `winget` 安装运行库：

```bash
winget install Microsoft.VCRedist.2015+.x64 Microsoft.DotNet.DesktopRuntime.10
```

### macOS

| 架构 | 下载文件 |
| --- | --- |
| Intel 处理器 | `MaaGakumasu-macos-x86_64-vXXX.zip` |
| Apple Silicon | `MaaGakumasu-macos-aarch64-vXXX.zip` |

压缩包已自带 `Python 3.12.9` 环境。解压后，在文件夹内打开终端并执行：

```bash
chmod a+x MFAAvalonia
chmod a+x python/bin/python3
./MFAAvalonia
```

若启动失败并提示缺少运行库，请按提示安装 `.NET` 运行库。若系统提示“Apple 无法检查其是否包含恶意软件”，请在“系统设置”中的“隐私与安全性”里选择“仍要打开”。由于文件较多，可能需要重复确认。

### Linux

Linux 版本提供基础包体，但当前主要测试仍集中在 Windows。遇到问题请携带日志反馈。

## 开发相关

MaaGakumasu 基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 开发，采用 `JSON + 自定义逻辑扩展` 模式。开发前建议先阅读：

- [功能说明](docs/zh_cn/功能说明.md)
- [开发相关](docs/zh_cn/开发相关.md)
- [提问与反馈指南](docs/zh_cn/提问与反馈指南.md)
- [MaaFramework 文档](https://maa.plus/docs/zh-cn/)

### YOLOv11 训练

YOLOv11 主要用于自动培育识别。模型训练参考 [MaaNeuralNetworkCookbook](https://github.com/MaaXYZ/MaaNeuralNetworkCookbook/tree/main/NeuralNetworkDetect)，数据集标注使用 [Roboflow](https://app.roboflow.com/gakumasu)。

| 数据集 | 用途 | 当前样本 |
| --- | --- | --- |
| `cards` | 出牌识别 | 约 902 份，已标注完成，并强化多卡重叠场景 |

> [!NOTE]
> 早期用于上课、冲刺选项识别的 `button` 数据集已废弃；相关按钮识别已改为普通模板匹配，不再使用 YOLOv11。

欢迎参与标注或提供样本，具体说明请查看 [开发相关](docs/zh_cn/开发相关.md)。

## Mirror 酱

自 `2025/11/08` 起，MaaGakumasu 已全面支持 Mirror 酱，在其他地方购买的 CDK 同样可以在此使用。

Mirror 酱是第三方应用分发平台，用于简化开源应用更新。用户付费使用，收益与开发者共享，Mirror 酱本身也是开源项目。

CDK 购买入口：[Mirror 酱官网](https://mirrorchyan.com/zh/projects?rid=MaaGakumasu)

## 免责声明

本软件开源、免费，仅供学习交流使用。若您遇到商家使用本软件进行代练并收费，可能是分发、设备或时间等费用，产生的费用、问题及后果与本软件无关。

**在使用过程中，MaaGakumasu 可能存在任何意想不到的问题，因 MaaGakumasu 自身漏洞、文本理解有歧义、异常操作导致的账号问题等开发组不承担任何责任，请在确保在阅读完用户手册、自行尝试运行效果后谨慎使用！**

## Star History

<a href="https://www.star-history.com/?repos=SuperWaterGod%2FMaaGakumasu&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=SuperWaterGod/MaaGakumasu&type=date&theme=dark&legend=top-left&sealed_token=jsOqvuuCRz8HSTe1rE4WEiB_lh4OkFRfvx0tVecSJiZ0HukPzmuA_rfbaruvDCzvY3Xr0MkWgQ0mrWa8Qk3qXQkr0Iv1jAo5nJzyq5JRSXSBkNYlI5iOzOyJDrIz2XE6n8kMs5EECJErxI-YosO1cGCktJdlL18XRt0gFlpDvXabHzX8SNB0N1cRKBcr" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=SuperWaterGod/MaaGakumasu&type=date&legend=top-left&sealed_token=jsOqvuuCRz8HSTe1rE4WEiB_lh4OkFRfvx0tVecSJiZ0HukPzmuA_rfbaruvDCzvY3Xr0MkWgQ0mrWa8Qk3qXQkr0Iv1jAo5nJzyq5JRSXSBkNYlI5iOzOyJDrIz2XE6n8kMs5EECJErxI-YosO1cGCktJdlL18XRt0gFlpDvXabHzX8SNB0N1cRKBcr" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=SuperWaterGod/MaaGakumasu&type=date&legend=top-left&sealed_token=jsOqvuuCRz8HSTe1rE4WEiB_lh4OkFRfvx0tVecSJiZ0HukPzmuA_rfbaruvDCzvY3Xr0MkWgQ0mrWa8Qk3qXQkr0Iv1jAo5nJzyq5JRSXSBkNYlI5iOzOyJDrIz2XE6n8kMs5EECJErxI-YosO1cGCktJdlL18XRt0gFlpDvXabHzX8SNB0N1cRKBcr" />
 </picture>
</a>

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！
UI 由 [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia) 大力支持！

感谢以下开发者对本项目作出的贡献:

[![Contributors](https://contrib.rocks/image?repo=SuperWaterGod/MaaGakumasu&max=1000)](https://github.com/SuperWaterGod/MaaGakumasu/graphs/contributors)
