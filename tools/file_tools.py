import os
from pathlib import Path
from pydoc import resolve


class FileTools:
    """在指定项目目录内执行文件读取操作"""

    def __init__(self, project_path: str):
        """
        初始化文件工具

        Args:
            project_path: 待分析项目的根目录
        """
        self.project_path = Path(project_path).resolve()

        if not self.project_path.exists():
            raise ValueError(f"项目路径不存在：{self.project_path}")
        if not self.project_path.is_dir():
            raise ValueError(f"项目路径不是目录：{self.project_path}")


    def read_file(self, relative_path: str)->str:
        """
        读取项目内的文本文件。

        Args:
            relative_path: 相对于根据路的文件路径。

        Return:
            文件内容，如果读取失败，返回错误信息。
        """

        try:
            requested_path = Path(relative_path)

            if requested_path.is_absolute():
                return "读取失败：请使用相对于根目录的路径"

            file_path = (self.project_path/requested_path).resolve()

            # 检查解析后的路径是否仍位于项目根目录内
            if not file_path.is_relative_to(self.project_path):
                return "读取失败：不允许访问项目目录之外的文件"

            if not file_path.exists():
                return f"读取失败：文件不存在：{relative_path}"

            if not file_path.is_file():
                return f"读取失败：目标不是问缉拿：{relative_path}"

            content = file_path.read_text(encoding="utf-8")

            return (
                f"文件路径：{relative_path}\n"
                f"文件内容：\n"
                f"{content}"
            )
        except UnicodeError:
            return f"读取失败：文件不是可用UTF-8解码的文本文件：{relative_path}"
        except OSError as exc:
            return f"读取失败：{exc}"

    def list_files(self) -> str:
        """
        列出项目中的文件.

        Returns:
            项目文件的相对路径列表，或相应的错误信息。
        """

        # 通常不需要agent读取
        excluded_dirs = (
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".idea"
        )

        files = []

        try:
            for file_path in self.project_path.rglob("*"):
                # 跳过目录，只保留文件
                if not file_path.is_file():
                    continue

                # 得到相对于项目根目录的路径
                relative_path = file_path.relative_to(self.project_path)

                # 如果路径中包含需要排除的目录，就跳过
                if any(part in excluded_dirs for part in relative_path.parts):
                    continue

                # 使用统一的/分隔符，便于后续交给read_file
                files.append(relative_path.as_posix())
            if not files:
                return "项目中没有找到文件"
            files.sort()

            return "项目文件列表：\n" + "\n".join(files)
        except OSError as exc:
            return f"列出文件失败：{exc}"


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

        excluded_dirs = {
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
        }

        max_results = 30
        max_file_bytes = 1_000_000

        results = []
        skipped_files = 0

        try:
            # followlinks=False,不沿着符号链接进入其他目录
            for root, dirs, files in os.walk(self.project_path, followlinks=False):
                # 在遍历时直接跳过不需要的目录。
                dirs[:] = [
                    name for name in dirs if name not in excluded_dirs
                ]

                for file_name in sorted(files):
                    # 第一版只搜索Python文件。
                    if not file_name.endswith(".py"):
                        continue

                    file_path = Path(root) / file_name

                    try:
                        resolved_path = file_path.resolve()

                        # 不读取指向项目外部的文件
                        if not resolved_path.is_relative_to(self.project_path):
                            continue

                        # 跳过过大的文件，控制本阶段的读取范围
                        if resolved_path.stat().st_size > max_file_bytes:
                            skipped_files += 1
                            continue

                        content = resolved_path.read_text(encoding="utf-8")

                    except(OSError, UnicodeError):
                        # 单个文件无法读取时，不中断整个搜索。
                        skipped_files += 1
                        continue

                    relative_path = file_path.relative_to(self.project_path).as_posix()

                    for line_number, line in enumerate(content.splitlines(), start=1):
                        # 使用普通字符串匹配，不区分大小写。
                        if query.casefold() in line.casefold():
                            display_line = line.strip()[:300]

                            results.append(f"{relative_path}:{line_number}: {display_line}")
                            if len(results) >= max_results:
                                return (
                                "搜索结果（达到 30 条上限，可能还有更多）：\n"
                                + "\n".join(results)
                                )

        except OSError as exc:
            return f"搜索失败：{exc}"

        if not results:
            message = f"没有找到包含关键词 '{query}' 的结果。"
        else:
            message = "搜索结果：\n" + "\n".join(results)

        if skipped_files:
            message += f"\n\n有 {skipped_files} 个文件过大或无法读取，已跳过。"

        return message








