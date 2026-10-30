import json

from langchain_core.messages import AIMessage, ToolMessage

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

    # 第一步：找到模型最后一次提出的preview_replace调用
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for tool_call in message.tool_calls:
            if tool_call.get("name") == "preview_replace":
                latest_call = tool_call

    if latest_call is None:
        return {
            "success": False,
            "message": "模型没有调用 preview_replace，无法提取修改提案。",
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

    required_keys = ("relative_path", "old_text", "new_text")

    if any(not isinstance(args.get(key), str) for key in required_keys):
        return {
            "success": False,
            "message": "修改提案缺少必要的字符串参数。",
            "proposal": None,
        }

    proposal = {
        "tool_call_id": tool_call_id,
        "relative_path": args["relative_path"],
        "old_text": args["old_text"],
        "new_text": args["new_text"],
        "diff": diff,
    }

    return {
        "success": True,
        "message": "成功提取修改提案。",
        "proposal": proposal,
    }