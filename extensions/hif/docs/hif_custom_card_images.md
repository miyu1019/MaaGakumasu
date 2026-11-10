# HIF 技能卡定制图维护

技能卡定制面板与执行端都按卡牌 ID 读取共享卡图，新卡不需要在这里登记；卡面导入和定制项目设置见 [HIF 技能卡定制面板](hif_custom_card_panel.md)。本文只说明最早 14 张卡留下的旧识别资料。

## 目录约定

- 共享卡图：`extensions/hif/resource/base/image/hif/hif_card_icons/<id>.webp`，技能卡定制、卡库、出牌优先级面板共用，来源与 SHA-256 记录在 `extensions/hif/catalog/hif_card_icon_sources.json`，由 `tests/test_hif_card_catalogs.py` 校验。更新卡图按 [新卡更新流程](hif_new_card_update.md) 的「选择共享缩略图」进行。
- 识别模板：`extensions/hif/resource/base/image/hif/cards/`，与展示图分开，不得用展示图新增或覆盖模板。

## 旧识别资料

`extensions/hif/catalog/cards_customize.json`（旧卡面模板）与 `extensions/hif/catalog/custom_card_images.json`（`gk_img_id` → 卡名，`file` 为 `<gk_img_id>.webp`）只覆盖原 14 张卡。`ProduceHIFCardCustomAuto` 读取它们，在卡名 OCR 读不完整时作为补充比对；两个文件缺失或损坏时只记录警告，继续使用卡名 OCR 与共享缩略图。

这两个文件不提供菜单坐标或目标次数，也不再随新卡扩充。需要修改已有条目时直接编辑 JSON，`file` 必须指向已在图源清单中的同 ID 卡图；旧逐卡任务选项已移除，不再维护对应的任务说明图。
