"""为模型调用构造脱敏消息，同时保留 Agent 内部的原始会话。"""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from redaction import REDACTION_MARKER, TextRedactor
from tools.langchain_tools import ToolResultSerializationError


_PREVIEW_TOOLS = {"preview_replace", "preview_replacements", "preview_file_changes"}


class ModelRedactionError(ValueError):
    """无法构造可发送给模型的消息。"""


class ModelContentRedaction(AgentMiddleware):
    def __init__(self, sensitive_values: list[str] | tuple[str, ...]):
        if type(sensitive_values) not in (tuple, list):
            raise ModelRedactionError("模型脱敏配置必须是字符串列表或元组。")
        try:
            self._redactor = TextRedactor(sensitive_values)
        except (TypeError, ValueError, OverflowError):
            raise ModelRedactionError("模型脱敏配置包含不支持的敏感值。") from None
        if self._redactor.redact_text(REDACTION_MARKER).redacted:
            raise ModelRedactionError("模型脱敏配置与遮盖标记冲突。")
        self.enabled = any(sensitive_values)

    def redact_feedback(self, feedback: dict) -> str:
        """在重试反馈被编码成 JSON 文本前处理，包括作为键的测试 ID。"""
        return self._check_unchanged(json.dumps(self._redact_data(feedback), ensure_ascii=False, indent=2))

    def _redact_data(self, value, *, preserve_keys=False):
        try:
            result = self._redactor.redact_data(value, preserve_keys=preserve_keys)
            if result.redacted and self._redactor.redact_data(result.value).redacted:
                raise ModelRedactionError("脱敏结果仍含有配置的敏感值。")
        except (TypeError, ValueError, RecursionError):
            raise ModelRedactionError("无法安全脱敏模型内容，已停止发送。") from None
        return result.value

    def _check_unchanged(self, value: str | None) -> str | None:
        if value is not None and self._redactor.redact_text(value).redacted:
            raise ModelRedactionError("敏感值命中了必须保留的模型协议字段或序列化内容，已停止发送。")
        return value

    def _message_content(self, content):
        # 项目使用文本消息。保留文本块的类型，其他类型需要独立的协议处理。
        if isinstance(content, list):
            for block in content:
                if type(block) is dict:
                    if block.get("type") not in ("text", "output_text") or set(block) != {"type", "text"}:
                        raise ModelRedactionError("模型脱敏暂不支持此消息内容块，已停止发送。")
                    self._check_unchanged(block["type"])
                elif type(block) is not str:
                    raise ModelRedactionError("模型脱敏暂不支持此消息内容块，已停止发送。")
        return self._redact_data(content, preserve_keys=True)

    def _tool_content(self, message: ToolMessage) -> str:
        # 项目工具用 JSON 在图内保存原始结果。先解码，再脱敏并序列化模型副本，
        # 以便匹配原始字符串中的换行和引号。
        if message.status == "error":
            # 框架验证错误可能包含 repr 格式的原始参数。
            return self._redact_data("工具调用失败，请检查工具名和参数。")
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError, RecursionError):
            raise ModelRedactionError("无法解析模型工具结果，已停止发送。") from None
        return self._check_unchanged(json.dumps(self._redact_data(result, preserve_keys=True), ensure_ascii=False))

    def _tool_call_arguments(self, arguments: dict) -> dict:
        safe_arguments = self._redact_data(arguments, preserve_keys=True)
        # 模型适配器会再次把工具参数编码为 JSON，包括数字等非字符串参数。
        self._check_unchanged(json.dumps(safe_arguments, ensure_ascii=False))
        return safe_arguments

    def _model_message(self, message):
        # 重建消息，避免把提供商的原始工具参数、artifact 或其他额外字段
        # 随模型副本一同传给提供商的序列化器。
        identity = {"id": self._check_unchanged(message.id), "name": self._check_unchanged(message.name)}
        self._check_unchanged(message.type)
        if isinstance(message, ToolMessage):
            return ToolMessage(
                content=self._tool_content(message), tool_call_id=self._check_unchanged(message.tool_call_id),
                status=self._check_unchanged(message.status), **identity,
            )
        content = self._message_content(message.content)
        if isinstance(message, AIMessage):
            calls = [
                {
                    "id": self._check_unchanged(call["id"]),
                    "name": self._check_unchanged(call["name"]),
                    "args": self._tool_call_arguments(call["args"]),
                    "type": self._check_unchanged(call["type"]),
                }
                for call in message.tool_calls
            ]
            invalid_calls = [
                {
                    "id": self._check_unchanged(call["id"]),
                    "name": self._check_unchanged(call["name"]),
                    "args": self._check_unchanged("{}"),
                    "type": self._check_unchanged(call["type"]),
                }
                for call in message.invalid_tool_calls
            ]
            return AIMessage(content=content, tool_calls=calls, invalid_tool_calls=invalid_calls, **identity)
        if isinstance(message, (HumanMessage, SystemMessage)):
            return type(message)(content=content, **identity)
        raise ModelRedactionError("模型脱敏暂不支持此消息类型，已停止发送。")

    def wrap_model_call(self, request, handler):
        messages = [self._model_message(message) for message in request.messages]
        system = self._model_message(request.system_message) if request.system_message is not None else None
        for tool in request.tools:
            if self._redactor.redact_data(convert_to_openai_tool(tool)).redacted:
                raise ModelRedactionError("敏感值命中了模型工具的定义，已停止发送。")
        return handler(request.override(messages=messages, system_message=system))

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] in _PREVIEW_TOOLS:
            # 拒绝把模型看到的遮盖标记用作原文本、目标路径或替换内容。
            # 原文已含有同样的标记时也拒绝，避免碰巧匹配另一段文字。
            try:
                arguments = call["args"]
                blocked = (
                    REDACTION_MARKER in json.dumps(arguments, ensure_ascii=False)
                    or self._redactor.redact_data(arguments).redacted
                )
            except (TypeError, ValueError, RecursionError):
                raise ModelRedactionError("无法安全检查补丁参数，已停止发送。") from None
            if blocked:
                return ToolMessage(
                    content=json.dumps({
                        "success": False,
                        "tool_name": call["name"],
                        "result": "补丁参数包含遮盖标记或已配置的敏感值。请改用可完整核对的路径和文本。",
                    }, ensure_ascii=False),
                    name=call["name"],
                    tool_call_id=call["id"],
                )
        try:
            return handler(request)
        except ToolResultSerializationError:
            raise ModelRedactionError("工具结果无法安全处理，已停止发送。") from None
