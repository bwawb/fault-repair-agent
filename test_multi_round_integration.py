import json
from functools import partial
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import diagnosis_agent

from rollback_file import rollback_file
from tools.edit_tools import EditTools


@pytest.mark.parametrize(
    ("round_count", "diff_fails"),
    [(1, False), (2, False), (2, True)],
    ids=["one-round", "two-rounds", "missing-baseline-file"],
)
def test_cumulative_patch_report(monkeypatch, tmp_path, round_count, diff_fails):
    """用真实补丁、pytest 和 JSON 验证累计 diff 的落盘。"""

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
        "    return original_price * (1 - discount_rate)"
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
        lambda project_path, **kwargs: object(),
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
            relative_path = "pricing.py"
            old_text = "    discount_rate = discount_percent // 100"
            new_text = (
                "    discount_rate = discount_percent / 100"
                if round_count == 1
                else "    discount_rate = discount_percent // 50"
            )

        elif proposal_number == 2 and round_count == 2:
            relative_path = "./pricing.py"
            old_text = "    discount_rate = discount_percent // 50"
            new_text = "    discount_rate = discount_percent / 100"

        else:
            raise AssertionError("预期最多只需要两轮提案。")

        # 真实生成预览，保证 confirm_and_apply() 的 diff 校验可以通过。
        edit_tools = EditTools(str(working_project))

        preview = edit_tools.preview_replace(
            relative_path=relative_path,
            old_text=old_text,
            new_text=new_text,
        )

        assert preview["success"], preview.get("message")

        return {
            "success": True,
            "proposal": {
                "tool_call_id": f"controlled_call_{proposal_number}",
                "relative_path": relative_path,
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
    # 6. 模拟人工确认，必要时制造基线文件读取失败。
    #
    # confirm_and_apply() 本身仍然真实执行：
    # 重新预览、检查 diff、检查文件变化、写入工作副本。
    # =========================================================

    def approve_proposal(prompt=""):
        if diff_fails and proposal_number == round_count:
            baseline_file = (
                workspace_holder["project"].parent / "baseline_project" / "pricing.py"
            )
            baseline_file.unlink()
        return "yes"

    monkeypatch.setattr("builtins.input", approve_proposal)

    # =========================================================
    # 7. 运行真正的 main()
    # =========================================================

    diagnosis_agent.main(keep_junit_xml=True)

    # =========================================================
    # 8. 读取真正保存的任务报告
    # =========================================================

    report_files = list(run_report_dir.glob("task_*.json"))

    assert len(report_files) == 1

    with report_files[0].open("r", encoding="utf-8") as file:
        report = json.load(file)

    rounds = report["rounds"]

    # =========================================================
    # 9. 验证轮次决策和 pytest 结果。
    # =========================================================

    assert len(rounds) == proposal_number == len(received_requests) == round_count
    assert rounds[-1]["decision"]["action"] == "stop"
    assert rounds[-1]["decision"]["reason"] == "targets_passed"
    assert report["final_status"] == "completed"

    if round_count == 2:
        assert rounds[0]["decision"]["action"] == "retry"
        assert rounds[0]["decision"]["reason"] == "targets_still_failing"
        assert "上一轮结果" in received_requests[1]
        assert "// 50" in received_requests[1]

    xml_paths = set()
    for number, round_record in enumerate(rounds, start=1):
        assert round_record["round_number"] == number
        assert round_record["approval"]["status"] == "approved"
        assert round_record["application"]["status"] == "applied"

        verification = round_record["verification"]
        after = verification["after_fix"]
        target_passed = number == round_count
        assert after["test_results"][target_test_id] == ("passed" if target_passed else "failed")
        assert after["exit_code"] == (0 if target_passed else 1)
        assert verification["comparison"]["newly_failed"] == []
        xml_path = Path(after["report_path"])
        assert xml_path.is_file()
        xml_paths.add(xml_path)

    assert len(xml_paths) == round_count

    # =========================================================
    # 10. 验证报告中的累计 diff 和快照路径。
    # =========================================================

    working_project = workspace_holder["project"]
    baseline_project = Path(report["workspace"]["baseline_project"])
    assert baseline_project == working_project.parent / "baseline_project"
    assert baseline_project.is_dir()
    if not diff_fails:
        assert (baseline_project / "pricing.py").read_text(encoding="utf-8") == original_code

    latest_patch = rounds[-1]["cumulative_patch"]
    assert report["patch_summary"]["files"] == {"pricing.py": latest_patch}
    assert report["rollbacks"] == []

    for number, round_record in enumerate(rounds, start=1):
        patch = round_record["cumulative_patch"]
        assert patch["relative_path"] == "pricing.py"
        if diff_fails and number == round_count:
            assert patch["status"] == "error"
            assert patch["diff"] is None
            assert "基线快照中没有找到文件" in patch["message"]
            continue

        assert patch["status"] == "generated"
        assert patch["message"] is None
        assert patch["diff"].startswith("--- baseline/pricing.py\n+++ project/pricing.py\n")
        added_text = "discount_percent / 100" if number == round_count else "discount_percent // 50"
        changed_lines = [line for line in patch["diff"].splitlines()[3:] if line.startswith(("-", "+"))]
        assert changed_lines == [
            "-    discount_rate = discount_percent // 100",
            f"+    discount_rate = {added_text}",
        ]
        assert patch["diff"].endswith("\n\\ No newline at end of file\n")

    working_file = working_project / "pricing.py"
    assert "discount_percent / 100" in working_file.read_text(encoding="utf-8")
    assert latest_patch["file_hash"] == sha256(working_file.read_bytes()).hexdigest()
    assert source_file.read_text(encoding="utf-8") == original_code

    if not diff_fails:
        baseline_bytes = (baseline_project / "pricing.py").read_bytes()
        result = rollback_file(report_files[0], "pricing.py")
        assert result["status"] == "rolled_back"
        assert working_file.read_bytes() == baseline_bytes == source_file.read_bytes()

        updated_report = json.loads(report_files[0].read_text(encoding="utf-8"))
        assert updated_report["rollbacks"] == [result]
        assert updated_report["rounds"] == rounds
        assert updated_report["patch_summary"]["files"]["pricing.py"] == {
            "status": "unchanged",
            "relative_path": "pricing.py",
            "diff": "",
            "message": None,
            "file_hash": sha256(baseline_bytes).hexdigest(),
        }


@pytest.mark.parametrize("second_approval", ["yes", "no"], ids=["approved", "rejected"])
def test_cumulative_patch_report_across_files(monkeypatch, tmp_path, second_approval):
    changes = {
        "pricing.py": (
            "BASE_PRICE = 100\n# 原价\nCURRENCY = 'USD'",
            "BASE_PRICE = 200\n# 修复原价\nCURRENCY = 'CNY'",
        ),
        "rules/discounts.py": ("DISCOUNT_PERCENT = 10", "DISCOUNT_PERCENT = 20"),
    }
    source_project = tmp_path / "source_project"
    for relative_path, (original, _) in changes.items():
        source_file = source_project / relative_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes((original + "\n").replace("\n", "\r\n").encode("utf-8"))

    tests_dir = source_project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_order_service.py").write_text(
        "from pricing import BASE_PRICE\n"
        "from rules.discounts import DISCOUNT_PERCENT\n\n"
        "def test_create_order_with_discount():\n"
        "    assert BASE_PRICE * (100 - DISCOUNT_PERCENT) // 100 == 160\n\n"
        "def test_create_order_without_discount():\n"
        "    assert BASE_PRICE > 0\n",
        encoding="utf-8",
    )

    working_project = diagnosis_agent.create_workspace(
        str(source_project), str(tmp_path / "workspaces")
    )
    report_dir = tmp_path / "run_reports"
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: working_project)
    monkeypatch.setattr(
        diagnosis_agent,
        "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(report_dir)),
    )
    monkeypatch.setattr(diagnosis_agent, "create_model", object)
    monkeypatch.setattr(diagnosis_agent, "create_agent", lambda **_: object())

    registry = diagnosis_agent.ToolRegistry(str(working_project))
    preview_tools = {tool.name: tool for tool in diagnosis_agent.create_langchain_tools(registry)}
    proposals = iter(changes.items())
    pricing_replacements = [
        {"old_text": "BASE_PRICE = 100\n# 原价", "new_text": "BASE_PRICE = 200\n# 修复原价"},
        {"old_text": "CURRENCY = 'USD'", "new_text": "CURRENCY = 'CNY'"},
    ]

    def fake_run_diagnosis(agent, user_request):
        relative_path, (old_text, new_text) = next(proposals)
        if relative_path == "pricing.py":
            tool_name = "preview_replacements"
            arguments = {"relative_path": relative_path, "replacements": pricing_replacements}
        else:
            tool_name = "preview_replace"
            arguments = {"relative_path": relative_path, "old_text": old_text, "new_text": new_text}
        tool_output = preview_tools[tool_name].invoke(arguments)
        preview = json.loads(tool_output)
        assert preview["success"] and preview["result"]["success"], preview

        return {"messages": [
            AIMessage(content="", tool_calls=[{
                "name": tool_name, "args": arguments, "id": "preview_call",
            }]),
            ToolMessage(content=tool_output, tool_call_id="preview_call"),
            AIMessage(content="已生成修复预览。"),
        ]}

    monkeypatch.setattr(diagnosis_agent, "run_diagnosis", fake_run_diagnosis)
    answers = iter(("yes", second_approval))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    diagnosis_agent.main()
    assert next(answers, None) is None

    report_path, = report_dir.glob("task_*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    first_round, second_round = report["rounds"]
    assert report["rollbacks"] == []
    target_test_id = "tests.test_order_service::test_create_order_with_discount"
    assert report["verification"]["baseline"]["test_results"][target_test_id] == "failed"
    assert first_round["application"]["status"] == "applied"
    assert first_round["decision"]["action"] == "retry"
    assert first_round["verification"]["comparison"]["still_failing"] == [target_test_id]
    assert second_round["proposal"]["relative_path"] == "rules/discounts.py"

    baseline_project = Path(report["workspace"]["baseline_project"])
    expected_patches = {}
    for relative_path, (original, updated) in changes.items():
        baseline_text = (baseline_project / relative_path).read_text(encoding="utf-8")
        working_bytes = (working_project / relative_path).read_bytes()
        assert baseline_text == original + "\n"
        if relative_path == "rules/discounts.py" and second_approval == "no":
            assert working_bytes == (baseline_project / relative_path).read_bytes()
            continue

        assert working_bytes == (updated + "\n").replace("\n", "\r\n").encode("utf-8")
        old_lines = original.splitlines()
        new_lines = updated.splitlines()
        old_range = "1" if len(old_lines) == 1 else f"1,{len(old_lines)}"
        new_range = "1" if len(new_lines) == 1 else f"1,{len(new_lines)}"
        expected_patches[relative_path] = {
            "status": "generated",
            "relative_path": relative_path,
            "diff": (
                f"--- baseline/{relative_path}\n"
                f"+++ project/{relative_path}\n"
                f"@@ -{old_range} +{new_range} @@\n"
                + "".join(f"-{line}\n" for line in old_lines)
                + "".join(f"+{line}\n" for line in new_lines)
            ),
            "message": None,
            "file_hash": sha256((working_project / relative_path).read_bytes()).hexdigest(),
        }

    assert report["patch_summary"]["files"] == expected_patches
    assert first_round["cumulative_patch"] == expected_patches["pricing.py"]
    assert first_round["proposal"]["replacements"] == pricing_replacements
    assert second_round["proposal"]["old_text"] == changes["rules/discounts.py"][0]
    assert second_round["proposal"]["new_text"] == changes["rules/discounts.py"][1]
    assert first_round["proposal"]["diff"].splitlines()[2:] == (
        first_round["cumulative_patch"]["diff"].splitlines()[2:]
    )
    if second_approval == "no":
        assert report["final_status"] == "cancelled"
        assert second_round["approval"]["status"] == "rejected"
        assert second_round["application"]["status"] == "not_attempted"
        assert "cumulative_patch" not in second_round
        assert second_round["verification"] is None
    else:
        assert report["final_status"] == "completed"
        assert second_round["application"]["status"] == "applied"
        assert second_round["decision"]["reason"] == "targets_passed"
        assert second_round["verification"]["comparison"]["fixed"] == [target_test_id]
        assert second_round["verification"]["comparison"]["newly_failed"] == []
        assert second_round["cumulative_patch"] == expected_patches["rules/discounts.py"]

        other_file = working_project / "rules/discounts.py"
        other_contents = other_file.read_bytes()
        result = rollback_file(report_path, "pricing.py")
        assert result["status"] == "rolled_back"
        original_bytes = (source_project / "pricing.py").read_bytes()
        assert (working_project / "pricing.py").read_bytes() == original_bytes
        assert (baseline_project / "pricing.py").read_bytes() == original_bytes
        assert other_file.read_bytes() == other_contents

        updated_report = json.loads(report_path.read_text(encoding="utf-8"))
        updated_patches = updated_report["patch_summary"]["files"]
        assert updated_patches["rules/discounts.py"] == expected_patches["rules/discounts.py"]
        assert updated_patches["pricing.py"]["diff"] == ""
        assert updated_report["verification"] == report["verification"]
