# app/services/onboarding.py
# Getting a NEW user started (Intelligence Design §G). The user's estimates are
# stored as ordinary transactions flagged is_estimate=1, so the whole existing
# pipeline works unchanged; once real spending is recorded the forecast switches
# to it automatically and the answers stop saying "based on what you entered".

from datetime import date, timedelta

from app.config import validate_category, validate_income_source
from app.database import crud
from app.ml.prediction import data_state
from app.preferences.user_preferences import seed_default_preferences


def create_user(name, email=None):
    uid = crud.add_user(name, email)
    seed_default_preferences(uid)
    return uid


def save_profile(user_id, monthly_income, category_estimates=None, current_savings=0.0,
                 income_source="Salary", goal=None):
    """Store what a new user tells us.

    category_estimates: {"Food": 1500, "Bills": 800, ...} typical MONTHLY amounts
                        (main categories; "I don't know" can simply be left out)
    goal:               {"name": "Laptop", "target_amount": 5000, "months": 4}
    """
    if crud.get_user(user_id) is None:
        raise ValueError(f"user {user_id} does not exist")
    if monthly_income is None or float(monthly_income) <= 0:
        raise ValueError("monthly_income must be greater than 0")
    source = validate_income_source(income_source)
    estimates = {c: float(v) for c, v in (category_estimates or {}).items() if v not in (None, "")}
    for category, amount in estimates.items():
        validate_category(category)
        if category == "Income":
            raise ValueError("income goes in monthly_income, not in the category estimates")
        if amount < 0:
            raise ValueError(f"{category}: amounts cannot be negative")

    month_start = date.today().replace(day=1).isoformat()
    crud.add_income(user_id, float(monthly_income), source, month_start)

    crud.delete_estimates(user_id)
    for category, amount in estimates.items():
        if amount > 0:
            crud.add_transaction(user_id, f"Monthly estimate: {category}", amount, category,
                                 month_start, is_estimate=True)

    crud.set_current_savings(user_id, float(current_savings or 0))

    goal_id = None
    if goal and goal.get("name") and goal.get("target_amount"):
        months = int(goal.get("months") or 6)
        target_date = (date.today() + timedelta(days=30 * months)).isoformat()
        goal_id = crud.add_goal(user_id, goal["name"], float(goal["target_amount"]),
                                float(goal.get("current_amount") or 0), target_date)

    state, months = data_state(user_id)
    return {
        "user_id": user_id,
        "monthly_income": float(monthly_income),
        "estimated_monthly_spending": round(sum(estimates.values()), 2),
        "current_savings": float(current_savings or 0),
        "goal_id": goal_id,
        "data_state": state,
        "note": "advice will say 'based on what you entered' until real spending is recorded",
    }
