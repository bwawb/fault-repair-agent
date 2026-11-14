import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from stat import S_IMODE
from tempfile import NamedTemporaryFile

from apply_proposal import sha256_bytes
from output_protection import OutputProtectionError, current_output, emit, protect_output
from run_report import write_run_report


def _task_file_paths(workspace: dict, relative_path: str) -> tuple[Path, Path, str]:
    working_project = workspace.get("working_project")
    baseline_project = workspace.get("baseline_project")
    source_project = workspace.get("source_project")
    paths = (working_project, baseline_project, source_project)
    if any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths):
        raise ValueError("任务报告缺少工作副本、基线快照或原始项目的绝对路径。")

    working_root = Path(working_project)
    baseline_root = Path(baseline_project)
    source_root = Path(source_project).resolve()
    if working_root.name != "project" or baseline_root != working_root.parent / "baseline_project":
        raise ValueError("工作副本与基线快照不属于同一个任务目录。")

    for root, label in ((working_root, "工作副本"), (baseline_root, "基线快照")):
        if not root.is_dir() or root.resolve() != root:
            raise ValueError(f"{label}目录不存在或已重定向：{root}")
        if root.is_relative_to(source_root) or source_root.is_relative_to(root):
            raise ValueError(f"{label}与原始项目路径重叠。")

    requested_path = Path(relative_path)
    if not relative_path.strip() or requested_path.anchor:
        raise ValueError("请提供相对于工作副本的文件路径。")

    requested_file = working_root / requested_path
    working_file = requested_file.resolve()
    if not working_file.is_relative_to(working_root):
        raise ValueError("目标路径超出工作副本目录。")

    project_path = working_file.relative_to(working_root)
    baseline_file = baseline_root / project_path
    resolved_baseline = baseline_file.resolve()
    if not resolved_baseline.is_relative_to(baseline_root):
        raise ValueError("目标路径超出基线快照目录。")

    for path, label in ((working_file, "工作副本"), (baseline_file, "基线快照")):
        if not path.exists():
            raise FileNotFoundError(f"{label}中不存在该文件：{project_path.as_posix()}")
        if not path.is_file():
            raise ValueError(f"{label}中的目标不是普通文件：{project_path.as_posix()}")
    if requested_file.is_symlink() or resolved_baseline != baseline_file:
        raise ValueError("回滚目标必须是普通文件，不能通过文件链接恢复。")

    return working_file, baseline_file, project_path.as_posix()


def _replace_working_file(path: Path, contents: bytes, expected_hash: str) -> None:
    """在目标同目录准备文件，并在替换前再次核对目标。"""
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError("工作副本文件的父目录已重定向，停止回滚。")

    temporary_file = NamedTemporaryFile(dir=path.parent, delete=False)
    temporary_path = Path(temporary_file.name)
    try:
        with temporary_file as file:
            file.write(contents)
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("工作副本文件已移动或重定向，停止回滚。")
        temporary_path.chmod(S_IMODE(path.stat().st_mode))
        if sha256_bytes(path.read_bytes()) != expected_hash:
            raise ValueError("工作副本文件在回滚期间发生变化，已保留当前内容。")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_finished_report(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    task = report.get("task") if isinstance(report, dict) else None
    if not isinstance(task, dict) or not task.get("completed_at"):
        raise ValueError("请使用已结束任务的 JSON 报告。")

    workspace = report.get("workspace", {})
    if not isinstance(workspace, dict) or not isinstance(report.get("rollbacks", []), list):
        raise ValueError("任务报告中的工作区或回滚记录格式无效。")

    for key in ("source_project", "baseline_project"):
        project = workspace.get(key)
        if isinstance(project, str) and report_path.is_relative_to(Path(project).resolve()):
            raise ValueError("任务报告必须位于原始项目和基线快照目录之外。")
    return report


def rollback_file(report_path: str | Path, relative_path: str, *, sensitive_values=()) -> dict:
    """显式恢复已结束任务中的一个文件，并更新指定的任务报告。"""
    with protect_output(sensitive_values) as protection:
        try:
            result = _rollback_file(report_path, relative_path)
        except (OSError, ValueError, RuntimeError) as exc:
            if not protection.enabled or isinstance(exc, OutputProtectionError):
                raise
            raise ValueError(protection.display(exc)) from None
        if protection.enabled and result["reason"]:
            return {**result, "reason": protection.display(result["reason"])}
        return result


def _rollback_file(report_path: str | Path, relative_path: str) -> dict:
    protection = current_output()
    protection.require_exact(report_path, relative_path)
    report_path = Path(report_path).resolve(strict=True)
    protection.require_exact(report_path)
    original_report = _read_finished_report(report_path)
    if original_report.get("output_protection", {}).get("known_values_redacted") and not protection.enabled:
        raise ValueError("此报告启用了脱敏，请通过 rollback_file(..., sensitive_values=...) 重新提供本次任务的配置。")
    protection.report_copy(original_report)

    # 同一任务的回滚串行进行，避免两个进程覆盖彼此的报告记录。
    lock_path = report_path.with_name(report_path.name + ".rollback.lock")
    try:
        with lock_path.open("x"):
            pass
    except FileExistsError as exc:
        raise ValueError("该任务报告已有回滚操作在进行。") from exc

    try:
        report = _read_finished_report(report_path)
        if report.get("output_protection", {}).get("known_values_redacted") and not protection.enabled:
            raise ValueError("此报告启用了脱敏，回滚需要重新提供本次任务的配置。")
        protection.report_copy(report)
        history = report.setdefault("rollbacks", [])
        result = {
            "relative_path": relative_path,
            "status": "error",
            "timestamp": datetime.now().astimezone().isoformat(),
            "reason": None,
        }
        try:
            working_file, baseline_file, project_path = _task_file_paths(
                report.get("workspace", {}), relative_path
            )
            result["relative_path"] = project_path
            if working_file == report_path:
                raise ValueError("不能回滚正在使用的任务报告。")

            summary = report.get("patch_summary")
            files = summary.get("files") if isinstance(summary, dict) else None
            patch = files.get(project_path) if isinstance(files, dict) else None
            expected_hash = patch.get("file_hash") if isinstance(patch, dict) else None
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise ValueError("报告缺少该文件上次记录的 SHA-256，无法确认文件状态。")

            previous_contents = working_file.read_bytes()
            if sha256_bytes(previous_contents) != expected_hash:
                raise ValueError("工作副本文件与上次记录的 SHA-256 不符，已保留当前内容。")
            baseline_contents = baseline_file.read_bytes()
            baseline_hash = sha256_bytes(baseline_contents)
            protection.require_exact(project_path, working_file, baseline_file, baseline_hash)
            changed = previous_contents != baseline_contents
            if changed:
                _replace_working_file(working_file, baseline_contents, expected_hash)
        except (OSError, ValueError, RuntimeError) as exc:
            result["reason"] = str(exc)
            history.append(result)
            write_run_report(report, report_path)
            return result

        result["status"] = "rolled_back" if changed else "already_at_baseline"
        history.append(result)
        files[project_path] = {
            "status": "unchanged",
            "relative_path": project_path,
            "diff": "",
            "message": None,
            "file_hash": baseline_hash,
        }
        try:
            write_run_report(report, report_path)
        except (OSError, ValueError) as exc:
            if changed:
                try:
                    _replace_working_file(working_file, previous_contents, baseline_hash)
                except (OSError, ValueError, RuntimeError) as restore_error:
                    raise RuntimeError(
                        f"回滚后的报告保存失败，且无法恢复回滚前内容：{restore_error}"
                    ) from exc
            raise RuntimeError(f"回滚报告保存失败，工作副本保留回滚前状态：{exc}") from exc
        return result
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="将工作副本中的一个文件恢复到任务开始时的基线。")
    parser.add_argument("report_path", type=Path, help="已结束任务的 JSON 报告")
    parser.add_argument("relative_path", help="相对于工作副本的文件路径")
    args = parser.parse_args()

    try:
        result = rollback_file(args.report_path, args.relative_path)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"回滚失败：{exc}\n")
    emit(json.dumps(result, ensure_ascii=False))
    return int(result["status"] == "error")


if __name__ == "__main__":
    raise SystemExit(main())
