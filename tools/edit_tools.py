from dataclasses import dataclass
from pathlib import Path

from typing_extensions import TypedDict

from file_access import DEFAULT_FILE_ACCESS, FileAccessPolicy
from patch_summary import build_text_diff


class TextReplacement(TypedDict):
    old_text: str
    new_text: str


class FileChange(TypedDict):
    relative_path: str
    replacements: list[TextReplacement]


@dataclass
class PreparedFile:
    requested_path: str
    path: Path
    original: bytes
    modified: bytes
    diff: str


def validate_file_changes(files: object) -> None:
    if not isinstance(files, list) or not files:
        raise ValueError("files 必须是非空列表。")
    for index, entry in enumerate(files, start=1):
        if not isinstance(entry, dict) or entry.keys() != {"relative_path", "replacements"}:
            raise ValueError(f"第 {index} 个文件必须只包含 relative_path 和 replacements。")


def resolve_edit_target(
    project_root: Path, relative_path: str, *, policy: FileAccessPolicy = DEFAULT_FILE_ACCESS
) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip() or Path(relative_path).anchor:
        raise ValueError("请提供相对于工作副本的文件路径。")
    path = policy.resolve_file(project_root, relative_path)
    if any(Path(part) == Path("tests") for part in path.relative_to(project_root).parts):
        raise ValueError("当前阶段不允许修改 tests 目录中的文件。")
    return path


def prepare_file_changes(
    project_root: Path, files: list[FileChange], results: list[dict],
    *, policy: FileAccessPolicy = DEFAULT_FILE_ACCESS,
) -> list[PreparedFile]:
    """预检所有文件，并在失败时留下对应的文件结果。"""
    validate_file_changes(files)
    results.extend({"relative_path": entry["relative_path"], "status": "not_attempted"} for entry in files)
    prepared = []
    targets = set()
    for entry, result in zip(files, results):
        relative_path = entry["relative_path"]
        try:
            path = resolve_edit_target(project_root, relative_path, policy=policy)
            if path in targets:
                raise ValueError("同一个文件不能在 files 中重复出现。")
            targets.add(path)
            original = path.read_bytes()
            modified, diff = prepare_replacements(original, relative_path, entry["replacements"])
        except UnicodeError as exc:
            message = "文件和提案必须能够按 UTF-8 处理。"
            result.update(status="validation_failed", message=message)
            raise ValueError(f"{relative_path}：{message}") from exc
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(status="validation_failed", message=str(exc))
            raise ValueError(f"{relative_path}：{exc}") from exc

        prepared.append(PreparedFile(relative_path, path, original, modified, diff))
        result.update(status="prepared", relative_path=path.relative_to(project_root).as_posix())
    return prepared


def validate_replacements(replacements: object) -> None:
    if not isinstance(replacements, list) or not replacements:
        raise ValueError("replacements 必须是非空列表。")
    for index, replacement in enumerate(replacements, start=1):
        if not isinstance(replacement, dict) or replacement.keys() != {"old_text", "new_text"}:
            raise ValueError(f"第 {index} 处修改必须只包含 old_text 和 new_text。")
        if not isinstance(replacement["old_text"], str) or not replacement["old_text"]:
            raise ValueError(f"第 {index} 处修改的原文本不能为空，且必须是字符串。")
        if not isinstance(replacement["new_text"], str):
            raise ValueError(f"第 {index} 处修改的新文本必须是字符串。")


def prepare_replacement(
    original_bytes: bytes, relative_path: str, old_text: str, new_text: str
) -> tuple[bytes, str]:
    return prepare_replacements(
        original_bytes, relative_path, [{"old_text": old_text, "new_text": new_text}]
    )


def prepare_replacements(
    original_bytes: bytes, relative_path: str, replacements: list[TextReplacement]
) -> tuple[bytes, str]:
    """先在原文中定位所有替换范围，再生成待写入字节和统一 diff。"""
    validate_replacements(replacements)
    original_text = original_bytes.decode("utf-8")
    if "\0" in original_text:
        raise ValueError("文本替换不支持包含 NUL 字符的内容。")

    uses_crlf = "\r\n" in original_text
    without_crlf = original_text.replace("\r\n", "")
    mixed_newlines = "\r" in without_crlf or (uses_crlf and "\n" in without_crlf)
    matches = []
    for index, replacement in enumerate(replacements, start=1):
        old_text = replacement["old_text"]
        new_text = replacement["new_text"]
        if "\0" in old_text or "\0" in new_text:
            raise ValueError(f"第 {index} 处修改包含 NUL 字符。")

        if any("\n" in text or "\r" in text for text in (old_text, new_text)):
            if mixed_newlines:
                raise ValueError("多行替换仅支持统一使用 LF 或 CRLF 的文件。")
            old_text = old_text.replace("\r\n", "\n")
            new_text = new_text.replace("\r\n", "\n")
            if "\r" in old_text or "\r" in new_text:
                raise ValueError(f"第 {index} 处修改只支持 LF 或 CRLF，不支持单独的 CR。")
            if uses_crlf:
                old_text = old_text.replace("\n", "\r\n")
                new_text = new_text.replace("\n", "\r\n")

        if old_text == new_text:
            raise ValueError(f"第 {index} 处修改的原文本和新文本相同，无需修改。")

        start = original_text.find(old_text)
        if start == -1:
            raise ValueError(f"第 {index} 处修改没有找到原文本，请检查文件内容。")
        if original_text.find(old_text, start + 1) != -1:
            raise ValueError(f"第 {index} 处修改的原文本出现多次，请提供更完整的原文本。")
        matches.append((start, start + len(old_text), index, new_text))

    parts = []
    previous_end = 0
    previous_index = 0
    for start, end, index, new_text in sorted(matches):
        if start < previous_end:
            raise ValueError(f"第 {previous_index} 处和第 {index} 处修改的匹配范围重叠。")
        parts.extend((original_text[previous_end:start], new_text))
        previous_end = end
        previous_index = index
    parts.append(original_text[previous_end:])
    modified_text = "".join(parts)
    diff = build_text_diff(
        original_text, modified_text,
        fromfile=f"修改前/{relative_path}", tofile=f"修改后/{relative_path}",
    )
    if not diff:
        raise ValueError("全部替换没有产生可预览的内容变化。")
    return modified_text.encode("utf-8"), diff


class EditTools:
    """在指定项目目录内预览文本修改"""

    def __init__(self, project_path: str, *, policy: FileAccessPolicy = DEFAULT_FILE_ACCESS):
        """
        初始化修改工具。

        Args:
            project_path: 待分析项目的根目录。
        """
        self.project_path=Path(project_path).resolve()
        self.policy = policy

        if not self.project_path.is_dir():
            raise ValueError( f"项目路径不是目录：{self.project_path}")

    def preview_file_changes(self, files: list[FileChange]) -> dict:
        """预览本次任务的全部文件变更。"""
        results = []
        try:
            prepared = prepare_file_changes(self.project_path, files, results, policy=self.policy)
        except ValueError as exc:
            return {"success": False, "message": str(exc), "diff": "", "files": results}
        return {
            "success": True,
            "message": "已生成所有文件的修改预览。",
            "diff": "".join(file.diff for file in prepared),
            "files": results,
        }

    def preview_replace(self, relative_path: str, old_text: str, new_text: str) -> dict:
        """预览一处替换。"""
        return self.preview_replacements(
            relative_path, [{"old_text": old_text, "new_text": new_text}]
        )

    def preview_replacements(self, relative_path: str, replacements: list[TextReplacement]) -> dict:
        """预览同一文件中的全部替换。"""
        try:
            file_path = resolve_edit_target(self.project_path, relative_path, policy=self.policy)
            _, diff = prepare_replacements(file_path.read_bytes(), relative_path, replacements)
        except UnicodeError:
            return {
                "success": False,
                "message": "文件和提案必须能够按 UTF-8 处理。",
                "diff": "",
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "success": False,
                "message": str(exc),
                "diff": "",
            }

        return {
            "success": True,
            "message": "修改预览已生成，原文件未修改。",
            "diff": diff,
        }
