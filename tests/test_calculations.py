# tests/test_calculations.py
from app.finance.calculations import (calculate_total_expenses, calculate_category_spending,
                                       calculate_savings, calculate_savings_rate)
TXNS = [{"amount": 1000, "category": "Food"}, {"amount": 500, "category": "Transport"},
        {"amount": 200, "category": "Food"}]
def test_total():
    assert calculate_total_expenses(TXNS) == 1700
def test_category():
    assert calculate_category_spending(TXNS) == {"Food": 1200, "Transport": 500}
def test_savings():
    assert calculate_savings(6000, 3200) == 2800
def test_savings_rate():
    assert calculate_savings_rate(6000, 2800) == 46.67
def test_savings_rate_zero_income():
    assert calculate_savings_rate(0, 0) == 0.0