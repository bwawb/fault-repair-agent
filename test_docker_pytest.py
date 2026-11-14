import json
import os
import shutil
import sys
from functools import partial
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

import diagnosis_agent
from compare_test_reports import compare_results
from output_protection import OutputProtectionError
from rollback_file import rollback_file
from test_model_redaction import RecordingModel
from tools import docker_test_backend as backend
from tools.test_tools import OUTPUT_LIMIT, TestTools as LocalTestTools


IMAGE = "fault-repair-pytest:test"
IMAGE_ID = "sha256:" + "1" * 64


def prepare_project(tmp_path):
    project = tmp_path / "project with space"
    (project / "tests").mkdir(parents=True)
    (project / "pricing.py").write_text("price = 100\n", encoding="utf-8")
    (project / "tests" / "test_order_service.py").write_text(
        "from pricing import price\n\ndef test_create_order_with_discount():\n    assert price == 80\n",
        encoding="utf-8",
    )
    return project


class FakeDocker:
    def __init__(self, monkeypatch):
        self.calls = []
        self.report_mounts = {}
        self.fail = None
        self.exit_codes = iter([0])
        self.exit_code = None
        monkeypatch.setattr(backend.shutil, "which", lambda command: "docker-test-double")
        monkeypatch.setattr(backend, "_docker_command", self.command)
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("MODEL_API_KEY", "test-secret")

    def command(self, command, env, timeout=10):
        self.calls.append((command, timeout))
        assert "MODEL_API_KEY" not in env
        assert "test-secret" not in command
        args = command[1:] if command[1] != "--host" else command[3:]
        result = {"status": None, "exit_code": 0, "output": "", "error": None, "process_cleanup": None}
        if args[:2] == ["context", "inspect"]:
            result["output"] = "npipe:////./pipe/docker_engine"
        elif args[0] == "version":
            if self.fail == "unavailable":
                result.update(exit_code=1, output="cannot connect to the Docker daemon")
            else:
                result["output"] = "linux"
        elif args[:2] == ["image", "inspect"]:
            if self.fail == "image":
                result.update(exit_code=1, output="No such image")
            else:
                result["output"] = IMAGE_ID
        elif args[:2] == ["container", "create"]:
            if self.fail == "create-timeout":
                return dict(result, status="timeout", error="create timed out", exit_code=1,
                            process_cleanup={"direct_process_exited": True, "errors": []})
            if self.fail == "create":
                result.update(exit_code=1, output="mount denied")
            name = args[args.index("--name") + 1]
            mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--mount"]
            self.report_mounts[name] = next((mount for mount in mounts if "target=/test-report" in mount), None)
        elif args[:2] == ["container", "start"]:
            if self.fail in {"timeout", "cleanup"}:
                return dict(result, status="timeout", error="command timed out", exit_code=1,
                            process_cleanup={"direct_process_exited": True, "errors": []})
            if self.fail == "start":
                return dict(result, exit_code=125, output="permission denied")
            self.exit_code = next(self.exit_codes)
            result["exit_code"] = self.exit_code
            mount = self.report_mounts[args[-1]]
            if mount is not None and self.fail != "xml":
                directory = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
                failure = "<failure/>" if self.exit_code == 1 else ""
                report = directory / "result.xml"
                if self.fail == "oversize-xml":
                    with report.open("wb") as file:
                        file.truncate(backend._XML_LIMIT + 1)
                else:
                    report.write_text(
                        '<testsuite><testcase classname="tests.test_order_service" '
                        f'name="test_create_order_with_discount">{failure}</testcase></testsuite>',
                        encoding="utf-8",
                    )
        elif args[:2] == ["container", "inspect"]:
            state = {"Status": "exited", "ExitCode": self.exit_code, "Error": ""}
            if self.fail == "start":
                state.update(Status="created", ExitCode=0, Error="permission denied")
            result["output"] = json.dumps(state)
        elif args[:2] == ["container", "rm"]:
            if self.fail in {"cleanup", "finished-cleanup-failure"}:
                result.update(exit_code=1, output="daemon disconnected")
        else:
            pytest.fail(f"unexpected Docker command: {command}")
        return result


@pytest.mark.parametrize(
    "scenario, status",
    [("unavailable", "docker_unavailable"), ("image", "image_missing"), ("create", "launch_error"),
     ("create-timeout", "launch_error"), ("start", "launch_error"), ("xml", "report_missing"),
     ("oversize-xml", "report_parse_error"), ("timeout", "timeout"), ("cleanup", "timeout"),
     ("finished-cleanup-failure", "cleanup_error")],
)
def test_docker_errors_and_cleanup(tmp_path, monkeypatch, scenario, status):
    fake = FakeDocker(monkeypatch)
    fake.fail = scenario
    tools = LocalTestTools(str(prepare_project(tmp_path)), execution_mode="docker", docker_image=IMAGE)
    result = tools.run_tests_with_report(str(tmp_path / "reports" / "tests.xml"), timeout=2)

    assert result["execution_mode"] == "docker"
    assert result["status"] == status
    assert result["error"] and result["test_results"] is None
    if scenario not in {"unavailable", "image", "create"}:
        cleanup = result["process_cleanup"]
        assert cleanup["container_removed"] == (scenario not in {"cleanup", "finished-cleanup-failure"})
        assert fake.calls[-1][0][-3:] == ["--force", "--volumes", cleanup["container_name"]]
        if scenario in {"timeout", "cleanup"}:
            assert result["exit_code"] is None
            assert cleanup["direct_process_exited"]
            assert fake.calls[-2][1] == 2
        if scenario in {"cleanup", "finished-cleanup-failure"}:
            assert "daemon disconnected" in cleanup["errors"][0]
    if scenario in {"create-timeout", "oversize-xml", "finished-cleanup-failure"}:
        assert not Path(result["report_path"]).exists()


def test_missing_docker_cli_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.shutil, "which", lambda command: None)
    monkeypatch.setattr(LocalTestTools, "_run_local", lambda *_: pytest.fail("fell back to local"))
    result = LocalTestTools(str(prepare_project(tmp_path)), execution_mode="docker", docker_image=IMAGE).run_tests()
    assert result["status"] == "docker_unavailable"
    assert "Docker CLI" in result["error"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_docker_xml_staging_and_default_disposal(tmp_path, monkeypatch, cleanup_fails):
    FakeDocker(monkeypatch)
    if cleanup_fails:
        class LockedTemporaryDirectory:
            def __init__(self, **kwargs):
                self.path = mkdtemp(**kwargs)

            def __enter__(self):
                return self.path

            def __exit__(self, *exc):
                raise PermissionError("simulated Docker report directory lock")

        monkeypatch.setattr(backend, "TemporaryDirectory", LockedTemporaryDirectory)

    runner = LocalTestTools(str(prepare_project(tmp_path)), execution_mode="docker",
                            docker_image=IMAGE, keep_junit_xml=False)
    result = runner.run_tests_with_report(str(tmp_path / "reports" / "tests.xml"))
    assert result["process_cleanup"]["container_removed"] is True
    assert result["report_path"] is None
    if cleanup_fails:
        assert result["status"] == "cleanup_error"
        assert result["test_results"] is None
        assert result["junit_xml"]["status"] == "cleanup_failed"
        staging = result["docker_xml_staging"]
        assert staging["status"] == "cleanup_failed"
        assert Path(staging["path"]).is_dir()
    else:
        assert result["status"] == "passed"
        assert result["junit_xml"]["status"] == "deleted"
        assert result["docker_xml_staging"] == {"status": "deleted"}


def test_remote_docker_is_rejected_before_mounting(tmp_path, monkeypatch):
    fake = FakeDocker(monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote")
    result = LocalTestTools(str(prepare_project(tmp_path)), execution_mode="docker", docker_image=IMAGE).run_tests()
    assert result["status"] == "docker_unavailable"
    assert fake.calls == []


@pytest.fixture
def real_image():
    image = os.getenv("FAULT_REPAIR_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("需要显式指定已准备好的本地 Docker 镜像")
    return image


def _assert_container_removed(cleanup):
    assert cleanup["container_removed"] and cleanup["remove_exit_code"] == 0, cleanup
    env = os.environ.copy()
    docker, error = backend._local_docker(shutil.which("docker"), env)
    assert error is None, error
    result = backend._docker_command(
        [*docker, "container", "inspect", cleanup["container_name"]], env,
    )
    assert result["error"] is None and result["exit_code"] != 0, result
    assert "no such" in result["output"].lower(), result


def _run_task(tmp_path, monkeypatch, image):
    project = prepare_project(tmp_path)
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: project)
    monkeypatch.setattr(diagnosis_agent, "create_model", object)
    monkeypatch.setattr(diagnosis_agent, "create_agent", lambda **kwargs: kwargs["tools"])
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(tmp_path / "task_reports")),
    )
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    agent_results = []

    def invoke_agent(agent, user_request):
        run_tests = next(tool for tool in agent if tool.name == "run_tests")
        call = {"name": "run_tests", "args": {}, "id": "acceptance-test", "type": "tool_call"}
        message = run_tests.invoke(call)
        agent_results.append(json.loads(message.content)["result"])
        return {"messages": [AIMessage(content="", tool_calls=[call]), message, AIMessage(content="test diagnosis")]}

    monkeypatch.setattr(diagnosis_agent, "run_diagnosis", invoke_agent)
    proposal = {"relative_path": "pricing.py", "old_text": "100", "new_text": "80", "tool_call_id": "preview"}
    preview = diagnosis_agent.ToolRegistry(str(project)).execute(
        "preview_replace", {key: proposal[key] for key in ("relative_path", "old_text", "new_text")}
    )
    proposal["diff"] = preview["result"]["diff"]
    monkeypatch.setattr(diagnosis_agent, "extract_latest_preview", lambda _: {"success": True, "proposal": proposal})

    diagnosis_agent.main(test_backend="docker", docker_image=image)

    report_path, = (tmp_path / "task_reports").glob("task_*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline, after = (report["verification"][key] for key in ("baseline", "after_fix"))
    assert report["final_status"] == "completed"
    assert (project / "pricing.py").read_text(encoding="utf-8") == "price = 80\n"
    assert [item["execution_mode"] for item in (baseline, *agent_results, after)] == ["docker"] * 3
    assert [item["status"] for item in (baseline, *agent_results, after)] == ["failed", "failed", "passed"]
    assert report["verification"]["comparison"]["fixed"] == ["tests.test_order_service::test_create_order_with_discount"]
    assert report["rounds"][0]["verification"]["after_fix"] == after
    saved_result, = report["agent"]["tool_results"]
    assert json.loads(saved_result["content"])["result"] == agent_results[0]
    return baseline, agent_results[0], after


def test_task_uses_docker_for_agent_baseline_and_retest(tmp_path, monkeypatch):
    fake = FakeDocker(monkeypatch)
    fake.exit_codes = iter([1, 1, 0])
    _run_task(tmp_path, monkeypatch, IMAGE)
    project = tmp_path / "project with space"
    creates = [command for command, _ in fake.calls if "create" in command]
    assert len(creates) == 3
    for index, command in enumerate(creates):
        assert IMAGE_ID in command and "--pull=never" in command
        assert {"--network=none", "--memory=512m", "--cpus=1", "--pids-limit=64", "--read-only"} <= set(command)
        mounts = [command[index + 1] for index, arg in enumerate(command) if arg == "--mount"]
        assert len(mounts) == (1 if index == 1 else 2)
        assert mounts[0] == backend._bind(project.resolve(), "/workspace", readonly=True)
        if index != 1:
            assert "target=/test-report" in mounts[1] and "readonly" not in mounts[1]
            assert "--junitxml=/test-report/result.xml" in command
        assert "--privileged" not in command and not any("docker.sock" in mount for mount in mounts)


def test_docker_cli_uses_bounded_output_and_reaps_timed_out_client():
    result = backend._docker_command(
        [sys.executable, "-u", "-c",
         "import sys; print('A' * 200000); print('B' * 200000, file=sys.stderr); print('last line')"],
        os.environ.copy(), timeout=5,
    )
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert "last line" in result["output"]
    assert len(result["output"].encode("utf-8")) <= 2 * OUTPUT_LIMIT + 512

    timed_out = backend._docker_command(
        [sys.executable, "-c", "import time; time.sleep(20)"], os.environ.copy(), timeout=0.2
    )
    assert timed_out["status"] == "timeout"
    assert timed_out["process_cleanup"]["direct_process_exited"]


def test_real_docker_failed_then_fixed(tmp_path, real_image):
    project = prepare_project(tmp_path)
    tools = LocalTestTools(str(project), execution_mode="docker", docker_image=real_image)
    before = tools.run_tests_with_report(str(tmp_path / "baseline.xml"))
    assert before["status"] == "failed", before
    assert before["exit_code"] == 1 and "assert 100 == 80" in before["output"], before
    assert before["junit_xml"] == {"status": "retained"}
    assert Path(before["report_path"]) == tmp_path / "baseline.xml"
    assert Path(before["report_path"]).is_file()
    (project / "pricing.py").write_text("price = 80\n", encoding="utf-8")
    after = tools.run_tests_with_report(str(tmp_path / "after.xml"))
    assert after["status"] == "passed", after
    assert after["exit_code"] == 0 and "1 passed" in after["output"], after
    assert after["junit_xml"] == {"status": "retained"}
    assert Path(after["report_path"]) == tmp_path / "after.xml"
    assert Path(after["report_path"]).is_file()
    for run in (before, after):
        assert run["docker_xml_staging"] == {"status": "deleted"}
        _assert_container_removed(run["process_cleanup"])
    assert not list(tmp_path.glob("docker-xml-*"))
    assert compare_results(before["test_results"], after["test_results"])["fixed"] == [
        "tests.test_order_service::test_create_order_with_discount"
    ]


@pytest.mark.parametrize("scenario", ["delete-xml", "retain-xml", "blocked-approval"])
def test_real_docker_task_report(tmp_path, monkeypatch, real_image, capsys, scenario):
    project = prepare_project(tmp_path)
    keep_xml = scenario == "retain-xml"
    blocked = scenario == "blocked-approval"
    marker = "虚构容器凭据-docker-acceptance"
    pricing = project / "pricing.py"
    if blocked:
        pricing.write_bytes(f"# {marker}\nprice = 100\n".encode())
    original = pricing.read_bytes()
    tests = project / "tests" / "test_order_service.py"
    tests.write_text(tests.read_text(encoding="utf-8").replace(
        "    assert price == 80", f"    print({marker!r})\n    assert price == 80, {marker!r}",
    ), encoding="utf-8")
    original_tests = tests.read_bytes()
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "run_tests", "args": {}, "id": "run"}]),
        AIMessage(content="", tool_calls=[{
            "name": "preview_replace", "id": "preview",
            "args": {"relative_path": "pricing.py", "old_text": "100", "new_text": "80"},
        }]),
        AIMessage(content=f"容器诊断 {marker}"),
    ])
    monkeypatch.setattr(diagnosis_agent, "create_model", lambda: model)
    approvals = []

    def approve(prompt):
        assert not blocked, "审批 diff 被遮盖时不应询问用户"
        approvals.append(prompt)
        return "yes"

    monkeypatch.setattr("builtins.input", approve)
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(tmp_path / "task_reports")),
    )
    options = {"keep_junit_xml": True} if keep_xml else {}
    diagnosis_agent.main(test_backend="docker", docker_image=real_image,
                         sensitive_values=(marker,), user_request=f"修复价格 {marker}",
                         source_project=project, workspace_base=tmp_path / "workspaces", **options)

    path, = (tmp_path / "task_reports").glob("task_*.json")
    saved = path.read_text(encoding="utf-8")
    terminal = capsys.readouterr()
    assert marker not in saved + terminal.out + terminal.err
    assert "[REDACTED]" in terminal.out
    assert len(model.requests) == 3
    assert all(marker not in str(message.model_dump()) for request in model.requests for message in request)
    visible_result = next(message for message in model.requests[1] if message.type == "tool")
    assert "[REDACTED]" in json.loads(visible_result.content)["result"]["output"]

    report = json.loads(saved)
    workspace = report["workspace"]
    assert Path(workspace["source_project"]) == project
    working = Path(workspace["working_project"])
    baseline_project = Path(workspace["baseline_project"])
    assert working.parent.parent == tmp_path / "workspaces"
    assert baseline_project == working.parent / "baseline_project"
    for location in (project, baseline_project, working):
        assert (location / tests.relative_to(project)).read_bytes() == original_tests
    for location in (project, baseline_project):
        assert (location / pricing.name).read_bytes() == original

    baseline, after = (report["verification"][key] for key in ("baseline", "after_fix"))
    agent_run = json.loads(report["agent"]["tool_results"][0]["content"])["result"]
    runs = (baseline, agent_run) if blocked else (baseline, agent_run, after)
    for run in runs:
        assert run["execution_mode"] == "docker" and run["docker_image"] == real_image
        assert "[REDACTED]" in run["output"]
        _assert_container_removed(run["process_cleanup"])
    assert agent_run["junit_xml"] == {"status": "not_requested"}
    reports_dir = working.parent / "reports"
    if blocked:
        assert report["final_status"] == "approval_blocked"
        assert report["approval"]["status"] == "not_requested"
        assert report["application"]["status"] == "approval_blocked"
        assert "无法完整展示" in report["application"]["message"]
        assert after is None and report["verification"]["comparison"] is None
        assert report["patch_summary"]["files"] == {}
        assert approvals == []
        assert (working / pricing.name).read_bytes() == original
        assert baseline["status"] == agent_run["status"] == "failed"
        assert baseline["junit_xml"] == baseline["docker_xml_staging"] == {"status": "deleted"}
        assert baseline["report_path"] is None and list(reports_dir.iterdir()) == []
        return

    assert report["final_status"] == "completed"
    assert report["approval"]["status"] == "approved" and len(approvals) == 1
    assert report["application"]["status"] == "applied"
    modified = original.replace(b"100", b"80")
    assert (working / pricing.name).read_bytes() == modified
    assert [run["status"] for run in runs] == ["failed", "failed", "passed"]
    assert report["verification"]["comparison"]["fixed"] == [
        "tests.test_order_service::test_create_order_with_discount",
    ]
    for run in (baseline, after):
        assert run["docker_xml_staging"] == {"status": "deleted"}
        if keep_xml:
            assert run["junit_xml"] == {"status": "retained"}
            xml_path = Path(run["report_path"])
            assert xml_path.parent == reports_dir and xml_path.is_file()
        else:
            assert run["junit_xml"] == {"status": "deleted"}
            assert run["report_path"] is None
    if keep_xml:
        assert set(reports_dir.iterdir()) == {Path(run["report_path"]) for run in (baseline, after)}
        assert marker in Path(baseline["report_path"]).read_text(encoding="utf-8")
        assert "原始 JUnit XML 可能含有敏感内容" in terminal.out
    else:
        assert list(reports_dir.iterdir()) == []

    before_rollback = path.read_bytes()
    with pytest.raises(OutputProtectionError, match="运行必需") as error:
        rollback_file(path, pricing.name, sensitive_values=(marker, str(project)))
    assert str(project) not in str(error.value)
    assert path.read_bytes() == before_rollback
    assert (working / pricing.name).read_bytes() == modified

    outcome = rollback_file(path, pricing.name, sensitive_values=(marker,))
    assert outcome["status"] == "rolled_back"
    for location in (project, baseline_project, working):
        assert (location / pricing.name).read_bytes() == original
    updated_json = path.read_text(encoding="utf-8")
    assert marker not in updated_json
    updated = json.loads(updated_json)
    assert updated["rollbacks"][-1]["status"] == "rolled_back"
    assert updated["patch_summary"]["files"][pricing.name]["status"] == "unchanged"


@pytest.mark.parametrize("damage,expected", [
    ("unlink()", "report_missing"),
    ("write_text('<invalid', encoding='utf-8')", "report_parse_error"),
], ids=["missing", "malformed"])
def test_real_docker_unreliable_xml_stops_verification(tmp_path, real_image, damage, expected):
    project = prepare_project(tmp_path)
    (project / "pricing.py").write_text("price = 80\n", encoding="utf-8")
    (project / "conftest.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "@pytest.hookimpl(tryfirst=True, wrapper=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        f"    Path(session.config.option.xmlpath).{damage}\n",
        encoding="utf-8",
    )
    tools = LocalTestTools(str(project), execution_mode="docker",
                           docker_image=real_image, keep_junit_xml=False)
    report = diagnosis_agent.build_run_report("Docker XML acceptance", [])
    verification = diagnosis_agent.verify_round(
        tools, tmp_path / "report.xml", {"tests.test_order_service::test_create_order_with_discount": "failed"},
        report, {},
    )
    run = report["verification"]["after_fix"]
    assert not verification["success"] and verification["status"] == "after_report_error"
    assert run["exit_code"] == 0 and run["status"] == expected
    assert run["test_results"] is None and report["verification"]["comparison"] is None
    expected_xml_status = "not_created" if expected == "report_missing" else "deleted"
    assert run["junit_xml"] == {"status": expected_xml_status}
    assert run["report_path"] is None
    assert run["docker_xml_staging"] == {"status": "deleted"}
    assert not list(tmp_path.glob("junit-*"))
    _assert_container_removed(run["process_cleanup"])


def test_real_docker_missing_image(tmp_path, monkeypatch, real_image):
    project = prepare_project(tmp_path)
    missing_image = f"fault-repair-pytest:acceptance-missing-{uuid4().hex}"
    monkeypatch.setattr(LocalTestTools, "_run_local", lambda *_: pytest.fail("unexpected local fallback"))
    result = LocalTestTools(str(project), execution_mode="docker", docker_image=missing_image).run_tests()
    assert result["status"] == "image_missing", result
    assert result["execution_mode"] == "docker" and missing_image in result["error"]
    assert result["exit_code"] is None and result["process_cleanup"] is None


def test_real_docker_timeout_cleanup_report(tmp_path, monkeypatch, real_image):
    project = prepare_project(tmp_path)
    (project / "tests" / "test_order_service.py").write_text(
        "import time\ndef test_slow():\n    print('acceptance: started')\n    time.sleep(60)\n",
        encoding="utf-8",
    )

    class ShortTimeoutTools(LocalTestTools):
        def run_tests_with_report(self, report_path):
            return super().run_tests_with_report(report_path, timeout=5)

    tools = ShortTimeoutTools(str(project), execution_mode="docker", docker_image=real_image)
    report = diagnosis_agent.build_run_report("Docker timeout acceptance", [])
    round_record = {}
    verification = diagnosis_agent.verify_round(
        tools, tmp_path / "timeout.xml", {"tests.test_order_service::test_slow": "failed"}, report, round_record,
    )
    assert not verification["success"] and verification["status"] == "after_test_error"
    report["rounds"] = [round_record]
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(tmp_path / "task_reports")),
    )
    path = diagnosis_agent.finish_report(report, final_status=verification["status"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["final_status"] == "after_test_error"
    result = saved["verification"]["after_fix"]
    assert result == saved["rounds"][0]["verification"]["after_fix"]
    assert result["execution_mode"] == "docker" and result["status"] == "timeout", result
    assert "acceptance: started" in result["output"], result
    assert result["test_results"] is None and saved["verification"]["comparison"] is None
    assert result["process_cleanup"]["direct_process_exited"]
    _assert_container_removed(result["process_cleanup"])


def test_real_docker_isolation(tmp_path, monkeypatch, real_image):
    project = prepare_project(tmp_path)
    (project / "tests" / "test_order_service.py").write_text(
        "import os, socket\nfrom pathlib import Path\nimport pytest\n"
        "def test_isolation():\n"
        "    with pytest.raises(OSError):\n"
        "        Path('/workspace/acceptance-write').touch()\n"
        "    assert {name for _, name in socket.if_nameindex()} == {'lo'}\n"
        "    assert os.getenv('MODEL_API_KEY') is None\n"
        "    assert not Path('/var/run/docker.sock').exists()\n",
        encoding="utf-8",
    )
    command = backend._docker_command
    configs = []

    def inspect_created_container(args, env, *timeouts):
        result = command(args, env, *timeouts)
        if backend._ok(result) and args[3:5] == ["container", "create"]:
            name = args[args.index("--name") + 1]
            inspect = command([*args[:3], "container", "inspect", name], env)
            configs.append(inspect)
        return result

    monkeypatch.setattr(backend, "_docker_command", inspect_created_container)
    monkeypatch.setenv("MODEL_API_KEY", "acceptance-placeholder")
    tools = LocalTestTools(str(project), execution_mode="docker", docker_image=real_image)
    result = tools.run_tests_with_report(str(tmp_path / "isolation.xml"))
    assert result["status"] == "passed", result
    assert not (project / "acceptance-write").exists()
    inspect, = configs
    assert backend._ok(inspect), inspect
    config, = json.loads(inspect["output"])
    limits = config["HostConfig"]
    assert limits["NetworkMode"] == "none" and limits["ReadonlyRootfs"]
    assert limits["NanoCpus"] == 1_000_000_000
    assert limits["Memory"] == limits["MemorySwap"] == 512 * 1024 * 1024
    assert limits["PidsLimit"] == 64
    mounts = {mount["Destination"]: mount["RW"] for mount in config["Mounts"] if mount["Type"] == "bind"}
    assert mounts == {"/workspace": False, "/test-report": True}
    _assert_container_removed(result["process_cleanup"])
