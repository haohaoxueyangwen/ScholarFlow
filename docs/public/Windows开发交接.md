# Windows 跨电脑开发交接

本仓库提供当前 ScholarFlow 的 Pi harness、领域服务、新版阅读台、可移植入口、Node 锁文件、Python 主要依赖清单和筛选后的开发测试。它不是原电脑整个系统或用户目录的镜像：Python/Node 软件、虚拟环境、模型/Embedding 服务、API 密钥、论文、笔记、项目事实、会话和知识库均不随 Git 迁移。不要把这些文件提交到公开仓库。

## 1. 获取与安装

在新电脑先安装 Git、Python 3.12 x64 和 Node.js 22.19 或更新版本，然后在 PowerShell 中：

```powershell
git clone https://github.com/haohaoxueyangwen/ScholarFlow.git
cd ScholarFlow
$agentHome = Join-Path $env:LOCALAPPDATA 'ScholarFlow-Dev'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-scholarflow.ps1 -AgentHome $agentHome -WithWeb -WithKnowledge
$sf = Join-Path $agentHome 'bin\scholarflow.cmd'
& $sf doctor
```

安装器在指定用户目录建立独立 Python 环境，在源码目录按 lockfile 安装 Pi 和网页依赖；不会修改系统 PATH 或删除已有环境。Python 的直接依赖已固定版本，但传递依赖尚未全部锁定，因此不同时间安装的结果可能不同。若目标 home 已安装，安装器会停止；选择新的 home，不要直接覆盖。联网和 npm/pip 源可用性取决于目标机器。

## 2. 模型、网页和知识库

```powershell
& $sf auth set-deepseek  # 私密输入，不要把密钥粘贴到聊天或仓库
& $sf web
```

也可连接自行部署的兼容模型；本仓库不会启动你的远端模型或 SSH 隧道。项目数据默认位于所选 `$agentHome`，首次为空。网页中的项目、工作区与知识归属保持分离；外部模型发送资料前需要明确同意。

批准笔记后的语义入库需要单独准备本机 Embedding 服务和经审核模型，再运行 `& $sf knowledge configure-embedding --help`。安装 LightRAG Python 依赖不等于 Embedding 服务已配置，也不复制旧索引。

桌面快捷方式可以在项目目录使用 `powershell -NoProfile -File .\scripts\install-reading-desk-shortcut.ps1 -AgentHome $agentHome` 创建，不覆盖同名的其他快捷方式。

## 3. 开发与回归

```powershell
$runtime = Get-Content (Join-Path $agentHome 'runtime.json') -Raw | ConvertFrom-Json
$env:PYTHONPATH = Join-Path (Get-Location).Path 'tests'
& $runtime.python -m unittest test_scholarflow_literature_search test_scholarflow_remote_pdf test_h008_pdf_sources test_h008c_pdf_notes test_research_workspace test_web_research_loop test_workspace_knowledge test_portable_knowledge test_research_http_security test_portable_desk_shortcut
Push-Location packages/scholarflow-pi
npm.cmd test
Pop-Location
Push-Location apps/research-workbench
npm.cmd run test:unit
npm.cmd run typecheck
Pop-Location
```

这些是隔离回归，不能代替真实模型、完整科研质量或跨机验收。`tests/browser/research-desk-slow-poll.js` 为指定隔离测试工作区的浏览器回归，需要先创建配套测试数据，不能在用户项目上直接使用。

## 4. 数据与安全

- Git 仓库是公开的；`.gitignore` 是辅助保护，不是凭据扫描器。不要使用 `git add -f` 绕过敏感文件排除，也不要提交模型权重或真实科研资料。
- `auth set-deepseek` 将凭据写入用户数据目录的明文认证文件；请在新电脑单独配置，按系统权限保护，不要复制到仓库。
- 现有研究事实、PDF、会话和知识索引不会由 Git 自动同步。若需要跨机迁移这些内容，应另行选择私有、可校验且可恢复的方式；不要传到公开仓库。
- Nature Skills 通过安装器按固定提交单独获取和验证。`packages/scholarflow-pi/vendor/nature-skills/SOURCE.json` 描述的是早期 Nature Writing 基线；当前完整 Skills 的固定提交和完整性校验值以 `scholarflow/nature_skill_package.py` 为准。启用方法文件不等于自动授权其外部工具、网络或文件写入。
