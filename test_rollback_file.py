import json
import subprocess
import sys
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

import rollback_file as rollback_module
from rollback_file import rollback_file
from run_report import build_run_report, save_run_report, write_run_report
from workspace import create_baseline_snapshot, create_workspace


@pytest.fixture
def saved_task(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_bytes(b"value = 1\r\n")
    (source / "subdir").mkdir()
    working = create_workspace(str(source), str(tmp_path / "workspaces"))
    baseline = create_baseline_snapshot(str(working))
    current = b"value = 2\r\n"
    (working / "code.py").write_bytes(current)

    report = build_run_report(
        "修复示例", [], source_project=str(source), working_project=str(working),
        baseline_project=str(baseline),
    )
    report["task"]["completed_at"] = datetime.now().astimezone().isoformat()
    report["final_status"] = "completed"
    report["patch_summary"]["files"]["code.py"] = {
        "status": "generated",
        "relative_path": "code.py",
        "diff": "-value = 1\n+value = 2\n",
        "message": None,
        "file_hash": sha256(current).hexdigest(),
    }
    report_path = save_run_report(report, str(tmp_path / "reports"))
    return report_path, working, baseline


def test_explicit_rollback_cli(saved_task):
    report_path, working, baseline = saved_task
    command = Path(__file__).with_name("rollback_file.py")
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(command), str(report_path), "./code.py"],
        capture_output=True, encoding="utf-8",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["relative_path"] == "code.py"
    assert result["status"] == "rolled_back"
    assert result["reason"] is None
    assert datetime.fromisoformat(result["timestamp"]).tzinfo is not None
    assert (working / "code.py").read_bytes() == b"value = 1\r\n"
    assert (baseline / "code.py").read_bytes() == b"value = 1\r\n"
    assert rollback_file(report_path, "code.py")["status"] == "already_at_baseline"


def test_refuses_to_overwrite_external_edit(saved_task):
    report_path, working, _ = saved_task
    prior_report = json.loads(report_path.read_text(encoding="utf-8"))
    target = working / "code.py"
    target.write_bytes(b"external = True\n")

    result = rollback_file(report_path, "code.py")

    assert result["status"] == "error"
    assert "SHA-256 不符" in result["reason"]
    assert target.read_bytes() == b"external = True\n"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["rollbacks"] == [result]
    assert report["patch_summary"] == prior_report["patch_summary"]


def test_rechecks_file_just_before_replacing_it(saved_task, monkeypatch):
    report_path, working, _ = saved_task
    replace_working_file = rollback_module._replace_working_file

    def change_before_replace(path, contents, expected_hash):
        path.write_bytes(b"concurrent_edit = True\n")
        replace_working_file(path, contents, expected_hash)

    monkeypatch.setattr(rollback_module, "_replace_working_file", change_before_replace)

    result = rollback_file(report_path, "code.py")

    assert result["status"] == "error"
    assert "回滚期间发生变化" in result["reason"]
    assert (working / "code.py").read_bytes() == b"concurrent_edit = True\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["rollbacks"] == [result]


@pytest.mark.parametrize(
    ("relative_path", "reason"),
    [
        ("../baseline_project/code.py", "超出工作副本目录"),
        ("missing.py", "工作副本中不存在"),
        ("subdir", "不是普通文件"),
    ],
)
def test_invalid_rollback_path(saved_task, relative_path, reason):
    report_path, working, baseline = saved_task

    result = rollback_file(report_path, relative_path)

    assert result["status"] == "error"
    assert reason in result["reason"]
    assert (working / "code.py").read_bytes() == b"value = 2\r\n"
    assert (baseline / "code.py").read_bytes() == b"value = 1\r\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["rollbacks"] == [result]


def test_refuses_missing_baseline_file(saved_task):
    report_path, working, baseline = saved_task
    (baseline / "code.py").unlink()

    result = rollback_file(report_path, "code.py")

    assert result["status"] == "error"
    assert "基线快照中不存在" in result["reason"]
    assert (working / "code.py").read_bytes() == b"value = 2\r\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["rollbacks"] == [result]


def test_refuses_report_without_file_hash(saved_task):
    report_path, working, _ = saved_task
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report["patch_summary"]["files"]["code.py"]["file_hash"]
    del report["rollbacks"]
    write_run_report(report, report_path)

    result = rollback_file(report_path, "code.py")

    assert result["status"] == "error"
    assert "缺少该文件上次记录的 SHA-256" in result["reason"]
    assert (working / "code.py").read_bytes() == b"value = 2\r\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["rollbacks"] == [result]


def test_report_save_failure_restores_previous_file(saved_task, monkeypatch):
    report_path, working, _ = saved_task
    prior_report = report_path.read_bytes()

    def fail_to_save(report, path):
        raise PermissionError("报告不可写")

    monkeypatch.setattr(rollback_module, "write_run_report", fail_to_save)

    with pytest.raises(RuntimeError, match="回滚报告保存失败"):
        rollback_file(report_path, "code.py")

    assert (working / "code.py").read_bytes() == b"value = 2\r\n"
    assert report_path.read_bytes() == prior_report
