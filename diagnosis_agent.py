from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from uuid import uuid4
from datetime import datetime
from copy import deepcopy
import json


from demo_tool_call import create_model
from tools.langchain_tools import create_langchain_tools
from tools.tool_registry import ToolRegistry
from run_report import build_run_report, save_run_report
from proposal import extract_latest_preview
from apply_proposal import confirm_and_apply
from tools.test_tools import TestTools
from workspace import create_workspace
from compare_test_reports import read_test_report, compare_results
from repair_decision import decide_next_step


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


def save_checkpoint(report):
    """
    保存当前任务报告。
    同一个 task_id 会覆盖更新同一份 JSON。
    """
    return save_run_report(report)


def finish_report(report, final_status):
    """
    完成任务，记录最终状态和完成时间。
    """
    report["task"]["completed_at"] = (
        datetime.now().astimezone().isoformat()
    )

    report["final_status"] = final_status

    return save_checkpoint(report)


def run_diagnosis(agent, user_request: str) -> dict:
    """
    让Agent针对当前任务调用工具并分析结果。

    Args:
        agent: 已经创建好的Agent
        user_request: 本轮需要分析的任务

    Returns:
        Agent返回的结果，包含本轮消息
    """

    return agent.invoke(
        {
            "messages": [
                HumanMessage(content=user_request),
            ]
        },
        config={
            "recursion_limit": 20,
        }
    )


def prepare_round_proposal(messages, report: dict, round_record: dict) -> dict:
    """
    从Agent消息中提取修改提案，并更新任务和当前轮次记录

    returns:
        extract_latest_preview()返回的结果
    """
    proposal_result = extract_latest_preview(messages)
    if not proposal_result["success"]:
        # 记录提案提取失败的原因
        report["proposal"]["status"] = "extraction_failed"
        report["proposal"]["message"] = proposal_result["message"]

        round_record["proposal"] = deepcopy(report["proposal"])
        round_record["status"] = "proposal_unavailable"

        return proposal_result

    proposal = proposal_result["proposal"]

    report["proposal"] = {
        "status": "generated",
        "tool_call_id": proposal["tool_call_id"],
        "relative_path": proposal["relative_path"],
        "old_text": proposal["old_text"],
        "new_text": proposal["new_text"],
        "diff": proposal["diff"],
    }

    # 为当前轮次保存独立副本
    round_record["proposal"] = deepcopy(report["proposal"])

    return proposal_result


def apply_round_proposal(
        working_project,
        proposal:dict,
        report: dict,
        round_record: dict,
) -> dict:
    """
    等待用户确认，尝试应用本轮提案，并记录结果。

    Args:
        working_project: 本次任务的工作副本路径。
        proposal: 当前轮次的修改提案。
        report: 整个任务的报告。
        round_record: 当前轮次的记录

    Returns:
        confirm_reply的返回结果
    """

    apply_result = confirm_and_apply(
        project_path=str(working_project),
        proposal=proposal,
    )

    # 记录用户是否同意
    report["approval"] = {
        "status": apply_result["approval_status"],
    }

    # 用户拒绝时，不尝试应用修改
    if apply_result["status"] == "rejected":
        report["application"] = {
            "status": "not_attempted",
            "message": apply_result["message"],
        }
    else:
        report["application"] = {
            "status": apply_result["status"],
            "message": apply_result["message"],
        }

    # 保存到当前轮次
    round_record["approval"] = deepcopy(report["approval"])
    round_record["application"] = deepcopy(report["application"])

    return apply_result

def verify_round(
        test_tools,
        after_fix_path,
        baseline_results: dict,
        report: dict,
        round_record: dict,
) -> dict:
    """
    运行修复后测试，解析报告，并与最初基线比较

    Returns:
        成功：
            {
                "success": True,
                "after_results": {…},
                "comparison": {…},
            }

        失败：
            {
                "success": False,
                "status": 结束原因,
                "messages": 错误说明
            }
    """
    print("\n===== 运行修复后测试 =====")

    after_run = test_tools.run_tests_with_report(
        report_path=str(after_fix_path)
    )

    print(f"运行状态：{after_run['status']}")
    print(f"pytest 退出码：{after_run['exit_code']}")
    print("测试输出：")
    print(after_run["output"])

    # 先记录测试进程信息
    report["verification"]["after_fix"] = {
        "status": after_run["status"],
        "exit_code": after_run["exit_code"],
        "output": after_run["output"],
        "report_path": str(after_fix_path),
        "test_results": None,
    }

    round_record["verification"] = {
        "after_fix": deepcopy(report["verification"]["after_fix"]),
        "comparison": None,
    }

    save_checkpoint(report)

    # completed表示进程结束，退出码仍需要单独检查
    if after_run["status"] != "completed" or after_run["exit_code"] not in (0, 1):
        message = "修复后测试未正常完成，无法比较测试结果。"
        round_record["status"] = "after_test_error"

        return {
            "success": False,
            "status": "after_test_error",
            "message": message,
        }

    try:
        after_results = read_test_report(after_run["report_path"])
    except (OSError, ValueError) as exc:
        message = f"读取修复后测试报告失败：{exc}"

        report["verification"]["after_fix"]["report_error"] = str(exc)
        round_record["verification"]["after_fix"] = deepcopy(
            report["verification"]["after_fix"]
        )
        round_record["status"] = "after_report_error"

        return {
            "success": False,
            "status": "after_report_error",
            "message": message,
        }

    # 记录每个测试的修复后状态
    report["verification"]["after_fix"]["test_results"] = after_results
    round_record["verification"]["after_fix"] = deepcopy(
        report["verification"]["after_fix"]
    )
    save_checkpoint(report)

    # 与任务开始时的基线比较
    comparison = compare_results(
        before=baseline_results,
        after=after_results,
    )

    report["verification"]["comparison"] = comparison
    round_record["verification"]["comparison"] = deepcopy(comparison)

    # 先保存结果；轮次决策由主程序继续处理
    save_checkpoint(report)

    return {
        "success": True,
        "after_results": after_results,
        "comparison": comparison,
    }



def main():
    # 修改为你自己的示例项目路径。

    source_project = r"E:\PythonProject\demo-python-project"

    # 1. 创建任务 ID
    run_id = uuid4().hex

    # 2. 记录任务开始时间
    started_at = datetime.now().astimezone().isoformat()

    # 3. 用户任务
    user_request = (
        "订单折扣计算存在问题。"
        "请运行测试，找到折扣问题的相关代码，"
        "分析故障原因，并生成最小修改的 diff 预览。"
        "本次只处理订单折扣问题。"
    )

    # 本次任务要修复的目标测试
    # 格式与read_test_report()返回测试标识保持一致
    target_test_ids = [
        "tests.test_order_service::test_create_order_with_discount",
    ]

    # 4. 创建工作副本
    working_project = create_workspace(source_project, r"E:\PythonProject\fault-repair-agent\copy")

    print(f"工作副本：{working_project}")

    # 5. 初始化统一任务报告
    report = build_run_report(
        user_request=user_request,
        messages=[],
        task_id=run_id,
        source_project=source_project,
        working_project=str(working_project),
        started_at=started_at,
    )

    # 即使后续步骤失败，也能在报告中看到本次的修复目标
    report["task"]["target_test_ids"] = target_test_ids

    report["rounds"] = []

    # 6. 首次保存报告
    report_path = save_checkpoint(report)

    print(f"任务报告：{report_path}")
    # project_path = r"E:\PythonProject\demo-python-project"

    # run_id = uuid4().hex
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

    # 将修复前测试的执行结果写入统一报告
    report['verification']['baseline']={
        "status": baseline_run["status"],
        "exit_code": baseline_run["exit_code"],
        "output": baseline_run["output"],
        "report_path":str(baseline_path),
        "test_results": None
    }

    # 保存当前进度
    save_checkpoint(report)

    # # 仅用于本次验证，验证后删除
    # baseline_run["status"] = "simulated_error"

    if baseline_run["status"] != "completed" or baseline_run["exit_code"] not in (0, 1):
        print("修复前测试未正常完成，无法建立测试基线。")
        print(f"工作副本：{working_project}")

        # 记录任务因基线测试无法完成而结束
        finish_report(report, final_status="baseline_test_error")
        return

    try:
        baseline_results = read_test_report(
            baseline_run["report_path"]
        )
    except (OSError, ValueError) as exc:
        print(f"读取修复前测试报告失败：{exc}")

        # 将失败原因写入报告，便于排查
        report["verification"]["baseline"]["report_error"] = str(exc)
        finish_report(report, final_status="baseline_report_error")

        return

    # 将解析后的测试状态写入报告
    report["verification"]["baseline"]["test_results"] = baseline_results

    print("\n===== 检查目标测试基线 =====")

    # 检查目标测试是否都存在于基线报告
    missing_targets = [
        test_id
        for test_id in target_test_ids
        if test_id not in baseline_results
    ]

    if missing_targets:
        print("以下目标测试不在基线报告中：")
        for test_id in missing_targets:
            print(f"  {test_id}")

        report["verification"]["target_baseline"] = {
            "status": "missing",
            "missing_tests": missing_targets,
            "test_results": None,
        }

        finish_report(report, final_status="target_tests_missing")
        return

    # 记录目标测试修复前的状态
    target_baseline_results = {
        test_id: baseline_results[test_id]
        for test_id in target_test_ids
    }

    report["verification"]["target_baseline"] = {
        "status": "recorded",
        "missing_tests": [],
        "test_results": target_baseline_results,
    }

    save_checkpoint(report)

    for test_id, status in target_baseline_results.items():
        print(f"  {test_id}: {status}")

    # # 保存更新
    # save_checkpoint(report)

    # 开始第一轮修复
    round_record = {
        "round_number" : 1,
        "status": "in_progress",
        "agent": None,
        "proposal": None,
        "application": None,
        "verification": None
    }

    report["rounds"].append(round_record)
    save_checkpoint(report)

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
        result = run_diagnosis(
            agent=agent,
            user_request=user_request
        )
    except GraphRecursionError:
        print("\nAgent 达到执行步数上限，分析未完成。")
        round_record["status"] = "agent_step_limit"
        finish_report(report, final_status="agent_step_limit")
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
    agent_report = build_run_report(
        user_request=user_request,
        messages=result["messages"],
    )

    # 只更新原有报告中的 Agent 信息。
    report["agent"] = agent_report["agent"]

    # 保存第一轮的Agent信息
    round_record["agent"] = deepcopy(report["agent"])

    # 使用原来的task_id保存信息
    report_path = save_checkpoint(report)

    # # 保存到原来的 JSON 文件。
    # report_path = save_checkpoint(report)

    print(f"任务报告已更新：{report_path}")

    print("\n===== 提取修改提案 =====")

    proposal_result = prepare_round_proposal(
        messages=result["messages"],
        report=report,
        round_record=round_record
    )

    if not proposal_result["success"]:
        print(proposal_result["message"])
        finish_report(report, final_status="proposal_unavailable")
        return

    proposal = proposal_result["proposal"]

    # 保存成功提取的提案
    save_checkpoint(report)


    print(f"工具调用 ID：{proposal['tool_call_id']}")
    print(f"目标文件：{proposal['relative_path']}")

    print("\n原文本：")
    print(proposal["old_text"])

    print("\n新文本：")
    print(proposal["new_text"])

    print("\n修改预览：")
    print(proposal["diff"])

    print("\n===== 确认并应用修改 =====")

    apply_result = apply_round_proposal(
        working_project=working_project,
        proposal=proposal,
        report=report,
        round_record=round_record
    )

    # 保存当前进度
    save_checkpoint(report)

    print(f"修改状态：{apply_result['status']}")
    print(f"说明：{apply_result['message']}")

    # 放在return之前是因为用户取消或者应用失败时，程序会直接return提前结束
    if apply_result["status"] != "applied":
        print(f"\n本次工作副本保存在：{working_project}")

        if apply_result["status"] == "rejected":
            round_record["status"] = "cancelled"
            finish_report(report, final_status="cancelled")
        else:
            round_record["status"] = "application_error"
            finish_report(report, final_status="application_error")

        return

    verification_result = verify_round(
        test_tools=test_tools,
        after_fix_path=after_fix_path,
        baseline_results=baseline_results,
        report=report,
        round_record=round_record,
    )

    if not verification_result["success"]:
        print(verification_result["message"])
        print(f"工作副本：{working_project}")

        finish_report(
            report,
            final_status=verification_result["status"],
        )

        return
    after_results = verification_result["after_results"]
    comparison = verification_result["comparison"]


    # 目前仍是单轮流程，先固定为第一轮
    current_round = 1
    max_rounds = 3

    decision = decide_next_step(
        target_test_ids=target_test_ids,
        baseline_results=baseline_results,
        after_results=after_results,
        current_round=current_round,
        max_rounds=max_rounds,
    )

    print("\n===== 下一步决策 =====")
    print(f"动作：{decision['action']}")
    print(f"原因：{decision['reason']}")
    print(f"详情：{decision['details']}")

    # 把决策保存到当前轮次
    round_record["decision"] = deepcopy(decision)

    # 记录修复前后的测试对比
    report["verification"]["comparison"] = comparison

    # 保存解析后的测试结果和对比结果
    round_record["verification"]["after_fix"] = deepcopy(
        report["verification"]["after_fix"]
    )
    round_record["verification"]["comparison"] = deepcopy(comparison)

    # 本轮流程均完成，不等同于所有测试都通过
    round_record["status"] = "completed"

    save_checkpoint(report)


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

    # 正常完成测试对比后，更新任务的结束状态
    report_path = finish_report(
        report,
        final_status="completed"
    )

    print(f"最终报任务报告：{report_path}")


    # print("\n===== 运行记录 =====")
    # print(f"模型消息数量：{report['model_message_count']}")
    # print(f"工具调用数量：{report['tool_call_count']}")
    # print(f"报告保存位置：{report_path.resolve()}")

if __name__ == "__main__":
    main()