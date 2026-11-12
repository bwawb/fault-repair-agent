from copy import deepcopy
from types import SimpleNamespace

import diagnosis_agent


def test_retry_then_target_passes(monkeypatch, tmp_path):
    """
    验证：
    第 1 轮目标仍失败，进入第 2 轮；
    第 2 轮目标通过，任务结束。
    """

    # 必须与 diagnosis_agent.py 中的 target_test_ids 保持一致。
    target = (
        "tests.test_order_service::"
        "test_create_order_with_discount"
    )

    # 一个原本就通过的测试，用于确认没有新增失败。
    passed_test = (
        "tests.test_order_service::"
        "test_create_order_without_discount"
    )

    baseline = {
        target: "failed",
        passed_test: "passed",
    }

    # 每次保存报告时，记录一份快照。
    saved_reports = []

    # 记录每轮传给 Agent 的任务内容。
    received_requests = []

    # 记录每轮使用的测试报告路径。
    after_report_paths = []

    # --------------------------------------------------
    # 1. 模拟工作副本
    # --------------------------------------------------

    working_project = tmp_path / "project"
    working_project.mkdir()

    monkeypatch.setattr(
        diagnosis_agent,
        "create_workspace",
        lambda source_project, workspace_root: working_project,
    )

    # --------------------------------------------------
    # 2. 模拟测试基线
    # --------------------------------------------------

    class FakeTestTools:
        def __init__(self, project_path):
            self.project_path = project_path

        def run_tests_with_report(self, report_path):
            # 这里模拟修复前测试结果。
            return {
                "status": "completed",
                "exit_code": 1,
                "output": "模拟：目标测试失败",
                "report_path": report_path,
            }

    monkeypatch.setattr(
        diagnosis_agent,
        "TestTools",
        FakeTestTools,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "read_test_report",
        lambda report_path: deepcopy(baseline),
    )

    # --------------------------------------------------
    # 3. 模拟任务报告的创建和保存
    # --------------------------------------------------

    def fake_build_run_report(
        user_request,
        messages,
        task_id=None,
        source_project=None,
        working_project=None,
        started_at=None,
    ):
        agent_data = {
            "model_message_count": 1 if messages else 0,
            "tool_call_count": 0,
            "tool_calls": [],
            "tool_results": [],
            "final_answer": "模拟分析结果",
        }

        # 初始化任务报告时会传入 task_id。
        if task_id is not None:
            return {
                "schema_version": "1.0",
                "task": {
                    "task_id": task_id,
                    "user_request": user_request,
                    "started_at": started_at,
                    "completed_at": None,
                },
                "workspace": {
                    "source_project": source_project,
                    "working_project": working_project,
                },
                "agent": agent_data,
                "proposal": {
                    "status": "not_generated",
                },
                "approval": {
                    "status": "not_requested",
                },
                "application": {
                    "status": "not_attempted",
                    "message": None,
                },
                "verification": {
                    "baseline": None,
                    "after_fix": None,
                    "comparison": None,
                },
                "final_status": "in_progress",
            }

        # 后续整理每轮 Agent 消息时，只需要 agent 字段。
        return {
            "agent": agent_data,
        }

    def fake_save_run_report(report):
        saved_reports.append(deepcopy(report))
        return tmp_path / "task_report.json"

    monkeypatch.setattr(
        diagnosis_agent,
        "build_run_report",
        fake_build_run_report,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "save_run_report",
        fake_save_run_report,
    )

    # --------------------------------------------------
    # 4. 模拟模型和 Agent
    # --------------------------------------------------

    monkeypatch.setattr(
        diagnosis_agent,
        "create_model",
        lambda: object(),
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "ToolRegistry",
        lambda project_path: object(),
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "create_langchain_tools",
        lambda registry: [],
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "create_agent",
        lambda **kwargs: object(),
    )

    def fake_run_diagnosis(agent, user_request):
        received_requests.append(user_request)

        # main() 会读取最后一条消息的 content。
        return {
            "messages": [
                SimpleNamespace(content="模拟 Agent 分析完成"),
            ],
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "run_diagnosis",
        fake_run_diagnosis,
    )

    # --------------------------------------------------
    # 5. 模拟生成提案和用户同意
    # --------------------------------------------------

    def fake_prepare_round_proposal(messages, report, round_record):
        round_number = round_record["round_number"]

        proposal = {
            "tool_call_id": f"call_round_{round_number}",
            "relative_path": "pricing.py",
            "old_text": f"old_round_{round_number}",
            "new_text": f"new_round_{round_number}",
            "diff": f"模拟第 {round_number} 轮修改",
        }

        report["proposal"] = {
            "status": "generated",
            **proposal,
        }

        round_record["proposal"] = deepcopy(report["proposal"])

        return {
            "success": True,
            "proposal": proposal,
        }

    def fake_apply_round_proposal(
        working_project,
        proposal,
        report,
        round_record,
    ):
        report["approval"] = {
            "status": "approved",
        }

        report["application"] = {
            "status": "applied",
            "message": "模拟用户同意并应用修改",
        }

        round_record["approval"] = deepcopy(report["approval"])
        round_record["application"] = deepcopy(
            report["application"]
        )

        return {
            "status": "applied",
            "approval_status": "approved",
            "message": "模拟用户同意并应用修改",
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "prepare_round_proposal",
        fake_prepare_round_proposal,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "apply_round_proposal",
        fake_apply_round_proposal,
    )

    # --------------------------------------------------
    # 6. 模拟每轮验证结果
    # --------------------------------------------------

    def fake_verify_round(
        test_tools,
        after_fix_path,
        baseline_results,
        report,
        round_record,
    ):
        round_number = round_record["round_number"]
        after_report_paths.append(str(after_fix_path))

        if round_number == 1:
            # 第 1 轮：目标仍然失败。
            after_results = {
                target: "failed",
                passed_test: "passed",
            }
        else:
            # 第 2 轮：目标恢复通过。
            after_results = {
                target: "passed",
                passed_test: "passed",
            }

        comparison = diagnosis_agent.compare_results(
            before=baseline_results,
            after=after_results,
        )

        after_fix = {
            "status": "completed",
            "exit_code": 1 if round_number == 1 else 0,
            "output": f"模拟第 {round_number} 轮测试输出",
            "report_path": str(after_fix_path),
            "test_results": deepcopy(after_results),
        }

        report["verification"]["after_fix"] = after_fix
        report["verification"]["comparison"] = comparison

        round_record["verification"] = {
            "after_fix": deepcopy(after_fix),
            "comparison": deepcopy(comparison),
        }

        return {
            "success": True,
            "after_results": after_results,
            "comparison": comparison,
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "verify_round",
        fake_verify_round,
    )

    # --------------------------------------------------
    # 7. 运行主程序并检查结果
    # --------------------------------------------------

    diagnosis_agent.main()

    # 取最后一次保存的报告。
    final_report = saved_reports[-1]
    rounds = final_report["rounds"]

    # 一共应该进行了两轮。
    assert len(rounds) == 2

    assert rounds[0]["round_number"] == 1
    assert rounds[0]["decision"]["action"] == "retry"
    assert rounds[0]["decision"]["reason"] == "targets_still_failing"

    assert rounds[1]["round_number"] == 2
    assert rounds[1]["decision"]["action"] == "stop"
    assert rounds[1]["decision"]["reason"] == "targets_passed"

    # 第二轮应收到上一轮反馈。
    assert len(received_requests) == 2
    assert "上一轮结果" in received_requests[1]
    assert "模拟第 1 轮测试输出" in received_requests[1]

    # 两轮报告路径不同。
    assert len(after_report_paths) == 2
    assert after_report_paths[0] != after_report_paths[1]
    assert "round_1" in after_report_paths[0]
    assert "round_2" in after_report_paths[1]

    # 最终目标测试通过，任务标记完成。
    assert final_report["final_status"] == "completed"


def test_stops_when_round_limit_is_reached(monkeypatch, tmp_path):
    """
    验证：
    第 1 轮目标仍失败，进入第 2 轮；
    第 2 轮目标通过，任务结束。
    """

    # 必须与 diagnosis_agent.py 中的 target_test_ids 保持一致。
    target = (
        "tests.test_order_service::"
        "test_create_order_with_discount"
    )

    # 一个原本就通过的测试，用于确认没有新增失败。
    passed_test = (
        "tests.test_order_service::"
        "test_create_order_without_discount"
    )

    baseline = {
        target: "failed",
        passed_test: "passed",
    }

    # 每次保存报告时，记录一份快照。
    saved_reports = []

    # 记录每轮传给 Agent 的任务内容。
    received_requests = []

    # 记录每轮使用的测试报告路径。
    after_report_paths = []

    # --------------------------------------------------
    # 1. 模拟工作副本
    # --------------------------------------------------

    working_project = tmp_path / "project"
    working_project.mkdir()

    monkeypatch.setattr(
        diagnosis_agent,
        "create_workspace",
        lambda source_project, workspace_root: working_project,
    )

    # --------------------------------------------------
    # 2. 模拟测试基线
    # --------------------------------------------------

    class FakeTestTools:
        def __init__(self, project_path):
            self.project_path = project_path

        def run_tests_with_report(self, report_path):
            # 这里模拟修复前测试结果。
            return {
                "status": "completed",
                "exit_code": 1,
                "output": "模拟：目标测试失败",
                "report_path": report_path,
            }

    monkeypatch.setattr(
        diagnosis_agent,
        "TestTools",
        FakeTestTools,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "read_test_report",
        lambda report_path: deepcopy(baseline),
    )

    # --------------------------------------------------
    # 3. 模拟任务报告的创建和保存
    # --------------------------------------------------

    def fake_build_run_report(
        user_request,
        messages,
        task_id=None,
        source_project=None,
        working_project=None,
        started_at=None,
    ):
        agent_data = {
            "model_message_count": 1 if messages else 0,
            "tool_call_count": 0,
            "tool_calls": [],
            "tool_results": [],
            "final_answer": "模拟分析结果",
        }

        # 初始化任务报告时会传入 task_id。
        if task_id is not None:
            return {
                "schema_version": "1.0",
                "task": {
                    "task_id": task_id,
                    "user_request": user_request,
                    "started_at": started_at,
                    "completed_at": None,
                },
                "workspace": {
                    "source_project": source_project,
                    "working_project": working_project,
                },
                "agent": agent_data,
                "proposal": {
                    "status": "not_generated",
                },
                "approval": {
                    "status": "not_requested",
                },
                "application": {
                    "status": "not_attempted",
                    "message": None,
                },
                "verification": {
                    "baseline": None,
                    "after_fix": None,
                    "comparison": None,
                },
                "final_status": "in_progress",
            }

        # 后续整理每轮 Agent 消息时，只需要 agent 字段。
        return {
            "agent": agent_data,
        }

    def fake_save_run_report(report):
        saved_reports.append(deepcopy(report))
        return tmp_path / "task_report.json"

    monkeypatch.setattr(
        diagnosis_agent,
        "build_run_report",
        fake_build_run_report,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "save_run_report",
        fake_save_run_report,
    )

    # --------------------------------------------------
    # 4. 模拟模型和 Agent
    # --------------------------------------------------

    monkeypatch.setattr(
        diagnosis_agent,
        "create_model",
        lambda: object(),
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "ToolRegistry",
        lambda project_path: object(),
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "create_langchain_tools",
        lambda registry: [],
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "create_agent",
        lambda **kwargs: object(),
    )

    def fake_run_diagnosis(agent, user_request):
        received_requests.append(user_request)

        # main() 会读取最后一条消息的 content。
        return {
            "messages": [
                SimpleNamespace(content="模拟 Agent 分析完成"),
            ],
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "run_diagnosis",
        fake_run_diagnosis,
    )

    # --------------------------------------------------
    # 5. 模拟生成提案和用户同意
    # --------------------------------------------------

    def fake_prepare_round_proposal(messages, report, round_record):
        round_number = round_record["round_number"]

        proposal = {
            "tool_call_id": f"call_round_{round_number}",
            "relative_path": "pricing.py",
            "old_text": f"old_round_{round_number}",
            "new_text": f"new_round_{round_number}",
            "diff": f"模拟第 {round_number} 轮修改",
        }

        report["proposal"] = {
            "status": "generated",
            **proposal,
        }

        round_record["proposal"] = deepcopy(report["proposal"])

        return {
            "success": True,
            "proposal": proposal,
        }

    def fake_apply_round_proposal(
        working_project,
        proposal,
        report,
        round_record,
    ):
        report["approval"] = {
            "status": "approved",
        }

        report["application"] = {
            "status": "applied",
            "message": "模拟用户同意并应用修改",
        }

        round_record["approval"] = deepcopy(report["approval"])
        round_record["application"] = deepcopy(
            report["application"]
        )

        return {
            "status": "applied",
            "approval_status": "approved",
            "message": "模拟用户同意并应用修改",
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "prepare_round_proposal",
        fake_prepare_round_proposal,
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "apply_round_proposal",
        fake_apply_round_proposal,
    )

    # --------------------------------------------------
    # 6. 模拟每轮验证结果
    # --------------------------------------------------

    def fake_verify_round(
        test_tools,
        after_fix_path,
        baseline_results,
        report,
        round_record,
    ):
        round_number = round_record["round_number"]
        after_report_paths.append(str(after_fix_path))

        # 模拟每一轮修复后目标测试仍然失败
        after_results = {
            target: "failed",
            passed_test: "passed",
        }

        comparison = diagnosis_agent.compare_results(
            before=baseline_results,
            after=after_results,
        )

        after_fix = {
            "status": "completed",
            "exit_code": 1,
            "output": f"模拟第 {round_number} 轮测试输出",
            "report_path": str(after_fix_path),
            "test_results": deepcopy(after_results),
        }

        report["verification"]["after_fix"] = after_fix
        report["verification"]["comparison"] = comparison

        round_record["verification"] = {
            "after_fix": deepcopy(after_fix),
            "comparison": deepcopy(comparison),
        }

        return {
            "success": True,
            "after_results": after_results,
            "comparison": comparison,
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "verify_round",
        fake_verify_round,
    )

    # --------------------------------------------------
    # 7. 运行主程序并检查结果
    # --------------------------------------------------

    diagnosis_agent.main()

    final_report = saved_reports[-1]
    rounds = final_report["rounds"]

    # 最多三轮，不应无限重试
    assert len(rounds) == 3

    assert rounds[0]["decision"]["action"] == "retry"
    assert rounds[0]["decision"]["reason"] == "targets_still_failing"

    assert rounds[1]["decision"]["action"] == "retry"
    assert rounds[1]["decision"]["reason"] == "targets_still_failing"

    assert rounds[2]["decision"]["action"] == "stop"
    assert rounds[2]["decision"]["reason"] == "round_limit_reached"

    # 目标没有修好，最终状态不能是 completed
    assert final_report["final_status"] == "round_limit_reached"

    # Agent 应被调用三次
    assert len(received_requests) == 3

    # 三轮验证应使用不同的报告路径
    assert len(after_report_paths) == 3
    assert len(set(after_report_paths)) == 3