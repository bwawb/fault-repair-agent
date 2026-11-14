import xml.etree.ElementTree as ET
from pathlib import Path
from stat import S_ISREG

def read_test_report(report_path: str) -> dict[str, str]:
    """
    读取pytest生成的JUnit XML报告

    Args:
        report_path: JUnit XML报告
    Returns:
        测试表示->测试状态

    状态可能为:
        passed、failed、skipped、error
    """
    path = Path(report_path)

    try:
        if not S_ISREG(path.stat().st_mode):
            raise ValueError(f"测试报告路径不是普通文件：{path}")
        tree = ET.parse(path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"测试报告不存在：{path}") from exc
    except (ET.ParseError, LookupError) as exc:
        raise ValueError(f"测试报告不是有效的 XML：{path}") from exc

    root = tree.getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError(f"测试报告不是 JUnit XML：{path}")
    results = {}

    for case in root.iter("testcase"):
        class_name = case.get("classname", "")
        test_name = case.get("name", "")

        if not test_name:
            raise ValueError("测试报告中存在没有名称的测试用例。")

        # 使用 pytest XML 中的 classname 和 name 共同标识用例
        test_id = f"{class_name}::{test_name}"

        if test_id in results:
            raise ValueError(
                f"测试标识重复，无法可靠比较：{test_id}"
            )

        if case.find("error") is not None:
            status = "error"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"

        results[test_id] = status

    if not results:
        raise ValueError(
            f"报告中没有找到测试用例，无法比较：{path}"
        )

    return results


def compare_results(before: dict[str, str], after: dict[str, str]) -> dict:
    """
    比较修复前后的测试结果

    这里只对passed和failed两种状态进行恢复或回归判断
    """

    before_ids = set(before)
    after_ids = set(after)

    # 防御性处理
    only_before = sorted(before_ids - after_ids)
    only_after = sorted(after_ids - before_ids)

    common_ids = before_ids & after_ids

    fixed = []
    still_failing = []
    newly_failed = []
    uncertain = []

    for test_id in sorted(common_ids):
        before_status = before[test_id]
        after_status = after[test_id]

        # skipped 或 error 不能直接当作通过或失败。
        if before_status not in {"passed", "failed"}:
            uncertain.append(
                (test_id, before_status, after_status)
            )
            continue
        if after_status not in {"passed", "failed"}:
            uncertain.append(
                (test_id, before_status, after_status)
            )
            continue

        if before_status == "failed" and after_status == "passed":
            fixed.append(test_id)
        elif before_status == "failed" and after_status == "failed":
            still_failing.append(test_id)
        elif before_status == "passed" and after_status == "failed":
            newly_failed.append(test_id)

    return {
        "fixed": fixed,
        "still_failing": still_failing,
        "newly_failed": newly_failed,
        "uncertain": uncertain,
        "only_before": only_before,
        "only_after": only_after,
    }


def print_items(title: str, items: list) -> None:
    """打印一组测试结果。"""
    print(f"\n{title}：")

    if not items:
        print("  无")
        return


    for item in items:
        print(f"  {item}")



def main():
    baseline_path = (
        r"E:\PythonProject\fault-repair-agent"
        r"\outputs\test_reports\baseline.xml"
    )

    after_fix_path = (
        r"E:\PythonProject\fault-repair-agent"
        r"\outputs\test_reports\after_fix.xml"
    )

    before = read_test_report(baseline_path)
    after = read_test_report(after_fix_path)

    comparison = compare_results(before, after)

    print("===== 修复前后测试结果对比 =====")
    print(f"修复前测试用例数：{len(before)}")
    print(f"修复后测试用例数：{len(after)}")

    print_items("恢复通过", comparison["fixed"])
    print_items("仍然失败", comparison["still_failing"])
    print_items("新增失败", comparison["newly_failed"])

    print_items(
        "状态无法直接比较",
        comparison["uncertain"],
    )

    print_items(
        "仅出现在修复前报告中",
        comparison["only_before"],
    )

    print_items(
        "仅出现在修复后报告中",
        comparison["only_after"],
    )

if __name__ == "__main__":
    main()
