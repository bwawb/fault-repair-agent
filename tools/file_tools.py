import stat
from pathlib import Path

from file_access import DEFAULT_FILE_ACCESS, FileAccessDenied, FileAccessPolicy


class FileTools:
    """在指定项目目录内执行文件读取操作"""

    def __init__(self, project_path: str, *, policy: FileAccessPolicy = DEFAULT_FILE_ACCESS):
        """
        初始化文件工具

        Args:
            project_path: 待分析项目的根目录
        """
        self.project_path = Path(project_path).resolve()
        self.policy = policy

        if not self.project_path.exists():
            raise ValueError(f"项目路径不存在：{self.project_path}")
        if not self.project_path.is_dir():
            raise ValueError(f"项目路径不是目录：{self.project_path}")


    def read_file(self, relative_path: str)->str:
        """
        读取项目内的文本文件。

        Args:
            relative_path: 相对于根目录的文件路径。

        Return:
            文件内容，如果读取失败，返回错误信息。
        """

        try:
            file_path = self.policy.resolve_file(self.project_path, relative_path)
            content = file_path.read_text(encoding="utf-8")

            return (
                f"文件路径：{relative_path}\n"
                f"文件内容：\n"
                f"{content}"
            )
        except UnicodeError:
            return f"读取失败：文件不是可用UTF-8解码的文本文件：{relative_path}"
        except FileNotFoundError:
            return f"读取失败：文件不存在：{relative_path}"
        except (OSError, ValueError, RuntimeError) as exc:
            return f"读取失败：{exc}"

    def _walk_files(self, *, hide_idea: bool = False):
        """逐级检查后再入栈，遇到被拒绝或不可检查的条目返回 None。"""
        directories = [Path()]
        while directories:
            relative_dir = directories.pop()
            try:
                if relative_dir.parts:
                    self.policy.inspect(self.project_path, relative_dir)
                elif self.project_path.resolve(strict=True) != self.project_path:
                    raise FileAccessDenied("工作目录已重定向。")
                children = sorted((self.project_path / relative_dir).iterdir())
            except (OSError, ValueError, RuntimeError):
                yield None
                continue
            for child in children:
                if hide_idea and child.name.casefold() == ".idea":
                    continue
                relative = child.relative_to(self.project_path)
                try:
                    resolved, info = self.policy.inspect(self.project_path, relative)
                except (OSError, ValueError, RuntimeError):
                    yield None
                    continue
                if stat.S_ISDIR(info.st_mode):
                    directories.append(relative)
                else:
                    yield relative, resolved, info

    def list_files(self) -> str:
        """
        列出项目中的文件.

        Returns:
            项目文件的相对路径列表，或相应的错误信息。
        """

        files = []
        skipped = 0
        for entry in self._walk_files(hide_idea=True):
            if entry is None:
                skipped += 1
            else:
                files.append(entry[0].as_posix())
        message = "项目文件列表：\n" + "\n".join(sorted(files)) if files else "项目中没有找到文件"
        if skipped:
            message += f"\n已跳过 {skipped} 个受保护、不安全或不可访问的条目。"
        return message


    def search_code(self, query: str) -> str:
        """
        在项目中的 Python 文件里搜索关键词。

        Args:
            query: 要搜索的文本，例如函数名或异常名称。

        Returns:
            匹配的文件路径、行号和代码行。
        """

        query = query.strip()

        if not query:
            return "搜索失败：关键词不能为空"

        max_results = 30
        max_file_bytes = 1_000_000

        results = []
        skipped_files = 0

        needle = query.casefold()
        for entry in self._walk_files():
            if entry is None:
                skipped_files += 1
                continue
            relative_path, resolved_path, info = entry
            if relative_path.suffix != ".py":
                continue
            if info.st_size > max_file_bytes:
                skipped_files += 1
                continue
            try:
                content = resolved_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                skipped_files += 1
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle in line.casefold():
                    results.append(f"{relative_path.as_posix()}:{line_number}: {line.strip()[:300]}")
                    if len(results) == max_results:
                        break
            if len(results) == max_results:
                break

        if len(results) == max_results:
            message = "搜索结果（达到 30 条上限，可能还有更多）：\n" + "\n".join(results)
        elif not results:
            message = f"没有找到包含关键词 '{query}' 的结果。"
        else:
            message = "搜索结果：\n" + "\n".join(results)

        if skipped_files:
            message += f"\n\n已跳过 {skipped_files} 个受保护、不安全、过大或不可读取的条目。"

        return message








