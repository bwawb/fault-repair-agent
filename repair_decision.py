def decide_next_step(
        target_test_ids: list[str],
        baseline_results: dict[str, str],
        after_results: dict[str, str],
        current_round: int,
        max_rounds: int,
) -> dict:
    """
    根据测试结果决定是否进入下一轮。

    前提：修复前后测试进行均正常完成，且报告已成功解析

    Returns：
        action:
            stop - 停止
            retry - 进入下一轮

        reason:
            决定原因，供程序和报告使用
    """

    if not target_test_ids:
        raise ValueError("目标测试列表不能为空。")

    if max_rounds < 1 or not 1 <= current_round <= max_rounds:
        raise ValueError("当前轮次和最大轮次不合法。")

    # 当前版本仅接受基线中确实失败的目标测试
    # 若含有成功的目标测试，则停止
    invalid_targets = {
        test_id: baseline_results.get(test_id, "missing")
        for test_id in target_test_ids
        if baseline_results.get(test_id) != "failed"
    }


    if invalid_targets:
        return {
            "action": "stop",
            "reason": "invalid_target_baseline",
            "details": invalid_targets,
        }

    # 确保本轮仍能看到所有基线测试
    missing_tests = sorted(set(baseline_results) - set(after_results))
    if missing_tests:
        return {
            "action": "stop",
            "reason": "tests_missing",
            "details": missing_tests,
        }

    # 已通过的基线测试如果变成失败，属于新增失败
    newly_failed = sorted(
        test_id
        for test_id, before_status in baseline_results.items()
        if before_status == "passed" and after_results[test_id] == "failed"
    )

    if newly_failed:
        return {
            "action": "stop",
            "reason": "regression",
            "details": newly_failed
        }

    # 原本通过的测试若变为skipped/error等，不能认定为没有回归
    uncertain_tests = {
        test_id: after_results[test_id]
        for test_id, before_status in baseline_results.items()
        if before_status == "passed" and after_results[test_id] not in {"passed", "failed"}
    }


    # 目标测试也必须有明确的passed/failed结果
    uncertain_targets = {
        test_id: after_results.get(test_id, "missing")
        for test_id in target_test_ids
        if after_results.get(test_id) not in {"passed", "failed"}
    }

    if uncertain_tests or uncertain_targets:
        return {
            "action": "stop",
            "reason": "verification_uncertain",
            "details": {
                "tests": uncertain_tests,
                "targets": uncertain_targets,
            },
        }

    if all(after_results[test_id] == "passed" for test_id in target_test_ids):
        return {
            "action": "stop",
            "reason": "targets_passed",
            "details": target_test_ids,
        }

    if current_round >= max_rounds:
        return {
            "action": "stop",
            "reason": "round_limit_reached",
            "details": {"current_round": current_round},
        }

    return {
        "action": "retry",
        "reason": "targets_still_failing",
        "details": [
            test_id
            for test_id in target_test_ids
            if after_results[test_id] == "failed"
        ],
    }

if __name__ == "__main__":
    target = "test_discount"
    existing_failure = "test_average"
    normal_test = "test_no_discount"

    baseline = {
        target: "failed",
        existing_failure: "failed",
        normal_test: "passed",
    }

    cases = [
        (
            "目标通过",
            {
                target: "passed",
                existing_failure: "failed",
                normal_test: "passed",
            },
            1,
        ),
        (
            "目标仍失败",
            {
                target: "failed",
                existing_failure: "failed",
                normal_test: "passed",
            },
            1,
        ),
        (
            "出现新增失败",
            {
                target: "passed",
                existing_failure: "failed",
                normal_test: "failed",
            },
            1,
        ),
        (
            "目标测试被跳过",
            {
                target: "skipped",
                existing_failure: "failed",
                normal_test: "passed",
            },
            1,
        ),
        (
            "达到最大轮数",
            {
                target: "failed",
                existing_failure: "failed",
                normal_test: "passed",
            },
            3,
        ),
    ]
    for name, after, current_round in cases:
        decision = decide_next_step(
            target_test_ids=[target],
            baseline_results=baseline,
            after_results=after,
            current_round=current_round,
            max_rounds=3,
        )

        print(f"{name}: {decision}")
