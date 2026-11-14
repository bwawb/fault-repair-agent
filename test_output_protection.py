import json
from pathlib import Path

import pytest

from apply_proposal import confirm_and_apply
from output_protection import OutputProtectionError, emit, protect_output
from run_report import write_run_report
from tools.edit_tools import EditTools
from tools.test_tools import TestTools as PytestRunner


FIRST = "虚构值🧪-first"
SECOND = "虚构值🧪-second"


def test_approval_blocks_hidden_context_before_prompt(tmp_path, monkeypatch, capsys):
    path = tmp_path / "pricing.py"
    original = f"# {FIRST}\nprice = 100\n".encode()
    path.write_bytes(original)
    proposal = {"relative_path": path.name, "old_text": "100", "new_text": "80"}
    proposal["diff"] = EditTools(str(tmp_path)).preview_replace(path.name, "100", "80")["diff"]
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("approval was requested"))
    with protect_output((FIRST,)):
        result = confirm_and_apply(str(tmp_path), proposal)
    assert result["status"] == "approval_blocked"
    assert result["approval_status"] == "not_requested"
    assert path.read_bytes() == original
    assert FIRST not in capsys.readouterr().out


def test_report_preserves_distinct_test_ids_and_checks_required_fields(tmp_path):
    destination = tmp_path / "report.json"
    test_results = {f"test_price[{FIRST}]": "failed", f"test_price[{SECOND}]": "passed"}
    report = {
        "task": {"task_id": "demo"},
        "verification": {"test_results": test_results},
        "agent": {"tool_results": [{"tool_call_id": "read", "content": json.dumps({"text": FIRST})}]},
    }
    with protect_output((FIRST, SECOND)) as protection:
        protection.register_test_ids(test_results)
        write_run_report(report, destination)
        previous = destination.read_text(encoding="utf-8")
        assert json.loads(previous)["verification"]["test_results"] == {
            "[test-id-1]": "failed", "[test-id-2]": "passed",
        }
        assert "[REDACTED]" in json.loads(json.loads(previous)["agent"]["tool_results"][0]["content"])["text"]
        report["patch_summary"] = {"files": {FIRST: {"file_hash": "a" * 64}}}
        with pytest.raises(OutputProtectionError, match="运行必需"):
            write_run_report(report, destination)
        assert destination.read_text(encoding="utf-8") == previous
    assert list(test_results.values()) == ["failed", "passed"]


def test_output_failures_do_not_fall_back_to_raw_values(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "report.json"

    def fail(*args, **kwargs):
        raise ValueError(FIRST)

    with protect_output((FIRST,)) as protection:
        monkeypatch.setattr(protection.display_redactor, "redact_text", fail)
        emit(FIRST)
        with pytest.raises(OutputProtectionError) as error:
            write_run_report({"task": {"task_id": "demo", "user_request": FIRST}}, destination)
    terminal = capsys.readouterr()
    assert "已隐藏" in terminal.out
    assert FIRST not in terminal.out + terminal.err + str(error.value)
    assert not destination.exists()


@pytest.mark.parametrize("mode,run_status,xml_status", [
    ("delete", "passed", "deleted"),
    ("keep", "passed", "retained"),
    ("invalid", "report_parse_error", "deleted"),
    ("unlink_failure", "passed", "cleanup_failed"),
    ("pytest_error", "pytest_error", "deleted"),
])
def test_junit_lifecycle(tmp_path, monkeypatch, mode, run_status, xml_status):
    (tmp_path / "tests").mkdir()
    runner = PytestRunner(str(tmp_path), keep_junit_xml=mode == "keep")
    xml = '<testsuite><testcase classname="test_demo" name="test_value"/></testsuite>'

    def run_local(args, timeout, result):
        report = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")))
        report.write_text("<invalid" if mode == "invalid" else xml, encoding="utf-8")
        result["exit_code"] = 2 if mode == "pytest_error" else 0

    monkeypatch.setattr(runner, "_run_local", run_local)
    if mode == "unlink_failure":
        original_unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path.name == "result.xml":
                raise PermissionError("simulated lock")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
    requested = tmp_path / "report.xml"
    result = runner.run_tests_with_report(str(requested))
    assert result["status"] == run_status
    assert result["junit_xml"]["status"] == xml_status
    if xml_status == "deleted":
        assert result["report_path"] is None
        assert not list(tmp_path.glob("junit-*"))
    else:
        assert Path(result["report_path"]).is_file()
    if run_status != "passed":
        assert result["test_results"] is None
