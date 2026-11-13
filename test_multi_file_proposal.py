import json
from functools import partial
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import apply_proposal
import diagnosis_agent
from rollback_file import rollback_file
from tools.edit_tools import EditTools


@pytest.mark.parametrize(
    "scenario",
    ["success", "invalid", "rejected", "changed", "restored", "partial", "external", "escape", "cross_task", "duplicate"],
)
def test_multi_file_proposal_and_report(tmp_path, monkeypatch, scenario):
    source = tmp_path / "source"
    tests = source / "tests"
    tests.mkdir(parents=True)
    original = {"pricing.py": b"PRICE = 100\r\nCURRENCY = 'USD'\r\n", "discount.py": b"RATE = 10\n"}
    modified = {"pricing.py": b"PRICE = 200\r\nCURRENCY = 'CNY'\r\n", "discount.py": b"RATE = 20\n"}
    for name, content in original.items():
        (source / name).write_bytes(content)
    (tests / "test_order_service.py").write_text(
        "from pricing import PRICE\nfrom discount import RATE\n\n"
        "def test_create_order_with_discount():\n"
        "    assert PRICE * (100 - RATE) // 100 == 160\n",
        encoding="utf-8",
    )
    working = diagnosis_agent.create_workspace(str(source), str(tmp_path / "workspaces"))
    other_task = diagnosis_agent.create_workspace(str(source), str(tmp_path / "workspaces"))
    report_dir = tmp_path / "reports"
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: working)
    monkeypatch.setattr(diagnosis_agent, "create_model", object)
    monkeypatch.setattr(diagnosis_agent, "create_agent", lambda **_: object())
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report", partial(diagnosis_agent.save_run_report, output_dir=str(report_dir))
    )

    files = [
        {"relative_path": "pricing.py", "replacements": [
            {"old_text": "PRICE = 100", "new_text": "PRICE = 200"},
            {"old_text": "CURRENCY = 'USD'", "new_text": "CURRENCY = 'CNY'"},
        ]},
        {"relative_path": "discount.py", "replacements": [{"old_text": "RATE = 10", "new_text": "RATE = 20"}]},
    ]
    if scenario == "invalid":
        files[1]["replacements"][0]["old_text"] = "RATE = 999"
    if scenario == "escape":
        files[1]["relative_path"] = "../baseline_project/discount.py"
    if scenario == "cross_task":
        files[1]["relative_path"] = f"../../{other_task.parent.name}/project/discount.py"
    if scenario == "duplicate":
        files[1]["relative_path"] = "./pricing.py"

    registry = diagnosis_agent.ToolRegistry(str(working))
    tool = next(tool for tool in diagnosis_agent.create_langchain_tools(registry) if tool.name == "preview_file_changes")

    def fake_run_diagnosis(agent, user_request):
        arguments = {"files": files}
        return {"messages": [
            AIMessage(content="", tool_calls=[{"name": tool.name, "args": arguments, "id": "preview"}]),
            ToolMessage(content=tool.invoke(arguments), tool_call_id="preview"),
            AIMessage(content="已运行补丁预览工具。"),
        ]}

    monkeypatch.setattr(diagnosis_agent, "run_diagnosis", fake_run_diagnosis)
    prompts = []
    external_edit = b"# external edit\n"

    def confirm(prompt):
        prompts.append(prompt)
        if scenario == "changed":
            (working / "discount.py").write_bytes(external_edit)
        return "no" if scenario == "rejected" else "yes"

    monkeypatch.setattr("builtins.input", confirm)
    replacements = []
    replace = apply_proposal._replace_file

    def replace_with_fault(staged, target):
        replacements.append((staged.name, target.name))
        if scenario in {"restored", "partial", "external"} and target.name == "discount.py":
            if scenario == "external":
                (working / "pricing.py").write_bytes(external_edit)
            raise OSError("simulated write failure")
        if scenario == "partial" and staged.name == "original":
            raise OSError("simulated restore failure")
        replace(staged, target)

    monkeypatch.setattr(apply_proposal, "_replace_file", replace_with_fault)
    diagnosis_agent.main()

    report_path, = report_dir.glob("task_*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current_round, = report["rounds"]
    app = current_round["application"]
    assert report["application"] == app
    patches = report["patch_summary"]["files"]
    baseline = Path(report["workspace"]["baseline_project"])
    for name, content in original.items():
        assert (baseline / name).read_bytes() == content
        assert (source / name).read_bytes() == content
        assert (other_task / name).read_bytes() == content

    if scenario in {"invalid", "escape", "cross_task", "duplicate"}:
        assert prompts == replacements == []
        assert report["final_status"] == "proposal_unavailable"
        assert app["status"] == "not_attempted"
        assert [entry["status"] for entry in app["files"]] == ["not_attempted", "validation_failed"]
        reason = {"invalid": "没有找到原文本", "duplicate": "重复出现"}.get(scenario, "工作目录之外")
        assert reason in app["files"][1]["message"]
    else:
        assert len(prompts) == 1
        assert current_round["proposal"]["files"] == files
        assert current_round["proposal"]["diff"].count("--- 修改前/") == 2

    states = {
        "success": ("applied", "applied", "applied", "completed"),
        "rejected": ("not_attempted", "not_attempted", "not_attempted", "cancelled"),
        "changed": ("error", "not_attempted", "precheck_failed", "application_error"),
        "restored": ("write_failed_restored", "restored", "write_failed", "application_error"),
        "partial": ("partial", "restore_failed", "write_failed", "application_partial"),
        "external": ("partial", "restore_failed", "write_failed", "application_partial"),
    }
    if scenario in states:
        status, first, second, final = states[scenario]
        assert app["status"] == status
        assert [entry["status"] for entry in app["files"]] == [first, second]
        assert report["final_status"] == final
    if scenario not in {"success", "restored", "partial", "external"}:
        assert replacements == []
    if scenario in {"restored", "partial", "external"}:
        assert replacements[:2] == [("modified", "pricing.py"), ("modified", "discount.py")]
        assert "simulated write failure" in app["files"][1]["message"]
        expected_restore_calls = [] if scenario == "external" else [("original", "pricing.py")]
        assert replacements[2:] == expected_restore_calls

    expected = dict(original)
    if scenario == "success":
        expected = modified
    elif scenario == "changed":
        expected["discount.py"] = external_edit
    elif scenario == "partial":
        expected["pricing.py"] = modified["pricing.py"]
    elif scenario == "external":
        expected["pricing.py"] = external_edit
    for name, content in expected.items():
        assert (working / name).read_bytes() == content

    if scenario == "success":
        assert current_round["decision"]["reason"] == "targets_passed"
        assert patches == current_round["cumulative_patches"]
        assert set(patches) == set(modified)
        for file in app["files"]:
            name = file["relative_path"]
            assert file["before_hash"] == sha256(original[name]).hexdigest()
            assert file["after_hash"] == patches[name]["file_hash"] == sha256(modified[name]).hexdigest()
        assert patches["pricing.py"]["diff"].splitlines()[3:] == [
            "-PRICE = 100", "-CURRENCY = 'USD'", "+PRICE = 200", "+CURRENCY = 'CNY'",
        ]
        assert patches["discount.py"]["diff"].splitlines()[3:] == ["-RATE = 10", "+RATE = 20"]
        assert rollback_file(report_path, "pricing.py")["status"] == "rolled_back"
        assert (working / "pricing.py").read_bytes() == original["pricing.py"]
        assert (working / "discount.py").read_bytes() == modified["discount.py"]
        assert rollback_file(report_path, "discount.py")["status"] == "rolled_back"
        assert (working / "discount.py").read_bytes() == original["discount.py"]
    else:
        assert current_round["verification"] is None
        if scenario in {"partial", "external"}:
            assert patches == current_round["cumulative_patches"]
            assert set(patches) == {"pricing.py"}
            if scenario == "external":
                assert patches["pricing.py"]["status"] == "error"
                assert patches["pricing.py"]["file_hash"] is None
            else:
                assert patches["pricing.py"]["file_hash"] == sha256(modified["pricing.py"]).hexdigest()
        else:
            assert patches == {}


def test_application_revalidates_all_files_after_preview(tmp_path, monkeypatch):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_bytes(b"old\n")
    files = [
        {"relative_path": name, "replacements": [{"old_text": "old", "new_text": "new"}]}
        for name in ("a.py", "b.py")
    ]
    preview = EditTools(str(tmp_path)).preview_file_changes(files)
    assert preview["success"]
    (tmp_path / "b.py").write_bytes(b"external\n")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("invalid proposal requested approval"))

    result = apply_proposal.confirm_and_apply(str(tmp_path), {"files": files, "diff": preview["diff"]})

    assert result["status"] == "error"
    assert result["approval_status"] == "not_requested"
    assert [file["status"] for file in result["files"]] == ["not_attempted", "validation_failed"]
    assert (tmp_path / "a.py").read_bytes() == b"old\n"
    assert (tmp_path / "b.py").read_bytes() == b"external\n"
