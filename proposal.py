import json
from copy import deepcopy

from langchain_core.messages import AIMessage, ToolMessage

from tools.edit_tools import validate_file_changes, validate_replacements

def extract_latest_preview(messages: list) -> dict:
    """
    从Agent消息中提取最后一次修改预览及其工具结果

    这里只整理提案，不修改文件

    Args:
        messages：Agent运行结束返回的消息列表

    Returns:
        包含状态、提示消息和修改提案的字典
    """
    latest_call = None

    # 第一步：找到模型最后一次调用的修改预览工具。
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for tool_call in message.tool_calls:
            if tool_call.get("name") in {"preview_replace", "preview_replacements", "preview_file_changes"}:
                latest_call = tool_call

    if latest_call is None:
        return {
            "success": False,
            "message": "模型没有调用修改预览工具，无法提取修改提案。",
            "proposal": None,
        }

    tool_call_id = latest_call.get("id")

    if not tool_call_id:
        return {
            "success": False,
            "message": "修改预览调用缺少 ID，无法匹配工具结果。",
            "proposal": None,
        }

    # 第二步：根据工具调用ID找到对应的ToolMessage。
    matched_result = None

    for message in messages:
        if (
                isinstance(message, ToolMessage)
                and message.tool_call_id == tool_call_id
        ):
            matched_result = message
            break

    if matched_result is None:
        return {
            "success": False,
            "message": "没有找到与修改预览对应的工具结果。",
            "proposal": None,
        }

    # 第三步：读取工具返回的 JSON 字符串。
    try:
        tool_result = json.loads(matched_result.content)

    except(TypeError, json.JSONDecodeError):
        return {
            "success": False,
            "message": "修改预览工具结果不是预期的 JSON 格式。",
            "proposal": None,
        }

    if not isinstance(tool_result, dict):
        return {
            "success": False,
            "message": "修改预览工具结果不是字典。",
            "proposal": None,
        }

    # 第四步：检查 ToolRegistry 是否成功调用工具。
    if not tool_result.get("success"):
        return {
            "success": False,
            "message": (
                "工具调用失败："
                f"{tool_result.get('result', '没有提供错误详情')}"
            ),
            "proposal": None,
        }

    preview_result = tool_result.get("result")

    if not isinstance(preview_result, dict):
        return {
            "success": False,
            "message": "修改预览结果格式不正确。",
            "proposal": None,
        }

    # 第五步：检查 EditTools 是否成功生成 diff
    if not preview_result.get("success"):
        return {
            "success": False,
            "message": (
                "未能生成修改预览："
                f"{preview_result.get('message', '没有提供错误详情')}"
            ),
            "proposal": None,
            "files": preview_result.get("files", []),
        }

    diff = preview_result.get("diff")

    if not isinstance(diff, str) or not diff:
        return {
            "success": False,
            "message": "修改预览未返回有效 diff。",
            "proposal": None,
        }

    # 第六步：整理工具调用参数和预览结果。
    args = latest_call.get("args")
    if not isinstance(args, dict):
        return {
            "success": False,
            "message": "修改预览的参数格式不正确。",
            "proposal": None,
        }

    proposal = {
        "tool_call_id": tool_call_id,
        "diff": diff,
    }
    try:
        if latest_call["name"] == "preview_file_changes":
            files = args.get("files")
            validate_file_changes(files)
            proposal["files"] = deepcopy(files)
        elif latest_call["name"] == "preview_replacements":
            replacements = args.get("replacements")
            validate_replacements(replacements)
            proposal["replacements"] = [replacement.copy() for replacement in replacements]
        else:
            replacement = {key: args.get(key) for key in ("old_text", "new_text")}
            validate_replacements([replacement])
            proposal.update(replacement)
        if "files" not in proposal:
            relative_path = args.get("relative_path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                raise ValueError("修改提案缺少有效的文件路径。")
            proposal["relative_path"] = relative_path
    except ValueError as exc:
        return {"success": False, "message": str(exc), "proposal": None}

    return {
        "success": True,
        "message": "成功提取修改提案。",
        "proposal": proposal,
    }
