# 前台源码

本目录是本 HIF Fork 的唯一前台源码，基于 MFAAvalonia v2.16.4，已包含 HIF 定制。上游来源为 https://github.com/MaaXYZ/MFAAvalonia ，固定提交、版本及基线归档校验值记录在仓库根目录 `hif-release.json`。原许可证保留在 `LICENSE`。

直接修改本目录并与 HIF 扩展一起提交；构建命令见 `../docs/hif/使用与构建.md`。不再手工维护或应用前台补丁，不包含嵌套 Git 仓库或构建产物。构建只下载固定的 MaaFramework 和 Python，前台直接编译这里的源码。

前台上游与 MaaGakumasu 脚本分别维护。需要升级前台时，在临时目录准备旧、新两个上游版本，将两者差异以三方合并方式合入本目录，保留 HIF 定制；审查、编译、验证后再更新 `hif-release.json` 的前台基线记录并单独提交。不要直接覆盖本目录，也不要仅修改提交号。

首次整合（v2.14.0）只移除了上游开发环境诊断文件和 SukiUI 中已有 PackageReference 覆盖的本机程序集引用；依赖版本保持不变。

升级到 v2.16.4 时按上游依赖版本合入，唯一例外是 `Maa.Framework.Runtimes` 保持 5.12.2：构建会用 `hif-release.json` 固定的 MaaFramework 5.14.2 覆盖原生库，上游的 5.14.3 包会额外带入 5.14.2 没有的 `dxcompiler.dll`、`dxil.dll`、`webgpu_dawn_maa.dll`。另外不收录上游新增的 GitHub 工作流（`.github/`、`workflows/`）、根目录 `.gitattributes`（换行规则由仓库根目录统一管理）以及 Android 预编译二进制（`MFAAvalonia.Android/Native/lib/*/libmfabridge.so`、`MFAAvalonia.Android/ThirdParty/Shizuku/shizuku.apk`）；HIF 只构建 Windows/Linux 桌面端，不构建 Android 工程。

HIF 在本目录加了 `Directory.Build.props`：关闭 SDK 的 git 查询（不在程序集版本号后追加提交 SHA，也不写 SourceLink），并用 `PathMap` 把编译器写入的源码路径统一成 `/_/`。这样前台源码不变时，不同提交构建出的程序集逐字节相同，更新只需下载真正改动的文件。Sentry 包仍会把项目绝对路径写进 `MFAAvalonia.dll` 与 `MFAAvalonia.Core.dll`，正式包都在 CI 的同一路径构建，不受影响。升级前台时保留此文件。
