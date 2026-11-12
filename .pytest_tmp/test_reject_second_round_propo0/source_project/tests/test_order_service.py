from pricing import calculate_final_price


def test_create_order_with_discount():
    assert calculate_final_price(200, 20) == 160


def test_create_order_without_discount():
    assert calculate_final_price(200, 0) == 200
