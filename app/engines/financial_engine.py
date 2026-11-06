# app/engines/financial_engine.py

# The Financial Analysis Engine: pulls the user's data from the database,
# runs the pure calculation functions, and returns one structured dictionary.
#
# UPDATED:
# * the original top-level keys (income, expenses, savings, savings_rate,
#   categories, recurring_expenses, prediction) are unchanged in meaning for a
#   single-month database, so everything built on them keeps working;
# * income / expenses / savings are now explicitly CURRENT-MONTH figures, which
#   is what makes them comparable once the user has several months of history;
# * four new blocks were added: category_analysis, income_analysis,
#   spending_patterns, savings_analysis, plus goal_analysis (the saving gap).


from app.database.crud import (
    get_income,
    get_income_records,
    get_recurring_expenses,
    get_expense_transactions,
    get_goals,
)

from app.finance.calculations import (
    calculate_total_expenses,
    calculate_category_spending,
    calculate_savings,
    calculate_savings_rate,
)

from app.finance.analysis import (
    analyze_categories,
    analyze_income,
    analyze_spending_patterns,
)

from app.engines.goals_engine import calculate_goal_gap


def _pick_primary_goal(goals):
    """
    The goal we plan against: the unreached goal the user ranked first
    (drag-and-drop priority); if no goal is ranked, the unreached goal with the
    earliest target date, falling back to the one that needs the most money.
    """

    unreached = [
        g
        for g in goals
        if g["current_amount"] < g["target_amount"]
    ]

    pool = unreached or goals

    if not pool:
        return None

    ranked = [
        g
        for g in pool
        if g.get("priority") is not None
    ]

    if ranked:
        return min(
            ranked,
            key=lambda g: g["priority"]
        )

    dated = [
        g
        for g in pool
        if g.get("target_date")
    ]

    if dated:
        return sorted(
            dated,
            key=lambda g: g["target_date"]
        )[0]

    return sorted(
        pool,
        key=lambda g: g["target_amount"] - g["current_amount"],
        reverse=True,
    )[0]


def _current_month_window(transactions):
    """
    Split the history into (current month string, that month's transactions).
    Undated transactions are kept so a database without dates still works.
    """

    months = sorted(
        {
            (t.get("date") or "")[:7]
            for t in transactions
            if (t.get("date") or "")
        }
    )

    if not months:
        return None, transactions

    current = months[-1]

    window = [
        t
        for t in transactions
        if (t.get("date") or "")[:7] == current
    ]

    return current, window


def analyze_finances(user_id, reference_date=None):

    income = get_income(user_id)  # current monthly income

    all_transactions = get_expense_transactions(user_id)

    current_month, month_transactions = _current_month_window(
        all_transactions
    )

    income_records = get_income_records(user_id)

    total_expenses = calculate_total_expenses(
        month_transactions
    )

    savings = calculate_savings(
        income,
        total_expenses
    )

    savings_rate = calculate_savings_rate(
        income,
        savings
    )

    category_analysis = analyze_categories(
        month_transactions,
        total_income=income,
        history_transactions=all_transactions,
    )

    category_analysis["window"] = (
        current_month
        or "all transactions (no dates recorded)"
    )

    income_analysis = analyze_income(
        income_records
    )

    spending_patterns = analyze_spending_patterns(
        all_transactions
    )

    goals = get_goals(user_id)

    primary_goal = _pick_primary_goal(
        goals
    )

    goal_analysis = (
        calculate_goal_gap(
            primary_goal,
            savings,
            reference_date,
        )
        if primary_goal
        else None
    )

    result = {

        # ---- original keys, unchanged in meaning ----

        "income": round(income, 2),

        "expenses": total_expenses,

        "savings": savings,

        "savings_rate": savings_rate,

        "categories": calculate_category_spending(
            month_transactions
        ),

        "recurring_expenses": get_recurring_expenses(
            user_id
        ),

        "prediction": None,

        # ---- new analysis blocks ----

        "current_month": current_month,

        "category_analysis": category_analysis,

        "income_analysis": income_analysis,

        "spending_patterns": spending_patterns,

        "savings_analysis": {

            "monthly_income": round(income, 2),

            "monthly_expenses": total_expenses,

            "monthly_saving": savings,

            "saving_rate": savings_rate,

            "average_monthly_spending": (
                spending_patterns[
                    "average_monthly_spending"
                ]
            ),

            "is_deficit": savings < 0,
        },

        "goal_analysis": goal_analysis,

        "goals": goals,
    }

    # Prediction is optional; only add it if the ML model is available.

    try:

        from app.ml.prediction import predict_spending

        result["prediction"] = predict_spending(
            user_id
        )

    except Exception:

        result["prediction"] = None

    return result

