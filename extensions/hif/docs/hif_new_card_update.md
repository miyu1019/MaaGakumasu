# HIF 新卡更新流程

用户说「更新新卡」即授权完成本项目 HIF 的新卡增量更新；不必让用户先提供卡名、ID 或单独批准每个可逆的本地修改。主动查找最新上游数据、完成配置与资源、运行静态检查，再列出需用户实机验证的项目。只改 HIF；保留用户当前的职业优先级、优先获取卡名单和技能卡定制选择，不自动把所有新卡加入这些个人配置。

## 1. 查增量并固定来源

1. 读取当前目录、`sync_hif_customization_catalog.py` 和 `extensions/hif/catalog/hif_card_icon_sources.json` 中记录的修订。查 `gakumas-tools/gakumas-tools` 最新 `packages/gakumas-data/csv/skill_cards.csv`、`customizations.csv`（2026-10-21 起上游不再提交 `json/` 生成文件），以及 `gakumas-tools/gk-img` 最新卡图；记录本次使用的提交 SHA，勿让运行包依赖浮动的 `master`。
2. 以卡牌 ID 与完整正式名称比较新旧数据，核对是否已存在于其他目录。同名卡只保留一个目录条目；不同目录引用同一张卡的相同 ID 是正常共享，目录内部的重复 ID 或一张图映射多个不同名称须先查清并修正。
3. 区分强化卡、没有强化版的 LR 卡和不能定制的卡。不要把普通未强化版误标为强化版，也不要假设每张新卡都能技能卡定制。

## 2. 更新 HIF 目录与识别

- `extensions/hif/catalog/hif_target_card_catalog.json`：卡库可加入的强化卡，以及无强化版的 LR 卡；保留稀有度、计划、分类和来源字段。
- `extensions/hif/catalog/hif_priority_card_catalog.json` 与 `extensions/hif/catalog/hif_priority_card_images.json`：加入同名键、去掉末尾 `+` 的完整卡名 OCR 名称与准确卡牌 ID；战斗手牌识别已改用卡名 OCR 与详情补认，不再制作 `extensions/hif/resource/base/image/hif/cards/` 手牌识别模板，`template` 字段留空以保持配置格式兼容，并检查 OCR 前缀冲突。展示图仅用于前台面板与定制缩略图，不作为游戏识别模板。
- `extensions/hif/catalog/hif_customization_catalog.json`：仅收录在上游 `customizations.csv` 中有定制项目的强化卡。同步脚本不会增删卡牌：新卡条目（卡牌 ID、名称、筛选字段、按游戏菜单顺序的项目 ID 与名称）需手工加入，再更新 `sync_hif_customization_catalog.py` 的固定修订并运行脚本写入逐项 `max`，保持定制项目的上游 ID 与顺序。当前通用 HIF 菜单只验证过 2～3 项；若新卡项目数或布局不同，先标出并补充界面证据，不能直接套用现有坐标。
- `extensions/hif/catalog/hif_card_renames.json`：上游修正已收录卡的名称时，在此登记「旧名 → 正式名」（目录键名，含末尾 `+`）。Agent 读取 `cards_priority.json`、`hif_drink_profiles.json` 时，以及优先级面板、饮料面板、职业配置导入时按此表把旧名换成正式名，面板保存时写回新名；新旧名并存时保留已有的正式名条目。不登记时，引用旧名的个人配置会失去优先级与使用限制，出牌编排中的旧名会使该职业的编排被忽略。正式名不能再出现在表的旧名一侧，OCR 词也不能与旧名相同。
- 只更新卡库目录与识别资料；`config/hif/cards_priority.json` 的 `priority_profiles`、`recognition_profiles`、`preferred_acquisition_profiles`、`swap_out_priority_profiles`、`use_condition_profiles` 和 `config/hif/hif_custom_card_selection.json` 中的用户选择按原样保留。卡库复用优先获取卡及优先级目录（含「眠気」），新卡会自动成为候选，不自动加入职业名单。新增卡由用户在「卡库」加入、设置并保存。

### 卡库排序与筛选

前台卡库（`HifCardPicker`）按卡牌字段筛选和排序，目录 JSON 的原始顺序不决定显示位置。更新新卡时必须核对 `rarity`、`type`、`sourceType`、`plan` 和 `id`，不能留空或用未知值，否则新卡会落在筛选结果之外或排到末尾：

1. 排序：卡库中同计划另一职业已在用的卡排在最前（出牌编排的选择区则是出牌卡表中的卡），其次按稀有度 `L(LR) → SSR → SR → R → N`、分类 `A(active) → M(mental) → T(trouble)`，最后按数字 ID 升序。上游 LR 的字段值为 `L`，前台也兼容 `LR`。只存在于优先级目录的负面卡「眠気」在优先级目录中标明 `type: trouble`。
2. 筛选：稀有度、类型、来源（偶像卡 `pIdol`、支援卡 `support`、其他为 `default/produce`）和计划；默认只看当前职业计划（`sense`/`logic`/`anomaly`）与不限 `free`。
3. 卡库选「获取与定制」时列出可获取的全部卡；只有定制目录中的可定制强化卡带定制项目，不向定制目录加入 T 卡或无强化版 LR 卡。
4. 更新后在「卡库」核对新卡出现在正确的筛选结果中；筛选条件只缩小结果，不改变排序。若上游出现新稀有度或分类，先更新前台排序映射与校验，再交付目录。

## 3. 选择共享缩略图

每张卡使用准确的同 ID 图源。优先 `gk-img/docs/skill_cards/icons/<id>.webp`；只有同 ID 无后缀图不存在、该 ID 仅有 `_1`～`_13` 等人物变体时，才选 `<id>_8.webp`。不要任取其他人物变体，也不要把 `details/<id>.webp` 当作缩略图。两种文件同时存在时必须用无后缀图，它们的角标数值和画面可能不同。若两者均无合适图，查用户指定的 game8 等补充来源并核对卡名、强化状态和画面；仍找不到时报告缺图，不伪造映射。

把源文件名、固定 `gk-img` 修订及 SHA-256 写入 `extensions/hif/catalog/hif_card_icon_sources.json`，本地统一保存为 `extensions/hif/resource/base/image/hif/hif_card_icons/<id>.webp`。保持 `extensions/hif/catalog/hif_priority_card_images.json` 与图源清单 ID 完全一致（由 `tests/test_hif_card_catalogs.py` 检查）；将缺少无后缀图的卡更新到 `extensions/hif/docs/hif_icons_without_plain_source.md`。增量操作前后核对重复 ID、WebP 内容及哈希。

## 4. 验证和交付

按新增卡调整有意义的目录测试和说明，运行 JSON/AST、目录同步检查及 `git diff --check`，构建后执行 `python tools/hif_verify.py`。需要修改前台时，直接编辑开发仓库的 `frontend/`，再用 `python tools/hif_build.py build` 生成新运行包；构建及前台升级流程见仓库根目录的 `docs/hif/使用与构建.md`。

最后列出新增卡、图源与修订、未解决的缺图或 OCR 冲突、静态检查结果，并明确哪些识别或界面行为尚待用户 MuMu 实机验证。用户只说「更新新卡」时，依本流程自行完成能做的工作；只有缺少必要实机证据或上游身份确实无法判断时才请用户补充截图。

## 2026-11-11 加入ガラクタロード 后四张强化卡

数据固定为 Gakumas Tools `f4149e63c96a6276e60bf82e962520ad835630cb`，图源固定为 gk-img `cac263162450fb2a265df57d757db6703843ae2e`。与前次数据修订相比，`skill_cards.csv` 无新增、删除或改名；上游 `utils/hidden.js` 清空了 ID 915、917、919、921 的预览隐藏，并修正这 4 张未强化卡的消耗与效果，对应强化卡转为正式数据。`customizations.csv` 逐字节不变，248 张定制卡的项目与 `max` 不变，只更新修订记录。

| ID | 卡名 | 偶像卡 | 计划 / 分类 | 定制 |
| ---: | --- | --- | --- | --- |
| 916 | なにかを続ける尊さ+ | ガラクタロード（pIdol 162） | 非凡 / M | 上游暂无项目 |
| 918 | わずかずつの歩み+ | ガラクタロード（pIdol 162） | 非凡 / M | 上游暂无项目 |
| 920 | ありのままのわたし+ | ガラクタロード（pIdol 163） | 理性 / M | 上游暂无项目 |
| 922 | 認めてくれるひと+ | ガラクタロード（pIdol 163） | 理性 / A | 上游暂无项目 |

目标卡目录增至 474 张，优先级及共享图源目录增至 475 张（377 张无后缀图、98 张 `_8`）；定制目录保持 248 张。4 张新图均为同 ID 无后缀 `icons/<id>.webp`（96×96 WebP）。更新前按 Git blob 哈希核对原有 471 张本地图与新 gk-img 修订中的同名文件逐字节一致，原 98 张 `_8` 卡在新修订中仍无无后缀图，因此整体提升图源修订。个人配置与职业名单不变。

OCR 词为去掉末尾 `+` 的完整卡名，无实测手牌模板。「なにかを続ける尊さ」以「なに聴いてるの？+」的旧短词 `なに` 开头，「ありのままのわたし」以「ありがとうの言葉+」的旧短词 `あり` 开头；HIF 完整卡名索引按全名或前缀匹配，可区分两组卡，旧短词兼容路径仍需实机确认。ガラクタロード 的强化卡至此全部收录，暂无待补卡。

## 2026-11-16 先加入氷渡香名江三张强化卡的基本数据

按用户确认，本批先补基本数据，卡图待上游补齐后再补。数据固定为 Gakumas Tools `86acfe50c11f17b67668be8eab749bbf7c1c8d95`，`skill_cards.csv` 仅新增 ID 923～928 三组 氷渡 香名江（`idolId=15`）的 `pIdol` 专属卡，既有卡名、效果及 `customizations.csv` 均不变（248 张定制卡只更新修订记录）。

| 普通 / 强化 ID | 强化卡名 | 稀有度 | 偶像卡 | 计划 / 分类 | 定制 |
| --- | --- | --- | --- | --- | --- |
| 923 / 924 | 柔らかな光の中で+ | SSR | メタモルフォーゼ（pIdol 164） | 感性 / A | 上游暂无项目 |
| 925 / 926 | 忠義を胸に+ | SR | 憧れを抱いて一歩ずつ（pIdol 165） | 感性 / M | 上游暂无项目 |
| 927 / 928 | 一途な従者+ | R | 学園生活（pIdol 166） | 感性 / A | 上游暂无项目 |

目标卡目录增至 477 张（450 张强化卡、27 张无强化版 LR 卡），优先级目录增至 478 张；定制目录保持 248 张。`gk-img` 修订 `e5878e7a64dfadbf0a495aa465e3efdd45f7c00d` 及分支尚无这 3 张卡的缩略图，本次不添加图源映射或占位图片，共享图源保持 475 张（377 张无后缀图、98 张 `_8` 图，仍固定于 `cac263162450fb2a265df57d757db6703843ae2e`），面板中将显示「无图」。个人配置与职业名单不变。

OCR 词为去掉末尾 `+` 的完整卡名，无实测手牌模板，与现有卡名及旧短词均无前缀冲突。手牌识别及定制项目待图源补齐与 MuMu 实机验证。

## 2026-11-19 补齐氷渡香名江三张强化卡缩略图

`gk-img` 图源修订提升为 `82eb3794cfdad82d95a226d669a097ba3371f21f`，补齐氷渡 香名江 3 张强化卡的同 ID 无后缀缩略图 `icons/<id>.webp`（96×96 VP8 WebP）：

| ID | 卡名 | 来源文件 | SHA-256 |
| ---: | --- | --- | --- |
| 924 | 柔らかな光の中で+ | `icons/924.webp` | `3f194e7958ab57835bdc51af7256782f59a7a00710fffbd83c1447b2fdb7566c` |
| 926 | 忠義を胸に+ | `icons/926.webp` | `18674f0b761d4371e573093b3d13b3a5299c905db754d77fd0b71fa15d8273f7` |
| 928 | 一途な従者+ | `icons/928.webp` | `808559fa845ec75b290eed034ab84804dd59f57ff0cd9a7c66ded14db5782442` |

经核对，Gakumas Tools 最新 `master` `d51e4d3fb4220d2132ac7cc1fef59cfe762c156b` 中的 `skill_cards.csv`、`customizations.csv` 与 `utils/hidden.js` 和当前固定修订 `86acfe50c11f17b67668be8eab749bbf7c1c8d95` 逐字节一致，无新卡、改名、预览转正式或定制变更，数据与定制目录保持 `86acfe50c11f17b67668be8eab749bbf7c1c8d95` 不变。更新前按 Git blob 哈希核对原有 475 张本地缩略图与新 `gk-img` 修订同名文件逐字节一致，原 98 张 `_8` 卡在新修订中仍无无后缀图，因此整体提升图源修订。

目标卡目录保持 477 张（450 张强化卡、27 张无强化版 LR 卡），优先级目录与共享图源目录均为 478 张（380 张无后缀图、98 张 `_8` 图），定制目录保持 248 张；`tests/test_hif_card_catalogs.py` 清空 `PENDING_ICON_IDS`，个人配置、职业名单及 `defaults/` 保持原样。

ID 924、926、928 的卡名 OCR 识别、面板卡图展示及游戏内是否实际存在定制项目（上游暂无）尚待 MuMu 实机验证（出牌已改用卡名 OCR 与详情补认，无需手牌识别模板）。
