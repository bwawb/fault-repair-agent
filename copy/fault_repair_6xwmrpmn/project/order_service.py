from pricing import calculate_final_price


def create_order(
    product_name: str,
    original_price: float,
    discount_percent: int,
) -> dict:
    """创建订单，并返回商品名称和最终价格。"""
    final_price = calculate_final_price(
        original_price,
        discount_percent,
    )

    return {
        "product_name": product_name,
        "final_price": final_price,
    }