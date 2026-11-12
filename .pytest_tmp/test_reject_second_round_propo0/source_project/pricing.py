def calculate_final_price(original_price, discount_percent):
    discount_rate = discount_percent // 100
    return original_price * (1 - discount_rate)
