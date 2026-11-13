from hashlib import sha256

import pytest

from apply_proposal import confirm_and_apply
from patch_summary import build_file_diff
from tools.edit_tools import EditTools


@pytest.mark.parametrize(
    ("file_newline", "proposal_newline", "before_eof", "after_eof"),
    [
        ("\n", "\n", "\n", "\n"),
        ("\r\n", "\n", "\r\n", "\r\n"),
        ("\r\n", "\r\n", "", ""),
        ("\n", "\r\n", "", "\n"),
        ("\r\n", "\n", "\r\n", ""),
    ],
    ids=["lf", "crlf", "no-final-newline", "add-final-newline", "remove-final-newline"],
)
def test_multiline_replacement_preserves_file_bytes(
    tmp_path, monkeypatch, file_newline, proposal_newline, before_eof, after_eof
):
    working = tmp_path / "project"
    working.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    old_lines = ["count = 1", "label = '旧'"]
    new_lines = ["count = 2", "enabled = True", "label = '新'"]
    original = (file_newline.join(["# 保留", *old_lines]) + before_eof).encode("utf-8")
    expected = (file_newline.join(["# 保留", *new_lines]) + after_eof).encode("utf-8")
    target = working / "code.py"
    target.write_bytes(original)
    (baseline / "code.py").write_bytes(original)

    proposal = {
        "relative_path": "code.py",
        "old_text": proposal_newline.join(old_lines) + (proposal_newline if before_eof else ""),
        "new_text": proposal_newline.join(new_lines) + (proposal_newline if after_eof else ""),
    }
    preview = EditTools(str(working)).preview_replace(**proposal)
    assert preview["success"], preview
    assert target.read_bytes() == original
    assert "-label = '旧'\n" in preview["diff"]
    assert "+enabled = True\n" in preview["diff"]
    assert preview["diff"].count("\\ No newline at end of file") == (
        int(not before_eof) + int(not after_eof)
    )

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    result = confirm_and_apply(str(working), {**proposal, "diff": preview["diff"]})

    assert result["status"] == "applied", result
    assert result["diff"] == preview["diff"]
    assert target.read_bytes() == expected
    assert result["before_hash"] == sha256(original).hexdigest()
    assert result["after_hash"] == sha256(expected).hexdigest()
    cumulative = build_file_diff(str(baseline), str(working), "code.py")
    assert cumulative.splitlines()[2:] == preview["diff"].splitlines()[2:]
    assert (baseline / "code.py").read_bytes() == original


@pytest.mark.parametrize(
    ("original", "old_text", "message"),
    [
        (b"a\nb\n", "a\nc", "没有找到原文本"),
        (b"a\nb\na\nb\n", "a\nb", "原文本出现多次"),
        (b"a\na\na\n", "a\na", "原文本出现多次"),
        (b"a\r\nb\nc\n", "a\nb", "统一使用 LF 或 CRLF"),
        (b"a\nb\n", "a\rb", "不支持单独的 CR"),
        (b"a\nb\xff\n", "a\nb", "UTF-8"),
    ],
    ids=["missing", "duplicate", "overlap", "mixed-newlines", "bare-cr", "invalid-utf8"],
)
def test_invalid_multiline_proposal_is_rejected(tmp_path, original, old_text, message):
    target = tmp_path / "code.py"
    target.write_bytes(original)
    proposal = {"relative_path": "code.py", "old_text": old_text, "new_text": "x\ny"}

    preview = EditTools(str(tmp_path)).preview_replace(**proposal)
    assert not preview["success"]
    assert message in preview["message"]

    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": "invalid preview"})
    assert result["status"] == "error"
    assert result["approval_status"] == "not_requested"
    assert message in result["message"]
    assert target.read_bytes() == original


@pytest.mark.parametrize("externally_edited", [False, True], ids=["rejected", "edited-during-approval"])
def test_multiline_write_requires_approval_and_unchanged_bytes(tmp_path, monkeypatch, externally_edited):
    target = tmp_path / "code.py"
    original = b"a = 1\r\nb = 2\r\n"
    external = b"a = 1\nb = 2\n"
    target.write_bytes(original)
    proposal = {"relative_path": "code.py", "old_text": "a = 1\nb = 2", "new_text": "a = 3\nb = 4"}
    preview = EditTools(str(tmp_path)).preview_replace(**proposal)
    assert preview["success"]

    def approve(_):
        if externally_edited:
            target.write_bytes(external)
            return "yes"
        return "no"

    monkeypatch.setattr("builtins.input", approve)
    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": preview["diff"]})

    if externally_edited:
        assert result["status"] == "error"
        assert "确认期间发生变化" in result["message"]
        assert target.read_bytes() == external
    else:
        assert result["status"] == "rejected"
        assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "new_text", "expected"),
    [
        (b"a = 1\r\nb = 2\nc = 3", "b = 4", b"a = 1\r\nb = 4\nc = 3"),
        (b"b = 2", "b = 4\nc = 5", b"b = 4\nc = 5"),
    ],
    ids=["single-line-in-mixed-file", "first-newline-defaults-to-lf"],
)
def test_replacement_newline_policy(tmp_path, monkeypatch, original, new_text, expected):
    target = tmp_path / "code.py"
    target.write_bytes(original)
    proposal = {"relative_path": "code.py", "old_text": "b = 2", "new_text": new_text}
    preview = EditTools(str(tmp_path)).preview_replace(**proposal)
    assert preview["success"], preview
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": preview["diff"]})
    assert result["status"] == "applied", result
    assert target.read_bytes() == expected


@pytest.mark.parametrize("reverse_order", [False, True], ids=["source-order", "reverse-order"])
def test_multiple_replacements_use_original_file(tmp_path, monkeypatch, reverse_order):
    target = tmp_path / "code.py"
    original = b"first = 1\r\nkeep = True\r\nsecond = 2"
    expected = b"first = 10\r\nsecond = 2\r\nkeep = True\r\nsecond = 20"
    target.write_bytes(original)
    replacements = [
        {"old_text": "first = 1", "new_text": "first = 10\nsecond = 2"},
        {"old_text": "second = 2", "new_text": "second = 20"},
    ]
    if reverse_order:
        replacements.reverse()
    proposal = {"relative_path": "code.py", "replacements": replacements}
    preview = EditTools(str(tmp_path)).preview_replacements(**proposal)
    assert preview["success"], preview
    assert target.read_bytes() == original
    assert "+second = 2\n" in preview["diff"]
    assert "+second = 20\n" in preview["diff"]
    assert "\\ No newline at end of file\n" in preview["diff"]

    prompts = []

    def approve(prompt):
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr("builtins.input", approve)
    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": preview["diff"]})

    assert len(prompts) == 1
    assert result["status"] == "applied", result
    assert result["diff"] == preview["diff"]
    assert target.read_bytes() == expected
    assert result["before_hash"] == sha256(original).hexdigest()
    assert result["after_hash"] == sha256(expected).hexdigest()


@pytest.mark.parametrize(
    ("old_text", "message"),
    [
        ("missing", "第 2 处修改没有找到原文本"),
        ("repeat", "第 2 处修改的原文本出现多次"),
        ("beta gamma", "匹配范围重叠"),
    ],
    ids=["missing", "duplicate", "overlapping"],
)
def test_one_invalid_replacement_rejects_entire_proposal(tmp_path, old_text, message):
    target = tmp_path / "code.py"
    original = b"alpha beta gamma\nrepeat repeat\n"
    target.write_bytes(original)
    proposal = {
        "relative_path": "code.py",
        "replacements": [
            {"old_text": "alpha beta", "new_text": "first"},
            {"old_text": old_text, "new_text": "second"},
        ],
    }

    preview = EditTools(str(tmp_path)).preview_replacements(**proposal)
    assert not preview["success"]
    assert message in preview["message"]
    assert preview["diff"] == ""

    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": "invalid preview"})
    assert result["status"] == "error"
    assert result["approval_status"] == "not_requested"
    assert message in result["message"]
    assert target.read_bytes() == original


@pytest.mark.parametrize("externally_edited", [False, True], ids=["rejected", "edited-during-approval"])
def test_multiple_replacements_do_not_write_without_valid_approval(tmp_path, monkeypatch, externally_edited):
    target = tmp_path / "code.py"
    original = b"a = 1\nb = 2\n"
    external = original + b"# manual edit\n"
    target.write_bytes(original)
    proposal = {
        "relative_path": "code.py",
        "replacements": [
            {"old_text": "a = 1", "new_text": "a = 3"},
            {"old_text": "b = 2", "new_text": "b = 4"},
        ],
    }
    preview = EditTools(str(tmp_path)).preview_replacements(**proposal)
    assert preview["success"], preview

    def approve(_):
        if externally_edited:
            target.write_bytes(external)
            return "yes"
        return "no"

    monkeypatch.setattr("builtins.input", approve)
    result = confirm_and_apply(str(tmp_path), {**proposal, "diff": preview["diff"]})

    if externally_edited:
        assert result["status"] == "error"
        assert "确认期间发生变化" in result["message"]
        assert target.read_bytes() == external
    else:
        assert result["status"] == "rejected"
        assert target.read_bytes() == original
