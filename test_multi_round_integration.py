import json
from pathlib import Path

from langchain_core.messages import AIMessage

import diagnosis_agent

from tools.edit_tools import EditTools


def test_two_rounds_with_real_patch_and_pytest(monkeypatch, tmp_path):
    """
    集成测试：

    第 1 轮：
        真正应用修改，但目标测试仍然失败。

    第 2 轮：
        在同一个工作副本上再次修改，目标测试通过。

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

    answers = iter(["yes", "yes"])

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
    # 9. 验证确实执行了两轮
    # =========================================================

    assert len(rounds) == 2
    assert proposal_number == 2
    assert len(received_requests) == 2

    assert rounds[0]["round_number"] == 1
    assert rounds[1]["round_number"] == 2

    # 第一轮目标仍然失败，因此重试。
    assert rounds[0]["decision"]["action"] == "retry"
    assert (
        rounds[0]["decision"]["reason"]
        == "targets_still_failing"
    )

    # 第二轮目标通过，因此停止。
    assert rounds[1]["decision"]["action"] == "stop"
    assert rounds[1]["decision"]["reason"] == "targets_passed"

    assert report["final_status"] == "completed"

    # 第二轮收到了第一轮反馈。
    assert "上一轮结果" in received_requests[1]
    assert "// 50" in received_requests[1]

    # =========================================================
    # 10. 验证每轮真实应用状态和测试结果
    # =========================================================

    assert rounds[0]["approval"]["status"] == "approved"
    assert rounds[1]["approval"]["status"] == "approved"

    assert rounds[0]["application"]["status"] == "applied"
    assert rounds[1]["application"]["status"] == "applied"

    first_after = rounds[0]["verification"]["after_fix"]
    second_after = rounds[1]["verification"]["after_fix"]

    assert first_after["test_results"][target_test_id] == "failed"
    assert second_after["test_results"][target_test_id] == "passed"

    assert first_after["exit_code"] == 1
    assert second_after["exit_code"] == 0

    # 两轮都没有引入新增失败。
    assert rounds[0]["verification"]["comparison"]["newly_failed"] == []
    assert rounds[1]["verification"]["comparison"]["newly_failed"] == []

    # 每轮 XML 文件真实存在，并且不是同一个文件。
    first_xml = Path(first_after["report_path"])
    second_xml = Path(second_after["report_path"])

    assert first_xml.is_file()
    assert second_xml.is_file()
    assert first_xml != second_xml

    # =========================================================
    # 11. 验证工作副本修改成功，原始项目未被修改
    # =========================================================

    working_project = workspace_holder["project"]
    repaired_file = working_project / "pricing.py"

    repaired_code = repaired_file.read_text(encoding="utf-8")
    source_code_after = source_file.read_text(encoding="utf-8")

    assert "discount_percent / 100" in repaired_code

    # 原始示例项目仍是修复前内容。
    assert source_code_after == original_code