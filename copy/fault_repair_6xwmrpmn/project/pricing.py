def calculate_final_price(
    original_price: float,
    discount_percent: int,
) -> float:
    """
    根据折扣百分比计算最终价格。

    例如：原价 200，折扣 20%，最终价格为 160。
    """
    discount_rate = discount_percent / 100
    final_price = original_price * (1 - discount_rate)

    return final_price