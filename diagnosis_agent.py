import argparse

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from uuid import uuid4
from datetime import datetime
from copy import deepcopy
from pathlib import Path
import json


from demo_tool_call import create_model
from model_redaction import ModelContentRedaction, ModelRedactionError
from output_protection import OutputProtectionError, current_output, emit, protect_output
from tools.langchain_tools import create_langchain_tools
from tools.tool_registry import ToolRegistry
from run_report import build_run_report, save_run_report
from proposal import extract_latest_preview
from apply_proposal import confirm_and_apply
from tools.test_tools import REPORT_ERROR_STATUSES, TEST_RESULT_STATUSES, TestTools
from workspace import create_workspace, create_baseline_snapshot
from compare_test_reports import compare_results
from repair_decision import decide_next_step
from patch_summary import build_file_diff


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
5. 单处替换使用 preview_replace，同一文件的多处替换使用 preview_replacements，多个文件使用 preview_file_changes。
6. 最终回答应包含故障位置、原因、证据、修改预览和不确定之处。
7. 预览工具只生成预览，不会修改文件。
8. 不要声称修改已经应用，也不要声称修改后的测试已经通过。
9. 如果看到 [REDACTED]，表示该处内容被遮盖。请使用可完整核对的路径和文本；如无法准确定位，请说明原因。
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
        if "files" in proposal_result:
            files = deepcopy(proposal_result["files"])
            for file in files:
                if file["status"] == "prepared":
                    file["status"] = "not_attempted"
            report["application"].update(files=files, message=proposal_result["message"])
            round_record["application"] = deepcopy(report["application"])

        round_record["proposal"] = deepcopy(report["proposal"])
        round_record["status"] = "proposal_unavailable"

        return proposal_result

    proposal = proposal_result["proposal"]

    report["proposal"] = {"status": "generated", **deepcopy(proposal)}

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

    if "files" in apply_result:
        report["application"]["files"] = deepcopy(apply_result["files"])

    # 保存到当前轮次
    round_record["approval"] = deepcopy(report["approval"])
    round_record["application"] = deepcopy(report["application"])

    return apply_result


def record_cumulative_patches(working_project, baseline_project, apply_result, report, round_record):
    files = apply_result.get("files")
    if files is None:
        # 兼容只返回单文件路径和哈希的调用结果。
        files = [{
            "relative_path": Path(apply_result["file_path"]).relative_to(working_project).as_posix(),
            "status": "applied",
            "after_hash": apply_result["after_hash"],
        }]

    patches = {}
    for file in files:
        if file["status"] not in {"applied", "restore_failed"}:
            continue
        relative_path = file["relative_path"]
        file_hash = file["after_hash"]
        try:
            if file["status"] == "restore_failed" and file["current_hash"] != file_hash:
                file_hash = None
                raise ValueError("恢复失败后无法确认文件仍是本轮写入的版本。")
            diff = build_file_diff(str(baseline_project), str(working_project), relative_path)
        except (OSError, ValueError, RuntimeError) as exc:
            emit(f"{relative_path} 生成累计 diff 失败：{exc}")
            patch = {"status": "error", "diff": None, "message": str(exc)}
        else:
            emit(diff or f"{relative_path} 与基线快照相比没有内容变化。")
            patch = {"status": "generated" if diff else "unchanged", "diff": diff, "message": None}

        patch.update(relative_path=relative_path, file_hash=file_hash)
        report["patch_summary"]["files"][relative_path] = deepcopy(patch)
        patches[relative_path] = patch

    if "files" in round_record["proposal"]:
        round_record["cumulative_patches"] = patches
    elif patches:
        round_record["cumulative_patch"] = next(iter(patches.values()))

def show_test_run(result: dict) -> None:
    emit(f"运行状态：{result['status']}")
    emit(f"pytest 退出码：{result['exit_code']}")
    if result["output"]:
        emit(result["output"])
    if result["error"]:
        emit(f"测试错误：{result['error']}")
    xml = result.get("junit_xml", {})
    if xml.get("status") in {"retained", "cleanup_failed", "unverified"}:
        path = result.get("report_path") or xml.get("staging_path")
        emit(f"原始 JUnit XML 可能含有敏感内容。状态 {xml['status']}，路径 {path}")
    docker_xml = result.get("docker_xml_staging", {})
    if docker_xml.get("status") == "cleanup_failed":
        emit(f"Docker 原始 XML 临时目录清理失败：{docker_xml['path']}")


def verify_round(
        test_tools,
        after_fix_path,
        baseline_results: dict,
        report: dict,
        round_record: dict,
) -> dict:
    """
    运行修复后测试，并将已核验的测试结果与最初基线比较。
    """
    emit("\n===== 运行修复后测试 =====")

    after_run = test_tools.run_tests_with_report(
        report_path=str(after_fix_path)
    )
    current_output().register_test_ids(after_run["test_results"])

    show_test_run(after_run)
    report["verification"]["after_fix"] = after_run
    round_record["verification"] = {
        "after_fix": deepcopy(after_run),
        "comparison": None,
    }
    after_results = after_run["test_results"]
    if after_run["status"] not in TEST_RESULT_STATUSES or not after_results:
        status = (
            "after_report_error"
            if after_run["status"] in REPORT_ERROR_STATUSES else "after_test_error"
        )
        round_record["status"] = status
        return {
            "success": False,
            "status": status,
            "message": "修复后没有可靠的测试结果，无法比较。",
        }

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



def main(
    test_backend="local", docker_image=None, *, local_env_names=(),
    sensitive_values=(), user_request=None, keep_junit_xml=False,
    source_project=r"E:\PythonProject\demo-python-project",
    workspace_base=r"E:\PythonProject\fault-repair-agent\copy",
):
    model_redaction = ModelContentRedaction(sensitive_values)
    with protect_output(sensitive_values) as protection:
        try:
            return _run_task(test_backend, docker_image, local_env_names, model_redaction,
                             user_request, keep_junit_xml, str(source_project), str(workspace_base))
        except OutputProtectionError as exc:
            emit(str(exc))
            return "output_protection_error"
        except Exception:
            if not protection.enabled:
                raise
            emit("任务遇到异常，已隐藏原始错误内容。")
            return "task_error"


def _run_task(test_backend, docker_image, local_env_names, model_redaction, user_request, keep_junit_xml,
              source_project, workspace_base):
    # 1. 创建任务 ID
    run_id = uuid4().hex

    # 2. 记录任务开始时间
    started_at = datetime.now().astimezone().isoformat()

    # 3. 用户任务
    if user_request is None:
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
    working_project = create_workspace(source_project, workspace_base)

    emit(f"工作副本：{working_project}")

    baseline_project = create_baseline_snapshot(str(working_project))
    emit(f"基线快照：{baseline_project}")

    # 5. 初始化统一任务报告
    report = build_run_report(
        user_request=user_request,
        messages=[],
        task_id=run_id,
        source_project=source_project,
        working_project=str(working_project),
        started_at=started_at,
        baseline_project=str(baseline_project),
    )

    # 即使后续步骤失败，也能在报告中看到本次的修复目标
    report["task"]["target_test_ids"] = target_test_ids

    report["rounds"] = []

    # 6. 首次保存报告
    report_path = save_checkpoint(report)

    emit(f"任务报告：{report_path}")
    # project_path = r"E:\PythonProject\demo-python-project"

    # run_id = uuid4().hex
    # 报告保存在本次任务目录下，与 project 同级
    report_dir = working_project.parent / "reports"

    baseline_path = report_dir / f"baseline_{run_id}.xml"
    # after_fix_path = report_dir / f"after_fix_{run_id}.xml"

    test_tools = TestTools(
        str(working_project), execution_mode=test_backend, docker_image=docker_image,
        local_env_names=local_env_names, keep_junit_xml=keep_junit_xml,
    )
    emit("\n===== 运行修复前测试 =====")

    baseline_run = test_tools.run_tests_with_report(
        report_path=str(baseline_path),
    )
    current_output().register_test_ids(baseline_run["test_results"])

    show_test_run(baseline_run)
    report["verification"]["baseline"] = baseline_run
    save_checkpoint(report)

    baseline_results = baseline_run["test_results"]
    if baseline_run["status"] not in TEST_RESULT_STATUSES or not baseline_results:
        emit("修复前没有可靠的测试结果，无法建立基线。")
        emit(f"工作副本：{working_project}")
        final_status = (
            "baseline_report_error"
            if baseline_run["status"] in REPORT_ERROR_STATUSES else "baseline_test_error"
        )
        finish_report(report, final_status=final_status)
        return

    emit("\n===== 检查目标测试基线 =====")

    # 检查目标测试是否都存在于基线报告
    missing_targets = [
        test_id
        for test_id in target_test_ids
        if test_id not in baseline_results
    ]

    if missing_targets:
        emit("以下目标测试不在基线报告中：")
        for test_id in missing_targets:
            emit(f"  {test_id}")

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
        emit(f"  {test_id}: {status}")

    # 1.创建模型
    model = create_model()

    # 2.创建已有工具的注册表
    registry = ToolRegistry(str(working_project), test_tools=test_tools)

    # 3.将现有工具包装为LangChain工具
    tools = create_langchain_tools(registry)

    # 4.创建Agent
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[model_redaction] if model_redaction.enabled else [],
    )

    # 多轮修复的最大轮数
    max_rounds = 3

    # 第一轮使用原始用户任务
    round_request = user_request
    feedback = None
    feedback_prompt = ""

    # 保存初始状态，用于每一轮开始时重置顶层字段
    initial_agent = deepcopy(report["agent"])
    initial_proposal = deepcopy(report["proposal"])
    initial_approval = deepcopy(report["approval"])
    initial_application = deepcopy(report["application"])

    emit(f"\n最多进行 {max_rounds} 轮修复。")



    for current_round in range(1, max_rounds + 1):

        emit(f"\n========== 第 {current_round} 轮修复 ==========")

        # 创建当前轮次的独立记录
        round_record = {
            "round_number" : current_round,
            "status": "in_progress",
            "agent": None,
            "proposal": None,
            "application": None,
            "verification": None
        }

        # report["rounds"].append(round_record)
        # save_checkpoint(report)

        report["rounds"].append(round_record)

        # 顶层字段表示当前轮次的数据
        # 之前轮次的数据仍然保存在 report["rounds"] 中。

        report["agent"] = deepcopy(initial_agent)
        report["proposal"] = deepcopy(initial_proposal)
        report["approval"] = deepcopy(initial_approval)
        report["application"] = deepcopy(initial_application)

        report["verification"]["after_fix"] = None
        report["verification"]["comparison"] = None

        # 为当前轮次创建独立的测试报告路径
        after_fix_path = (report_dir/ f"after_fix_{run_id}_round_{current_round}.xml")

        save_checkpoint(report)


        emit("===== 开始故障分析 =====")
        emit(f"本轮任务：{round_request}")

        try:
            model_request = round_request
            if feedback is not None and model_redaction.enabled:
                safe_ids = current_output().with_test_aliases(feedback)
                model_request = feedback_prompt + model_redaction.redact_feedback(safe_ids)
            result = run_diagnosis(
                agent=agent,
                user_request=model_request,
            )
        except ModelRedactionError as exc:
            emit(str(exc))
            round_record.update(status="model_redaction_error", error=str(exc))
            finish_report(report, final_status="model_redaction_error")
            return
        except GraphRecursionError:
            emit("\nAgent 达到执行步数上限，分析未完成。")
            round_record["status"] = "agent_step_limit"
            finish_report(report, final_status="agent_step_limit")
            return

        # 6. 查看 Agent 运行过程中产生的消息。
        # emit("\n===== Agent 执行过程 =====")
        # for message in result["messages"]:
        #     if isinstance(message, AIMessage) and message.tool_calls:
        #         for tool_call in message.tool_calls:
        #             emit(f"\n模型请求工具：{tool_call['name']}")
        #             emit(f"工具参数：{tool_call['args']}")
        #     elif isinstance(message, ToolMessage):
        #         emit(f"\n工具返回，tool_call_id：{message.tool_call_id}")
        #         emit(message.content)

        # 7. 显示最终回答。
        emit("\n===== 最终故障分析 =====")
        emit(result["messages"][-1].content)

        # 整理 Agent 本次运行的消息。
        agent_report = build_run_report(
            user_request=round_request,
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

        emit(f"任务报告已更新：{report_path}")

        emit("\n===== 提取修改提案 =====")

        proposal_result = prepare_round_proposal(
            messages=result["messages"],
            report=report,
            round_record=round_record
        )

        if not proposal_result["success"]:
            emit(proposal_result["message"])
            finish_report(report, final_status="proposal_unavailable")
            return

        proposal = proposal_result["proposal"]

        # 保存成功提取的提案
        save_checkpoint(report)


        emit(f"工具调用 ID：{proposal['tool_call_id']}")
        target_files = proposal.get("files", [proposal])
        target_paths = [file["relative_path"] for file in target_files]
        emit("目标文件：" + "、".join(target_paths))

        emit("\n===== 确认并应用修改 =====")

        apply_result = apply_round_proposal(
            working_project=working_project,
            proposal=proposal,
            report=report,
            round_record=round_record
        )

        emit(f"修改状态：{apply_result['status']}")
        emit(f"说明：{apply_result['message']}")

        if apply_result["status"] in {"applied", "partial"}:
            emit("\n===== 当前文件的累计修改 =====")
            record_cumulative_patches(
                working_project, baseline_project, apply_result, report, round_record
            )

        if apply_result["status"] != "applied":
            emit(f"\n本次工作副本保存在：{working_project}")

            if apply_result["status"] == "rejected":
                round_record["status"] = "cancelled"
                finish_report(report, final_status="cancelled")
            else:
                final_status = {
                    "partial": "application_partial",
                    "approval_blocked": "approval_blocked",
                }.get(apply_result["status"], "application_error")
                round_record["status"] = final_status
                finish_report(report, final_status=final_status)

            return

        # 在运行修复后测试之前保存。
        save_checkpoint(report)

        verification_result = verify_round(
            test_tools=test_tools,
            after_fix_path=after_fix_path,
            baseline_results=baseline_results,
            report=report,
            round_record=round_record,
        )

        if not verification_result["success"]:
            emit(verification_result["message"])
            emit(f"工作副本：{working_project}")

            finish_report(
                report,
                final_status=verification_result["status"],
            )

            return
        after_results = verification_result["after_results"]
        comparison = verification_result["comparison"]

        # 根据当前轮结果决定停止还是重试
        decision = decide_next_step(
            target_test_ids=target_test_ids,
            baseline_results=baseline_results,
            after_results=after_results,
            current_round=current_round,
            max_rounds=max_rounds,
        )

        emit("\n===== 本轮测试结果对比 =====")
        emit(f"恢复通过：{comparison['fixed']}")
        emit(f"仍然失败：{comparison['still_failing']}")
        emit(f"新增失败：{comparison['newly_failed']}")
        emit(f"状态无法直接比较：{comparison['uncertain']}")
        emit(f"仅出现在修复前：{comparison['only_before']}")
        emit(f"仅出现在修复后：{comparison['only_after']}")

        emit("\n===== 下一步决策 =====")
        emit(f"动作：{decision['action']}")
        emit(f"原因：{decision['reason']}")
        emit(f"详情：{decision['details']}")

        # 保存当前轮决策
        round_record["decision"] = deepcopy(decision)

        # 本轮流程执行完毕；不代表所有测试都通过。
        round_record["status"] = "completed"

        save_checkpoint(report)

        # 如果决策为 stop，结束整个任务。
        if decision["action"] == "stop":

            if decision["reason"] == "targets_passed":
                final_status = "completed"
            else:
                # 例如 regression、round_limit_reached。
                final_status = decision["reason"]

            report_path = finish_report(
                report,
                final_status=final_status,
            )

            emit(f"\n任务结束：{final_status}")
            for label, run in (("修复前", baseline_run), ("本轮修复后", report["verification"]["after_fix"])):
                if run and run.get("junit_xml", {}).get("status") == "retained":
                    emit(f"{label}报告：{run['report_path']}")
            emit(f"工作副本：{working_project}")
            emit(f"最终任务报告：{report_path}")

            return

        # 防止未知决策动作被误认为 retry。
        if decision["action"] != "retry":
            round_record["status"] = "invalid_decision"

            finish_report(
                report,
                final_status="invalid_decision",
            )

            emit(f"\n无法识别的决策动作：{decision['action']}")
            return

        # 只有 decision["action"] == "retry" 才会执行到这里。
        # 将当前轮结果提供给下一轮 Agent。
        feedback = {
            "previous_round": current_round,
            "target_test_ids": target_test_ids,
            "previous_change": {"relative_paths": target_paths, "diff": proposal["diff"]}
            if "files" in proposal else {
                "relative_path": proposal["relative_path"], "diff": proposal["diff"],
            },
            "test_results": after_results,
            "comparison_with_baseline": comparison,
            "decision": decision,
            "test_output": report["verification"]["after_fix"]["output"],
        }

        feedback_prompt = (
            f"{user_request}\n\n"
            f"这是第 {current_round + 1} 轮修复。\n"
            "上一轮的修改已经应用到当前工作副本，"
            "但目标测试仍然失败。\n"
            "请结合上一轮结果重新分析。\n"
            "生成新提案前，请重新读取当前工作副本中的相关文件。\n"
            "继续只处理原任务指定的目标。\n\n"
            "上一轮结果：\n"
        )
        round_request = feedback_prompt + json.dumps(feedback, ensure_ascii=False, indent=2)

        emit(
            f"\n目标测试仍未通过，"
            f"准备进入第 {current_round + 1} 轮。"
        )

    # 这里在 for 循环外、main() 内。
    # 正常情况下，最后一轮应返回 stop。
    finish_report(
        report,
        final_status="loop_ended_unexpectedly",
    )

    emit("\n修复循环异常结束，请检查轮次决策。")


    # emit("\n===== 运行记录 =====")
    # emit(f"模型消息数量：{report['model_message_count']}")
    # emit(f"工具调用数量：{report['tool_call_count']}")
    # emit(f"报告保存位置：{report_path.resolve()}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行故障诊断和修复任务。")
    parser.add_argument("--test-backend", choices=("local", "docker"), default="local")
    parser.add_argument("--docker-image", help="预先准备好的本地 Linux Python/pytest 镜像")
    parser.add_argument("--keep-junit-xml", action="store_true", help="保留可能含有敏感内容的原始 JUnit XML")
    parser.add_argument(
        "--test-env", action="append", default=[], metavar="NAME",
        help="传入一个已设置的环境变量，仅适用于 local，可重复指定",
    )
    options = parser.parse_args()
    if (options.test_backend == "docker") != bool(options.docker_image):
        parser.error("--test-backend docker 必须与 --docker-image 一起使用。")
    if options.test_backend == "docker" and options.test_env:
        parser.error("--test-env 仅适用于 local 后端。")
    status = main(test_backend=options.test_backend, docker_image=options.docker_image,
                  local_env_names=options.test_env, keep_junit_xml=options.keep_junit_xml)
    if status:
        raise SystemExit(1)
