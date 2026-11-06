import os
import pytest

from app.database.schema import create_tables
from app.database.crud import (
    add_user,
    get_user,
    get_categories,
    get_category_tree,
    add_income,
    get_income,
    get_income_records,
    update_income,
    add_transaction,
    get_transactions,
    get_expense_transactions,
    update_transaction,
    delete_transaction,
    add_goal,
    get_goals,
    get_goal,
    add_recurring_expense,
    get_recurring_expenses,
    add_preference,
    get_preferences,
    get_preference,
    update_preference,
    delete_preference,
    delete_all_preferences,
)


@pytest.fixture(autouse=True)
def setup_database():
    """Create a fresh database before each test."""
    if os.path.exists("personal_finance.db"):
        os.remove("personal_finance.db")

    create_tables()

    yield

    if os.path.exists("personal_finance.db"):
        os.remove("personal_finance.db")


def test_database_categories():
    """Test that categories are seeded correctly."""
    categories = get_categories()

    assert len(categories) > 0

    main_categories = [c["main_category"] for c in categories]

    assert "Food" in main_categories
    assert "Income" in main_categories

    tree = get_category_tree()

    assert "Food" in tree
    assert "Income" in tree


def test_user_and_transaction_operations():
    """Test adding a user and storing a transaction."""
    user_id = add_user(
        "PytestUser",
        "pytest@example.com",
    )

    assert user_id is not None

    user = get_user(user_id)

    assert user is not None
    assert user["username"] == "PytestUser"

    transaction_id = add_transaction(
        user_id,
        "Coffee",
        20.0,
        "Food",
        "2026-08-10",
    )

    assert transaction_id is not None

    transactions = get_transactions(user_id)

    assert len(transactions) == 1
    assert transactions[0]["description"] == "Coffee"
    assert float(transactions[0]["amount"]) == 20.0
    assert transactions[0]["category"] == "Food"


def test_income_operations():
    """Test adding, reading, and updating income."""
    user_id = add_user(
        "IncomeUser",
        "income@example.com",
    )

    add_income(
        user_id,
        5000.0,
        "Salary",
        "2026-08-01",
    )

    add_income(
        user_id,
        1000.0,
        "Freelancer",
        "2026-08-15",
    )

    assert get_income(user_id) == 6000.0

    records = get_income_records(user_id)

    assert len(records) == 2

    update_income(user_id, 5500.0)

    records_after_update = get_income_records(user_id)

    assert len(records_after_update) == 2
    assert float(records_after_update[-1]["amount"]) == 5500.0


def test_transaction_update_delete_and_expense_filter():
    """Test transaction update, delete, and expense filtering."""
    user_id = add_user(
        "TransactionUser",
        "transaction@example.com",
    )

    transaction_id = add_transaction(
        user_id,
        "Lunch",
        100.0,
        "Food",
        "2026-08-10",
    )

    assert transaction_id is not None

    transactions = get_transactions(user_id)

    assert len(transactions) == 1

    updated = update_transaction(
        transaction_id,
        description="Dinner",
        amount=150.0,
        category="Food",
        date="2026-08-11",
    )

    assert updated is True

    transactions = get_transactions(user_id)

    assert transactions[0]["description"] == "Dinner"
    assert float(transactions[0]["amount"]) == 150.0
    assert transactions[0]["date"] == "2026-08-11"

    expense_transactions = get_expense_transactions(user_id)

    assert len(expense_transactions) == 1

    delete_transaction(transaction_id)

    assert len(get_transactions(user_id)) == 0


def test_goals_recurring_expenses_and_preferences():
    """Test goals, recurring expenses, and user preferences."""
    user_id = add_user(
        "FeatureUser",
        "feature@example.com",
    )

    goal_id = add_goal(
        user_id,
        "Laptop",
        19000.0,
        3000.0,
        "2026-12-01",
    )

    assert goal_id is not None

    goals = get_goals(user_id)

    assert len(goals) == 1
    assert goals[0]["name"] == "Laptop"
    assert float(goals[0]["target_amount"]) == 19000.0
    assert float(goals[0]["current_amount"]) == 3000.0

    goal = get_goal(goal_id)

    assert goal is not None
    assert goal["name"] == "Laptop"

    add_recurring_expense(
        user_id,
        "Internet",
        500.0,
        "Bills",
        "monthly",
    )

    recurring = get_recurring_expenses(user_id)

    assert len(recurring) == 1
    assert recurring[0]["description"] == "Internet"
    assert float(recurring[0]["amount"]) == 500.0
    assert recurring[0]["frequency"] == "monthly"

    preference_id = add_preference(
        user_id,
        "Food",
        None,
        True,
    )

    assert preference_id is not None

    preferences = get_preferences(user_id)

    assert len(preferences) == 1
    assert preferences[0]["main_category"] == "Food"
    assert preferences[0]["is_essential"] is True

    preference = get_preference(
        user_id,
        "Food",
        None,
    )

    assert preference is not None
    assert preference["is_essential"] is True

    updated = update_preference(
        user_id,
        "Food",
        None,
        False,
    )

    assert updated is True

    preference = get_preference(
        user_id,
        "Food",
        None,
    )

    assert preference["is_essential"] is False

    deleted = delete_preference(
        user_id,
        "Food",
        None,
    )

    assert deleted is True

    assert get_preferences(user_id) == []

    add_preference(
        user_id,
        "Food",
        None,
        True,
    )

    add_preference(
        user_id,
        "Transport",
        None,
        False,
    )

    deleted_count = delete_all_preferences(user_id)

    assert deleted_count == 2
    assert get_preferences(user_id) == []
