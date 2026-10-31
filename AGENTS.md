# AGENTS.md

本文件为在本仓库工作的 AI/自动化代理提供 HIF 路线的项目上下文与协作规则。用户使用与构建细节见 `docs/hif/使用与构建.md`，各面板与功能说明见 `extensions/hif/docs/`。

## 项目定位

本仓库是 MaaGakumasu（《学園アイドルマスター》MaaFramework 自动化助手）的 Fork，只维护 HIF 培育路线：「HIF培育」任务、五个策略面板（出牌优先级、优先获取卡、授业优先换出卡、饮料、技能卡定制）、定制前台和独立的 HIF 发行包与更新。原版日常、商店、初/NIA 培育等功能来自上游，不在本 Fork 中开发。

目标环境为 Windows x64、MuMu 模拟器、`1280x720 (240DPI)` 基准；Linux x64 包为实验性。自动培育属测试性质，不要描述为稳定功能。

## 分支与发布

- `hif-dev`：开发分支，日常提交推送到这里。每日 AutoBuild（`.github/workflows/hif-nightly.yml`，北京时间 04:00）构建该分支并更新 `nightly` 预发布。
- `hif-independent`：默认分支与稳定发行分支。GitHub 定时任务只从默认分支触发，因此工作流文件的改动须进入 `hif-independent`；稳定内容验证后再从 `hif-dev` 合入。
- 正式发布用 `vYYMMDD.序号` 标签（与 `hif-release.json` 的 `version` 一致，同日递增序号），由 `hif-release.yml` 构建 Windows 包；`hif-linux-release.yml` 向已有 Release 追加 Linux 包，在 `hif-independent` 推送该工作流文件本身也会触发发布。不要移动已发布的标签。
- 客户端只从 GitHub 更新本 Fork 的 HIF Releases（自动检查与完整包更新）：更新渠道「正式版」跟随稳定 Release，「测试版」跟随 `nightly` 预发布（按源码提交判断新旧，仅 Windows x64）；不提供 Mirror 酱下载源，不跟踪官方前台或 MaaFramework。
- 本地实施不等于上传授权：推送、触发远程工作流和发布须由用户明确确认。

## 冻结的上游部分

- 原版 `agent/`、`assets/` 与 `hif-release.json` 的 `upstream_commit`（v1.5.1）一致，不直接修改；构建检测到改动会拒绝打包。上游同步由维护者用 `tools/sync_upstream.py check/apply` 手动完成。
- `frontend/` 是已定制的 MFAAvalonia v2.14.0 源码，与 HIF 一起提交、直接编译；基线提交与归档校验记录在 `hif-release.json`。前台升级须单独评估、三方合并并单独提交，不随脚本上游同步。
- 固定输入：MaaFramework / maafw 5.14.2、Python 3.12.9（`requirements-hif.txt`）、.NET SDK 10。

## 目录职责

- `extensions/hif/agent/hif/action/`：HIF 自定义动作，按职责分模块。`produce.py` 只负责导入各模块完成注册并保留原有导出名（导入顺序不影响结果）。`common.py` 放路径常量与共用工具；`battle.py` 的 `ProduceCardsAuto` 由 `battle_recognition.py`（识别与操作）、`battle_drink.py`（本战用饮）、`battle_decision.py`（出牌决策）三个 mixin 组成；`drinks.py`、`deck.py`、`shop.py`、`schedule.py`、`entry_recovery.py` 分别对应饮料页、换卡删卡、相谈与定制、日程行动、日期与入口恢复。`common.py` 与 `battle_decision.py` 不得导入 maa（由 `tests/test_hif_decision_standalone.py` 守护）；跨模块调用共用函数写作 `common.X` / `drinks.X`，测试用 `hif_patch` 打补丁。
- `extensions/hif/agent/hif/reco/`：HIF 自定义识别。`extensions/hif/` 根下的 `followups.py`（后续卡规则与本战状态，不依赖识别/界面）、`drink_keep.py`（满栏饮料勾选页的视觉与选择规则）、`recovery.py`（任务内恢复预算）供动作模块调用。
- `extensions/hif/resource/`：HIF 流水线（`base/pipeline/HIF.json`、`HIFDateLogin.json`、`HIFDynamicRecognition.json`）、`zh_CN` 叠加层和图片模板。`namespace.json` 把节点映射为 `ProduceHIF__` 前缀；`resource-variants.json` 规定官服、DMM 只加载 `base`，插件版汉化另叠加 `zh_CN`。
- `extensions/hif/tasks/produce_hif.json`、`extensions/hif/lang/`：「HIF培育」任务定义及 `zh-CN`、`zh-Hant` 文案，新增选项时两种语言同步。
- `extensions/hif/catalog/`：卡牌、定制、饮料目录与卡图来源清单（`hif_card_icon_sources.json` 固定 gk-img 修订与 SHA-256）。新卡更新见下文「上游卡牌更新」。
- `extensions/hif/defaults/`：公开默认策略，仅含集中职业，其他职业保持空白模板；首次运行复制到 `config/hif/`，已存在不覆盖。
- `extensions/hif/first-run/`：新运行包的首次启动模板（界面设置、不含设备信息的 HIF 实例、布局）。
- 运行时策略文件为 `config/hif/cards_priority.json` 等，面板与 Agent 共用；结构变化须保持旧配置可读，必要时提供迁移。
- `frontend/MFAAvalonia/Views/Windows/Hif*.cs`：HIF 面板窗口（出牌优先级、后续卡、饮料、技能卡定制等）；`frontend/MFAAvalonia/Helper/Hif*.cs`：面板说明、布局、后续卡配置、职业配置导入导出校验（`HifPriorityShare.cs`）与 HIF 更新服务。面板新增字段时同步 Agent 端校验和导入校验。
- `tools/`：`hif_build.py`（build/package）、`hif_verify.py`（完整验证）、`hif_audit.py`（发行包审计）、`sync_runtime.ps1`（本地同步运行包）、`hif_update.py` 与更新器脚本、`sync_upstream.py`、设置导出/导入与清理脚本；`tools/PanelQa/` 为前台原生面板验证。
- `docs/hif/`：使用与构建、发布说明（`发布说明.md` 为当前版本说明）、Linux 发布说明。

## 构建、验证与同步

```powershell
python -m unittest discover -s tests          # Python 回归（本机 Python 无 pytest/ruff，使用 unittest）
dotnet build frontend/MFAAvalonia/MFAAvalonia.csproj -c Release
python tools/hif_build.py build               # 新运行目录 build/MaaGakumasu-HIF，不覆盖已有目录
python tools/hif_verify.py                    # 完整验证（Python 回归、原生面板、更新回滚、上游一致性等）
python tools/hif_build.py package             # dist/MaaGakumasu-HIF-win-x64-<version>.zip 与 .sha256
```

- 源码根目录直接运行 `test_isolation` 时，`test_actual_graph_and_upstream_byte_identity` 因缺少 `upstream/` 报错属预期，该项由 `hif_verify.py` 在构建运行目录中覆盖。
- 本地运行包位于相邻目录 `..\MaaGakumasu-HIF`。用户要求「同步运行包」时运行 `tools/sync_runtime.ps1`（或根目录 `sync_runtime_同步运行包.bat`）：构建 → 验证 → 打包 → 安装，保持版本号，安装前备份配置并导回设置，完成后自动启动新版。运行包有进程在运行时脚本会拒绝安装。
- 改动 Python 决策逻辑时补充或更新 `tests/test_hif_*.py`；改动面板时确认前台编译通过，必要时运行 PanelQa。
- 游戏内效果需要 MuMu 实机长流程验证；未做实机验证时须在交付说明中明确。

## 上游卡牌更新

完整流程与历次增量记录见 `extensions/hif/docs/hif_new_card_update.md`（文中 `config/`、`resource/` 为运行包路径，对应仓库的 `extensions/hif/catalog/`、`extensions/hif/resource/`）。用户说「更新新卡」即授权完成本地增量更新，无需先提供卡名或 ID；推送与发布仍需另行确认。

1. **查来源并固定修订**：卡牌与定制数据取 `gakumas-tools/gakumas-tools` 的 `packages/gakumas-data/csv/skill_cards.csv`、`customizations.csv`（2026-10-21 起上游不再提交 `json/`）；缩略图取 `gakumas-tools/gk-img` 的 `docs/skill_cards/icons/`。当前固定修订记录在 `tools/sync_hif_customization_catalog.py` 的 `REVISION` 与 `extensions/hif/catalog/hif_card_icon_sources.json` 的 `revision`。用上游最新提交的完整 SHA 取数据，不依赖浮动的 `master`。
2. **比对增量**：按卡牌 ID 与完整正式名称比较新旧数据；区分强化卡、无强化版 LR 卡和不可定制卡。上游仍为预览数据或缺同 ID 图的卡暂不加入，在文档中记录待补。
3. **更新目录**：
   - `hif_target_card_catalog.json`：可导入的强化卡与无强化版 LR 卡，核对 `rarity`、`type`、`sourceType`、`plan`、`id`（前台按这些字段排序，不能留空）。
   - `hif_priority_card_catalog.json` 与 `hif_priority_card_images.json`：同名键、去掉末尾 `+` 的完整卡名 OCR 词、准确卡牌 ID；无实测模板时模板留空，并检查与现有卡名的 OCR 前缀冲突。
   - `hif_customization_catalog.json`：只收录有定制项目的强化卡；更新 `REVISION` 后运行 `python tools/sync_hif_customization_catalog.py`（`--check` 仅核对），保持上游项目 ID 与菜单顺序。
   - 饮料关联入口只接受 A 卡。
4. **卡图**：优先同 ID 无后缀 `icons/<id>.webp`，不存在时才用 `<id>_8.webp`，不用其他人物变体或 `details/` 图；本地存为 `extensions/hif/resource/base/image/hif/hif_card_icons/<id>.webp`，在 `hif_card_icon_sources.json` 记录源文件名与 SHA-256。提升图源修订前核对原有本地图与新修订逐字节一致；使用 `_8` 的卡更新到 `extensions/hif/docs/hif_icons_without_plain_source.md`。
5. **不动个人配置**：`defaults/` 与用户 `config/hif/` 中的职业优先级、识别名单、优先获取/换出名单、使用限制和定制选择保持原样，新卡由用户在面板中自行导入。
6. **验证与记录**：运行 `tests/test_hif_card_catalogs.py`、`tests/test_hif_customization_catalog.py`、`git diff --check` 及完整验证；在 `hif_new_card_update.md` 追加本次增量（修订、新卡表、目录数量、OCR 冲突、待补卡），同步更新 `hif_native_priority_panel.md`、`hif_custom_card_panel.md` 中的目录数量与修订。交付时说明新卡识别与定制菜单尚待 MuMu 实机验证。

## 协作规则

- 不得提交个人实例、连接信息、密钥、日志、截图、备份或非集中职业的个人策略；不提交嵌套 Git 仓库、构建产物（`build/`、`dist/`）或二进制（卡图 webp 等资源除外）。
- 不要回退用户已有修改；工作区有不相关改动时保持原样，提交时按主题拆分。
- 不要在未确认时调整发布、安装、更新器、依赖固定版本或许可证/免责声明。
- 修改 JSON 与流水线时保持原有排序、缩进和注释风格，不做无关格式化。Python 遵循 `pyproject.toml` 的 Ruff 配置（`py312`，行宽 144）。
- 用户可见文案用中文，游戏内名称保留日文原名；HIF 战斗职业为 好调、集中、好印象、元気、全力、強気，全力与強気有当前指针状态（全力/強気/温存）。
- 新增识别模板或卡图须说明来源与分辨率；卡图来源固定到具体上游提交，不依赖浮动分支。
- 排查运行问题时优先查看运行包的 `debug/` 日志、模拟器类型、分辨率、游戏版本与 HIF 版本（`hif-build-info.json`）。
- 需要联网查询 MaaFramework、MFAAvalonia、Gakumas Tools 等外部信息时优先官方来源，并在回复中注明。
