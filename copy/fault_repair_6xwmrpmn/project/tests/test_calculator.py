import pytest
from calculator import calculate_average

def test_calculate_average_with_numbers():
    """输入正常值，应该返回正确的平均值"""
    result = calculate_average([10, 20, 30])

    assert result == 20


def test_calculate_average_with_empty_list():
    """输入空列表时，应该抛出ValueError"""
    with pytest.raises(ValueError):
        calculate_average([])