"""根据调用方显式提供的敏感值，为展示或传输生成脱敏副本。"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Generic, TypeVar


REDACTION_MARKER = "[REDACTED]"
MAX_CONTAINER_DEPTH = 64

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
T = TypeVar("T")


@dataclass(frozen=True)
class RedactionResult(Generic[T]):
    value: T = field(repr=False)
    match_count: int

    @property
    def redacted(self) -> bool:
        """是否命中过敏感值，包括原文本恰好等于替换标记的情况。"""
        return self.match_count > 0


class TextRedactor:
    """按原样匹配字面子串，优先选择最靠左、同起点最长的匹配。"""

    def __init__(self, sensitive_values: Iterable[str] = ()):
        if isinstance(sensitive_values, (str, bytes, bytearray)):
            raise TypeError("请传入多个字符串组成的可迭代对象。")
        literals: set[str] = set()
        for value in sensitive_values:
            if type(value) is not str:
                raise TypeError("敏感值必须是字符串。")
            if value:
                literals.add(value)
        ordered = sorted(literals, key=lambda literal: (-len(literal), literal))
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def _replace(self, value: str) -> tuple[str, int]:
        if self._pattern is None:
            return value, 0
        return self._pattern.subn(REDACTION_MARKER, value)

    def redact_text(self, text: str) -> RedactionResult[str]:
        """替换文本中的不重叠匹配，不重新扫描替换标记。"""
        if type(text) is not str:
            raise TypeError("待脱敏文本必须是字符串。")
        value, match_count = self._replace(text)
        return RedactionResult(value, match_count)

    def redact_data(self, data: JsonValue, *, preserve_keys: bool = False) -> RedactionResult[JsonValue]:
        """复制内建 JSON 风格的数据，处理字符串键和值。

        根容器计为一层。循环引用、超过层级上限或键冲突时抛出 ValueError。
        共享的子容器按各处出现分别复制，命中次数也按各处统计。
        preserve_keys 用于协议对象。键命中时拒绝处理，避免改写协议字段。
        """
        active_containers: set[int] = set()
        match_count = 0

        def redact_string(value: str) -> str:
            nonlocal match_count
            redacted, count = self._replace(value)
            match_count += count
            return redacted

        def visit(value: JsonValue, depth: int) -> JsonValue:
            if type(value) is str:
                return redact_string(value)
            if value is None or type(value) in (int, float, bool):
                return value
            if type(value) not in (list, dict):
                raise TypeError("只支持 JSON 风格的内建数据类型。")

            identity = id(value)
            if identity in active_containers:
                raise ValueError("脱敏数据包含循环引用。")
            if depth >= MAX_CONTAINER_DEPTH:
                raise ValueError("脱敏数据超过容器嵌套层级上限。")
            active_containers.add(identity)
            try:
                if type(value) is list:
                    return [visit(item, depth + 1) for item in value]

                result: dict[str, JsonValue] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise TypeError("待脱敏字典的键必须是字符串。")
                    redacted_key = redact_string(key)
                    if preserve_keys and redacted_key != key:
                        raise ValueError("敏感值命中了必须保留的字典键。")
                    if redacted_key in result:
                        raise ValueError("脱敏后的字典键发生冲突。")
                    result[redacted_key] = visit(item, depth + 1)
                return result
            finally:
                active_containers.remove(identity)

        value = visit(data, 0)
        return RedactionResult(value, match_count)
