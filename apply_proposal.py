import hashlib
from pathlib import Path

from tools.edit_tools import EditTools


def sha256_bytes(content: bytes) -> str:
    """计算字节内容的SHA256-摘要"""
    return hashlib.sha256(content).hexdigest()

def confirm_and_apply(project_path: str, proposal: dict, ) -> dict:
    """
    展示提案，等待人工确认，并应用单文件文本替换。

    当前学习版本只支持单行 old_text 和 new_text。

    Args:
        project_path: 工作副本的项目根目录。
        proposal: 从Agent消息中提取的修改提案。

    Returns:
        应用结果
    """

    project_root = Path(project_path).resolve()

    # 获取Agent调用工具的传入的参数内容
    relative_path = proposal.get("relative_path")
    old_text = proposal.get("old_text")
    new_text = proposal.get("new_text")
    original_diff = proposal.get("diff")

    # 1.检查填内容
    if not isinstance(relative_path, str) or not relative_path.strip():
        return {
            "status": "error",
            "message": "提案缺少有效的目标文件路径。",
        }

    if not isinstance(old_text, str) or not old_text:
        return {
            "status": "error",
            "message": "提案缺少有效的原文本。",
        }

    if not isinstance(new_text, str):
        return {
            "status": "error",
            "message": "提案的新文本必须是字符串。",
        }

    if not isinstance(original_diff, str) or not original_diff:
        return {
            "status": "error",
            "message": "提案缺少有效的 diff。",
        }

    # 当前版本先限定为单行替换，便于和字节匹配保持一致。
    if any(char in old_text or char in new_text for char in ("\n", "\r")):
        return {
            "status": "error",
            "message": "当前版本只支持单行文本替换。",
        }

    requested_path = Path(relative_path)

    if requested_path.is_absolute():
        return {
            "status": "error",
            "message": "目标路径必须相对于工作目录。",
        }

    target_path = (project_root / requested_path).resolve()

    # 2. 检查目标文件是否位于工作目录。
    if not target_path.is_relative_to(project_root):
        return {
            "status": "error",
            "message": "不允许修改工作目录之外的文件。",
        }


    # 当前阶段不修改测试文件。
    relative_parts = target_path.relative_to(project_root).parts

    if "tests" in relative_parts:
        return {
            "status": "error",
            "message": "当前阶段不允许修改 tests 目录中的文件。",
        }

    if not target_path.is_file():
        return {
            "status": "error",
            "message": f"目标文件不存在：{relative_path}",
        }

    try:
        # 3. 在重新预览前记录文件内容。
        before_preview = target_path.read_bytes()
        before_preview_hash = sha256_bytes(before_preview)

        # 4. 在同一个工作副本中重新生成预览。
        edit_tools = EditTools(str(project_root))

        preview = edit_tools.preview_replace(
            relative_path=relative_path,
            old_text=old_text,
            new_text=new_text,
        )

        if not preview["success"]:
            return {
                "status": "error",
                "message": f"重新预览失败：{preview['message']}",
            }

        # 确认预览期间文件没有变化。
        after_preview = target_path.read_bytes()



        if sha256_bytes(after_preview) != before_preview_hash:
            return {
                "status": "error",
                "message": "文件在生成预览时发生变化，请重新分析。",
            }

        # 确认重新生成的 diff 与 Agent 提案中的 diff 一致。
        if preview["diff"] != original_diff:
            return {
                "status": "error",
                "message": "当前 diff 与 Agent 的原始预览不一致，请重新分析。",
            }

        # 5. 展示本次实际准备应用的 diff。
        print("\n===== 待确认的修改 =====")
        print(preview["diff"])

        decision = input(
            "\n是否将上述修改应用到工作副本？"
            "输入 yes 确认，其他内容取消："
        ).strip().lower()

        if decision != "yes":
            return {
                "status": "rejected",
                "message": "用户取消了修改。",
            }

        # 6. 确认后再次检查文件版本。
        current_bytes = target_path.read_bytes()

        if sha256_bytes(current_bytes) != before_preview_hash:
            return {
                "status": "error",
                "message": "文件在确认期间发生变化，请重新预览。",
            }

        old_bytes = old_text.encode("utf-8")
        new_bytes = new_text.encode("utf-8")

        match_count = current_bytes.count(old_bytes)

        if match_count != 1:
            return {
                "status": "error",
                "message": (
                    f"原文本出现 {match_count} 次，"
                    "无法确定唯一修改位置。"
                ),
            }

        # 7. 替换并写入工作副本。
        modified_bytes = current_bytes.replace(
            old_bytes,
            new_bytes,
            1,
        )

        target_path.write_bytes(modified_bytes)

        return {
            "status": "applied",
            "message": "修改已应用到工作副本。",
            "file_path": str(target_path),
            "before_hash": before_preview_hash,
            "after_hash": sha256_bytes(modified_bytes),
            "diff": preview["diff"],
        }




    except (OSError, UnicodeError) as exc:

        return {

            "status": "error",

            "message": f"处理修改时发生错误：{exc}",

        }
