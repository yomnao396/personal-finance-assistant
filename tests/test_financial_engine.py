import os
import pytest

from app.database.schema import create_tables
from app.database.crud import (
    add_user,
    add_income,
    add_transaction,
    add_goal,
)

from app.engines.financial_engine import analyze_finances


@pytest.fixture(autouse=True)
def setup_database():
    """Create a fresh database before each test."""
    if os.path.exists("personal_finance.db"):
        os.remove("personal_finance.db")

    create_tables()

    yield

    if os.path.exists("personal_finance.db"):
        os.remove("personal_finance.db")


def test_analyze_finances_basic():
    """Test the main financial analysis result."""
    user_id = add_user(
        "FinancialUser",
        "financial@example.com",
    )

    add_income(
        user_id,
        6000.0,
        "Salary",
        "2026-08-01",
    )

    add_transaction(
        user_id,
        "Groceries",
        1200.0,
        "Food",
        "2026-08-10",
    )

    add_transaction(
        user_id,
        "Transport",
        500.0,
        "Transport",
        "2026-08-15",
    )

    result = analyze_finances(user_id)

    assert isinstance(result, dict)

    assert result["income"] == 6000.0
    assert result["expenses"] == 1700.0
    assert result["savings"] == 4300.0

    assert "savings_rate" in result
    assert "categories" in result
    assert "recurring_expenses" in result
    assert "prediction" in result


def test_current_month_filtering():
    """Test that expenses are calculated for the current month."""
    user_id = add_user(
        "MonthUser",
        "month@example.com",
    )

    add_income(
        user_id,
        6000.0,
        "Salary",
        "2026-08-01",
    )

    add_transaction(
        user_id,
        "Old Expense",
        2000.0,
        "Food",
        "2026-07-10",
    )

    add_transaction(
        user_id,
        "Current Expense",
        1000.0,
        "Food",
        "2026-08-10",
    )

    result = analyze_finances(user_id)

    assert result["current_month"] == "2026-08"
    assert result["expenses"] == 1000.0
    assert result["savings"] == 5000.0


def test_analysis_blocks_exist():
    """Test that all analysis blocks are returned."""
    user_id = add_user(
        "AnalysisUser",
        "analysis@example.com",
    )

    add_income(
        user_id,
        6000.0,
        "Salary",
        "2026-08-01",
    )

    add_transaction(
        user_id,
        "Food",
        1800.0,
        "Food",
        "2026-08-10",
    )

    result = analyze_finances(user_id)

    assert "category_analysis" in result
    assert "income_analysis" in result
    assert "spending_patterns" in result
    assert "savings_analysis" in result
    assert "goal_analysis" in result
    assert "goals" in result

    assert isinstance(result["category_analysis"], dict)
    assert isinstance(result["income_analysis"], dict)
    assert isinstance(result["spending_patterns"], dict)
    assert isinstance(result["savings_analysis"], dict)


def test_savings_analysis():
    """Test the savings analysis block."""
    user_id = add_user(
        "SavingsUser",
        "savings@example.com",
    )

    add_income(
        user_id,
        6000.0,
        "Salary",
        "2026-08-01",
    )

    add_transaction(
        user_id,
        "Food",
        1800.0,
        "Food",
        "2026-08-10",
    )

    add_transaction(
        user_id,
        "Transport",
        400.0,
        "Transport",
        "2026-08-15",
    )

    result = analyze_finances(user_id)

    savings_analysis = result["savings_analysis"]

    assert savings_analysis["monthly_income"] == 6000.0
    assert savings_analysis["monthly_expenses"] == 2200.0
    assert savings_analysis["monthly_saving"] == 3800.0
    assert savings_analysis["is_deficit"] is False


def test_goal_analysis():
    """Test that the primary financial goal is included in the analysis."""
    user_id = add_user(
        "GoalUser",
        "goal@example.com",
    )

    add_income(
        user_id,
        6000.0,
        "Salary",
        "2026-08-01",
    )

    add_transaction(
        user_id,
        "Food",
        1800.0,
        "Food",
        "2026-08-10",
    )

    add_goal(
        user_id,
        "Laptop",
        19000.0,
        3000.0,
        "2026-12-01",
    )

    result = analyze_finances(
        user_id,
        reference_date="2026-08-20",
    )

    assert result["goal_analysis"] is not None
    assert result["goals"] is not None
    assert len(result["goals"]) == 1
    assert result["goals"][0]["name"] == "Laptop"