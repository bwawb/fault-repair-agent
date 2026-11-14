import logging
import shutil
import tempfile
from pathlib import Path

from file_access import DEFAULT_FILE_ACCESS, FileAccessDenied, FileAccessPolicy


def _copy_project(source: Path, destination: Path, policy: FileAccessPolicy) -> None:
    skipped = 0

    def ignore(directory, names):
        nonlocal skipped
        excluded = []
        for name in names:
            relative_path = (Path(directory) / name).relative_to(source)
            try:
                policy.inspect(source, relative_path)
            except FileAccessDenied:
                excluded.append(name)
        skipped += len(excluded)
        return excluded

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    if skipped:
        logging.getLogger(__name__).warning("复制项目时，文件访问策略跳过了 %d 个文件或目录。", skipped)


def create_workspace(
    source_project: str,
    workspace_base: str,
    *,
    policy: FileAccessPolicy = DEFAULT_FILE_ACCESS,
) -> Path:
    """
    将示例项目复制到用户指定的工作目录。

    Args:
        source_project: 原始项目的根目录。
        workspace_base: 保存工作副本的父目录。

    Returns:
        本次任务工作副本的项目根目录。
    """
    source_path = Path(source_project).resolve()
    base_path = Path(workspace_base).resolve()

    if not source_path.is_dir():
        raise ValueError(
            f"源项目不存在或不是目录：{source_path}"
        )

    # 避免将工作副本创建在源项目内部。
    if base_path.is_relative_to(source_path):
        raise ValueError(
            "工作副本的父目录不能放在源项目内部，"
            "请指定一个独立的目录。"
        )

    # 如果指定的父目录不存在，就创建它。
    base_path.mkdir(parents=True, exist_ok=True)

    # 在指定父目录下创建一个唯一的任务目录。
    workspace_root = Path(
        tempfile.mkdtemp(
            prefix="fault_repair_",
            dir=str(base_path),
        )
    )

    working_project = workspace_root / "project"

    # 将原项目复制到任务目录。
    _copy_project(source_path, working_project, policy)

    return working_project

def create_baseline_snapshot(
    working_project: str, *, policy: FileAccessPolicy = DEFAULT_FILE_ACCESS
) -> Path:
    """
    复制刚创建的工作副本，作为任务开始时的项目快照。

    Args:
        working_project:当前任务的工作副本目录

    Returns:
        基线快照的目录路径

    Raises:
        ValueError: 工作副本不存在或不是目录
        FileExistsError: 基线快照目录已经存在
    """
    project_path = Path(working_project).resolve()
    if not project_path.is_dir():
        raise ValueError(
            f"工作副本不存在或不是目录：{project_path}"
        )

    # 与 project 处于同一个任务目录中。
    snapshot_path = project_path.parent / "baseline_project"

    # 不覆盖已有快照，避免丢失最初状态。
    if snapshot_path.exists():
        raise FileExistsError(
            f"基线快照已经存在：{snapshot_path}"
        )

    _copy_project(project_path, snapshot_path, policy)

    return snapshot_path
