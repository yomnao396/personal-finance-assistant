# app/ml/goal_assessment.py
# "Can I afford it?" — a forward-looking, probabilistic goal assessment.
#
#   forecast spending (mu, sigma)        <- learned from the user's own data (prediction.py)
#   expected saving  = income - mu       <- per month
#   projected        = saved + expected saving x months
#   spread           = sigma x sqrt(months)   (monthly uncertainty adds up)
#   probability      = P(projected >= target) under a normal distribution
#   label            = Likely (>= 70%) / Borderline (>= 40%) / Unlikely
#   action           = extra saving per month, or extra months, needed for 80% confidence
#
# The learning is in the forecast; the probability is a principled normal
# calculation on it (not a classifier that re-learns its own inputs).

import math
from datetime import date

from app.database.crud import get_goals, get_income, get_user
from app.engines.goals_engine import _parse_date, months_between
from app.ml.prediction import forecast_spending

LIKELY = 0.70
BORDERLINE = 0.40
CONFIDENT = 0.80
DEFAULT_MONTHS = 6
MAX_MONTHS_SEARCH = 240


def _phi(z):
    """Standard normal cumulative distribution."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _z_for(p):
    """Inverse of _phi by bisection (no scipy needed)."""
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def probability_of_reaching(target, saved, monthly_saving, sigma, months):
    projected = saved + monthly_saving * months
    spread = sigma * math.sqrt(months)
    if spread <= 0:
        return 1.0 if projected >= target else 0.0
    return 1 - _phi((target - projected) / spread)


def label_for(probability):
    if probability >= LIKELY:
        return "Likely"
    if probability >= BORDERLINE:
        return "Borderline"
    return "Unlikely"


def _months_until(target_date):
    months = months_between(date.today(), _parse_date(target_date))
    return max(months, 1) if months is not None else None


def _find_goal(user_id, goal_name):
    """(goal, error). A loosely written name is matched; an unknown or ambiguous
    name is reported, never replaced by another goal."""
    if goal_name:
        from app.services.goal_context import find_goal
        return find_goal(user_id, goal_name)
    from app.engines.financial_engine import _pick_primary_goal
    goal = _pick_primary_goal(get_goals(user_id))
    if goal is None:
        return None, {"error": "the user has no saved goals — give a target amount and "
                               "months, or add a goal on the Goals page"}
    return goal, None


def assess_goal(user_id, target_amount=None, months=None, goal_name=None,
                current_savings=None, extra_monthly_saving=0.0,
                income_change_pct=0.0, spending_cut=0.0):
    """Probability of reaching a savings target in `months`.

    Either pass target_amount (+ months) for a hypothetical purchase ("a 5,000
    laptop in 4 months"), or goal_name / nothing to assess a stored goal.
    The last three arguments are what-if overrides used by scenario.py.
    """
    goal, deadline = None, None
    if target_amount is None:
        goal, error = _find_goal(user_id, goal_name)
        if error:
            return error
        target_amount = goal["target_amount"]
        saved = goal["current_amount"] if current_savings is None else current_savings
        if saved >= target_amount:
            return _reached(goal, saved)
        deadline = _deadline(goal.get("target_date"))
        months = months or _months_until(goal.get("target_date")) or DEFAULT_MONTHS
    else:
        user = get_user(user_id) or {}
        saved = (user.get("current_savings") or 0.0) if current_savings is None else current_savings
        months = months or DEFAULT_MONTHS

    try:
        target_amount, months = float(target_amount), int(months)
        saved = float(saved)
    except (TypeError, ValueError):
        return {"error": "target_amount and months must be numbers"}
    if target_amount <= 0:
        return {"error": "target_amount must be greater than 0"}
    if months <= 0:
        return {"error": "months must be at least 1"}

    forecast = forecast_spending(user_id)
    if "error" in forecast:
        return forecast
    income = float(get_income(user_id)) * (1 + income_change_pct / 100.0)
    if income <= 0:
        return {"error": "no income recorded — add income before assessing a goal"}

    mu = max(forecast["mu"] - spending_cut, 0.0)
    sigma = forecast["sigma"]
    monthly_saving = income - mu + extra_monthly_saving
    projected = saved + monthly_saving * months
    spread = sigma * math.sqrt(months)
    probability = probability_of_reaching(target_amount, saved, monthly_saving, sigma, months)
    label = label_for(probability)

    extra_needed, extra_months, action = _action(
        target_amount, saved, monthly_saving, sigma, months, probability)

    summary = _summary(label, probability, target_amount, months, monthly_saving,
                       projected, extra_needed, extra_months)
    remaining = max(target_amount - saved, 0.0)
    finish = _finish(remaining, monthly_saving)
    if goal:
        summary = _goal_summary(goal, deadline, months, remaining, monthly_saving, finish, summary)

    return {
        "summary": summary,
        "goal_name": goal["name"] if goal else None,
        "target_amount": round(target_amount, 2),
        "current_savings": round(saved, 2),
        "months": months,
        "monthly_income": round(income, 2),
        "predicted_monthly_spending": round(mu, 2),
        "spending_uncertainty": round(sigma, 2),
        "expected_monthly_saving": round(monthly_saving, 2),
        "projected_savings": round(projected, 2),
        "projected_spread": round(spread, 2),
        "probability": round(probability, 3),
        "label": label,
        "remaining_amount": round(remaining, 2),
        "required_monthly_saving": round(remaining / months, 2),
        "monthly_shortfall": round(max(remaining / months - monthly_saving, 0.0), 2),
        "monthly_surplus": round(max(monthly_saving - remaining / months, 0.0), 2),
        "months_to_finish_at_this_rate": finish["months"],
        "estimated_completion": finish["date"],
        "deadline": deadline,
        "extra_monthly_saving_needed": extra_needed,
        "extra_months_needed": extra_months,
        "action": action,
        "data_state": forecast["state"],
        "history_months": forecast["months_used"],
        "forecast_based_on": forecast["based_on"],
        "method": "normal approximation: P(saved + saving x months >= target), "
                  "spread = sigma x sqrt(months)",
    }


def _deadline(target_date):
    """{"date", "status"} for a stored goal: upcoming, passed or missing."""
    when = _parse_date(target_date)
    if when is None:
        return {"date": None, "status": "missing"}
    status = "passed" if when < date.today() else "upcoming"
    return {"date": when.isoformat(), "status": status}


def _finish(remaining, monthly_saving):
    """When the goal is reached at the expected saving rate (None if never)."""
    from app.services.goal_context import months_to_finish
    months = months_to_finish(remaining, monthly_saving)
    if months is None:
        return {"months": None, "date": None}
    today = date.today()
    total = today.year * 12 + today.month - 1 + months
    return {"months": months, "date": f"{total // 12}-{total % 12 + 1:02d}"}


def _reached(goal, saved):
    return {
        "summary": (f"Already reached: {saved:,.0f} SDG saved of the "
                    f"{goal['target_amount']:,.0f} SDG needed for '{goal['name']}'."),
        "goal_name": goal["name"],
        "target_amount": round(float(goal["target_amount"]), 2),
        "current_savings": round(float(saved), 2),
        "remaining_amount": 0.0,
        "probability": 1.0,
        "label": "Reached",
        "extra_monthly_saving_needed": 0.0,
        "extra_months_needed": 0,
        "action": "This goal is fully funded — no more saving is needed for it.",
    }


def _goal_summary(goal, deadline, months, remaining, monthly_saving, finish, summary):
    """The verdict first, then the stored goal's own facts."""
    head = (f"Goal '{goal['name']}': {remaining:,.0f} SDG still needed "
            f"({goal['current_amount']:,.0f} of {goal['target_amount']:,.0f} SDG saved). ")
    if deadline["status"] == "missing":
        head += f"No deadline is saved, so it is assessed over {months} month(s). "
    elif deadline["status"] == "passed":
        head += (f"The deadline {deadline['date']} has already passed, so it is assessed "
                 f"over {months} month(s) from now. ")
    else:
        head += (f"Deadline {deadline['date']} ({months} month(s) left): it needs "
                 f"{remaining / months:,.0f} SDG a month. ")
    if monthly_saving <= 0:
        tail = " At the current spending nothing is left to save each month."
    else:
        tail = (f" At {monthly_saving:,.0f} SDG a month it would be reached in about "
                f"{finish['months']} month(s), around {finish['date']}.")
    return summary + " " + head.strip() + tail


def _summary(label, probability, target, months, monthly_saving, projected, extra, extra_months):
    """One plain sentence a language model can repeat without misreading the fields."""
    head = (f"{label} ({probability:.0%} chance): saving {monthly_saving:,.0f} SDG a month "
            f"gives about {projected:,.0f} SDG in {months} month(s), against {target:,.0f} SDG needed.")
    if extra == 0:
        return head + " No extra saving is needed."
    if extra_months is None:
        return head + f" To be confident, save about {extra:,.0f} SDG more each month."
    return head + (f" To be confident, save about {extra:,.0f} SDG more each month, "
                   f"or wait about {extra_months} more month(s).")


def _action(target, saved, monthly_saving, sigma, months, probability):
    """(extra saving per month, extra months, sentence) for CONFIDENT probability."""
    if probability >= CONFIDENT:
        return 0.0, 0, "You are on track — keep saving at your current rate."

    z = _z_for(CONFIDENT)
    needed_per_month = (target - saved + z * sigma * math.sqrt(months)) / months
    extra = max(round(needed_per_month - monthly_saving), 0)

    extra_months = None
    if monthly_saving > 0:
        for n in range(months + 1, months + MAX_MONTHS_SEARCH + 1):
            if probability_of_reaching(target, saved, monthly_saving, sigma, n) >= CONFIDENT:
                extra_months = n - months
                break

    if extra_months is None:
        sentence = (f"Save about {extra:,.0f} more per month to be confident — at your current "
                    f"spending a longer deadline alone will not get you there.")
    else:
        sentence = (f"Save about {extra:,.0f} more per month, or extend the goal by "
                    f"~{extra_months} month(s), to be confident.")
    return float(extra), extra_months, sentence
