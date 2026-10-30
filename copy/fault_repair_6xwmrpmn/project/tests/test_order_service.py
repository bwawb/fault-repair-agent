from order_service import create_order


def test_create_order_without_discount():
    """没有折扣时，最终价格应等于原价。"""
    result = create_order("book", 200, 0)

    assert result["final_price"] == 200


def test_create_order_with_discount():
    """原价 200，折扣 20% 时，最终价格应为 160。"""
    result = create_order("book", 200, 20)

    assert result["final_price"] == 160