import json

from langchain_core.tools import tool

from tools.tool_registry import ToolRegistry


def create_langchain_tools(registry: ToolRegistry) -> list:
    """
    将现有的ToolRegistry包装成LangChain工具

    Args:
        registry: 已绑定待分析项目的工具注册表。

    Returns:
        LangChain 工具列表。
    """

    @tool
    def list_files() -> str:
        "列出待分析项目中的文件，返回相对于项目根目录的领"
        result = registry.execute(
            tool_name='list_files',
            arguments={}
        )
        return json.dumps(result, ensure_ascii=False)

    @tool
    def read_file(relative_path: str) -> str:
        """
        读取待分析项目中的文本文件。

        relative_path 必须是相对于项目根目录的路径，
        例如 calculator.py 或 tests/test_calculator.py。
        """
        result = registry.execute(
            tool_name='read_file',
            arguments={
                "relative_path": relative_path
            }
        )

        return json.dumps(result, ensure_ascii=False)

    @tool
    def run_tests() -> str:
        """
        运行分析项目tests目录中的pytest测试

        返回测试运行状态、退出码和测试输出。
        """
        result = registry.execute(
            tool_name="run_tests",
            arguments={},
        )
        return json.dumps(result, ensure_ascii=False)

    @tool
    def search_code(query: str) -> str:
        """
        在待分析项目的 Python 文件中搜索关键词。

        适合查找函数名、变量名、异常名等。
        返回匹配行的文件路径、行号和内容。
        """
        result = registry.execute(
            tool_name="search_code",
            arguments={
                "query": query,
            },
        )

        return json.dumps(result, ensure_ascii=False)

    @tool
    def preview_replace(relative_path: str, old_text: str, new_text: str,) -> str:
        """
        预览对项目中文本文件的一次替换，不会修改文件。

        relative_path 是相对于项目根目录的文件路径。
        old_text 必须与文件中的原文完全一致，且只出现一次。
        new_text 是建议替换后的内容。

        工具返回修改前后的 diff，供检查。
        """
        result = registry.execute(
            tool_name="preview_replace",
            arguments={
                "relative_path": relative_path,
                "old_text": old_text,
                "new_text": new_text,
            },
        )
        return json.dumps(result, ensure_ascii=False)

    return [
        list_files,
        read_file,
        run_tests,
        search_code,
        preview_replace,
    ]