# HIF 新卡更新流程

用户说「更新新卡」即授权完成本项目 HIF 的新卡增量更新；不必让用户先提供卡名、ID 或单独批准每个可逆的本地修改。主动查找最新上游数据、完成配置与资源、运行静态检查，再列出需用户实机验证的项目。只改 HIF；保留用户当前的职业优先级、优先获取卡名单和技能卡定制选择，不自动把所有新卡加入这些个人配置。

## 1. 查增量并固定来源

1. 读取当前目录、`sync_hif_customization_catalog.py` 和 `config/hif_card_icon_sources.json` 中记录的修订。查 `surisuririsu/gakumas-tools` 最新 `packages/gakumas-data/json/skill_cards.json`、`customizations.json`，以及 `surisuririsu/gk-img` 最新卡图；记录本次使用的提交 SHA，勿让运行包依赖浮动的 `master`。
2. 以卡牌 ID 与完整正式名称比较新旧数据，核对是否已存在于其他目录。同名卡只保留一个导入条目；不同目录引用同一张卡的相同 ID 是正常共享，目录内部的重复 ID 或一张图映射多个不同名称须先查清并修正。
3. 区分强化卡、没有强化版的 LR 卡和不能定制的卡。不要把普通未强化版误标为强化版，也不要假设每张新卡都能技能卡定制。

## 2. 更新 HIF 目录与识别

- `config/hif_target_card_catalog.json`：加入可供优先获取卡导入的强化卡，以及无强化版的 LR 卡；保留稀有度、计划、分类和来源字段。
- `config/hif_priority_card_catalog.json` 与 `config/hif_priority_card_images.json`：加入同名键、可区分的 OCR 名称与准确卡牌 ID。没有实测模板时留空模板路径，并检查 OCR 前缀冲突；需要手牌识别模板时放在 `resource/base/image/cards/`。展示图不能代替游戏识别模板。
- `config/hif_customization_catalog.json`：仅收录有 `availableCustomizations` 的强化卡。更新 `sync_hif_customization_catalog.py` 的固定上游修订并重生成，保持定制项目的上游 ID 与顺序。当前通用 HIF 菜单只验证过 2～3 项；若新卡项目数或布局不同，先标出并补充界面证据，不能直接套用现有坐标。
- 只更新可导入目录与识别资料；`config/cards_priority.json` 的 `priority_profiles`、`recognition_profiles`、`preferred_acquisition_profiles`、`swap_out_priority_profiles`、`use_condition_profiles` 和 `config/hif_custom_card_selection.json` 中的用户选择按原样保留。优先换出卡面板复用优先获取卡及优先级目录（含「眠気」），新卡会自动成为候选，不自动加入职业名单。新增卡由用户在原生面板导入、设置并保存。

### 导入列表排序

前台会在筛选后按卡牌字段重新排序，目录 JSON 的原始顺序不会决定导入列表位置。更新新卡时必须核对 `rarity`、`type`、`sourceType`、`plan` 和 `id`，不能用空值或未知值让新卡落到末尾：

1. 优先级卡和优先获取卡：T 卡先于其他卡；其余依次按稀有度 `N → R → SR → SSR → LR` 分组。上游 LR 的字段值为 `L`，前台也兼容 `LR`。T 卡属于分类 `trouble`，如只存在于优先级目录的负面卡「眠気」，在优先级目录中标明 `type: trouble`。
2. 同一稀有度内，依次按来源 `无(default/produce) → 偶像卡(pIdol) → 支援卡(support)`、分类 `A(active) → M(mental)`、计划 `不限(free) → 感性(sense) → 理性(logic) → 非凡(anomaly)` 分组，最后按数字 ID 升序。
3. 技能卡定制导入只含可定制强化卡，按 `R → SR → SSR`，再按上述来源、分类、计划、ID 排序；不向该目录加入 T 卡或无强化版 LR 卡。
4. 更新后打开相应导入窗口，确认新卡进入正确分组；筛选条件只缩小结果，不改变组内顺序。若上游出现新稀有度或分类，先更新前台排序映射与校验，再交付目录。

## 3. 选择共享缩略图

每张卡使用准确的同 ID 图源。优先 `gk-img/docs/skill_cards/icons/<id>.webp`；只有同 ID 无后缀图不存在、该 ID 仅有 `_1`～`_13` 等人物变体时，才选 `<id>_8.webp`。不要任取其他人物变体，也不要把 `details/<id>.webp` 当作缩略图。两种文件同时存在时必须用无后缀图，它们的角标数值和画面可能不同。若两者均无合适图，查用户指定的 game8 等补充来源并核对卡名、强化状态和画面；仍找不到时报告缺图，不伪造映射。

把源文件名、固定 `gk-img` 修订及 SHA-256 写入 `config/hif_card_icon_sources.json`，本地统一保存为 `resource/base/image/hif_card_icons/<id>.webp`。保持 `config/hif_priority_card_images.json` 与图源清单 ID 完全一致，两个同步脚本按同一清单工作；将缺少无后缀图的卡更新到 `docs/hif_icons_without_plain_source.md`。增量操作前后核对重复 ID、WebP 内容及哈希。

## 4. 验证和交付

运行 `python sync_hif_customization_catalog.py --check`、`./sync_hif_priority_images.ps1 -Check`、`./sync_custom_card_images.ps1 -Check`、`python -m unittest discover -s tests`、JSON/AST 检查及 `git diff --check`；按新增卡调整有意义的目录测试和说明。需要前台源码改变时，更新 `patches/mfaavalonia-v2.14.0-hif-priority.patch`，编译后在 Maa 退出时安装 DLL。

最后列出新增卡、图源与修订、未解决的缺图或 OCR 冲突、静态检查结果，并明确哪些识别或界面行为尚待用户 MuMu 实机验证。用户只说「更新新卡」时，依本流程自行完成能做的工作；只有缺少必要实机证据或上游身份确实无法判断时才请用户补充截图。
