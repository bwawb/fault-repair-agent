import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

import diagnosis_agent
from tools.langchain_tools import create_langchain_tools
from tools.test_tools import OUTPUT_LIMIT, TestTools as LocalTestTools
from tools.tool_registry import ToolRegistry


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    return project


@pytest.mark.parametrize("assertion, status, exit_code", [("True", "passed", 0), ("False", "failed", 1)])
def test_agent_tool_and_report_use_same_result(project, assertion, status, exit_code):
    (project / "tests" / "test_example.py").write_text(
        f"def test_example():\n    assert {assertion}\n", encoding="utf-8"
    )
    tool = next(
        tool for tool in create_langchain_tools(ToolRegistry(str(project))) if tool.name == "run_tests"
    )
    agent_result = json.loads(tool.invoke({}))["result"]
    report_result = LocalTestTools(str(project)).run_tests_with_report(str(project.parent / "result.xml"))

    for result in (agent_result, report_result):
        assert result["execution_mode"] == "local"
        assert result["status"] == status
        assert result["exit_code"] == exit_code
        assert result["error"] is None
        if status == "failed":
            assert "assert False" in result["output"]
    assert agent_result["test_results"] is None
    assert report_result["test_results"] == {"tests.test_example::test_example": status}


@pytest.mark.parametrize("with_report", [False, True], ids=["agent", "report"])
def test_output_is_bounded_for_both_streams(project, with_report):
    (project / "tests" / "test_output.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "def test_output():\n"
        "    assert Path('tests').is_dir()\n"
        "    for _ in range(512):\n"
        "        print('A' * 8192)\n"
        "        print('B' * 8192, file=sys.stderr)\n"
        "    print('stdout tail')\n"
        "    print('stderr tail', file=sys.stderr)\n",
        encoding="utf-8",
    )
    if with_report:
        result = LocalTestTools(str(project)).run_tests_with_report(str(project.parent / "result.xml"))
        assert result["test_results"] == {"tests.test_output::test_output": "passed"}
    else:
        tool = next(
            tool for tool in create_langchain_tools(ToolRegistry(str(project))) if tool.name == "run_tests"
        )
        result = json.loads(tool.invoke({}))["result"]

    assert result["status"] == "passed", result["error"]
    assert result["output_truncated"] and result["stdout_truncated"] and result["stderr_truncated"]
    assert "stdout tail" in result["output"] and "stderr tail" in result["output"]
    assert result["output"].count("已截断") == 2
    assert len(result["output"].encode("utf-8")) <= OUTPUT_LIMIT * 2 + 512


def test_launch_failure_is_separate_from_pytest_error(project, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(project / "missing-python.exe"))
    result = LocalTestTools(str(project)).run_tests()
    assert result["status"] == "launch_error"
    assert result["exit_code"] is None
    assert "启动 pytest 失败" in result["error"]


def test_collection_error_is_not_a_test_failure(project):
    (project / "tests" / "test_invalid.py").write_text("def invalid(\n", encoding="utf-8")
    result = LocalTestTools(str(project)).run_tests_with_report(str(project.parent / "result.xml"))
    assert result["status"] == "pytest_error"
    assert result["exit_code"] == 2
    assert result["test_results"] is None


@pytest.mark.parametrize(
    "operation, expected",
    [
        ("report.unlink()", "report_missing"),
        ("report.unlink(); report.mkdir()", "report_parse_error"),
        ("report.write_text('<broken')", "report_parse_error"),
        ("report.write_text('<unexpected><testcase name=\"test_example\"/></unexpected>')", "report_parse_error"),
        (
            "report.write_text('<testsuite><testcase name=\"test_example\"><failure/></testcase></testsuite>')",
            "report_parse_error",
        ),
    ],
    ids=["missing", "directory", "malformed", "not-junit", "exit-code-mismatch"],
)
def test_unreliable_report_stops_verification(project, operation, expected):
    (project / "tests" / "test_example.py").write_text("def test_example(): pass\n", encoding="utf-8")
    (project / "conftest.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    report = Path(session.config.option.xmlpath)\n"
        f"    {operation}\n",
        encoding="utf-8",
    )
    report = {"verification": {"comparison": None}}
    round_record = {}
    verification = diagnosis_agent.verify_round(
        LocalTestTools(str(project)), project.parent / "result.xml",
        {"tests.test_example::test_example": "failed"}, report, round_record,
    )

    assert not verification["success"]
    assert verification["status"] == "after_report_error"
    result = report["verification"]["after_fix"]
    assert result["status"] == expected
    assert result["exit_code"] == 0
    assert result["error"]
    assert result["test_results"] is None
    assert round_record["verification"]["after_fix"] == result
    assert report["verification"]["comparison"] is None


def test_existing_report_is_not_reused(project):
    report_path = project.parent / "result.xml"
    report_path.write_text("old report", encoding="utf-8")
    result = LocalTestTools(str(project)).run_tests_with_report(str(report_path))
    assert result["status"] == "setup_error"
    assert result["exit_code"] is None
    assert "已存在" in result["error"]
    assert report_path.read_text(encoding="utf-8") == "old report"


def test_missing_baseline_report_is_saved_and_prevents_agent_start(project, monkeypatch):
    (project / "tests" / "test_example.py").write_text("def test_example(): pass\n", encoding="utf-8")
    (project / "conftest.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    Path(session.config.option.xmlpath).unlink()\n",
        encoding="utf-8",
    )
    report_dir = project.parent / "run_reports"
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: project)
    monkeypatch.setattr(diagnosis_agent, "create_model", lambda: pytest.fail("Agent started without XML"))
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(report_dir)),
    )

    diagnosis_agent.main()

    report_path, = report_dir.glob("task_*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_status"] == "baseline_report_error"
    assert report["rounds"] == []
    baseline = report["verification"]["baseline"]
    assert baseline["execution_mode"] == "local"
    assert baseline["status"] == "report_missing"
    assert baseline["exit_code"] == 0
    assert baseline["output_truncated"] is False
    assert baseline["error"]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows Job Object 绑定失败")
def test_windows_job_assignment_failure_prevents_pytest_from_starting(project, monkeypatch):
    from tools.windows_test_job import WindowsTestJob

    (project / "conftest.py").write_text(
        "from pathlib import Path\nPath('pytest-started').touch()\n", encoding="utf-8"
    )

    def fail_assignment(job, process):
        raise PermissionError("job assignment denied")

    monkeypatch.setattr(WindowsTestJob, "attach", fail_assignment)
    result = LocalTestTools(str(project)).run_tests()
    assert result["status"] == "launch_error"
    assert "job assignment denied" in result["error"]
    assert result["process_cleanup"]["strategy"] == "direct_process"
    assert result["process_cleanup"]["direct_process"] == "launcher"
    assert result["process_cleanup"]["direct_process_exited"]
    assert not (project / "pytest-started").exists()


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 启动器无法创建 pytest 进程时的状态")
def test_windows_launcher_failure_is_launch_error(project, monkeypatch):
    from tools import windows_test_job

    launcher = windows_test_job.PYTEST_LAUNCHER.replace(
        "[sys.executable,", f"[{str(project / 'missing-python.exe')!r},", 1
    )
    monkeypatch.setattr(windows_test_job, "PYTEST_LAUNCHER", launcher)
    result = LocalTestTools(str(project)).run_tests()
    assert result["status"] == "launch_error"
    assert "未能启动 pytest" in result["error"]
    assert "无法创建 pytest 进程" in result["output"]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows Job Object 的后代进程清理")
def test_windows_timeout_cleans_up_pytest_and_child(project, monkeypatch):
    import _winapi

    (project / "tests" / "test_slow.py").write_text(
        "from pathlib import Path\n"
        "import os, subprocess, sys, time\n"
        "def test_slow():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])\n"
        "    Path('child.pid').write_text(str(child.pid))\n"
        "    Path('pytest.pid').write_text(str(os.getpid()))\n"
        "    print('started child')\n"
        "    time.sleep(15)\n",
        encoding="utf-8",
    )
    class ShortTimeoutTools(LocalTestTools):
        def run_tests_with_report(self, report_path):
            return super().run_tests_with_report(report_path, timeout=4)

    report = {"verification": {"comparison": None}}
    round_record = {}
    monkeypatch.setattr(diagnosis_agent, "compare_results", lambda **_: pytest.fail("compared after timeout"))
    verification = diagnosis_agent.verify_round(
        ShortTimeoutTools(str(project)), project.parent / "result.xml",
        {"tests.test_slow::test_slow": "failed"}, report, round_record,
    )
    result = report["verification"]["after_fix"]
    for name in ("pytest", "child"):
        pid = int((project / f"{name}.pid").read_text())
        try:
            handle = _winapi.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        except OSError as exc:
            assert exc.winerror == 87  # 进程对象已销毁，PID 无法再被打开。
        else:
            try:
                exited = _winapi.WaitForSingleObject(handle, 1000) == _winapi.WAIT_OBJECT_0
                if not exited:
                    subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"], check=False, timeout=3)
                assert exited, f"{name} 在超时清理后仍在运行"
            finally:
                _winapi.CloseHandle(handle)

    assert result["status"] == "timeout"
    assert result["process_cleanup"]["strategy"] == "windows_job"
    assert result["process_cleanup"]["direct_process"] == "launcher"
    assert result["process_cleanup"]["direct_process_exited"]
    assert result["process_cleanup"]["tree_request_succeeded"]
    assert "started child" in result["output"]
    assert result["test_results"] is None
    assert verification["status"] == round_record["status"] == "after_test_error"
    assert not verification["success"]
    assert round_record["verification"]["after_fix"] == result
    assert report["verification"]["comparison"] is None
