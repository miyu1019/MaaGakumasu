# 前台源码

本目录是本 HIF Fork 的唯一前台源码，基于 MFAAvalonia v2.14.0，已包含 HIF 定制。上游来源为 https://github.com/MaaXYZ/MFAAvalonia ，固定提交、版本及基线归档校验值记录在仓库根目录 `hif-release.json`。原许可证保留在 `LICENSE`。

直接修改本目录并与 HIF 扩展一起提交；构建命令见 `../docs/hif/使用与构建.md`。不再手工维护或应用前台补丁，不包含嵌套 Git 仓库或构建产物。构建只下载固定的 MaaFramework 和 Python，前台直接编译这里的源码。

前台上游与 MaaGakumasu 脚本分别维护。需要升级前台时，在临时目录准备旧、新两个上游版本，将两者差异以三方合并方式合入本目录，保留 HIF 定制；审查、编译、验证后再更新 `hif-release.json` 的前台基线记录并单独提交。不要直接覆盖本目录，也不要仅修改提交号。

本次整合只移除了上游开发环境诊断文件和 SukiUI 中已有 PackageReference 覆盖的本机程序集引用；依赖版本保持不变。
