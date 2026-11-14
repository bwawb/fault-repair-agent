import json
import os
import sys
from functools import partial
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import diagnosis_agent
from tools.local_test_env import build_local_test_env
from tools.test_tools import TestTools as LocalTestTools


TEST_VALUE = "synthetic-test-mode-value"
SECRET_VALUE = "synthetic-model-secret-value"


@pytest.fixture
def configured_host(tmp_path, monkeypatch):
    test_temp = tmp_path / "child-temp"
    test_temp.mkdir()
    for name in (("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)):
        monkeypatch.setenv(name, str(test_temp))
    monkeypatch.setenv("MODEL_API_KEY", SECRET_VALUE)
    monkeypatch.setenv("EXAMPLE_UNAPPROVED", "synthetic-unapproved-value")
    monkeypatch.setenv("EXAMPLE_TEST_MODE", TEST_VALUE)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "host-imports"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--invalid-option-from-host")
    monkeypatch.setenv("PATH", str(tmp_path / "host-path"))
    return test_temp


def test_environment_is_built_from_allowlist(configured_host, monkeypatch):
    env = build_local_test_env()
    assert {"MODEL_API_KEY", "EXAMPLE_UNAPPROVED", "EXAMPLE_TEST_MODE", "PYTHONPATH", "PYTEST_ADDOPTS"}.isdisjoint(env)
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert os.environ["PATH"] not in env["PATH"].split(os.pathsep)
    if os.name == "nt":
        assert str(Path(env["SYSTEMROOT"]) / "System32") in env["PATH"].split(os.pathsep)
        assert env["TEMP"] == str(configured_host)
    else:
        assert env["TMPDIR"] == str(configured_host)

    requested = "example_test_mode" if os.name == "nt" else "EXAMPLE_TEST_MODE"
    extra = build_local_test_env((requested, "PYTHONPATH", "PATH"))
    assert extra["EXAMPLE_TEST_MODE"] == TEST_VALUE
    assert extra["PYTHONPATH"] == os.environ["PYTHONPATH"]
    assert extra["PATH"] == os.environ["PATH"]
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    assert build_local_test_env()["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("name", ["EXAMPLE_MISSING", "EXAMPLE_TEST_MODE=" + TEST_VALUE, "PYTHONIOENCODING"])
def test_invalid_environment_configuration_is_not_logged(tmp_path, monkeypatch, name):
    (tmp_path / "tests").mkdir()
    monkeypatch.delenv("EXAMPLE_MISSING", raising=False)
    result = LocalTestTools(str(tmp_path), local_env_names=(name,)).run_tests()
    assert result["status"] == "setup_error" and result["error"]
    assert result["exit_code"] is None and not result["output"]
    assert TEST_VALUE not in json.dumps(result)


def test_main_task_uses_same_controlled_env_for_all_three_runs(tmp_path, configured_host, monkeypatch, capsys):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pricing.py").write_text("price = 100\n", encoding="utf-8")
    expected_digest = sha256(TEST_VALUE.encode()).hexdigest()
    (project / "tests" / "test_order_service.py").write_text(
        "import hashlib, os, sys, tempfile\n"
        "from pathlib import Path\n"
        "from pricing import price\n"
        "def test_create_order_with_discount():\n"
        "    for name in ('MODEL_API_KEY', 'EXAMPLE_UNAPPROVED', 'PYTHONPATH', 'PYTEST_ADDOPTS'):\n"
        "        if name in os.environ:\n"
        "            raise RuntimeError('unexpected inherited variable: ' + name)\n"
        "    assert os.environ['PATH'].split(os.pathsep)[0] == str(Path(sys.executable).parent)\n"
        f"    assert Path(tempfile.gettempdir()) == Path({str(configured_host)!r})\n"
        "    mode = os.getenv('EXAMPLE_TEST_MODE', '')\n"
        f"    assert hashlib.sha256(mode.encode()).hexdigest() == {expected_digest!r}\n"
        "    print('environment checked')\n"
        "    assert price == 80\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: project)
    monkeypatch.setattr(diagnosis_agent, "create_model", object)
    monkeypatch.setattr(diagnosis_agent, "create_agent", lambda **kwargs: kwargs["tools"])
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    report_dir = tmp_path / "task-reports"
    monkeypatch.setattr(diagnosis_agent, "save_run_report",
                        partial(diagnosis_agent.save_run_report, output_dir=str(report_dir)))

    def invoke_agent(agent, user_request):
        tools = {tool.name: tool for tool in agent}
        test_call = {"name": "run_tests", "args": {}, "id": "test", "type": "tool_call"}
        patch_call = {
            "name": "preview_replace", "args": {"relative_path": "pricing.py", "old_text": "100", "new_text": "80"},
            "id": "preview", "type": "tool_call",
        }
        return {"messages": [
            AIMessage(content="", tool_calls=[test_call, patch_call]),
            tools["run_tests"].invoke(test_call), tools["preview_replace"].invoke(patch_call),
            AIMessage(content="synthetic diagnosis"),
        ]}

    monkeypatch.setattr(diagnosis_agent, "run_diagnosis", invoke_agent)
    diagnosis_agent.main(local_env_names=("EXAMPLE_TEST_MODE",))

    report_file, = report_dir.glob("task_*.json")
    report_text = report_file.read_text(encoding="utf-8")
    terminal = capsys.readouterr()
    assert all(value not in report_text + terminal.out + terminal.err for value in (TEST_VALUE, SECRET_VALUE))
    report = json.loads(report_text)
    baseline = report["verification"]["baseline"]
    after = report["verification"]["after_fix"]
    test_result = report["agent"]["tool_results"][0]
    agent = json.loads(test_result["content"])["result"]
    for run, status in ((baseline, "failed"), (agent, "failed"), (after, "passed")):
        assert run["execution_mode"] == "local" and run["status"] == status
        assert "environment checked" in run["output"]
    target = "tests.test_order_service::test_create_order_with_discount"
    assert baseline["test_results"] == {target: "failed"}
    assert after["test_results"] == {target: "passed"}
    assert report["verification"]["comparison"]["fixed"] == [target]
    assert report["final_status"] == "completed"


@pytest.mark.skipif(os.name != "nt", reason="Windows pytest 启动器验证")
def test_real_windows_launcher_and_pytest_share_controlled_env(tmp_path, configured_host, monkeypatch):
    from tools import windows_test_job

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_env.py").write_text(
        "import os\n"
        "def test_environment():\n"
        "    assert os.getenv('MODEL_API_KEY') is None\n"
        "    assert os.getenv('EXAMPLE_UNAPPROVED') is None\n"
        "    assert os.getenv('EXAMPLE_TEST_MODE') is not None\n",
        encoding="utf-8",
    )
    # 在真实启动器进程中检查环境，只输出固定标记。
    launcher = windows_test_job.PYTEST_LAUNCHER.replace(
        "import subprocess\n",
        "import subprocess, os\n"
        "assert 'MODEL_API_KEY' not in os.environ\n"
        "assert 'EXAMPLE_UNAPPROVED' not in os.environ\n"
        "assert 'EXAMPLE_TEST_MODE' in os.environ\n"
        "print('launcher environment checked', flush=True)\n",
        1,
    )
    monkeypatch.setattr(windows_test_job, "PYTEST_LAUNCHER", launcher)
    result = LocalTestTools(str(tmp_path), local_env_names=("example_test_mode",)).run_tests_with_report(
        str(tmp_path / "result.xml")
    )
    assert result["status"] == "passed", result
    assert result["test_results"] == {"tests.test_env::test_environment": "passed"}
    assert "launcher environment checked" in result["output"]
    assert TEST_VALUE not in json.dumps(result) and SECRET_VALUE not in json.dumps(result)
