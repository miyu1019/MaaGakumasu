# HIF 技能卡定制图维护

技能卡定制面板与执行端都按卡牌 ID 读取共享卡图，新卡不需要在这里登记；卡面导入和定制项目设置见 [HIF 技能卡定制面板](hif_custom_card_panel.md)。本文说明卡图目录约定。

## 目录约定

- 共享卡图：`extensions/hif/resource/base/image/hif/hif_card_icons/<id>.webp`，技能卡定制、卡库、出牌优先级面板共用，来源与 SHA-256 记录在 `extensions/hif/catalog/hif_card_icon_sources.json`，由 `tests/test_hif_card_catalogs.py` 校验。更新卡图按 [新卡更新流程](hif_new_card_update.md) 的「选择共享缩略图」进行。

最早 14 张卡留下的旧卡面模板目录（`cards_customize.json`、`custom_card_images.json`）已移除；手牌出牌与技能卡定制均已改用卡名 OCR 与共享缩略图识别，不再新增或使用 `extensions/hif/resource/base/image/hif/cards/` 下的卡牌识别模板。
