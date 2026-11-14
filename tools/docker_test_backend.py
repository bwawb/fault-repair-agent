"""使用本地 Docker CLI 运行 Linux 测试容器。"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from stat import S_ISREG
from tempfile import TemporaryDirectory
from uuid import uuid4

from tools.test_tools import collect_process_result


_CONTROL_TIMEOUT = 10
_XML_LIMIT = 8 * 1024 * 1024
_CONTEXT_HOST = '{{(index .Endpoints "docker").Host}}'


def _stop_cli(process: subprocess.Popen) -> dict:
    cleanup = {"strategy": "docker_cli", "direct_process": "docker_cli", "direct_process_exited": False, "errors": []}
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup["errors"].append(f"结束 Docker CLI 失败：{exc}")
    cleanup["direct_process_exited"] = process.poll() is not None
    return cleanup


def _docker_command(command: list[str], env: dict, timeout: float = _CONTROL_TIMEOUT) -> dict:
    result = {"status": None, "error": None, "exit_code": None, "output": "", "process_cleanup": None}
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, shell=False, env=env,
        )
    except OSError as exc:
        result.update(status="launch_error", error=str(exc))
    else:
        collect_process_result(process, timeout, result, lambda: _stop_cli(process))
    return result


def _ok(result: dict) -> bool:
    return result["error"] is None and result["exit_code"] == 0


def _details(result: dict) -> str:
    return result["error"] or result["output"] or f"Docker CLI 退出码 {result['exit_code']}"


def _local_docker(cli: str, env: dict) -> tuple[list[str] | None, str | None]:
    endpoint = env.get("DOCKER_HOST") if not env.get("DOCKER_CONTEXT") else None
    if not endpoint:
        context = _docker_command([cli, "context", "inspect", "--format", _CONTEXT_HOST], env)
        if not _ok(context):
            return None, _details(context)
        endpoint = context["output"]
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        return None, "Docker 后端需要本机 Unix socket 或 Windows named pipe。"
    # 固定已验证的端点，防止后续 Docker 命令因 context 变化连到其他主机。
    env.pop("DOCKER_HOST", None)
    env.pop("DOCKER_CONTEXT", None)
    docker = [cli, "--host", endpoint]
    server = _docker_command([*docker, "version", "--format", "{{.Server.Os}}"], env)
    if not _ok(server):
        return None, _details(server)
    if server["output"] != "linux":
        return None, "Docker 后端需要 Linux 容器模式。"
    return docker, None


def _bind(path: Path, target: str, *, readonly: bool = False) -> str:
    source = str(path)
    if any(char in source for char in ',"\r\n'):
        raise ValueError(f"Docker 挂载路径不支持逗号、引号或换行：{path}")
    options = f"type=bind,source={source},target={target},bind-recursive=disabled"
    return options + ",readonly" if readonly else options


def _create_command(docker, name, image_id, project, report_dir, args):
    user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "65534:65534"
    command = [
        *docker, "container", "create", "--name", name, "--pull=never",
        "--network=none", "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", user,
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--no-healthcheck",
        "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=2",
        "--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONUNBUFFERED=1", "--env=PYTHONIOENCODING=utf-8",
        "--workdir=/workspace", "--mount", _bind(project, "/workspace", readonly=True),
    ]
    if report_dir is not None:
        command.extend(["--mount", _bind(report_dir, "/test-report")])
        args = [*args, "--junitxml=/test-report/result.xml"]
    return [*command, "--entrypoint=python", image_id, "-m", "pytest", *args, "-p", "no:cacheprovider"]


def _remove_container(docker, env, name, result) -> None:
    removal = _docker_command([*docker, "container", "rm", "--force", "--volumes", name], env)
    cleanup = result["process_cleanup"] or {"errors": []}
    cleanup.update(
        strategy="docker_force_remove", container_name=name,
        container_removed=_ok(removal), remove_exit_code=removal["exit_code"],
    )
    if not cleanup["container_removed"]:
        cleanup["errors"].append(f"Docker 未确认容器已移除：{_details(removal)}")
        if result["error"] is None:
            result.update(status="cleanup_error", error=cleanup["errors"][-1])
    result["process_cleanup"] = cleanup


def _read_container_state(docker, env, name, result) -> None:
    state_run = _docker_command([*docker, "container", "inspect", "--format", "{{json .State}}", name], env)
    result["docker_cli_exit_code"] = result["exit_code"]
    result["exit_code"] = None
    if not _ok(state_run):
        result.update(status="docker_execution_error", error=f"无法读取容器状态：{_details(state_run)}")
        return
    try:
        state = json.loads(state_run["output"])
        if not isinstance(state, dict) or type(state.get("ExitCode")) is not int:
            raise ValueError("容器状态缺少有效退出码。")
    except ValueError as exc:
        result.update(status="docker_execution_error", error=f"Docker 容器状态无效：{exc}")
        return
    if state.get("Error") or state.get("Status") == "created":
        result.update(status="launch_error", error=f"Docker 容器启动失败：{state.get('Error') or result['output']}")
    elif state.get("Status") != "exited":
        result.update(status="docker_execution_error", error=f"容器尚未正常退出：{state.get('Status')}")
    elif state.get("OOMKilled"):
        result.update(status="pytest_error", exit_code=state["ExitCode"], error="测试容器因内存不足被终止。")
    else:
        result["exit_code"] = state["ExitCode"]
        if result["docker_cli_exit_code"] not in {0, state["ExitCode"]}:
            result.update(status="docker_execution_error", error="Docker CLI 与容器的退出状态不一致。")


def _copy_report(report_dir: Path, report_file: Path, result: dict) -> None:
    source = report_dir / "result.xml"
    try:
        if not S_ISREG(source.lstat().st_mode):
            raise ValueError("容器 XML 必须是普通文件，不能是链接。")
        with source.open("rb") as file:
            content = file.read(_XML_LIMIT + 1)
        if len(content) > _XML_LIMIT:
            raise ValueError("容器 XML 超过 8 MiB。")
        with report_file.open("xb") as file:
            file.write(content)
    except FileNotFoundError as exc:
        result.update(status="report_missing", error=f"Docker 测试未提供可用的 XML：{exc}")
    except (OSError, ValueError) as exc:
        result.update(status="report_parse_error", error=f"无法导出 Docker 测试报告：{exc}")


def _run_container(docker, env, image_id, project, args, report_file, report_dir, timeout, result):
    name = f"fault-repair-pytest-{uuid4().hex}"
    command = _create_command(docker, name, image_id, project, report_dir, args)
    needs_cleanup = True  # 创建请求超时或被中断时，Docker 仍可能已经创建了容器。
    try:
        created = _docker_command(command, env)
        if not _ok(created):
            needs_cleanup = created["status"] in {"timeout", "execution_error"}
            result.update(
                status="launch_error", error=f"无法创建 Docker 测试容器：{_details(created)}",
                docker_cli_exit_code=created["exit_code"], process_cleanup=created["process_cleanup"],
            )
            return

        attached = _docker_command([*docker, "container", "start", "--attach", name], env, timeout)
        result.update(attached)
        if result["error"] is None:
            _read_container_state(docker, env, name, result)
        else:
            result["docker_cli_exit_code"] = result["exit_code"]
            result["exit_code"] = None
    finally:
        if needs_cleanup:
            _remove_container(docker, env, name, result)

    if report_file is not None and result["error"] is None and result["exit_code"] in (0, 1):
        _copy_report(report_dir, report_file, result)


def run_docker_pytest(project, image, args, report_file, timeout, result) -> None:
    result["docker_image"] = image
    if not isinstance(image, str) or not image or image.startswith("-") or any(char.isspace() for char in image):
        result.update(status="setup_error", error="请指定有效的本地 Docker 测试镜像。")
        return
    cli = shutil.which("docker")
    if cli is None:
        result.update(status="docker_unavailable", error="找不到 Docker CLI。")
        return
    env = os.environ.copy()
    for key in ("MODEL_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    docker, error = _local_docker(cli, env)
    if error is not None:
        result.update(status="docker_unavailable", error=f"Docker 不可用：{error}")
        return
    inspection = _docker_command([*docker, "image", "inspect", "--format", "{{.Id}}", image], env)
    if not _ok(inspection):
        details = _details(inspection)
        missing = any(message in details.lower() for message in ("no such image", "no such object"))
        status = "image_missing" if missing else "docker_execution_error"
        result.update(status=status, error=f"无法读取本地镜像 {image}：{details}")
        return

    image_id = inspection["output"]
    if not image_id.startswith("sha256:") or len(image_id) != 71 or any(
        char not in "0123456789abcdef" for char in image_id[7:]
    ):
        result.update(status="docker_execution_error", error="Docker 未返回有效的本地镜像 ID。")
        return
    directory = None
    container_run_finished = False
    try:
        if report_file is None:
            _run_container(docker, env, image_id, project, args, None, None, timeout, result)
        else:
            with TemporaryDirectory(prefix="docker-xml-", dir=report_file.parent) as directory:
                _run_container(docker, env, image_id, project, args, report_file, Path(directory), timeout, result)
                container_run_finished = True
            result["docker_xml_staging"] = {"status": "deleted"}
    except (OSError, ValueError) as exc:
        if directory is not None:
            remaining = Path(directory).exists()
            result["docker_xml_staging"] = {
                "status": "cleanup_failed" if remaining else "deleted",
                "path": directory if remaining else None,
            }
        elif report_file is not None:
            result["docker_xml_staging"] = {"status": "not_created"}
        if result["error"] is None:
            status = "cleanup_error" if container_run_finished else "setup_error"
            result.update(status=status, error=f"Docker 测试目录准备或清理失败：{exc}")
        else:
            result["error"] += f" 测试目录清理失败：{exc}"
