# The months are relative to today, so the demonstration numbers are the same
# whenever the project is run.

from datetime import date

from app.database.schema import create_tables
from app.database.crud import (
    add_user,
    add_income,
    add_transaction,
    add_recurring_expense,
    add_goal,
)
from app.preferences.user_preferences import seed_default_preferences


def _add_months(d, months):
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _month(offset):
    d = _add_months(date.today().replace(day=1), offset)
    return f"{d.year:04d}-{d.month:02d}"


# month offset 0 = this month, -1 = last month, -2 = two months ago
SPENDING = {
    -2: [
        ("Breakfast at home", 100, "Food", "Breakfast"),
        ("Work lunches", 300, "Food", "Lunch"),
        ("Dinner groceries", 200, "Food", "Dinner"),
        ("Restaurant evenings", 500, "Food", "Restaurant"),
        ("Fast food", 300, "Food", "Fast Food"),
        ("Bus and taxi", 300, "Transport", None),
        ("Electricity bill", 150, "Bills", "Electricity"),
        ("Water bill", 50, "Bills", "Water"),
        ("Internet bill", 100, "Bills", "Internet"),
        ("Gas and cleaning", 100, "Utilities", None),
        ("Cinema", 100, "Entertainment", "Movie"),
        ("Clothes", 200, "Shopping", "Clothes"),
        ("Online course", 100, "Education", None),
        ("Pharmacy", 50, "Healthcare", None),
    ],
    -1: [
        ("Breakfast at home", 100, "Food", "Breakfast"),
        ("Work lunches", 300, "Food", "Lunch"),
        ("Dinner groceries", 200, "Food", "Dinner"),
        ("Restaurant evenings", 650, "Food", "Restaurant"),
        ("Fast food", 350, "Food", "Fast Food"),
        ("Bus and taxi", 300, "Transport", None),
        ("Electricity bill", 150, "Bills", "Electricity"),
        ("Water bill", 50, "Bills", "Water"),
        ("Internet bill", 100, "Bills", "Internet"),
        ("Gas and cleaning", 100, "Utilities", None),
        ("Cinema", 120, "Entertainment", "Movie"),
        ("Clothes", 300, "Shopping", "Clothes"),
        ("Online course", 100, "Education", None),
        ("Pharmacy", 50, "Healthcare", None),
    ],
    0: [
        ("Breakfast at home", 100, "Food", "Breakfast"),
        ("Work lunches", 300, "Food", "Lunch"),
        ("Dinner groceries", 200, "Food", "Dinner"),
        ("Restaurant evenings", 800, "Food", "Restaurant"),
        ("Fast food", 400, "Food", "Fast Food"),
        ("Bus and taxi", 300, "Transport", None),
        ("Electricity bill", 150, "Bills", "Electricity"),
        ("Water bill", 50, "Bills", "Water"),
        ("Internet bill", 100, "Bills", "Internet"),
        ("Gas and cleaning", 100, "Utilities", None),
        ("Cinema", 150, "Entertainment", "Movie"),
        ("Clothes", 300, "Shopping", "Clothes"),
        ("Headphones", 100, "Shopping", "Electronics"),
        ("Online course", 100, "Education", None),
        ("Pharmacy", 50, "Healthcare", None),
    ],
}

INCOME = [
    ("Salary", 5000),
    ("Freelancer", 800),
    ("From Dad", 200),
]


def seed():
    create_tables()

    uid = add_user(
        "Test User",
        "test@example.com",
    )

    for offset in (-2, -1, 0):
        month = _month(offset)

        for source, amount in INCOME:
            add_income(
                uid,
                amount,
                source,
                f"{month}-01",
            )

        for description, amount, category, subcategory in SPENDING[offset]:
            add_transaction(
                uid,
                description,
                amount,
                category,
                f"{month}-10",
                subcategory,
            )

    add_recurring_expense(
        uid,
        "Internet Bill",
        100,
        "Bills",
        "monthly",
        "Internet",
    )

    add_recurring_expense(
        uid,
        "Gas and cleaning",
        100,
        "Utilities",
        "monthly",
    )

    # A goal 4 months away that needs 4000/month while the user saves 2800:
    # a real, non-trivial monthly saving gap of 1200.
    target_month = _add_months(
        date.today().replace(day=1),
        4,
    )

    add_goal(
        uid,
        "Laptop and course",
        19000,
        3000,
        target_month.isoformat(),
    )

    # A second, undated goal so the goal priority board has something to order.
    # Undated goals never outrank a dated one unless the user drags them up.
    add_goal(
        uid,
        "Emergency fund",
        10000,
        2500,
    )

    n_prefs = seed_default_preferences(uid)

    print(f"Seeded sample user id = {uid}")
    print(f"  months: {_month(-2)}, {_month(-1)}, {_month(0)}")
    print(
        f"  income: {sum(a for _, a in INCOME)} per month from "
        f"{', '.join(s for s, _ in INCOME)}"
    )
    print(
        f"  goal:   Laptop and course 19000 "
        f"(saved 3000) by {target_month.isoformat()}"
    )
    print(f"  {n_prefs} essential/flexible preferences written")

    return uid


if __name__ == "__main__":
    seed()