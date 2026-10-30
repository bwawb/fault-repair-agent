def calculate_average(numbers: list[float]) -> float:
    """
    计算一组数字的平均值。

    当列表为空时，抛出 ValueError。
    """
    return sum(numbers) / len(numbers)