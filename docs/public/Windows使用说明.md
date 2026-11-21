# ScholarFlow Windows 预览版：安装和使用

这是一个可安装的 **Pi Agent 源码预览包**，不是把模型或知识库一并打包的成品。网页是可选客户端，默认只在本机访问。安装不会自动复制你另一台电脑上的论文、笔记、会话或密钥。

## 1. 准备

- 一台 Windows x64 电脑，已安装 Python 3.11 或更新版本、Node.js 22.19 或更新版本，并能联网下载依赖。
- 如果要从上游安装 Nature Skills，还需安装 Git。
- 使用 DeepSeek 等外部模型需要自己的 API 凭据；使用本地 Qwen 则需要事先启动或连接相应服务。本包不会自动启动这些模型。

## 2. 安装

解压 ZIP，在解压后的 `ScholarFlow` 文件夹内打开 PowerShell，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-scholarflow.ps1 -WithWeb -WithKnowledge
```

这会安装 Pi、网页和 LightRAG 的软件依赖，可能需要几分钟。安装程序不会自动修改系统 PATH，也不会清理已有数据；如果提示已有依赖或运行环境，它会停止而不是覆盖。

## 3. 启动网页

```powershell
$sf = Join-Path $env:LOCALAPPDATA 'ScholarFlow\bin\scholarflow.cmd'
& $sf doctor
& $sf web
```

首次启动会建立空的数据目录，不会自动创建项目。网页打开后，可以先创建项目并确认预览，再选择 PDF 与授权阅读页。选择 DeepSeek 时，需要在对话区勾选资料外发同意。

如果端口已被占用，程序会提示冲突，不会强行关闭其他进程。命令行还可用 `& $sf --help` 查看项目、工作区、资料目录和 Nature 命令。

## 4. 配置 DeepSeek

```powershell
& $sf auth set-deepseek
```

程序会在当前终端隐藏输入；不要把密钥发到聊天里。它会把密钥以**明文**保存在当前用户的 ScholarFlow 数据目录中，请保护该目录。也可以只在启动环境设置 `DEEPSEEK_API_KEY`，或使用本机已有的 Pi 凭据。`& $sf auth status` 只显示是否配置，不显示密钥，也不请求模型。

## 5. 安装 Nature Skills

```powershell
& $sf nature install --fetch
# 把上一条输出的 installed 版本填入下面的命令：
& $sf nature activate --version '<installed版本>'
& $sf nature check
```

安装的是固定提交的 23 项 Nature Skill 方法文件和共享资料，Pi 可按任务发现它们；`nature-shared` 是公共资料，不是独立任务。`check` 只检查上游，不自动下载、覆盖或执行新版本。此前版本保留；要切回已安装的旧版本，可用 `nature activate --version '<旧版本>' --replace`，然后启动新的 Pi 会话。

如果 GitHub 下载超时，可先准备包含该固定提交的官方仓库副本，然后用 `& $sf nature install --checkout '<仓库目录>'`；程序仍会验证提交和全部文件，失败不会替换活跃版本。

**安装不等于全部功能已经配好。** 写作、润色、审稿等可参考其方法；文献检索、下载、绘图、PDF/PPT 生成等可能需要单独配置工具、MCP、浏览器或服务。当前 Pi 默认只开放已授权的 ScholarFlow 工具，不会因为加载 Skill 而自动运行上游脚本或开放文件/网络权限。未配置的能力会受限，需要逐项接入和测试。

阅读台生成的笔记先显示为待审阅。可点“编辑提案”修改标题和正文，核对原文后生成新提案；再次点确认才会保存和尝试入库。浏览器会按项目和提案保存未提交的修订草稿。原文未证明的内容不要作为结论保留，AI 仍可能出现理解错误。

## 6. 资料目录与 AI 知识库

`sources add` 可将明确选择的 PDF／Markdown 目录绑定到某个已有项目；加 `--auto` 后，启动 Agent 会刷新该目录的清单。**自动盘点不是自动全文阅读，也不是自动批准入库。** 不要把包含密钥或私密配置的目录加入：敏感内容检测只是启发式检查。

已批准的阅读笔记可以进入项目知识库，但语义检索还需要单独准备兼容的本机 Embedding 服务及审核过的模型。本包不包含该服务或模型，也不会自动启动未知服务；未配置时笔记仍可保存，索引会提示待处理。检查和配置命令：

```powershell
& $sf knowledge embedding-status
& $sf knowledge configure-embedding --help
```

现有知识库或工作区只能通过 `--library-home` 和 `--workspace-home` 明确选择；程序不会替你跨电脑同步或迁移。这个预览包尚未在第二台实机上验收，使用真实资料前应先用少量非敏感样本确认。
