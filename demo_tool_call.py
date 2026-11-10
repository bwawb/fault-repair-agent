import json
import os
from dotenv import load_dotenv
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from tools.tool_registry import ToolRegistry

load_dotenv()


def create_model() -> ChatOpenAI:
    """根据环境变量创建聊天模型"""
    model_name = os.getenv("MODEL_NAME")
    api_key = os.getenv("MODEL_API_KEY")
    base_url = os.getenv("MODEL_BASE_URL")

    if not model_name:
        raise ValueError("缺少 MODEL_NAME，请检查 .env 文件。")

    if not api_key:
        raise ValueError("缺少 MODEL_API_KEY，请检查 .env 文件。")

    model_config = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
        "timeout": 60,
        "max_retries": 1,
    }

    # 配置了自定义地址时，使用服务商提供的地址
    if base_url:
        model_config["base_url"] = base_url

    return ChatOpenAI(**model_config)


# 工具定义：告诉模型工具的名称、用途和参数
# 这只是工具说明，不会自动执行ToolRegistry中的函数
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description":(
            "读取待分析项目中的文本文件。"
            "文件路径必须相对于项目根目录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "relative_path":{
                    "type": "string",
                    "description":(
                        "相对于项目根目录的文件路径。"
                        "例如 caculator.py"
                    ),
                }
            },
            "required": ["relative_path"],
            "additionalProperties": False,
        },
    },
}

def main():
    # 修改为你自己的示例项目路径。
    project_path = r"E:\PythonProject\demo-python-project"

    # 创建工具注册表和模型
    registry = ToolRegistry(project_path)
    model = create_model()

    # 把工具绑定给模型
    model_with_tools = model.bind_tools([READ_FILE_TOOL])

    # 准备对话消息
    messages = [
        SystemMessage(
            content=(
                "你是一个辅助分析 Python 项目的助手。"
                "请先使用提供的工具读取用户指定的文件，"
                "再根据工具返回的实际内容回答。"
            )
        ),
        HumanMessage(
            content=(
                "请读取项目中的 calculator.py，"
                "并解释 calculate_average 函数的作用。"
            )
        ),
    ]

    print("===== 第一次调用模型 =====")

    # 第一次调用：让模型决定是否请求工具。
    ai_message = model_with_tools.invoke(messages)

    print("模型返回的工具调用：")
    print(ai_message.tool_calls)

    # 将模型消息加入对话历史。
    messages.append(ai_message)

    # 模型也可能不调用工具
    if not ai_message.tool_calls:
        print("\n模型没有请求调用工具。")
        print("模型回复：")
        print(ai_message.content)
        return

    # 一条AIMessage中可能包含多个工具调用
    for tool_call in ai_message.tool_calls:
        tool_name = tool_call['name']
        tool_args = tool_call['args']
        tool_call_id = tool_call.get("id")

        if not tool_call_id:
            raise ValueError("模型返回的工具调用缺少 id，无法关联工具结果。")

        print("\n===== 执行工具 =====")
        print(f"工具名称：{tool_name}")
        print(f"工具参数：{tool_args}")
        print(f"工具调用 ID：{tool_call_id}")


        # 通过实现的注册表执行工具
        result = registry.execute(
            tool_name=tool_name,
            arguments=tool_args,
        )

        print("工具执行结果：")
        print(result)

        # 将结果转换为JSON字符串，作为工具消息内容
        tool_message = ToolMessage(
            content=json.dumps(result, ensure_ascii=False),
            tool_call_id=tool_call_id
        )

        # 把工具结果加入对话历史
        messages.append(tool_message)

        print("\n===== 第二次调用模型 =====")

        # 这次使用未绑定工具的 model，让模型根据已有结果回答。
        final_message = model.invoke(messages)

        print("模型最终回答：")
        print(final_message.content)

if __name__ == "__main__":
        main()