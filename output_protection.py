"""在终端和报告出口处理任务配置的已知敏感值。"""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from redaction import REDACTION_MARKER, TextRedactor


class OutputProtectionError(ValueError):
    """不能安全展示或保存输出。"""


class OutputProtection:
    def __init__(self, sensitive_values):
        if type(sensitive_values) not in (tuple, list) or any(type(value) is not str for value in sensitive_values):
            raise OutputProtectionError("输出脱敏配置必须是字符串列表或元组。")
        self.enabled = any(sensitive_values)
        self.redactor = TextRedactor(sensitive_values)
        if self.redactor.redact_text(REDACTION_MARKER).redacted:
            raise OutputProtectionError("敏感值配置与遮盖标记冲突。")
        escaped = [
            variant for value in sensitive_values
            for variant in (value, json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1])
        ]
        self.display_redactor = TextRedactor(escaped)
        self.test_aliases = {}
        self.known_test_ids = set()

    def display(self, text):
        if not self.enabled:
            return str(text)
        try:
            safe = self.display_redactor.redact_text(str(text)).value
            if not self.display_redactor.redact_text(safe).redacted:
                return safe
        except Exception:
            pass
        return "输出脱敏失败，已隐藏该内容。"

    def require_exact(self, *values):
        if not self.enabled:
            return
        try:
            matched = any(
                self.display_redactor.redact_text(str(value)).redacted for value in values if value is not None
            )
        except Exception:
            raise OutputProtectionError("无法验证运行必需字段，已停止相关操作。") from None
        if matched:
            raise OutputProtectionError("运行必需的路径、标识或哈希命中敏感值，已停止相关操作。")

    def register_test_ids(self, results):
        if not self.enabled or not results:
            return
        self.known_test_ids.update(results)
        # 按原始 ID 分配不同别名，原始比较和决策仍使用真实 ID。
        for test_id in results:
            if self.redactor.redact_text(test_id).redacted and test_id not in self.test_aliases:
                self.test_aliases[test_id] = f"[test-id-{len(self.test_aliases) + 1}]"
        if self.known_test_ids.intersection(self.test_aliases.values()):
            raise OutputProtectionError("测试 ID 别名与真实 ID 冲突，已停止输出。")

    def with_test_aliases(self, value):
        if not self.test_aliases:
            return value
        if isinstance(value, str):
            return self.test_aliases.get(value, value)
        if isinstance(value, list):
            return [self.with_test_aliases(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                alias = self.test_aliases.get(key, key)
                if alias in result:
                    raise OutputProtectionError("测试 ID 别名与现有键冲突，已停止输出。")
                result[alias] = self.with_test_aliases(item)
            return result
        return value

    def report_copy(self, report):
        if not self.enabled:
            return report
        self.require_exact(report["task"].get("task_id"), report["task"].get("completed_at"))
        workspace = report.get("workspace", {})
        self.require_exact(*(workspace.get(key) for key in ("source_project", "working_project", "baseline_project")))
        for path, patch in report.get("patch_summary", {}).get("files", {}).items():
            self.require_exact(path, patch.get("relative_path"), patch.get("file_hash"))

        def visit(value):
            if isinstance(value, list):
                return [visit(item) for item in value]
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    self.require_exact(key)
                    if key == "content" and isinstance(item, str) and "tool_call_id" in value:
                        try:
                            decoded = json.loads(item)
                        except ValueError:
                            pass
                        else:
                            result[key] = json.dumps(visit(decoded), ensure_ascii=False)
                            continue
                    result[key] = visit(item)
                return result
            if isinstance(value, str):
                return self.display_redactor.redact_text(value).value
            return value

        try:
            safe = visit(self.with_test_aliases(report))
            safe["output_protection"] = {"known_values_redacted": True}
            payload = json.dumps(safe, ensure_ascii=False, indent=2)
            if (self.display_redactor.redact_text(payload).redacted
                    or self.display_redactor.redact_data(safe).redacted):
                raise OutputProtectionError("报告仍含有已配置敏感值。")
            return safe
        except OutputProtectionError:
            raise
        except Exception:
            raise OutputProtectionError("无法安全脱敏任务报告，已停止保存。") from None


_current = ContextVar("task_output_protection", default=OutputProtection(()))


def current_output():
    return _current.get()


@contextmanager
def protect_output(sensitive_values):
    policy = OutputProtection(sensitive_values)
    token = _current.set(policy)
    try:
        yield policy
    finally:
        _current.reset(token)


def emit(text):
    print(current_output().display(text))
