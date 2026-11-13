from difflib import unified_diff
from io import StringIO
from pathlib import Path


def build_text_diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    """统一 diff 的显示换行，并标记原文末尾缺少换行的位置。"""
    diff_lines = unified_diff(
        StringIO(before, newline=None).readlines(),
        StringIO(after, newline=None).readlines(),
        fromfile=fromfile,
        tofile=tofile,
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in diff_lines
    )


def build_file_diff(
        baseline_project: str,
        working_project: str,
        relative_path: str
) -> str:
    """
    比较基线快照和当前工作副本中的同一个文本文件。

    Args:
        baseline_project: 任务开始时的基线快照目录。
        working_project: 当前任务的工作副本目录。
        relative_path: 相对于项目根目录的文件路径。

    Returns:
        unified diff 字符串；文件内容相同时返回空字符串。

    Raises:
        ValueError: 路径不是有效的项目内相对路径。
        FileNotFoundError: 基线或工作副本中的文件不存在。
        UnicodeError: 文件无法按 UTF-8 解码。
    """
    baseline_root = Path(baseline_project).resolve()
    working_root = Path(working_project).resolve()
    requested_path = Path(relative_path)

    if requested_path.is_absolute():
        raise ValueError("文件路径必须是相对路径。")

    baseline_file = (baseline_root / requested_path).resolve()
    working_file = (working_root / requested_path).resolve()

    if not baseline_file.is_relative_to(baseline_root):
        raise ValueError("基线文件路径超出项目目录。")


    if not working_file.is_relative_to(working_root):
        raise ValueError("工作文件路径超出项目目录。")

    if not baseline_file.is_file():
        raise FileNotFoundError(f"基线快照中没有找到文件：{relative_path}")

    if not working_file.is_file():
        raise FileNotFoundError(f"工作副本中没有找到文件：{relative_path}")

    return build_text_diff(
        baseline_file.read_text(encoding="utf-8"),
        working_file.read_text(encoding="utf-8"),
        fromfile=f"baseline/{requested_path.as_posix()}",
        tofile=f"project/{requested_path.as_posix()}",
    )

