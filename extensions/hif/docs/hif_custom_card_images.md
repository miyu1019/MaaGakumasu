# HIF 技能卡定制图维护

HIF 技能卡定制面板直接读取共享卡图；旧的逐卡任务选项已从主界面隐藏。这里保留原 14 张卡的说明图映射和识别模板维护方式，供旧资源校验使用。

卡面导入和定制项目勾选见 [HIF 技能卡定制面板](hif_custom_card_panel.md)。

## 目录约定

- 识别模板固定在 `resource/base/image/cards/`，不得为说明图新增或覆盖文件。
- 技能卡定制面板与目标卡、优先级面板共用 `resource/base/image/hif_card_icons/`。
- 说明图文件名使用 `<gk_img_id>.webp`；识别模板仍在独立的 `cards/` 目录。

## 维护旧说明图

1. 在 `config/custom_card_images.json` 修改已有映射，令 `file` 为 `<gk_img_id>.webp`。图源优先使用同 ID 的 `<id>.webp`，不存在时才使用 `<id>_8.webp`；已有 `gk_source_file` 字段仅保留旧清单兼容，不能指定 `_8`。新面板卡牌无需在此逐张登记。
2. 旧任务定义仍留在 `tasks/produce.json` 和 `tasks/produce_cn.json` 供校验，但不列入「开始培育」主界面。其 `description` 使用：

   ```json
   "description": "![](resource/base/image/hif_card_icons/<gk_img_id>.webp)"
   ```

3. 运行以下命令下载或更新图片：

   ```powershell
   .\sync_custom_card_images.ps1 -Force
   ```

4. 在提交前验证配置与图片：

   ```powershell
   .\sync_custom_card_images.ps1 -Check
   ```

脚本读取 `config/hif_card_icon_sources.json` 中的固定 `gk-img` 提交与逐 ID 来源；优先取 `icons/<id>.webp`，只有同 ID 无后缀图缺失、仅有人物变体时才取 `icons/<id>_8.webp`。同步同一 ID 会更新三处共用的卡图；不要复制或重命名识别模板。
