# app/engines/budget_engine.py
# Personalized Budget Engine. It builds a monthly budget from the user's income,
# their historical category spending, recurring expenses and savings goals.
# All arithmetic is done in Python. Order: recurring expenses, then the spending
# the user marked essential (never squeezed to fund a goal), then saving, then the
# flexible categories. Total <= income whenever income covers the essentials.
from app.database.crud import (get_income, get_transactions, get_recurring_expenses, get_goals)
from app.finance.calculations import calculate_category_spending
from app.engines.financial_engine import _pick_primary_goal
from app.engines.goals_engine import calculate_goal_gap
from app.preferences.user_preferences import is_essential, preference_map
DEFAULT_SAVINGS_RATE = 0.15   # if the user has no goal, save 15% of income by default
CATEGORY_CAP = 1.30           # never budget a category above 1.3x what they normally spend
def _goal_monthly_need(goals):
    # Monthly amount the main goal needs by its date: the same goal and figure the
    # dashboard and the plan show (an undated goal uses the 6-month default horizon).
    goal = _pick_primary_goal(goals)
    if not goal or goal["current_amount"] >= goal["target_amount"]:
        return 0
    return calculate_goal_gap(goal, 0)["required_monthly_saving"]
def _monthly_average(transactions):
    # Category spending per month on average, so a 3-month history is not read as one month.
    months = len({(t.get("date") or "")[:7] for t in transactions}) or 1
    return {c: v / months for c, v in calculate_category_spending(transactions).items()}
def _essential_average(user_id, transactions, recurring):
    # Monthly average of the spending marked essential, per category. A subcategory
    # with a recurring bill is left out: that bill is already in the fixed part.
    months = len({(t.get("date") or "")[:7] for t in transactions}) or 1
    prefs = preference_map(user_id)
    billed = {(r["category"], r.get("subcategory") or "") for r in recurring}
    out = {}
    for t in transactions:
        key = (t["category"], t.get("subcategory") or "")
        if key not in billed and is_essential(user_id, t["category"], t.get("subcategory"), prefs):
            out[t["category"]] = out.get(t["category"], 0) + t["amount"] / months
    return out
def calculate_budget(user_id):
    income = get_income(user_id)
    if income <= 0:
        return {"error": "No income recorded for this user."}
    transactions = get_transactions(user_id)
    history = _monthly_average(transactions)
    recurring = get_recurring_expenses(user_id)
    goals = get_goals(user_id)
    # 1) Fixed part: recurring expenses are covered first, then essential spending.
    fixed = {}
    for r in recurring:
        cat = r["category"] or "Bills"
        fixed[cat] = round(fixed.get(cat, 0) + r["amount"], 2)
    essential = _essential_average(user_id, transactions, recurring)
    for cat, amount in essential.items():
        fixed[cat] = round(fixed.get(cat, 0) + amount, 2)
    fixed_total = round(sum(fixed.values()), 2)
    # 2) Savings target from goals, else a default percentage, from what is left.
    savings_target = max(_goal_monthly_need(goals), income * DEFAULT_SAVINGS_RATE)
    savings_target = round(max(min(savings_target, income - fixed_total), 0), 2)
    # 3) Money left for the flexible (discretionary) categories.
    remaining = round(income - fixed_total - savings_target, 2)
    # 4) Split the remaining across categories in proportion to past flexible
    #    spending, capped at 1.3x its normal level so allocations stay realistic.
    #    A category keeps its covered part (e.g. Bills: Internet is recurring and
    #    Electricity is essential; only "Bills > Other" would be flexible).
    flexible_history = {c: round(v - fixed.get(c, 0), 2) for c, v in history.items()
                        if v - fixed.get(c, 0) > 0}
    hist_total = sum(flexible_history.values())
    budget = dict(fixed)
    if hist_total > 0 and remaining > 0:
        for cat, spent in flexible_history.items():
            share = spent / hist_total
            proposed = remaining * share
            capped = min(proposed, spent * CATEGORY_CAP)   # the realism cap
            budget[cat] = round(budget.get(cat, 0) + capped, 2)
    # 5) Buffer = anything left, so the total never exceeds income (a cent of
    #    rounding over is taken back from the largest flexible allocation).
    allocated = round(sum(budget.values()) + savings_target, 2)
    over = round(allocated - income, 2)
    if 0 < over < 1 and flexible_history:
        biggest = max(flexible_history, key=lambda c: budget.get(c, 0))
        budget[biggest] = round(budget[biggest] - over, 2)
        allocated = round(allocated - over, 2)
    buffer = round(max(income - allocated, 0), 2)
    budget["Savings"] = savings_target
    budget["Buffer"] = buffer
    budget["_total"] = round(sum(v for k, v in budget.items() if not k.startswith("_")), 2)
    budget["_income"] = round(income, 2)
    if fixed_total > income:
        budget["_essentials_exceed_income"] = round(fixed_total - income, 2)
    return budget