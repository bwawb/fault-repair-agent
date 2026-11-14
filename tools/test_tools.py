import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISREG
from tempfile import mkdtemp

from compare_test_reports import read_test_report
from tools.local_test_env import build_local_test_env


DEFAULT_TIMEOUT = 30
OUTPUT_LIMIT = 64 * 1024
_READ_SIZE = 8192
_POLL_INTERVAL = 0.02
_CLEANUP_TIMEOUT = 3
TEST_RESULT_STATUSES = {"passed", "failed"}
REPORT_ERROR_STATUSES = {"report_missing", "report_parse_error"}


def _is_report_file(path: Path) -> bool:
    info = path.lstat()
    return (S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


class _OutputTail:
    """持续排空管道，只保留最近的 OUTPUT_LIMIT 字节。"""

    def __init__(self, stream):
        self.fd = stream.fileno()
        os.set_blocking(self.fd, False)
        self.data = bytearray()
        self.truncated = False

    def read_chunk(self) -> bool:
        try:
            chunk = os.read(self.fd, _READ_SIZE)
        except BlockingIOError:
            return False
        if not chunk:
            return False

        overflow = len(self.data) + len(chunk) - OUTPUT_LIMIT
        if overflow > 0:
            self.truncated = True
            del self.data[:overflow]
        self.data.extend(chunk)
        return True

    def render(self, name: str) -> str:
        output = self.data.decode("utf-8", errors="replace").strip()
        if self.truncated:
            return f"[{name} 输出已截断，仅保留末尾 {OUTPUT_LIMIT} 字节]\n{output}"
        return output


def _terminate_process(process: subprocess.Popen, *, job=None, is_launcher=False) -> dict:
    """尝试清理进程树，并单独检查直接管理的进程。"""
    cleanup = {
        "strategy": "windows_job" if job else "process_group" if os.name != "nt" else "direct_process",
        "tree_request_succeeded": False,
        "direct_process": "launcher" if is_launcher else "pytest",
        "direct_process_exited": False,
        "errors": [],
    }
    try:
        if job:
            job.terminate()
            cleanup["tree_request_succeeded"] = True
        elif os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
            cleanup["tree_request_succeeded"] = True
    except OSError as exc:
        cleanup["errors"].append(f"进程树清理失败：{exc}")

    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_CLEANUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup["errors"].append(f"直接管理的进程清理失败：{exc}")
    cleanup["direct_process_exited"] = process.poll() is not None
    return cleanup


def _collect_output(process: subprocess.Popen, stdout: _OutputTail, stderr: _OutputTail, timeout: float) -> bool:
    """同时排空两个管道，不等待后代进程关闭继承的句柄。"""
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if time.monotonic() >= deadline:
            return False
        # 两边都读，避免一侧输出太多导致另一侧的管道堵塞。
        stdout_read = stdout.read_chunk()
        stderr_read = stderr.read_chunk()
        if not stdout_read and not stderr_read:
            time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))
    return True


def _drain_pending_output(stream: _OutputTail) -> None:
    # pytest 已退出时排空管道中已到达的数据。仍持有管道的后代不能让收集无限等待。
    for _ in range(128):
        if not stream.read_chunk():
            return
    stream.truncated = True


def collect_process_result(process, timeout, result, cleanup) -> None:
    """本地 pytest 和 Docker CLI 共用同一套限量输出和超时收集逻辑。"""
    stdout = stderr = None
    try:
        stdout = _OutputTail(process.stdout)
        stderr = _OutputTail(process.stderr)
        if not _collect_output(process, stdout, stderr, timeout):
            result.update(status="timeout", error=f"命令运行超过 {timeout} 秒。")
            result["process_cleanup"] = cleanup()
        _drain_pending_output(stdout)
        _drain_pending_output(stderr)
    except (OSError, ValueError) as exc:
        if result["status"] == "timeout":
            result["error"] += f" 读取剩余输出失败：{exc}"
        else:
            result.update(status="execution_error", error=f"读取输出失败：{exc}")
            result["process_cleanup"] = cleanup()
    except BaseException:
        cleanup()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()

    result["exit_code"] = process.poll()
    if stdout is not None and stderr is not None:
        output = stdout.render("stdout")
        error_output = stderr.render("stderr")
        if error_output:
            output = f"{output}\n错误输出：\n{error_output}".strip()
        result.update(
            output=output,
            output_truncated=stdout.truncated or stderr.truncated,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
        )


class TestTools:
    """在指定工作副本中执行 pytest。"""

    def __init__(
        self, project_path: str, *, execution_mode: str = "local", docker_image: str | None = None,
        local_env_names: Sequence[str] = (), keep_junit_xml: bool = True,
    ):
        self.project_path = Path(project_path).resolve()
        if not self.project_path.is_dir():
            raise ValueError(f"项目路径不存在或不是目录：{self.project_path}")
        if execution_mode not in {"local", "docker"}:
            raise ValueError(f"未知测试执行后端：{execution_mode}")
        if execution_mode != "local" and local_env_names:
            raise ValueError("额外测试环境变量仅适用于 local 后端。")
        if isinstance(local_env_names, str):
            raise ValueError("local_env_names 必须是变量名序列，例如 ('TEST_MODE',)。")
        self.execution_mode = execution_mode
        self.docker_image = docker_image
        self.local_env_names = tuple(local_env_names)
        self.keep_junit_xml = keep_junit_xml

    def run_tests(self, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """运行 tests 目录，返回 pytest 状态和限量输出。"""
        return self._run_pytest(timeout)

    def run_tests_with_report(self, report_path: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """运行同一套 pytest 命令，并解析本次生成的 JUnit XML。"""
        return self._run_pytest(timeout, report_path)

    def _run_pytest(self, timeout: int, report_path: str | None = None) -> dict:
        result = {
            "execution_mode": self.execution_mode,
            "status": "setup_error",
            "exit_code": None,
            "output": "",
            "output_truncated": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "error": None,
            "report_path": None,
            "test_results": None,
            "process_cleanup": None,
            "junit_xml": {"status": "not_requested"},
        }
        args = ["tests", "-q", "--capture=no"]
        report_file = None
        staging = None
        try:
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("测试超时必须是有限的正数。")
            if not (self.project_path / "tests").is_dir():
                raise ValueError("项目中没有找到 tests 目录。")
            if report_path is not None:
                requested_report = Path(report_path)
                if requested_report.is_symlink() or requested_report.exists():
                    raise ValueError(f"报告路径已存在：{requested_report}")
                report_file = requested_report.resolve()
                report_file.parent.mkdir(parents=True, exist_ok=True)
                if not self.keep_junit_xml:
                    staging = Path(mkdtemp(prefix="junit-", dir=report_file.parent))
                    report_file = staging / "result.xml"
                result["report_path"] = str(report_file)
                result["junit_xml"]["status"] = "pending"
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            result["error"] = str(exc)
            return result

        try:
            self._execute_pytest(args, timeout, report_file, result)
        finally:
            if report_file is not None:
                self._finish_xml(report_file, staging, result)
        return result

    def _execute_pytest(self, args, timeout, report_file, result):
        if self.execution_mode == "docker":
            from tools.docker_test_backend import run_docker_pytest

            run_docker_pytest(self.project_path, self.docker_image, args, report_file, timeout, result)
        else:
            if report_file is not None:
                args.append(f"--junitxml={report_file}")
            self._run_local(args, timeout, result)
        if result["error"] is not None:
            return
        if result["exit_code"] not in (0, 1):
            result.update(status="pytest_error", error=f"pytest 异常结束，退出码为 {result['exit_code']}。")
            return

        result["status"] = "passed" if result["exit_code"] == 0 else "failed"
        if report_file is not None:
            self._read_report(result, report_file)

    @staticmethod
    def _finish_xml(report_file, staging, result):
        state = result["junit_xml"]
        try:
            if report_file.parent.resolve(strict=True) != report_file.parent:
                raise ValueError("JUnit XML 目录已重定向。")
            try:
                ordinary_file = _is_report_file(report_file)
            except FileNotFoundError:
                state["status"] = "not_created"
            else:
                if not ordinary_file:
                    raise ValueError("JUnit XML 不是可确认归属的普通文件。")
                if staging is None:
                    state["status"] = "retained"
                else:
                    report_file.unlink()
                    state["status"] = "deleted"
                    result["report_path"] = None
            if staging is not None:
                result["report_path"] = None
                staging.rmdir()
        except ValueError as exc:
            state.update(status="unverified", error=str(exc))
        except OSError as exc:
            state.update(status="cleanup_failed", error=str(exc))
        if staging is not None and state["status"] in {"cleanup_failed", "unverified"}:
            state["staging_path"] = str(staging)

    def _run_local(self, args: list[str], timeout: float, result: dict) -> None:
        try:
            env = build_local_test_env(self.local_env_names)
        except ValueError as exc:
            result.update(status="setup_error", error=str(exc))
            return
        process = job = None
        launcher_failure_code = None
        try:
            if os.name == "nt":
                from tools.windows_test_job import PYTEST_LAUNCHER, PYTEST_START_FAILED, WindowsTestJob

                job = WindowsTestJob()
                launcher_failure_code = PYTEST_START_FAILED
                command = [sys.executable, "-I", "-S", "-X", "utf8", "-c", PYTEST_LAUNCHER, *args]
                # 启动器加入 Job 后，才会启动常规的 python -m pytest 子进程。
            else:
                command = [sys.executable, "-m", "pytest", *args]
            process = subprocess.Popen(
                command, cwd=self.project_path, stdin=subprocess.PIPE if job else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                shell=False, env=env, start_new_session=os.name != "nt",
            )
            if job:
                job.attach(process)
                process.stdin.write(b"1")
                process.stdin.close()
        except OSError as exc:
            result.update(status="launch_error", error=f"启动 pytest 失败：{exc}")
            if process is not None:
                result["process_cleanup"] = _terminate_process(
                    process, job=job if job and job.process_attached else None, is_launcher=job is not None,
                )
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
            if job:
                job.close()
            return

        try:
            collect_process_result(
                process, timeout, result,
                lambda: _terminate_process(process, job=job, is_launcher=job is not None),
            )
        finally:
            if job:
                job.close()

        if result["error"] is None and launcher_failure_code is not None and result["exit_code"] == launcher_failure_code:
            result.update(status="launch_error", error="Windows 启动器未能启动 pytest，详情见输出。")

    @staticmethod
    def _read_report(result: dict, report_file: Path) -> None:
        try:
            if (report_file.parent.resolve(strict=True) != report_file.parent
                    or not _is_report_file(report_file)):
                raise ValueError("JUnit XML 不是工作目录中生成的普通报告文件。")
            test_results = read_test_report(str(report_file))
            has_failure = any(status in {"failed", "error"} for status in test_results.values())
            if has_failure != (result["exit_code"] == 1):
                raise ValueError("JUnit XML 的测试状态与 pytest 退出码不一致。")
        except FileNotFoundError as exc:
            result.update(status="report_missing", error=str(exc))
        except (OSError, ValueError) as exc:
            result.update(status="report_parse_error", error=str(exc))
        else:
            result["test_results"] = test_results
