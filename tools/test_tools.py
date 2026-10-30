import subprocess
import sys
from pathlib import Path

class TestTools:
    """在指定项目中运行pytest测试"""

    def __init__(self, project_path: str):
        """
        初始化测试工具

        Args:
            project_path：待分析项目的根目录
        """
        self.project_path = Path(project_path).resolve()

        if not self.project_path.exists():
            raise ValueError(f"项目路径不存在：{self.project_path}")

        if not self.project_path.is_dir():
            raise ValueError(f"项目路径不是目录：{self.project_path}")

    def run_tests(self, timeout: int = 30) -> dict:
        """
        运行项目tests目录总的pytest测试

        Args:
            timeout：测试运行的最长等待四件，单位为秒

        Returns:
            包含执行状态、退出码和测试输出的字典
        """
        tests_path = self.project_path / "tests"

        if not tests_path.is_dir():
            return {
                "status": "error",
                "exit_code": None,
                "output": "运行失败，项目中没有找到tests目录",
            }

        # sys.executable适当agent使用的是python解释器
        command = [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
        ]

        try:
            result = subprocess.run(
                command,
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors="replace",
                timeout=timeout,
                shell=False
            )

            output = result.stdout

            if result.stderr:
                output += "\n错误输出：\n" + result.stderr

            return {
                "status": "completed",
                "exit_code": result.returncode,
                "output": output.strip()
            }

        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "exit_code": None,
                "output": f"测试运行超时，已超过 {timeout} 秒。",
            }
        except OSError as exc:
            return {
                "status": "error",
                "exit_code": None,
                "output": f"启动测试失败：{exc}",
            }

    def run_tests_with_report(self, report_path: str, timeout: int=30,) -> dict:
        """
        运行tests目录中的pytest测试，并生成JUnit XML报告

        Args:
            report_path:本次测试报告的保存路径
            time_out:测试运行最长等待时间，单位为秒

        Returns:
            包含运行状态、退出码、输出和报告路径的字典
        """

        tests_path = self.project_path / "tests"
        report_file = Path(report_path).resolve()

        if not tests_path.is_dir():
            return {
                "status": "error",
                "exit_code": None,
                "output": "运行失败：项目中没有找到 tests 目录。",
                "report_path": None,
            }

        # 为避免误用旧报告，要求本次报告路径尚不存在。
        if report_file.exists():
            return {
                "status": "error",
                "exit_code": None,
                "output": f"报告文件已经存在：{report_file}",
                "report_path": None,
            }

        try:
            report_file.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            command = [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-q",
                f"--junitxml={report_file}"
            ]

            result = subprocess.run(
                command,
                cwd=self.project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )

            output = result.stdout

            if result.stderr:
                output += "\n错误输出：\n" + result.stderr

            # 0：测试通过；1：测试执行完成但有失败。
            # 其他退出码暂时视为不能用于本阶段比较。

            if result.returncode not in (0, 1):
                return {
                    "status": "error",
                    "exit_code": result.returncode,
                    "output": output.strip(),
                    "report_path": None,
                }

            if not report_file.is_file():
                return {
                    "status": "error",
                    "exit_code": result.returncode,
                    "output": (
                            "pytest 已结束，但没有找到生成的 XML 报告。\n"
                            + output.strip()
                    ),
                    "report_path": None,
                }

            return {
                "status": "completed",
                "exit_code": result.returncode,
                "output": output.strip(),
                "report_path": str(report_file),
            }

        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "exit_code": None,
                "output": f"测试运行超过 {timeout} 秒，已超时。",
                "report_path": None,
            }

        except OSError as exc:
            return {
                "status": "error",
                "exit_code": None,
                "output": f"运行测试或创建报告失败：{exc}",
                "report_path": None,
            }