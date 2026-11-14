import json
from functools import partial

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import diagnosis_agent
from apply_proposal import confirm_and_apply
from model_redaction import ModelContentRedaction, ModelRedactionError
from proposal import extract_latest_preview
from redaction import REDACTION_MARKER
from tools.langchain_tools import create_langchain_tools
from tools.tool_registry import ToolRegistry


TOKEN = "虚构标记🧪-fixture"
MULTILINE = '虚构"标记\n第二行🧪'
SENSITIVE_VALUES = (TOKEN, MULTILINE)


class RecordingModel(BaseChatModel):
    responses: list[AIMessage]
    requests: list[list] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "recording-fake-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[len(self.requests)]
        self.requests.append(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])


def tool_call(name, call_id, **args):
    return {"name": name, "id": call_id, "args": args}


def preview(call_id, **changes):
    return tool_call("preview_replace", call_id, relative_path="pricing.py", **changes)


def make_agent(model, registry, sensitive_values=SENSITIVE_VALUES):
    return create_agent(
        model=model, tools=create_langchain_tools(registry), system_prompt="请诊断项目。",
        middleware=[ModelContentRedaction(sensitive_values)],
    )


def assert_no_fixture_values(model):
    for messages in model.requests:
        for message in messages:
            payload = json.dumps(message.model_dump(), ensure_ascii=False)
            # 额外检查序列化成字符串的工具结果，覆盖换行和引号。
            if isinstance(message, ToolMessage) and message.status != "error":
                payload += json.dumps(json.loads(message.content), ensure_ascii=False)
            for value in SENSITIVE_VALUES:
                assert value not in payload
                assert json.dumps(value, ensure_ascii=False)[1:-1] not in payload


def test_actual_model_requests_use_copies_for_all_tools(tmp_path, monkeypatch):
    source = f"# evidence {TOKEN}\nprice = 100\n"
    (tmp_path / "pricing.py").write_bytes(source.encode())
    (tmp_path / f"{TOKEN}.txt").touch()
    registry = ToolRegistry(str(tmp_path))
    test_result = {"output": MULTILINE, "nested": [{"error": TOKEN}], "status": "failed"}
    registry.tools["run_tests"] = lambda: test_result

    def fail_search(query):
        raise RuntimeError(f"虚构错误 {MULTILINE}")

    original_search = registry.tools["search_code"]
    registry.tools["search_code"] = (
        lambda query: original_search(query) if query == "evidence" else fail_search(query)
    )
    calls = [
        tool_call("list_files", "list"),
        tool_call("read_file", "read", relative_path="pricing.py"),
        tool_call("search_code", "search", query="evidence"),
        tool_call("run_tests", "tests"),
        tool_call("search_code", "error", query=TOKEN),
        tool_call("read_file", "invalid", extra=MULTILINE),
        preview("single", old_text="100", new_text="90"),
        tool_call("preview_replacements", "multiple", relative_path="pricing.py",
                  replacements=[{"old_text": "100", "new_text": "90"}]),
        tool_call("preview_file_changes", "files", files=[{
            "relative_path": "pricing.py", "replacements": [{"old_text": "100", "new_text": "80"}],
        }]),
    ]
    model = RecordingModel(responses=[
        AIMessage(content=[{"type": "text", "text": f"检查 {TOKEN}"}], tool_calls=calls,
                  additional_kwargs={"tool_calls": [{"raw": MULTILINE}]}),
        AIMessage(content="正常诊断结果"),
    ])
    result = diagnosis_agent.run_diagnosis(make_agent(model, registry), f"定位订单故障\n{MULTILINE}")

    assert len(model.requests) == 2
    assert "定位订单故障" in model.requests[0][-1].content
    assert REDACTION_MARKER in model.requests[0][-1].content
    history = model.requests[1]
    assert [message.type for message in history] == ["system", "human", "ai"] + ["tool"] * len(calls)
    assert [(message.name, message.tool_call_id) for message in history[3:]] == [
        (call["name"], call["id"]) for call in calls
    ]
    assert history[2].tool_calls[4]["args"] == {"query": REDACTION_MARKER}
    assert history[2].content[0]["text"] == f"检查 {REDACTION_MARKER}"
    assert set(model.tool_names) == set(registry.tools)
    assert_no_fixture_values(model)
    visible = {
        message.tool_call_id: json.loads(message.content)["result"]
        for message in history[3:] if message.status != "error"
    }
    assert "pricing.py" in visible["list"]
    assert "price = 100" in visible["read"]
    assert "evidence" in visible["search"]
    assert visible["tests"] == {
        "output": REDACTION_MARKER, "nested": [{"error": REDACTION_MARKER}], "status": "failed",
    }
    assert f"虚构错误 {REDACTION_MARKER}" in visible["error"]
    assert "检查工具名和参数" in history[8].content
    for name in ("single", "multiple", "files"):
        assert REDACTION_MARKER in visible[name]["diff"]
    assert test_result == {"output": MULTILINE, "nested": [{"error": TOKEN}], "status": "failed"}
    original_call = result["messages"][1]
    assert original_call.tool_calls[4]["args"] == {"query": TOKEN}
    assert TOKEN in original_call.content[0]["text"]

    proposal = extract_latest_preview(result["messages"])["proposal"]
    assert TOKEN in proposal["diff"]
    assert REDACTION_MARKER not in proposal["diff"]
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    outcome = confirm_and_apply(str(tmp_path), proposal)
    assert outcome["status"] == "applied"
    assert (tmp_path / "pricing.py").read_bytes() == source.replace("100", "80").encode()


@pytest.mark.parametrize("field,value", [
    ("old_text", TOKEN), ("old_text", REDACTION_MARKER), ("new_text", REDACTION_MARKER),
])
def test_sensitive_or_masked_patch_arguments_are_rejected(tmp_path, field, value):
    source = f"price = 100\n# {TOKEN}\n# {REDACTION_MARKER}\n"
    (tmp_path / "pricing.py").write_bytes(source.encode())
    arguments = {"old_text": "100", "new_text": "80"}
    arguments[field] = value
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[preview("patch", **arguments)]),
        AIMessage(content="无法安全生成提案"),
    ])
    result = diagnosis_agent.run_diagnosis(make_agent(model, ToolRegistry(str(tmp_path))), "修复价格")
    proposal = extract_latest_preview(result["messages"])

    assert proposal["success"] is False
    assert "可完整核对" in proposal["message"]
    assert (tmp_path / "pricing.py").read_bytes() == source.encode()
    assert_no_fixture_values(model)


@pytest.mark.parametrize("payload", [
    {"nested": {TOKEN: "protocol value"}},
    {"nested": object()},
])
def test_failed_tool_result_redaction_blocks_next_model_call(tmp_path, payload):
    registry = ToolRegistry(str(tmp_path))
    registry.tools["run_tests"] = lambda: {"output": TOKEN, **payload}
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[tool_call("run_tests", "tests")]),
    ])
    with pytest.raises(ModelRedactionError, match="已停止发送"):
        diagnosis_agent.run_diagnosis(make_agent(model, registry), "运行测试")

    assert len(model.requests) == 1
    assert_no_fixture_values(model)


def test_protocol_collision_blocks_first_model_call(tmp_path):
    model = RecordingModel(responses=[])
    with pytest.raises(ModelRedactionError, match="工具的定义"):
        diagnosis_agent.run_diagnosis(make_agent(model, ToolRegistry(str(tmp_path)), ("read_file",)), "诊断")
    assert model.requests == []


def test_tool_call_id_is_never_rewritten_or_sent_when_it_contains_a_sensitive_value(tmp_path):
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[tool_call("list_files", TOKEN)]),
    ])
    with pytest.raises(ModelRedactionError, match="协议字段"):
        diagnosis_agent.run_diagnosis(make_agent(model, ToolRegistry(str(tmp_path))), "列出文件")
    assert len(model.requests) == 1
    assert_no_fixture_values(model)


def test_sensitive_value_in_serialized_numeric_argument_blocks_replay(tmp_path):
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[tool_call("read_file", "read", relative_path=803923)]),
    ])
    agent = make_agent(model, ToolRegistry(str(tmp_path)), ("803923",))
    with pytest.raises(ModelRedactionError, match="序列化内容"):
        diagnosis_agent.run_diagnosis(agent, "读取文件")
    assert len(model.requests) == 1


@pytest.mark.parametrize("values", [TOKEN, [None], [REDACTION_MARKER]])
def test_invalid_configuration_fails_before_workspace_or_model(monkeypatch, values):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid redaction configuration reached task setup")

    monkeypatch.setattr(diagnosis_agent, "create_workspace", unexpected)
    monkeypatch.setattr(diagnosis_agent, "create_model", unexpected)
    with pytest.raises(ModelRedactionError) as error:
        diagnosis_agent.main(sensitive_values=values)
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("blocked", [False, True], ids=["two-rounds", "hidden-approval"])
def test_main_redacts_retry_feedback_before_json_encoding(tmp_path, monkeypatch, capsys, blocked):
    working = tmp_path / "workspace" / "project"
    tests = working / "tests"
    tests.mkdir(parents=True)
    (working / "pytest.ini").write_text(
        "[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n",
        encoding="utf-8",
    )
    original = f"# {TOKEN}\nprice = 100\n" if blocked else "price = 100\n"
    (working / "pricing.py").write_bytes(original.encode())
    (working / "fixture.txt").write_text(MULTILINE, encoding="utf-8")
    (tests / "test_order_service.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "from pricing import price\n"
        "def test_create_order_with_discount():\n"
        "    print('x' * 70000)\n"
        "    print(Path('fixture.txt').read_text(encoding='utf-8'))\n"
        "    assert price == 80\n"
        f"@pytest.mark.parametrize('value', [1, 2], ids=[{TOKEN!r}, {'虚构标记🧪-another'!r}])\n"
        "def test_extra(value):\n"
        "    assert value > 0\n",
        encoding="utf-8",
    )
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[
            tool_call("read_file", "fixture-read", relative_path="fixture.txt"),
            preview("first", old_text="price = 100", new_text="price = 90"),
        ]),
        AIMessage(content="第一轮诊断"),
        AIMessage(content="", tool_calls=[
            preview("second", old_text="price = 90", new_text="price = 80"),
        ]),
        AIMessage(content="第二轮诊断"),
    ])
    monkeypatch.setattr(diagnosis_agent, "create_workspace", lambda *_: working)
    monkeypatch.setattr(diagnosis_agent, "create_model", lambda: model)
    def approve(prompt):
        if blocked:
            pytest.fail("redacted patch reached the approval prompt")
        return "yes"

    monkeypatch.setattr("builtins.input", approve)
    report_dir = tmp_path / "task-reports"
    monkeypatch.setattr(
        diagnosis_agent, "save_run_report",
        partial(diagnosis_agent.save_run_report, output_dir=str(report_dir)),
    )

    diagnosis_agent.main(
        sensitive_values=(*SENSITIVE_VALUES, MULTILINE.replace("\n", "\r\n"), "虚构标记🧪-another"),
        user_request=f"修复价格 {TOKEN}",
    )

    assert len(model.requests) == (2 if blocked else 4)
    assert_no_fixture_values(model)

    report_path, = report_dir.glob("task_*.json")
    report_text = report_path.read_text(encoding="utf-8")
    terminal = capsys.readouterr()
    for value in SENSITIVE_VALUES:
        assert value not in report_text + terminal.out + terminal.err
        assert json.dumps(value, ensure_ascii=False)[1:-1] not in report_text
    assert "虚构标记🧪-another" not in report_text + terminal.out + terminal.err
    report = json.loads(report_text)
    if blocked:
        assert report["final_status"] == "approval_blocked"
        assert report["approval"]["status"] == "not_requested"
        assert report["application"]["status"] == "approval_blocked"
        assert (working / "pricing.py").read_bytes() == original.encode()
        return

    retry = model.requests[2][1].content
    assert "第 2 轮修复" in retry
    feedback = json.loads(retry.split("上一轮结果：\n", 1)[1])
    assert REDACTION_MARKER in feedback["test_output"]
    assert MULTILINE.replace("\n", "\r\n") not in feedback["test_output"]
    assert "price = 90" in feedback["previous_change"]["diff"]
    assert feedback["decision"]["reason"] == "targets_still_failing"
    assert report["final_status"] == "completed"
    assert [item["status"] for item in report["rounds"]] == ["completed", "completed"]
    target, = report["task"]["target_test_ids"]
    assert feedback["test_results"] == {target: "failed", "[test-id-1]": "passed", "[test-id-2]": "passed"}
    assert report["verification"]["after_fix"]["test_results"] == {
        target: "passed", "[test-id-1]": "passed", "[test-id-2]": "passed",
    }
    for run in (report["verification"]["baseline"], report["verification"]["after_fix"]):
        assert run["junit_xml"]["status"] == "deleted"
        assert run["report_path"] is None
        assert run["stdout_truncated"] is True
    assert (working / "pricing.py").read_text(encoding="utf-8") == "price = 80\n"
    from rollback_file import rollback_file
    assert rollback_file(report_path, "pricing.py", sensitive_values=SENSITIVE_VALUES)["status"] == "rolled_back"
    assert (working / "pricing.py").read_text(encoding="utf-8") == "price = 100\n"
