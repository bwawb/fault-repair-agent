# Fault Repair Agent

一个面向 Python 项目的故障定位与修复 Agent。

项目使用大语言模型分析故障描述、测试结果和项目代码，提出修改方案；程序生成补丁预览，在用户确认后将修改应用到独立工作副本，并通过 pytest 比较修复前后的测试结果。

当前版本适用于使用 pytest 的本地 Python 示例项目。一次提案可修改工作副本中的多个已有 UTF-8 文件，每个文件可替换多处互不重叠的文本片段，片段可以包含多行。

## 项目目标

软件项目出现测试失败时，开发者通常需要查看错误信息、搜索相关代码、定位原因、修改实现并重新运行测试。

本项目尝试通过 Agent 辅助完成这一过程：

1. 创建待分析项目的工作副本；
2. 运行测试并记录修复前的测试基线；
3. 根据故障描述和测试结果查找相关代码；
4. 分析故障原因并提出修改；
5. 生成 diff，等待用户确认；
6. 检查文件状态后应用修改；
7. 运行修复后的测试；
8. 比较修复前后的测试结果并输出报告。

## 当前功能

### 故障定位

- 列出项目中的文件；
- 读取项目内的文本文件；
- 在 Python 代码中搜索关键词；
- 运行 `tests` 目录中的 pytest 测试；
- 使用大语言模型结合测试输出与代码分析故障原因。

### 修改预览与应用

- 提取模型调用 `preview_replace` 时产生的修改提案；
- 根据原文本和新文本生成 diff；
- 展示修改内容并等待用户确认；
- 对目标路径和原文本匹配情况进行检查；
- 在预览与应用之间检查文件内容是否发生变化；
- 用户批准后，将修改应用到本次任务的工作副本。

### 测试结果对比

- 在修改前后分别运行 pytest；
- 生成 JUnit XML 测试报告；
- 按测试标识比较状态变化；
- 输出恢复通过、仍然失败和新增失败的测试；
- 单独列出无法直接比较或只出现在一份报告中的测试。

### 运行记录

- 记录 Agent 的模型消息数量、工具调用及工具结果；
- 保存 Agent 的故障分析报告；
- 保留工作副本和任务 JSON，可显式保留原始测试 XML。

统一任务报告保存测试基线、每轮提案与审批、应用结果和复测对比。

## 工作流程

```text
用户提供故障描述
        ↓
创建独立工作副本
        ↓
运行测试并保存基线报告
        ↓
Agent 读取代码、搜索代码并分析故障
        ↓
模型提出修改并生成 diff 预览
        ↓
程序提取提案，用户确认
        ↓
检查文件状态并应用修改
        ↓
重新运行测试
        ↓
比较修复前后的测试结果
        ↓
目标仍失败且满足重试条件时，携带本轮反馈进入下一轮
```

## 项目结构

当前项目的主要文件如下，实际结构可随开发调整：

```text
fault-repair-agent/
├── diagnosis_agent.py          # 故障分析与修复流程入口
├── demo_tool_call.py           # 模型创建及单次工具调用示例
├── proposal.py                 # 从 Agent 消息中提取修改提案
├── apply_proposal.py           # 补丁确认、检查与应用
├── workspace.py                # 创建项目工作副本
├── compare_test_reports.py     # 读取和比较测试报告
├── run_report.py               # Agent 运行记录
├── tools/
│   ├── __init__.py
│   ├── file_tools.py           # 列文件、读取文件和搜索代码
│   ├── test_tools.py           # 运行 pytest 并生成测试报告
│   ├── edit_tools.py           # 生成文本修改预览
│   ├── tool_registry.py        # 工具注册与分发
│   └── langchain_tools.py      # 将工具包装为 LangChain 工具
└── outputs/                    # 运行记录等输出
```

## 环境要求

- Windows、macOS 或 Linux；目前主要在 Windows 环境中开发和验证；
- Python 3.12；
- 支持工具调用的模型及其 API；
- 待分析项目使用 pytest，并包含 `tests` 目录。

当前测试工具使用运行 Agent 的 Python 解释器执行 pytest。因此，待分析项目所需的依赖也需要在该环境中可用。后续计划支持为待分析项目单独指定解释器或执行环境。

## 安装与配置

### 1. 创建虚拟环境

在项目根目录运行：

```powershell
python -m venv .venv
```

Windows PowerShell 中激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

如果使用 Windows 命令提示符：

```cmd
.venv\Scripts\activate.bat
```

### 2. 安装依赖

根据当前代码使用的模块，安装：

```powershell
python -m pip install langchain langchain-openai python-dotenv pytest
```

建议后续在项目中维护依赖清单并记录已验证的版本，以便复现运行环境。

### 3. 配置模型

在项目根目录创建 `.env` 文件：

```dotenv
MODEL_NAME=你的模型名称
MODEL_API_KEY=你的API密钥
MODEL_BASE_URL=你的API基础地址
```

模型配置由 `demo_tool_call.py` 中的 `create_model()` 读取。请根据实际模型服务商填写配置，并确认模型支持工具调用。

不要将真实 API 密钥提交到 Git 仓库。

## 准备示例项目

项目需要一个待分析的 Python 项目。例如：

```text
demo-python-project/
├── calculator.py
├── order_service.py
├── pricing.py
└── tests/
    ├── test_calculator.py
    └── test_order_service.py
```

`tests` 目录中的 pytest 测试应能复现待分析的故障。

当前版本的测试工具默认运行整个 `tests` 目录。如果测试文件位于其他位置，需要调整测试位置或扩展测试工具。

## 运行项目

### 1. 配置项目路径

可通过 Python 入口指定原始项目路径和工作副本的保存目录。

```python
from diagnosis_agent import main

main(
    source_project=r"E:\PythonProject\demo-python-project",
    workspace_base=r"E:\PythonProject\fault-repair-workspaces",
)
```

直接运行 `diagnosis_agent.py` 时使用 `main()` 定义的默认路径。

### 2. 配置故障描述

在 `diagnosis_agent.py` 中设置任务，例如：

```python
user_request = (
    "订单折扣计算存在问题。"
    "请运行测试，找到折扣问题的相关代码，"
    "分析故障原因，并生成最小修改的 diff 预览。"
    "本次只处理订单折扣问题。"
)
```

### 3. 启动 Agent

在 `fault-repair-agent` 项目根目录运行：

```powershell
python diagnosis_agent.py
```

程序会创建一个新的工作副本，运行修复前测试，再启动 Agent 进行分析。

### 4. 确认修改

Agent 生成修改提案后，程序会展示 diff：

```diff
-    discount_rate = discount_percent // 100
+    discount_rate = discount_percent / 100
```

程序询问：

```text
是否将上述修改应用到工作副本？输入 yes 确认，其他内容取消：
```

- 输入 `yes`：在检查通过后，将修改应用到工作副本；
- 输入其他内容：取消修改。

### 5. 查看验证结果

修改后，程序会重新运行测试并比较结果。例如：

```text
===== 修复前后测试结果对比 =====

恢复通过：
  tests.test_order_service::test_create_order_with_discount

仍然失败：
  tests.test_calculator::test_calculate_average_with_empty_list

新增失败：
  无
```

该结果表示订单折扣测试在本次运行中恢复通过，计算器空列表测试仍然失败。

测试结果只反映已执行测试的覆盖范围；没有发现新增失败，不代表补丁对所有输入都正确。

### 6. 手动回滚一个文件

任务结束后，把终端中显示的任务 JSON 路径和文件的项目内相对路径传给回滚脚本。

```powershell
python rollback_file.py "outputs/run_reports/task_<任务ID>.json" "pricing.py"
```

Python 程序也可以调用 `rollback_file(report_path, relative_path)`，该函数位于 `rollback_file.py`。
命令会先核对工作副本文件的 SHA-256，再从本次任务的初始快照恢复文件。
结果写入报告的 `rollbacks`，成功后该文件的顶层累计 diff 为空。
回滚只处理在快照和工作副本中都存在的普通文件。没有记录文件哈希的旧报告无法回滚。
测试结果保留为修复时的历史记录，回滚命令不会自动复测。

## 核心设计

### 工具注册与调用

`ToolRegistry` 将工具名称映射到对应的 Python 方法，并在执行前检查工具名称及参数。

LangChain 工具包装层将已有工具暴露给模型。模型提出工具调用后，框架执行工具并将结果返回给模型。

### 补丁提案与人工确认

模型可以调用 `preview_replace(relative_path, old_text, new_text)` 预览单处替换，或调用 `preview_replacements(relative_path, replacements)` 预览同一文件的多处替换。`replacements` 是非空列表，每项包含 `old_text` 和 `new_text`。
多文件提案使用 `preview_file_changes(files)`，每项包含 `relative_path` 和 `replacements`，路径都相对于本次任务的工作副本。
程序在同一份原文中验证各处唯一匹配且互不重叠，全部通过后生成一份 diff，一次审批后写入。统一报告保留单处提案原有字段，多处提案记录 `replacements` 列表。

用户批准后，程序会检查目标路径、原文本及文件内容是否符合当前实现的要求，然后将修改写入工作副本。

多行提案可使用 LF 或 CRLF。程序按目标文件的换行风格匹配和写入。文件没有换行时，新增换行使用 LF。
替换未涉及的字节保持原样。修改涉及文件末尾时，可通过提案增删末尾换行，diff 会标记末尾没有换行的情况。混合换行文件只支持不含换行的替换片段。

多文件提案在第一次替换前准备所有待写入内容和原始内容，并重新核对所有文件的路径与哈希。普通写入错误会触发逆序恢复，恢复前会核对文件仍是程序刚写入的版本。
报告的 `application.files` 记录逐文件状态和相关哈希。恢复成功时应用状态为 `write_failed_restored`，恢复失败时为 `partial`，任务以 `application_partial` 结束。成功后，多文件轮次在 `cumulative_patches` 中按路径记录累计 diff，顶层 `patch_summary.files` 也同步更新。
文件分别替换。进程崩溃、断电或恢复失败时可能留下部分修改，当前没有跨文件原子性或重启后自动恢复。

### 测试基线比较

程序在同一工作副本中，分别生成修复前和修复后的 JUnit XML 报告，并按照报告中的测试标识比较测试状态。

当前比较模块会列出：

- 恢复通过；
- 仍然失败；
- 新增失败；
- 状态无法直接比较；
- 只出现在某一份报告中的测试。

如果测试未正常运行、XML 缺失、无法解析或与退出码不一致，本轮没有可用于比较的结果。

### 本地 pytest 执行

Agent 的 `run_tests()` 和主程序的 `run_tests_with_report()` 共用同一个执行器。两者都以工作副本为当前目录，使用当前 Python 解释器、参数列表和 `shell=False` 执行 `python -m pytest tests -q --capture=no`，默认超时 30 秒。主程序额外请求本次运行的 JUnit XML，并使用核验过的逐用例结果。

运行中持续读取 stdout 和 stderr，每路只保留末尾 64 KiB 原始字节。输出超出上限时，文本和报告字段均标记截断。`--capture=no` 关闭 pytest 的默认输出捕获，使通常的测试输出直接进入限量管道。

local 测试使用单独构造的环境。Windows 默认继承已设置的 `SYSTEMROOT`、`WINDIR`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`，用于系统运行、子命令和临时目录。其他平台默认继承已设置的 `TMPDIR`、`LANG`、`LC_ALL`、`LC_CTYPE`，用于临时目录和区域设置。`PATH` 默认由当前 Python 解释器所在目录和系统搜索目录组成。Windows 使用 `System32` 和 `SYSTEMROOT`，其他平台使用 Python 的系统默认绝对路径。执行器固定设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUNBUFFERED=1`。Windows 启动器和它启动的 pytest 使用这份受控环境。

目标项目需要额外变量时，先在当前会话设置值，再用 `--test-env NAME` 点名传入。参数可重复指定，只适用于 local。Python 调用可使用 `TestTools(project_path, local_env_names=("APP_TEST_MODE",))`。未设置的变量会使测试返回 `setup_error`。

```powershell
$env:APP_TEST_MODE = 'fixture'
python diagnosis_agent.py --test-backend local --test-env APP_TEST_MODE
```

宿主机的 `PYTHONPATH`、`PYTEST_ADDOPTS`、`PYTEST_PLUGINS`、`HOME`、代理和应用变量默认不会传入。依赖外部工具的测试可显式传入可信的 `PATH`，依赖额外导入路径或测试参数时也可显式点名相应变量。目标项目自己的 pytest 配置、`conftest.py` 和已安装插件仍可生效。程序不会把环境变量配置值主动写到终端或任务报告。待测代码和插件可以读取显式传入的值，也可以将其打印到输出或写入 XML，因此只传入你愿意交给待测代码的值。

| 运行状态 | 含义 |
| --- | --- |
| `passed` / `failed` | pytest 正常结束，退出码分别为 0 和 1。要求 XML 的入口还须成功解析并核验报告。 |
| `setup_error` | 执行前准备失败，例如找不到 `tests` 目录、超时参数无效或 XML 路径已存在。 |
| `launch_error` | 无法启动 pytest 子进程。 |
| `timeout` | 到达运行时限，程序尝试清理进程并记录清理结果。 |
| `execution_error` | 启动后的本地输出读取异常。 |
| `pytest_error` | pytest 返回 0、1 以外的退出码，例如收集失败、无测试或 pytest 内部错误。 |
| `report_missing` / `report_parse_error` | 应生成的 XML 缺失，或 XML 无法读取、格式无效或与退出码不一致。 |

`verification.baseline`、`verification.after_fix` 和每轮的 `verification.after_fix` 记录 `execution_mode`、运行状态、实际退出码、输出、截断标记、错误原因和适用的清理结果。Agent 主动运行测试的结果也会保存在相应工具调用记录里。

### 可选的 Docker pytest 执行

默认使用 `local`。显式选择 `docker` 时，Agent 的 `run_tests`、基线测试和修复后测试共用该后端。

```powershell
python diagnosis_agent.py --test-backend local
python diagnosis_agent.py --test-backend docker --docker-image fault-repair-pytest:demo
```

Docker 模式需要本机 Docker CLI 和运行中的 Linux 容器引擎。先用可信的 Python 基础镜像构建本地测试镜像。最小示例适用于只依赖 pytest 的项目。

```dockerfile
FROM python:3.12-slim
RUN python -m pip install --no-cache-dir pytest
```

将上面的内容保存到独立构建目录的 `Dockerfile`，然后在该目录运行 `docker build -t fault-repair-pytest:demo .`。对有额外依赖的项目，把依赖清单复制进这个构建目录，并在构建时安装目标项目的 Linux 依赖。构建可以按环境要求使用联网源或本地 wheel。测试执行阶段只检查本地镜像，使用 `--pull=never`，也不会运行依赖安装。

- **挂载与权限**：工作副本只读挂载到 `/workspace`。生成 XML 时额外挂载单次运行的可写目录 `/test-report`，测试结束且容器确认清理后，宿主程序只复制最多 8 MiB 的普通 XML 文件到本轮报告路径。容器也有 64 MiB 的 `/tmp`。根文件系统只读，pytest 缓存插件关闭。需要写入源码目录的测试须改用临时目录。
- **Windows 兼容性**：Docker Desktop 须使用 Linux 容器并可访问工作副本、报告目录所在的驱动器。Windows 宿主启动的容器使用 UID/GID `65534:65534`，Linux 等宿主使用当前 UID/GID。挂载需允许该容器用户读取源码及写入临时报告目录。Windows 专用依赖、宿主虚拟环境和 `.bat` 脚本不能当作 Linux 容器依赖使用。Docker 挂载路径目前不支持逗号、双引号或换行。远程 TCP/SSH Docker 端点不在本版范围内。
- **资源与超时**：容器默认 `--network=none`、1 CPU、512 MiB 内存且不额外使用 swap、64 个进程/线程，Docker 日志启用轮转。附加运行默认超时 30 秒，探测、创建和清理命令分别最多等待 10 秒，另有短暂的 CLI 退出等待。Agent 对 stdout 和 stderr 仍分别只保留末尾 64 KiB。
- **退出与清理**：正常完成时通过 Docker 容器状态读取测试退出码。超时后结束等待中的 CLI，并单独向 Docker 请求 `container rm --force --volumes`。报告记录容器名、移除命令退出码和 `container_removed`，该字段只在 Docker 返回移除成功时为真。守护进程失联或 Agent 被强制终止时可能留下容器，可按报告中的名称检查和移除。
- **失败状态**：Docker 缺失或守护进程不可用记为 `docker_unavailable`，本地镜像缺失记为 `image_missing`，容器创建或启动失败记为 `launch_error`。无法读取可靠的容器状态记为 `docker_execution_error`，正常结束后移除未确认记为 `cleanup_error`，超时后的移除失败仍记为 `timeout` 并附清理错误。XML 缺失或不可用仍会停止逐测试比较。Docker 失败时任务不会切换到 local。

Docker 提供额外隔离，但仍应使用可信镜像和可信项目。程序不把 Agent 的模型密钥作为容器环境变量传入，也不挂载 Docker socket、原始项目目录、基线快照或宿主用户目录。工作副本内已有的文件，包括项目自己的配置和密钥，仍可被测试读取。可写报告目录受测试代码控制，XML 的导出上限不限制该目录在运行中的磁盘占用。容器隔离依赖本机 Docker、操作系统和镜像，不能保证安全执行任意不可信代码。

已准备镜像后，可以运行真实容器验收用例。测试会生成可信示例，验证失败到通过的 JUnit XML 对比、三个测试入口的后端一致性、镜像缺失、超时清理和容器限制。三入口测试使用预设诊断与补丁，容器内的 pytest 实际运行。

```powershell
$env:FAULT_REPAIR_DOCKER_TEST_IMAGE = 'fault-repair-pytest:demo'
$dockerAcceptanceTemp = Join-Path (Get-Location).Path ('.pytest_tmp\docker-acceptance-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $dockerAcceptanceTemp) { throw 'pytest 临时路径必须全新' }
python -m pytest -q -p no:cacheprovider --basetemp $dockerAcceptanceTemp test_docker_pytest.py -k real_docker
```

## 当前限制

- 当前主要面向本地 Python 项目和 pytest；
- 测试工具默认执行项目中的 `tests` 目录；
- 当前提案提取流程只选择最后一次修改预览；
- 只支持修改已有 UTF-8 文件，暂不支持新建、删除或重命名文件；
- 每个任务最多尝试三轮。目标仍失败且满足重试条件时会携带反馈进入下一轮，每轮应用补丁都需人工确认；
- pytest 结果对比尚未完整处理 `xfail`、`xpass` 等特殊情况；
- 测试通过不能证明代码在所有场景下都正确。

## 后续计划

- 为测试结果比较补充异常场景处理；
- 支持读取指定代码行范围，减少无关上下文；
- 构建可重复运行的故障任务集，评估定位和修复效果；
- 在核心流程稳定后，探索代码语义检索或混合检索。

## 安全与隐私

### 模型消息、终端和任务 JSON

通过 Python 入口可为单次诊断任务显式传入已知敏感值。以下使用虚构值。

```python
from diagnosis_agent import main

main(sensitive_values=("synthetic-credential-value",))
```

参数接受字符串列表或元组，默认无配置值。应用可以从自己信任的配置中注入值。脱敏器不会从环境变量或文件里自动收集敏感值。配置随任务留在内存中。程序现有的模型认证配置仍按原有方式加载。

启用后，每次模型调用前会处理任务、重试反馈、工具结果和要重新发送的助手历史。工具结果先按 JSON 结构处理，再重新编码。命中工具名、调用 ID 或必须保留的协议字段时会停止发送。补丁工具拒绝含有遮盖标记或已配置敏感值的参数。程序仍使用本地保留的原始 diff 校验提案。

任务终端和 JSON 检查相同的已知值。若审批 diff 需要遮盖，任务以 `approval_blocked` 结束，工作副本保留当前内容。程序使用原始测试 ID 比较与决策，展示报告及重试反馈可使用每个任务内唯一的测试 ID 别名。原始报告数据留在内存中，程序只持久化一份任务 JSON。回滚所需的路径、任务标识和哈希保持原样；这些字段命中敏感值时停止相关保存或操作，并保留上一个已保存的检查点。

启用脱敏的任务报告带有 `output_protection` 标记。通过 `rollback_file(report_path, relative_path, sensitive_values=...)` 提供同一任务配置，回滚更新也会保护输出。报告不保存敏感值列表，程序依赖调用方重新提供相同配置。既有报告仍可通过原命令回滚，旧报告不会自动脱敏。

### 原始 JUnit XML

主流程默认在独立的单次测试临时目录生成 XML，完成退出状态核验和可用报告的解析后清理。任务 JSON 的 `junit_xml.status` 记录 `deleted`、`retained`、`not_created`、`cleanup_failed` 或 `unverified`。删除后 `report_path` 为空。解析失败仍由测试执行状态记录。Docker 容器挂载的临时目录另有 `docker_xml_staging` 清理状态。

需要检查原始 XML 时使用 `python diagnosis_agent.py --keep-junit-xml` 或 `main(keep_junit_xml=True)`。原始 XML 可能含有敏感内容，保留和清理失败时会提示。直接调用 `TestTools.run_tests_with_report()` 仍默认保留显式指定的报告，可通过构造参数 `keep_junit_xml=False` 启用清理。

原始 XML、外部追踪和回调可能接触原始数据。系统依赖本地文件权限，崩溃、文件锁或并发替换可能留下文件。脱敏仅覆盖已配置的字面值及终端和报告中的部分转义形式。未知值、其他编码变体、不同换行风格和拆分片段可能漏掉。

工作副本和基线快照的复制、文件列表、直接读取、代码搜索和补丁预览共用文件访问策略。默认按不区分大小写的名称保护 `.env`、`.env.*`、以 `.pem` 或 `.key` 结尾的名称，以及 `.git`、`.venv` 和 `venv` 路径段。Python 和 pytest 缓存目录也会被排除。调用方可通过 `FileAccessPolicy(extra_paths=("config/private.json",))` 增加项目相对路径，并把同一策略传给复制、工具注册和补丁应用入口。当前命令行使用默认策略。

直接访问只接受项目相对路径。含 `..` 的路径会被拒绝，Windows 的数据流和尾随点、空格的路径也会被拒绝。程序检查请求路径及解析结果，并拒绝路径中的符号链接、junction、可识别的其他重解析点以及有多个硬链接的普通文件。复制、列表和搜索会在进入被排除目录前跳过，并提示跳过数量。拒绝读取或预览时返回原因，不返回文件内容。

按名称排除文件不会扫描内容。普通源码仍可能含敏感信息。依赖被排除配置、证书、虚拟环境或链接的目标项目，可能需要用可信测试配置、环境中的依赖或普通文件替代。路径检查与实际打开、遍历和复制是分开的，无法防止并发替换引起的所有竞态。

**工作副本不是执行沙箱。** 本地 pytest 使用受控的环境变量，仍以当前用户权限执行待分析项目的测试、插件和导入代码，能够访问该用户可访问的文件和网络。输出限制只约束 Agent 保留的 stdout 和 stderr；目标项目仍可能消耗 CPU、内存和磁盘。请只运行可信项目。

Windows 会先把启动器加入 Job Object，再放行它启动 pytest。若无法加入 Job 或无法创建 pytest 进程，本次测试会以 `launch_error` 结束。超时后程序请求结束整个 Job，并检查启动器是否退出。`process_cleanup` 分别记录进程组清理请求是否成功、直接管理的进程名称和它是否退出。Windows 集成测试另外检查了 pytest 本身及它创建的一个普通子进程是否退出。其他平台向 pytest 所在的进程组发送终止信号。清理请求失败、进程不在该 Job 或进程组、或 Agent 被强制退出时，无法保证后代进程全部清理。清理操作可能使总耗时超过测试超时值。

Agent 读取的代码、测试输出和任务描述可能会发送给配置的模型服务。使用前请检查项目中是否包含不希望发送的敏感信息。

工作副本和运行报告可能包含项目内容，公开分享前应进行检查。

## Git 忽略建议

可以在 `.gitignore` 中加入：

```gitignore
.venv/
.env
__pycache__/
.pytest_cache/
outputs/
fault_repair_*/
```

如果你把工作副本放在其他目录，也应根据实际位置调整忽略规则，避免将工作副本、运行记录或密钥误提交到仓库。
