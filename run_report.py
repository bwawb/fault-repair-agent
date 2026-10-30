import json
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage


def build_run_report(user_request: str, messages: list) -> dict:
    """
    根据 Agent 返回的消息，整理运行记录。

    Args:
        user_request: 用户输入的任务。
        messages: Agent 运行结束后返回的消息列表。

    Returns:
        包含工具调用、工具结果和最终回答的字典。
    """
    tool_calls = []
    tool_results = []
    model_message_count = 0

    for message in messages:
        if isinstance(message, AIMessage):
            model_message_count += 1

            for tool_call in message.tool_calls:
                tool_calls.append(
                    {
                        "id": tool_call.get("id"),
                        "name": tool_call.get("name"),
                        "args": tool_call.get("args"),
                    }
                )

        elif isinstance(message, ToolMessage):
            tool_results.append(
                {
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )

    final_answer = ""

    if messages and isinstance(messages[-1], AIMessage):
        final_answer = messages[-1].content

    return {
        "task": user_request,
        "model_message_count": model_message_count,
        "tool_call_count": len(tool_calls),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "final_answer": final_answer,
    }


def save_run_report(report: dict, output_dir: str = "outputs") -> Path:
    """
    将运行记录保存为 JSON 文件。

    Args:
        report: build_run_report 生成的运行记录。
        output_dir: 输出目录，相对于当前工作目录。

    Returns:
        保存的文件路径。
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 使用日期、时间和微秒，降低文件名重复的概率。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = output_path / f"diagnosis_{timestamp}.json"

    with report_path.open("w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return report_path