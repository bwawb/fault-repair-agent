import difflib
from pathlib import Path

class EditTools:
    """在指定项目目录内预览文本修改"""

    def __init__(self, project_path: str):
        """
        初始化修改工具。

        Args:
            project_path: 待分析项目的根目录。
        """
        self.project_path=Path(project_path).resolve()

        if not self.project_path.is_dir():
            raise ValueError( f"项目路径不是目录：{self.project_path}")

    def preview_replace(self, relative_path: str, old_text: str, new_text: str) -> dict:
        """
        预览一次文本替换，不修改原文件。

        Args:
            relative_path: 相对于项目根目录的文件路径。
            old_text: 原文件中需要被替换的文本。
            new_text: 替换后的文本。

        Returns:
            包含预览状态、说明和 diff 的字典。
        """

        if not old_text:
            return {
                "success": False,
                "message": "原文本不能为空。",
                "diff": "",
            }

        if old_text == new_text:
            return {
                "success": False,
                "message": "原文本和新文本相同，无需修改。",
                "diff": "",
            }

        requested_path = Path(relative_path)

        if requested_path.is_absolute():
            return {
                "success": False,
                "message": "请使用相对于项目根目录的路径。",
                "diff": "",
            }

        try:
            file_path = (self.project_path / requested_path).resolve()
            if not file_path.is_relative_to(self.project_path):
                return {
                    "success": False,
                    "message": "不允许访问项目目录之外的文件。",
                    "diff": "",
                }

            if not file_path.is_file():
                return {
                    "success": False,
                    "message": f"目标文件不存在或不是文件：{relative_path}",
                    "diff": "",
                }

            original_content = file_path.read_text(encoding="utf-8")

        except(OSError, UnicodeError) as exc:
            return {
                "success": False,
                "message": f"读取文件失败：{exc}",
                "diff": "",
            }

        match_count = original_content.count(old_text)

        if match_count == 0:
            return {
                "success": False,
                "message": "没有找到原文本，请检查文件内容。",
                "diff": "",
            }

        if match_count > 1:
            return {
                "success": False,
                "message": (
                    f"原文本出现了 {match_count} 次，"
                    "无法确定要修改哪一处。请提供更完整的原文本。"
                ),
                "diff": "",
            }

        modified_content = original_content.replace(
            old_text,
            new_text,
            1,
        )

        diff_lines = difflib.unified_diff(
            original_content.splitlines(),
            modified_content.splitlines(),
            fromfile=f'修改前/{relative_path}',
            tofile=f"修改后/{relative_path}",
            lineterm="",
        )

        diff = "\n".join(diff_lines)

        return {
            "success": True,
            "message": "修改预览已生成，原文件未修改。",
            "diff": diff,
        }