import json
import os
import subprocess
from pathlib import Path

import pytest

from apply_proposal import confirm_and_apply
from file_access import FileAccessPolicy
from tools.edit_tools import EditTools
from tools.file_tools import FileTools
from tools.langchain_tools import create_langchain_tools
from tools.tool_registry import ToolRegistry
from workspace import create_baseline_snapshot, create_workspace


MARKER = "synthetic-sensitive-marker"
PROTECTED_PATHS = (
    ".env", "config/.EnV.LoCaL", "config/private.PEM", "server.KeY",
    ".GiT/hidden.py", ".VeNv/hidden.py", "VeNv/hidden.py",
)


def write_file(root, relative_path, content=MARKER):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def file_change(relative_path):
    return {
        "relative_path": relative_path,
        "replacements": [{"old_text": "old", "new_text": "new"}],
    }


def assert_preview_denied(root, relative_path, reason):
    edits = EditTools(str(root))
    for result in (
        edits.preview_replace(relative_path, "old", "new"),
        edits.preview_replacements(relative_path, file_change(relative_path)["replacements"]),
        edits.preview_file_changes([file_change(relative_path)]),
    ):
        assert not result["success"] and not result["diff"]
        assert reason in result["message"] and MARKER not in str(result)


def test_copy_snapshot_and_file_tools_share_sensitive_rules(tmp_path, caplog, monkeypatch):
    source = tmp_path / "source"
    for name in PROTECTED_PATHS:
        write_file(source, name)
    write_file(source, "src/normal.py", "public_value = 'old'\n")
    listing = FileTools(str(source))
    original_iterdir = Path.iterdir

    def check_pruning(path):
        assert path.name.casefold() not in {".git", ".venv", "venv"}
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", check_pruning)
    names = listing.list_files()
    assert "src/normal.py" in names and "已跳过 7 个" in names
    assert all(name.casefold() not in names.casefold() for name in PROTECTED_PATHS)
    assert "hidden.py" not in names
    assert "public_value" in listing.search_code("public_value")
    assert "hidden.py" not in listing.search_code("synthetic-sensitive")
    assert "已跳过 7 个" in listing.search_code("synthetic-sensitive")

    working = create_workspace(str(source), str(tmp_path / "copies"))
    assert all(not (working / name).exists() for name in PROTECTED_PATHS)
    assert (working / "src/normal.py").read_text(encoding="utf-8") == "public_value = 'old'\n"
    assert "跳过了 7 个" in caplog.text and MARKER not in caplog.text
    write_file(working, "late/.ENV.runtime")
    snapshot = create_baseline_snapshot(str(working))
    assert not (snapshot / "late/.ENV.runtime").exists()
    assert (snapshot / "src/normal.py").is_file()


def test_sensitive_paths_rejected_before_file_content_is_read(tmp_path, monkeypatch):
    for name in PROTECTED_PATHS:
        write_file(tmp_path, name)

    def unexpected_read(*args, **kwargs):
        pytest.fail("a protected file was opened")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    registry = ToolRegistry(str(tmp_path))
    tool = next(tool for tool in create_langchain_tools(registry) if tool.name == "read_file")
    for name in (*PROTECTED_PATHS, ".env.missing"):
        result = json.loads(tool.invoke({"relative_path": name}))["result"]
        assert "访问策略保护" in result and MARKER not in result
        assert_preview_denied(tmp_path, name, "访问策略保护")

    result = confirm_and_apply(str(tmp_path), {
        "relative_path": ".env", "old_text": "old", "new_text": "new", "diff": "untrusted preview",
    })
    assert result["approval_status"] == "not_requested"
    assert result["files"][0]["status"] == "validation_failed"


def test_relative_paths_and_optional_project_rules(tmp_path):
    source = tmp_path / "project"
    normal = write_file(source, "src/code.py", "value = 'old'\n")
    write_file(source, "internal/settings.py")
    policy = FileAccessPolicy(extra_paths=("Internal/Settings.py",))
    files = FileTools(str(source), policy=policy)
    assert "old" in files.read_file("src/code.py")
    assert "文件不存在" in files.read_file("src/missing.py")
    assert "访问策略保护" in files.read_file("internal/settings.py")
    assert "settings.py" not in files.list_files()
    assert MARKER not in files.search_code("synthetic-sensitive")
    registry = ToolRegistry(str(source), policy=policy)
    result = registry.execute("preview_file_changes", {
        "files": [file_change("src/code.py"), file_change("internal/settings.py")],
    })["result"]
    assert not result["success"] and not result["diff"]
    assert result["files"][1]["status"] == "validation_failed"
    assert "访问策略保护" in result["message"] and MARKER not in str(result)
    assert EditTools(str(source)).preview_replace("src/code.py", "old", "new")["success"]
    for path in (str(normal), "src/../src/code.py", "../outside.py", "C:code.py", r"\code.py"):
        assert files.read_file(path).startswith("读取失败")
        assert_preview_denied(source, path, "路径" if ".." not in path else "工作目录之外")
    working = create_workspace(str(source), str(tmp_path / "copies"), policy=policy)
    assert (working / "src/code.py").is_file() and not (working / "internal/settings.py").exists()


def test_hardlink_alias_is_not_copied_or_read(tmp_path):
    source = tmp_path / "project"
    target = write_file(source, ".env.local")
    os.link(target, source / "alias.py")
    files = FileTools(str(source))
    assert "alias.py" not in files.list_files()
    assert MARKER not in files.search_code("synthetic-sensitive")
    assert "硬链接" in files.read_file("alias.py")
    assert_preview_denied(source, "alias.py", "硬链接")
    working = create_workspace(str(source), str(tmp_path / "copies"))
    assert not (working / "alias.py").exists()


@pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
def test_symlink_alias_is_not_copied_or_read(tmp_path, kind):
    source = tmp_path / "project"
    target = write_file(source, ".env.local")
    alias = source / "alias.py"
    read_path = "alias.py"
    if kind == "directory":
        target = write_file(tmp_path / "outside", "settings.py").parent
        alias = source / "alias"
        read_path = "alias/settings.py"
    elif kind == "dangling":
        target = source / "not-created"
    try:
        alias.symlink_to(target, target_is_directory=kind == "directory")
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows 当前账号无权创建符号链接")
        raise

    assert alias.is_symlink()
    files = FileTools(str(source))
    assert alias.name not in files.list_files()
    assert "已跳过" in files.search_code("synthetic-sensitive")
    assert MARKER not in files.search_code("synthetic-sensitive")
    assert "符号链接" in files.read_file(read_path)
    assert_preview_denied(source, read_path, "符号链接")
    working = create_workspace(str(source), str(tmp_path / "copies"))
    assert not (working / alias.name).exists() and not (working / alias.name).is_symlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS junction")
def test_windows_junction_is_not_traversed_or_copied(tmp_path, caplog):
    source = tmp_path / "project"
    write_file(source, "normal.py", "value = 'old'\n")
    outside = write_file(tmp_path / "outside", "settings.py").parent
    junction = source / "alias"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True, timeout=10)

    assert junction.is_junction()
    files = FileTools(str(source))
    assert "alias" not in files.list_files()
    assert "settings.py" not in files.search_code("synthetic-sensitive")
    assert "junction" in files.read_file("alias/settings.py")
    assert_preview_denied(source, "alias/settings.py", "junction")
    working = create_workspace(str(source), str(tmp_path / "copies"))
    assert not (working / "alias").exists()
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(working / "alias"), str(outside)],
                   check=True, capture_output=True, timeout=10)
    snapshot = create_baseline_snapshot(str(working))
    assert not (snapshot / "alias").exists() and "跳过了 1 个" in caplog.text
    assert (outside / "settings.py").read_text(encoding="utf-8") == MARKER


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS 路径别名")
def test_windows_rejects_data_stream_and_trailing_dot_aliases(tmp_path):
    write_file(tmp_path, "normal.py", "value = 'old'\n")
    files = FileTools(str(tmp_path))
    for alias in ("normal.py:hidden", "normal.py.", "normal.py "):
        assert "Windows 数据流" in files.read_file(alias)
        assert_preview_denied(tmp_path, alias, "Windows 数据流")
