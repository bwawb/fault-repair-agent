
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from run_report import build_run_report, save_run_report


def main():

    # 模拟 Agent 的消息记录
    messages = [
        HumanMessage(
            content="请检查订单折扣计算问题"
        ),

        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "relative_path": "pricing.py"
                    },
                    "id": "call_001",
                    "type": "tool_call",
                }
            ],
        ),

        ToolMessage(
            content="discount_rate = discount_percent // 100",
            tool_call_id="call_001",
        ),

        AIMessage(
            content="发现订单折扣使用了整数地板除法，建议改为普通除法。"
        ),
    ]

    # 构建任务报告
    report = build_run_report(
        user_request="检查订单折扣计算问题",
        messages=messages,
        task_id="test_task_002",
        source_project="demo-project",
        working_project="workspace/demo-project",
    )

    print("===== Agent 消息提取结果 =====")

    print("模型消息数量：", report["agent"]["model_message_count"])
    print("工具调用数量：", report["agent"]["tool_call_count"])

    print("\n工具调用：")
    print(report["agent"]["tool_calls"])

    print("\n工具返回：")
    print(report["agent"]["tool_results"])

    print("\n最终回答：")
    print(report["agent"]["final_answer"])

    # 保存报告
    report_path = save_run_report(report)

    print("\n报告保存成功：", report_path)


if __name__ == "__main__":
    main()
