import json
from pathlib import Path

from langchain_core.messages import AIMessage

import diagnosis_agent

from tools.edit_tools import EditTools


def test_reject_second_round_proposal(monkeypatch, tmp_path):
    """
    集成测试：

    第 1 轮：
        真正应用修改，但目标测试仍然失败。

    第 2 轮：
        拒绝下一次提案，保留第一轮已应用的修改和累计 diff。

    测试中不调用真实模型，
    但会真实应用补丁、执行 pytest、生成并解析 XML 报告。
    """

    # =========================================================
    # 1. 构造一个待修复的临时示例项目
    # =========================================================

    source_project = tmp_path / "source_project"
    source_project.mkdir()

    tests_dir = source_project / "tests"
    tests_dir.mkdir()

    original_code = (
        "def calculate_final_price(original_price, discount_percent):\n"
        "    discount_rate = discount_percent // 100\n"
        "    return original_price * (1 - discount_rate)\n"
    )

    source_file = source_project / "pricing.py"
    source_file.write_text(original_code, encoding="utf-8")

    test_code = (
        "from pricing import calculate_final_price\n"
        "\n"
        "\n"
        "def test_create_order_with_discount():\n"
        "    assert calculate_final_price(200, 20) == 160\n"
        "\n"
        "\n"
        "def test_create_order_without_discount():\n"
        "    assert calculate_final_price(200, 0) == 200\n"
    )

    test_file = tests_dir / "test_order_service.py"
    test_file.write_text(test_code, encoding="utf-8")

    # 这个名称需要与你 main() 中的 target_test_ids 一致。
    target_test_id = (
        "tests.test_order_service::"
        "test_create_order_with_discount"
    )

    # =========================================================
    # 2. 使用真实 create_workspace，但指定测试用源项目和目录
    # =========================================================

    real_create_workspace = diagnosis_agent.create_workspace

    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    workspace_holder = {}

    def create_test_workspace(_source_project, _workspace_root):
        working_project = real_create_workspace(
            str(source_project),
            str(workspace_root),
        )

        # 记录真实创建的工作副本路径，供后面断言使用。
        workspace_holder["project"] = Path(working_project)

        return working_project

    monkeypatch.setattr(
        diagnosis_agent,
        "create_workspace",
        create_test_workspace,
    )

    # =========================================================
    # 3. 使用真实报告保存函数，但把报告放在测试临时目录
    # =========================================================

    real_save_run_report = diagnosis_agent.save_run_report

    run_report_dir = tmp_path / "run_reports"

    def save_test_report(report):
        return real_save_run_report(
            report,
            output_dir=str(run_report_dir),
        )

    monkeypatch.setattr(
        diagnosis_agent,
        "save_run_report",
        save_test_report,
    )

    # =========================================================
    # 4. 不创建真实模型，避免调用 API
    # =========================================================

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

    # 保存每轮发给 Agent 的任务，检查第二轮是否收到反馈。
    received_requests = []

    def fake_run_diagnosis(agent, user_request):
        received_requests.append(user_request)

        return {
            "messages": [
                AIMessage(content="集成测试：使用预设提案。"),
            ],
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "run_diagnosis",
        fake_run_diagnosis,
    )

    # =========================================================
    # 5. 用预设内容代替从模型消息提取的提案
    #
    # 注意：
    # 实际的 prepare_round_proposal() 仍然执行；
    # 只替换它内部调用的 extract_latest_preview()。
    # =========================================================

    proposal_number = 0

    def controlled_extract_latest_preview(messages):
        nonlocal proposal_number

        proposal_number += 1

        working_project = workspace_holder["project"]

        if proposal_number == 1:
            old_text = "    discount_rate = discount_percent // 100"
            new_text = "    discount_rate = discount_percent // 50"

        elif proposal_number == 2:
            old_text = "    discount_rate = discount_percent // 50"
            new_text = "    discount_rate = discount_percent / 100"

        else:
            raise AssertionError("预期最多只需要两轮提案。")

        # 真实生成预览，保证 confirm_and_apply() 的 diff 校验可以通过。
        edit_tools = EditTools(str(working_project))

        preview = edit_tools.preview_replace(
            relative_path="pricing.py",
            old_text=old_text,
            new_text=new_text,
        )

        assert preview["success"], preview.get("message")

        return {
            "success": True,
            "proposal": {
                "tool_call_id": f"controlled_call_{proposal_number}",
                "relative_path": "pricing.py",
                "old_text": old_text,
                "new_text": new_text,
                "diff": preview["diff"],
            },
        }

    monkeypatch.setattr(
        diagnosis_agent,
        "extract_latest_preview",
        controlled_extract_latest_preview,
    )

    # =========================================================
    # 6. 模拟人工两次输入 yes
    #
    # confirm_and_apply() 本身仍然真实执行：
    # 重新预览、检查 diff、检查文件变化、写入工作副本。
    # =========================================================

    answers = iter(["yes", "no"])

    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": next(answers),
    )

    # =========================================================
    # 7. 运行真正的 main()
    # =========================================================

    diagnosis_agent.main()

    # =========================================================
    # 8. 读取真正保存的任务报告
    # =========================================================

    report_files = list(run_report_dir.glob("task_*.json"))

    assert len(report_files) == 1

    with report_files[0].open("r", encoding="utf-8") as file:
        report = json.load(file)

    rounds = report["rounds"]

    # =========================================================
    # 9. 验证任务在第二轮取消
    # =========================================================

    assert len(rounds) == 2
    assert proposal_number == 2
    assert len(received_requests) == 2

    first_round = rounds[0]
    second_round = rounds[1]

    assert first_round["round_number"] == 1
    assert second_round["round_number"] == 2

    # 第一轮目标测试仍失败，因此进入第二轮。
    assert first_round["decision"]["action"] == "retry"
    assert (
            first_round["decision"]["reason"]
            == "targets_still_failing"
    )

    # 用户在第二轮拒绝提案，任务应取消。
    assert second_round["status"] == "cancelled"
    assert report["final_status"] == "cancelled"

    # =========================================================
    # 10. 验证两轮的确认和应用状态
    # =========================================================

    # 第一轮确认并应用。
    assert first_round["approval"]["status"] == "approved"
    assert first_round["application"]["status"] == "applied"

    # 第二轮拒绝，不应应用修改。
    assert second_round["approval"]["status"] == "rejected"
    assert second_round["application"]["status"] == "not_attempted"

    first_patch = first_round["cumulative_patch"]
    assert first_patch["status"] == "generated"
    assert first_patch["relative_path"] == "pricing.py"
    assert "-    discount_rate = discount_percent // 100\n" in first_patch["diff"]
    assert "+    discount_rate = discount_percent // 50\n" in first_patch["diff"]
    assert "cumulative_patch" not in second_round
    assert report["patch_summary"]["files"] == {"pricing.py": first_patch}

    # =========================================================
    # 11. 验证第二轮没有执行修复后测试
    # =========================================================

    first_after = first_round["verification"]["after_fix"]

    assert first_after["test_results"][target_test_id] == "failed"

    # 第一轮的 XML 报告存在。
    first_xml = Path(first_after["report_path"])
    assert first_xml.is_file()

    # 第二轮取消前没有执行 verify_round()。
    assert second_round["verification"] is None

    # 顶层当前轮的验证结果也应为空。
    assert report["verification"]["after_fix"] is None

    # 不应产生第二轮的修复后 XML。
    report_dir = workspace_holder["project"].parent / "reports"

    second_round_xml_files = list(
        report_dir.glob("*_round_2.xml")
    )

    assert second_round_xml_files == []

    # =========================================================
    # 12. 验证第一轮修改保留，第二轮修改未应用
    # =========================================================

    working_project = workspace_holder["project"]
    repaired_file = working_project / "pricing.py"

    working_code = repaired_file.read_text(encoding="utf-8")

    # 第一轮的修改仍在工作副本中。
    assert "discount_percent // 50" in working_code

    # 第二轮提案被拒绝，没有应用。
    assert "discount_percent / 100" not in working_code

    # 原始项目没有被修改。
    source_code_after = source_file.read_text(encoding="utf-8")
    assert source_code_after == original_code
