from file_access import DEFAULT_FILE_ACCESS, FileAccessPolicy
from tools.file_tools import FileTools
from tools.test_tools import TestTools
from tools.edit_tools import EditTools

class ToolRegistry:
    """注册工具，并根据工具名称执行对应的函数"""

    def __init__(
        self, project_path: str, *, test_tools: TestTools | None = None,
        policy: FileAccessPolicy = DEFAULT_FILE_ACCESS,
    ):
        """
        初始化工具注册表。

        Args:
            project_path: 待分析项目的根目录。
            test_tools: 主任务的测试执行器。省略时使用本地执行器。
            policy: 文件读取和补丁预览共用的策略。
        """

        self.file_tools = FileTools(project_path, policy=policy)
        self.test_tools = TestTools(project_path) if test_tools is None else test_tools
        self.edit_tools = EditTools(project_path, policy=policy)

        # 工具名称与python方法的对应关系
        self.tools = {
            "list_files": self.file_tools.list_files,
            "read_file": self.file_tools.read_file,
            "run_tests": self.test_tools.run_tests,
            "search_code": self.file_tools.search_code,
            "preview_replace": self.edit_tools.preview_replace,
            "preview_replacements": self.edit_tools.preview_replacements,
            "preview_file_changes": self.edit_tools.preview_file_changes,
        }

        # 每个工具允许接收的参数
        self.allowed_arguments = {
            "list_files": set(),
            "read_file": {"relative_path"},
            "run_tests": set(),
            "search_code": {"query"},
            "preview_replace": {
                "relative_path",
                "old_text",
                "new_text",
            },
            "preview_replacements": {"relative_path", "replacements"},
            "preview_file_changes": {"files"},
        }

    def execute(self, tool_name: str, arguments: dict) -> dict:
        """
        执行指定工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            包含调用状态和工具结果的字典
        """
        # 1.工具名称是否存在
        if tool_name not in self.tools:
            return {
                "success": False,
                "tool_name": tool_name,
                "result": f"工具不存在：{tool_name}",
            }

        # 2.检查参数是否为字典
        if not isinstance(arguments, dict):
            return {
                "success": False,
                "tool_name": tool_name,
                "result": "工具参数必须是字典。",
            }

        # 3. 检查是否存在不允许的参数
        unexpected_arguments = (
            set(arguments) - self.allowed_arguments[tool_name]
        )

        if unexpected_arguments:
            return {
                "success": False,
                "tool_name": tool_name,
                "result": f"包含不允许的参数：{sorted(unexpected_arguments)}",
            }

        # 4. 检查read_file的必须参数
        if tool_name == "read_file":
            relative_path = arguments.get("relative_path")

            if not isinstance(relative_path, str) or not relative_path.strip():
                return {
                    "success": False,
                    "tool_name": tool_name,
                    "result": "read_file 需要非空字符串参数 relative_path。",
                }

        if tool_name == "search_code":
            query = arguments.get("query")

            if not isinstance(query, str) or not query.strip():
                return {
                    "success": False,
                    "tool_name": tool_name,
                    "result": "search_code 需要非空字符串参数 query。",
                }

        if tool_name in {"preview_replace", "preview_replacements"}:
            relative_path = arguments.get("relative_path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                return {
                    "success": False,
                    "tool_name": tool_name,
                    "result": f"{tool_name} 需要非空的 relative_path。",
                }

        if tool_name == "preview_replace":
            old_text = arguments.get("old_text")
            new_text = arguments.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                return {
                    "success": False,
                    "tool_name": tool_name,
                    "result": "preview_replace 需要非空的 old_text。",
                }

            if not isinstance(new_text, str):
                return {
                    "success": False,
                    "tool_name": tool_name,
                    "result": "preview_replace 的 new_text 必须是字符串。",
                }

        # 5. 查找并调用工具
        tool_function = self.tools[tool_name]

        try:
            result = tool_function(**arguments)

            return {
                "success": True,
                "tool_name": tool_name,
                "result": result,
            }
        except Exception as exc:
            return {
                "success": False,
                "tool_name": tool_name,
                "result": f"工具执行发生异常：{type(exc).__name__}: {exc}",
            }
