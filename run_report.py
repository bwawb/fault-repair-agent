import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage


def build_run_report(
        user_request: str,
        messages: list,
        task_id: str = None,
        source_project: str = None,
        working_project: str = None,
        started_at: str = None
) -> dict:
    """
    构建统一运行任务。

    Args:
        user_request: 用户输入的任务
        messages: Agent执行结束后的消息列表
        task_id: 当前任务唯一标识
        source_project: 原项目路径
        working_project: 工作副本路径
        started_at: 任务开始时间

    Return:
        完成的任务报告字典
    """

    # ==================================
    # 1. 提取 Agent 执行信息
    # ==================================
    tool_calls = []
    tool_results = []
    model_message_count = 0
    for message in messages:

        if isinstance(message, AIMessage):
            model_message_count += 1
            for tool_call in message.tool_calls:
                tool_calls.append({
                    "id": tool_call.get("id"),
                    "name": tool_call.get("name"),
                    "args": tool_call.get("args"),
                })
        elif isinstance(message, ToolMessage):
            tool_results.append({
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            })

    final_answer = ""

    if messages and isinstance(messages[-1], AIMessage):
        final_answer = messages[-1].content

    # ==================================
    # 2. 初始化统一任务报告
    # ==================================

    now = datetime.now().astimezone().isoformat()

    report = {
        "schema_version": "1.0",

        "task": {
            "task_id": task_id or uuid4().hex,
            "user_request": user_request,
            "started_at": started_at,
            "created_at": now,
            "completed_at": None,
        },

        "workspace": {
            "source_project": source_project,
            "working_project": working_project,
        },

        "agent": {
            "model_message_count": model_message_count,
            "tool_call_count": len(tool_calls),
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "final_answer": final_answer,
        },

        "proposal": {
            "status": "not_generated",
            "tool_call_id": None,
            "relative_path": None,
            "old_text": None,
            "new_text": None,
            "diff": None,
        },

        "approval": {
            "status": "not_requested",
        },

        "application": {
            "status": "not_attempted",
            "message": None,
        },

        "verification": {
            "baseline": None,
            "after_fix": None,
            "comparison": None,
        },

        "final_status": "in_progress",
    }

    return report

def save_run_report(
    report: dict,
    output_dir: str = "outputs/run_reports",
) -> Path:
    """
    将完整任务报告保存为JSON文件
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    task_id = report["task"]["task_id"]

    report_path = output_path / f"task_{task_id}.json"

    with report_path.open("w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return report_path