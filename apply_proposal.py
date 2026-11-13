import hashlib
from contextlib import ExitStack
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory

from tools.edit_tools import PreparedFile, prepare_file_changes, resolve_edit_target


def sha256_bytes(content: bytes) -> str:
    """计算文件字节的 SHA-256。"""
    return hashlib.sha256(content).hexdigest()


def _read_current(project_root: Path, file: PreparedFile) -> bytes:
    if project_root.resolve(strict=True) != project_root:
        raise ValueError("工作副本目录已重定向。")
    if resolve_edit_target(project_root, file.requested_path) != file.path:
        raise ValueError("文件路径发生变化，请重新预览。")
    return file.path.read_bytes()


def _check_current(project_root: Path, file: PreparedFile, expected_hash: str) -> None:
    if sha256_bytes(_read_current(project_root, file)) != expected_hash:
        raise ValueError("文件在确认期间发生变化或已被外部修改，请重新预览。")


def _replace_file(staged: Path, target: Path) -> None:
    staged.replace(target)


def _write_files(project_root: Path, prepared: list[PreparedFile], results: list[dict]) -> str:
    """准备所有临时文件、复核所有目标，再逐个替换。失败时逆序恢复。"""
    entries = list(zip(prepared, results))
    with ExitStack() as stack:
        staged_files = []
        for file, result in entries:
            try:
                _check_current(project_root, file, result["before_hash"])
                directory = Path(stack.enter_context(
                    TemporaryDirectory(dir=file.path.parent, ignore_cleanup_errors=True)
                ))
                original = directory / "original"
                modified = directory / "modified"
                mode = S_IMODE(file.path.stat().st_mode)
                for path, content in ((original, file.original), (modified, file.modified)):
                    path.write_bytes(content)
                    path.chmod(mode)
                staged_files.append((original, modified))
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="precheck_failed", message=str(exc))
                return "error"

        # 在第一次替换前复核所有文件，包括较早暂存的文件。
        for file, result in entries:
            try:
                _check_current(project_root, file, result["before_hash"])
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="precheck_failed", message=str(exc))
                return "error"

        written = []
        for (file, result), (original, modified) in zip(entries, staged_files):
            try:
                _check_current(project_root, file, result["before_hash"])
                _replace_file(modified, file.path)
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="write_failed", message=str(exc))
                break
            result.update(status="applied", after_hash=sha256_bytes(file.modified))
            written.append((file, result, original))
        else:
            return "applied"

        recovery_failed = False
        for file, result, original in reversed(written):
            try:
                _check_current(project_root, file, result["after_hash"])
                _replace_file(original, file.path)
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="restore_failed", message=str(exc), current_hash=None)
                try:
                    result["current_hash"] = sha256_bytes(_read_current(project_root, file))
                except (OSError, ValueError, RuntimeError):
                    pass
                recovery_failed = True
            else:
                result.update(status="restored", current_hash=result["before_hash"])

        if recovery_failed:
            return "partial"
        return "write_failed_restored" if written else "write_failed"


def confirm_and_apply(project_path: str, proposal: dict) -> dict:
    """一次确认并应用所有文件的变更，同时支持旧的单文件提案。"""
    approval_status = "not_requested"
    results = []

    def outcome(status: str, message: str) -> dict:
        for result in results:
            if result["status"] == "prepared":
                result["status"] = "not_attempted"
        return {
            "status": status, "message": message,
            "approval_status": approval_status, "files": results,
        }

    try:
        project_root = Path(project_path).resolve()
        if "files" in proposal and Path(project_path).absolute() != project_root:
            raise ValueError("多文件提案必须使用本次任务工作副本的实际目录。")
        if not project_root.is_dir() or project_root.resolve(strict=True) != project_root:
            raise ValueError("工作副本目录不存在或已重定向。")

        original_diff = proposal.get("diff")
        if not isinstance(original_diff, str) or not original_diff:
            raise ValueError("提案缺少有效的 diff。")

        single_file_fields = {"relative_path", "replacements", "old_text", "new_text"}
        if "files" in proposal:
            if single_file_fields.intersection(proposal):
                raise ValueError("files 不能与顶层单文件字段一起使用。")
            files = proposal["files"]
        else:
            if "replacements" in proposal and {"old_text", "new_text"}.intersection(proposal):
                raise ValueError("提案不能同时提供 replacements 和顶层 old_text 或 new_text。")
            files = [{
                "relative_path": proposal.get("relative_path"),
                "replacements": proposal.get("replacements", [{
                    "old_text": proposal.get("old_text"), "new_text": proposal.get("new_text"),
                }]),
            }]

        prepared = prepare_file_changes(project_root, files, results)
        for file, result in zip(prepared, results):
            result["before_hash"] = sha256_bytes(file.original)
            try:
                _check_current(project_root, file, result["before_hash"])
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="precheck_failed", message=str(exc))
                raise

        diff = "".join(file.diff for file in prepared)
        if diff != original_diff:
            raise ValueError("当前 diff 与 Agent 的原始预览不一致，请重新分析。")

        print("\n===== 待确认的修改 =====")
        print(diff)
        decision = input("是否将上述修改应用到工作副本？输入 yes 确认，其他内容取消：").strip().lower()
        if decision != "yes":
            approval_status = "rejected"
            return outcome("rejected", "用户取消了修改。")
        approval_status = "approved"

        status = _write_files(project_root, prepared, results)
        if status == "applied":
            result = outcome(status, "修改已应用到工作副本。")
            result["diff"] = diff
            if len(prepared) == 1:
                result.update(
                    file_path=str(prepared[0].path),
                    before_hash=results[0]["before_hash"],
                    after_hash=results[0]["after_hash"],
                )
            return result

        failure = next(item for item in results if item["status"] in {"precheck_failed", "write_failed"})
        message = f'{failure["relative_path"]}：{failure["message"]}'
        if status == "partial":
            message += " 已有文件恢复失败，工作副本可能处于部分应用状态。"
        elif status == "write_failed_restored":
            message += " 本轮已经写入的文件均已恢复到应用前的内容。"
        return outcome(status, message)
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        return outcome("error", f"处理修改时发生错误：{exc}")
