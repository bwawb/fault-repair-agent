from dataclasses import asdict

import pytest

from redaction import MAX_CONTAINER_DEPTH, REDACTION_MARKER, TextRedactor


def test_redact_text_reports_hits_and_misses():
    redactor = TextRedactor(["fixture-credential"])

    result = redactor.redact_text("a fixture-credential and another fixture-credential")
    assert result.value == f"a {REDACTION_MARKER} and another {REDACTION_MARKER}"
    assert result.match_count == 2
    assert result.redacted is True
    assert asdict(result) == {"value": result.value, "match_count": 2}
    assert "fixture-credential" not in repr(result)
    assert "fixture-credential" not in repr(redactor)

    unchanged = redactor.redact_text("normal output")
    assert unchanged.value == "normal output"
    assert unchanged.match_count == 0
    assert unchanged.redacted is False


def test_matches_are_literal_leftmost_longest_and_non_overlapping():
    redactor = TextRedactor(["fake", "fake", "fake-value", "value-extended", "x.*[1]"])

    result = redactor.redact_text("fake-value-extended fake x.*[1] xABC1 Fake")
    assert result.value == f"{REDACTION_MARKER}-extended {REDACTION_MARKER} {REDACTION_MARKER} xABC1 Fake"
    assert result.match_count == 3


def test_replacement_marker_is_not_scanned_again():
    redactor = TextRedactor(["fixture-value", REDACTION_MARKER])
    result = redactor.redact_text(f"fixture-value {REDACTION_MARKER}")

    assert result.value == f"{REDACTION_MARKER} {REDACTION_MARKER}"
    assert result.match_count == 2
    assert redactor.redact_text(REDACTION_MARKER).redacted is True


def test_redact_data_copies_unicode_keys_and_nested_values():
    data = {
        "报告": [{"文件-示例🧪": "旧值 示例🧪", "文本": "é / e\u0301"}],
        "状态": [None, True, 7, 0.5, ""],
    }
    redactor = TextRedactor(["示例🧪", "é"])
    result = redactor.redact_data(data)

    assert result.value == {
        "报告": [{f"文件-{REDACTION_MARKER}": f"旧值 {REDACTION_MARKER}",
                  "文本": f"{REDACTION_MARKER} / e\u0301"}],
        "状态": [None, True, 7, 0.5, ""],
    }
    assert result.match_count == 3
    assert result.redacted is True
    result.value["报告"][0]["文本"] = "changed"
    result.value["状态"].append(False)
    assert data == {
        "报告": [{"文件-示例🧪": "旧值 示例🧪", "文本": "é / e\u0301"}],
        "状态": [None, True, 7, 0.5, ""],
    }


def test_empty_literals_are_ignored_and_no_hit_data_is_still_copied():
    redactor = TextRedactor(["", ""])
    data = {"items": ["", "plain"], "count": 1}

    result = redactor.redact_data(data)
    assert result.value == data
    assert result.value is not data
    assert result.value["items"] is not data["items"]
    assert result.match_count == 0
    assert TextRedactor(["fixture-value"]).redact_text("").match_count == 0
    assert TextRedactor().redact_data(None).value is None


@pytest.mark.parametrize("values", ["fixture-value", b"fixture-value", [None], [123]])
def test_invalid_sensitive_value_configuration_is_rejected(values):
    with pytest.raises(TypeError) as error:
        TextRedactor(values)
    assert "fixture-value" not in str(error.value)


def test_unsupported_input_is_rejected():
    redactor = TextRedactor()

    with pytest.raises(TypeError, match="必须是字符串"):
        redactor.redact_text(None)
    with pytest.raises(TypeError, match="内建数据类型"):
        redactor.redact_data({"item": ("fixture-value",)})
    with pytest.raises(TypeError, match="字典的键必须是字符串"):
        redactor.redact_data({123: "item"})


@pytest.mark.parametrize("other_key", ["fixture-beta", REDACTION_MARKER])
def test_key_conflict_raises_without_mutating_source_or_reporting_keys(other_key):
    data = {"fixture-alpha": "first", other_key: "second"}
    redactor = TextRedactor(["fixture-alpha", "fixture-beta"])

    with pytest.raises(ValueError, match="字典键发生冲突") as error:
        redactor.redact_data(data)

    assert "fixture-alpha" not in str(error.value)
    assert "fixture-beta" not in str(error.value)
    assert data == {"fixture-alpha": "first", other_key: "second"}


def test_shared_containers_are_copied_and_counted_per_occurrence():
    shared = ["fixture-value"]
    data = [shared, shared]

    result = TextRedactor(["fixture-value"]).redact_data(data)

    assert result.value == [[REDACTION_MARKER], [REDACTION_MARKER]]
    assert result.match_count == 2
    assert result.value[0] is not result.value[1]
    assert data[0] is data[1] is shared
    assert shared == ["fixture-value"]


def test_cycles_are_rejected_in_lists_and_dicts():
    items = []
    items.append(items)
    mapping = {}
    mapping["items"] = [mapping]

    for data in (items, mapping):
        with pytest.raises(ValueError, match="循环引用"):
            TextRedactor().redact_data(data)


def test_container_depth_boundary():
    data = "fixture-value"
    for _ in range(MAX_CONTAINER_DEPTH):
        data = [data]

    redactor = TextRedactor(["fixture-value"])
    assert redactor.redact_data(data).match_count == 1
    with pytest.raises(ValueError, match="嵌套层级上限"):
        redactor.redact_data([data])
