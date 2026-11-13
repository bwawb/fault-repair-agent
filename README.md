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
- 保留本次任务的工作副本及测试 XML 报告。

> 当前 Agent 分析记录与修复后测试对比结果的保存方式，以实际代码为准。后续计划将补丁、审批和测试对比整合到统一的任务报告中。

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

打开 `diagnosis_agent.py`，设置原始项目路径和工作副本的保存目录：

```python
source_project = r"E:\PythonProject\demo-python-project"

working_project = create_workspace(
    source_project,
    r"E:\PythonProject\fault-repair-workspaces",
)
```

请根据自己的电脑修改上述路径。

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

如果测试未正常运行或报告无法读取，程序应报告无法比较，而不是直接认定修复成功。

## 当前限制

- 当前主要面向本地 Python 项目和 pytest；
- 测试工具默认执行项目中的 `tests` 目录；
- 当前提案提取流程只选择最后一次修改预览；
- 只支持修改已有 UTF-8 文件，暂不支持新建、删除或重命名文件；
- 当前流程以一次修复尝试为主，尚未形成完整的自动多轮修复；
- pytest 结果对比尚未完整处理 `xfail`、`xpass` 等特殊情况；
- 工作副本用于避免直接修改原始项目，**不构成完整安全沙箱**；
- pytest 会实际执行项目代码，当前应先用于可信的示例项目；
- 测试通过不能证明代码在所有场景下都正确。

## 后续计划

- 将补丁提案、审批和测试对比整合到统一任务报告；
- 为测试结果比较补充异常场景处理；
- 支持有限次数的“修改—测试—反馈”迭代；
- 增加补丁提案与审批状态的持久化记录；
- 支持读取指定代码行范围，减少无关上下文；
- 构建可重复运行的故障任务集，评估定位和修复效果；
- 在核心流程稳定后，探索代码语义检索或混合检索。

## 安全与隐私

运行测试会执行待分析项目中的代码。当前版本应优先用于自己准备或信任的项目。

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
