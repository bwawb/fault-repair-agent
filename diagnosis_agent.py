from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from uuid import uuid4

from demo_tool_call import create_model
from tools.langchain_tools import create_langchain_tools
from tools.tool_registry import ToolRegistry
from run_report import build_run_report, save_run_report
from proposal import extract_latest_preview
from apply_proposal import confirm_and_apply
from tools.test_tools import TestTools
from workspace import create_workspace
from compare_test_reports import read_test_report, compare_results


# SYSTEM_PROMPT = """
# 你是一个 Python 项目故障分析助手。
#
# 你的任务是根据用户描述，使用提供的工具收集证据并分析故障。
#
# 要求：
# 1. 优先使用工具获取实际信息，不要猜测文件内容或测试结果。
# 2. 根据需要运行测试、查看项目文件并读取相关代码。
# 3. 区分测试预期行为和代码实际行为。
# 4. 最终回答应包含：故障位置、错误现象、原因、证据和修复建议。
# 5. 如果证据不足，请说明不确定之处。
# 6. 当前阶段只做分析，不修改项目文件。
# """

SYSTEM_PROMPT = """
你是一个 Python 项目故障分析助手。

你的任务是根据用户描述，使用工具收集证据、分析故障，
并为目标问题生成最小修改的预览。

要求：
1. 使用工具获取实际信息，不要猜测文件内容或测试结果。
2. 根据需要运行测试、搜索代码并读取相关文件。
3. 区分测试预期行为和代码实际行为。
4. 在提出修改前，先读取待修改文件。
5. 使用 preview_replace 生成修改预览。
6. 最终回答应包含故障位置、原因、证据、修改预览和不确定之处。
7. preview_replace 只生成预览，不会修改文件。
8. 不要声称修改已经应用，也不要声称修改后的测试已经通过。
"""

def main():
    # 修改为你自己的示例项目路径。

    source_project = r"E:\PythonProject\demo-python-project"


    working_project = create_workspace(source_project, r"E:\PythonProject\fault-repair-agent")

    print(f"工作副本：{working_project}")

    # project_path = r"E:\PythonProject\demo-python-project"

    run_id = uuid4().hex
    # 报告保存在本次任务目录下，与 project 同级
    report_dir = working_project.parent / "reports"

    baseline_path = report_dir / f"baseline_{run_id}.xml"
    after_fix_path = report_dir / f"after_fix_{run_id}.xml"

    test_tools = TestTools(str(working_project))
    print("\n===== 运行修复前测试 =====")

    baseline_run = test_tools.run_tests_with_report(
        report_path=str(baseline_path),
    )

    print(f"运行状态：{baseline_run['status']}")
    print(f"pytest 退出码：{baseline_run['exit_code']}")
    print(baseline_run["output"])

    if baseline_run["status"] != "completed":
        print("修复前测试未正常完成，无法建立测试基线。")
        print(f"工作副本：{working_project}")
        return

    try:
        baseline_results = read_test_report(
            baseline_run["report_path"]
        )
    except (OSError, ValueError) as exc:
        print(f"读取修复前测试报告失败：{exc}")
        return

    # 1.创建模型
    model = create_model()

    # 2.创建已有工具的注册表
    registry = ToolRegistry(str(working_project))

    # 3.将现有工具包装为LangChain工具
    tools = create_langchain_tools(registry)

    # 4.创建Agent
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT
    )

    # 5.提供需要分析的故障
    user_request = (
        "订单折扣计算存在问题。"
        "请运行测试，找到折扣问题的相关代码，"
        "分析故障原因，并生成最小修改的 diff 预览。"
        "本次只处理订单折扣问题。"
    )

    print("===== 开始故障分析 =====")
    print(f"用户任务：{user_request}")

    try:
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(content=user_request),
                ]
            },
            config={
                "recursion_limit": 20,
            }
        )
    except GraphRecursionError:
        print("\nAgent 达到执行步数上限，分析未完成。")
        return

    # 6. 查看 Agent 运行过程中产生的消息。
    # print("\n===== Agent 执行过程 =====")
    # for message in result["messages"]:
    #     if isinstance(message, AIMessage) and message.tool_calls:
    #         for tool_call in message.tool_calls:
    #             print(f"\n模型请求工具：{tool_call['name']}")
    #             print(f"工具参数：{tool_call['args']}")
    #     elif isinstance(message, ToolMessage):
    #         print(f"\n工具返回，tool_call_id：{message.tool_call_id}")
    #         print(message.content)

    # 7. 显示最终回答。
    print("\n===== 最终故障分析 =====")
    print(result["messages"][-1].content)

    # 整理 Agent 本次运行的消息。
    report = build_run_report(
        user_request=user_request,
        messages=result["messages"],
    )

    # 保存运行记录。
    report_path = save_run_report(report)

    print("\n===== 提取修改提案 =====")

    proposal_result = extract_latest_preview(
        result["messages"]
    )

    if not proposal_result["success"]:
        print(proposal_result["message"])
        return

    proposal = proposal_result["proposal"]

    print(f"工具调用 ID：{proposal['tool_call_id']}")
    print(f"目标文件：{proposal['relative_path']}")

    print("\n原文本：")
    print(proposal["old_text"])

    print("\n新文本：")
    print(proposal["new_text"])

    print("\n修改预览：")
    print(proposal["diff"])

    print("\n===== 确认并应用修改 =====")

    apply_result = confirm_and_apply(
        project_path=str(working_project),
        proposal=proposal,
    )

    print(f"修改状态：{apply_result['status']}")
    print(f"说明：{apply_result['message']}")

    if apply_result["status"] != "applied":
        print(f"\n本次工作副本保存在：{working_project}")
        return

    print("\n===== 运行修复后测试 =====")

    after_run = test_tools.run_tests_with_report(
        report_path=str(after_fix_path),
    )

    print(f"运行状态：{after_run['status']}")
    print(f"pytest 退出码：{after_run['exit_code']}")
    print("测试输出：")
    print(after_run["output"])

    if after_run["status"] != "completed":
        print("修复后测试未正常完成，无法比较测试结果。")
        print(f"工作副本：{working_project}")
        return

    try:
        after_results = read_test_report(
            after_run["report_path"]
        )
    except (OSError, ValueError) as exc:
        print(f"读取修复后测试报告失败：{exc}")
        print(f"工作副本：{working_project}")
        return

    # 比较每个测试在修复前后的状态。
    comparison = compare_results(
        before=baseline_results,
        after=after_results,
    )

    print("\n===== 修复前后测试结果对比 =====")

    print("\n恢复通过：")
    if comparison["fixed"]:
        for test_id in comparison["fixed"]:
            print(f"  {test_id}")
    else:
        print("  无")

    print("\n仍然失败：")
    if comparison["still_failing"]:
        for test_id in comparison["still_failing"]:
            print(f"  {test_id}")
    else:
        print("  无")

    print("\n新增失败：")
    if comparison["newly_failed"]:
        for test_id in comparison["newly_failed"]:
            print(f"  {test_id}")
    else:
        print("  无")

    print("\n状态无法直接比较：")
    if comparison["uncertain"]:
        for item in comparison["uncertain"]:
            print(f"  {item}")
    else:
        print("  无")

    print("\n仅出现在修复前报告中：")
    if comparison["only_before"]:
        for test_id in comparison["only_before"]:
            print(f"  {test_id}")
    else:
        print("  无")

    print("\n仅出现在修复后报告中：")
    if comparison["only_after"]:
        for test_id in comparison["only_after"]:
            print(f"  {test_id}")
    else:
        print("  无")

    print(f"\n修复前报告：{baseline_path}")
    print(f"修复后报告：{after_fix_path}")
    print(f"工作副本：{working_project}")


    # print("\n===== 运行记录 =====")
    # print(f"模型消息数量：{report['model_message_count']}")
    # print(f"工具调用数量：{report['tool_call_count']}")
    # print(f"报告保存位置：{report_path.resolve()}")

if __name__ == "__main__":
    main()