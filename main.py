from tools.tool_registry import ToolRegistry


def main():
    # 使用你原来的示例项目路径。
    project_path = r"E:\PythonProject\demo-python-project"

    registry = ToolRegistry(project_path)

    result = registry.execute(
        tool_name="preview_replace",
        arguments={
            "relative_path": "pricing.py",
            "old_text": "discount_rate = discount_percent // 100",
            "new_text": "discount_rate = discount_percent / 100",
        },
    )

    print(result)


if __name__ == "__main__":
    main()