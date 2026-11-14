"""工作副本复制和项目工具共用的文件访问规则。"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


class FileAccessDenied(ValueError):
    """访问被拒绝。错误消息只说明规则。"""


@dataclass(frozen=True)
class FileAccessPolicy:
    # 项目内的相对路径。目录规则同时保护其后代。
    extra_paths: tuple[str, ...] = ()

    def check_relative(self, relative_path: str | Path) -> Path:
        path = Path(relative_path)
        if path.anchor or PureWindowsPath(relative_path).anchor or not path.parts:
            raise FileAccessDenied("请提供相对于工作副本的文件或目录路径。")
        if ".." in path.parts:
            raise FileAccessDenied("路径含有 ..，不允许访问工作目录之外的文件。")
        if os.name == "nt" and any(":" in part or part.endswith((" ", ".")) for part in path.parts):
            raise FileAccessDenied("不允许 Windows 数据流或带尾随空格、句点的路径。")

        parts = tuple(part.casefold() for part in path.parts)
        for name in parts:
            if (name in {".env", ".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
                    or name.startswith(".env.") or name.endswith((".pem", ".key"))):
                raise FileAccessDenied("路径被文件访问策略保护。")
        for extra in self.extra_paths:
            prefix = tuple(part.casefold() for part in Path(extra).parts)
            if prefix and parts[:len(prefix)] == prefix:
                raise FileAccessDenied("路径被项目文件访问策略保护。")
        return path

    def inspect(self, root: Path, relative_path: str | Path) -> tuple[Path, os.stat_result]:
        """先用 lstat 检查路径各级，再检查解析后的实际路径。"""
        relative = self.check_relative(relative_path)
        if root.resolve(strict=True) != root:
            raise FileAccessDenied("工作目录已重定向。")

        current = root
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FileAccessDenied("不允许访问符号链接、junction 或其他重解析点。")

        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise FileAccessDenied("不允许访问工作目录之外的文件。")
        self.check_relative(resolved.relative_to(root))

        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise FileAccessDenied("不允许访问有多个硬链接或链接数不可确认的文件。")
        elif not stat.S_ISDIR(info.st_mode):
            raise FileAccessDenied("只允许访问普通文件或目录。")
        return resolved, info

    def resolve_file(self, root: Path, relative_path: str | Path) -> Path:
        resolved, info = self.inspect(root, relative_path)
        if not stat.S_ISREG(info.st_mode):
            raise FileAccessDenied("目标不是普通文件。")
        return resolved


DEFAULT_FILE_ACCESS = FileAccessPolicy()
