# app/ml/prediction.py
# Next-month SPENDING FORECAST — the forecasting component learns the
# user's own spending pattern and predicts the next month WITH its uncertainty.
#
# Two outputs:
#   forecast_spending(user_id) -> total next month as mu (expected) + sigma (spread);
#                                 this drives the goal-probability assessment.
#   predict_spending(user_id)  -> the same idea per category, for the dashboard.
#
# Three data states (Intelligence Design §H), switched automatically:
#   "entered" — no real history yet: use what the user entered at onboarding,
#               sigma = 15% of it.
#   "recent"  — 1–2 months of real spending: their average, sigma = 15%.
#   "pattern" — 3+ months: a LinearRegression fitted on their own monthly totals
#               (re-fitted on every request — milliseconds), sigma = the
#               regression's prediction standard error for the next month
#               (never below 5% of the forecast).
# A forecast is clipped to [0, MAX_GROWTH x the highest month] so one odd month
# can never produce an absurd value.

import numpy as np
from scipy.stats import t as student_t
from sklearn.linear_model import LinearRegression

from app.database.crud import get_expense_transactions, get_income
from app.finance.calculations import monthly_totals, monthly_totals_by_category

LOOKBACK_MONTHS = 12          # history used for the total forecast
CATEGORY_LOOKBACK_MONTHS = 6  # history used per category
MAX_GROWTH = 1.5
COLD_START_SIGMA = 0.15       # 15% uncertainty while history is short
MIN_SIGMA = 0.05              # never claim to be more certain than +/-5%
MIN_MONTHS_FOR_PATTERN = 3

STATE_NOTES = {
    "entered": "based on what you entered (no spending history yet)",
    "recent": "based on your recent spending ({n} month(s) — limited history)",
    "pattern": "based on your spending pattern over {n} months",
}


def _next_month(month):
    year, mon = (int(x) for x in month.split("-"))
    year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return f"{year:04d}-{mon:02d}"


def _month_range(first, last):
    """Every 'YYYY-MM' from first to last, inclusive."""
    months = [first]
    while months[-1] != last:
        months.append(_next_month(months[-1]))
    return months


def _fit(values):
    """LinearRegression on month index -> (one-step forecast, prediction std).

    The prediction std is the regression's standard error for a NEW month:
    s x sqrt(1 + 1/n + (x0 - mean(x))^2 / sum((x - mean(x))^2)), where s is the
    residual spread, widened by a Student-t factor for n - 2 degrees of freedom.
    It is wider than s alone because the fitted line is itself uncertain —
    essential with only a few months of history (checked by
    scripts/validate_forecast.py)."""
    n = len(values)
    x = np.arange(1, n + 1, dtype=float)
    y = np.asarray(values, dtype=float)
    model = LinearRegression().fit(x.reshape(-1, 1), y)
    x0 = n + 1
    forecast = float(model.predict([[x0]])[0])
    residuals = y - model.predict(x.reshape(-1, 1))
    dof = max(n - 2, 1)                           # two fitted parameters
    s = float(np.sqrt((residuals ** 2).sum() / dof))
    leverage = 1 + 1 / n + (x0 - x.mean()) ** 2 / ((x - x.mean()) ** 2).sum()
    # Student-t correction: with few months s itself is uncertain, so widen the
    # spread until a normal +/-1 sigma band has the t-distribution's 68% coverage.
    t_factor = float(student_t.ppf(0.8413, dof))
    return forecast, s * float(np.sqrt(leverage)) * t_factor


def forecast_series(values):
    """One-step-ahead linear-trend forecast for a list of monthly values."""
    n = len(values)
    if n == 0:
        return 0.0
    if n == 1:
        return round(float(values[0]), 2)
    forecast, _ = _fit(values)
    forecast = min(max(forecast, 0.0), max(values) * MAX_GROWTH)
    return round(forecast, 2)


def _split(user_id):
    """(real expense transactions, onboarding estimates)."""
    txns = get_expense_transactions(user_id)
    real = [t for t in txns if not t.get("is_estimate")]
    estimates = [t for t in txns if t.get("is_estimate")]
    return real, estimates


def data_state(user_id):
    """'none' | 'entered' | 'recent' | 'pattern' and the months of real history."""
    real, estimates = _split(user_id)
    months = len(monthly_totals(real))
    if months >= MIN_MONTHS_FOR_PATTERN:
        return "pattern", months
    if months:
        return "recent", months
    return ("entered", 0) if estimates else ("none", 0)


def forecast_spending(user_id):
    """Total spending next month as mu +/- sigma, with an honest label."""
    real, estimates = _split(user_id)
    totals = monthly_totals(real)

    if not totals:
        entered = monthly_totals(estimates)
        if not entered:
            return {"error": "no spending recorded yet — add expenses or complete onboarding"}
        mu = list(entered.values())[-1]
        return _forecast(mu, mu * COLD_START_SIGMA, "entered", 0, "the monthly amounts you entered")

    months = _month_range(min(totals), max(totals))[-LOOKBACK_MONTHS:]
    return forecast_from_values([totals.get(m, 0.0) for m in months])


def forecast_from_values(values):
    """The forecast itself, on a plain list of monthly totals (oldest first).
    Used by forecast_spending and by scripts/validate_forecast.py."""
    if len(values) < MIN_MONTHS_FOR_PATTERN:
        mu = float(np.mean(values))
        return _forecast(mu, mu * COLD_START_SIGMA, "recent", len(values),
                         "average of your monthly spending")

    mu, spread = _fit(values)
    mu = min(max(mu, 0.0), max(values) * MAX_GROWTH)
    return _forecast(mu, max(spread, mu * MIN_SIGMA), "pattern", len(values),
                     "linear regression on your monthly totals; sigma = prediction standard error")


def _forecast(mu, sigma, state, months, method):
    return {
        "mu": round(mu, 2),
        "sigma": round(sigma, 2),
        "state": state,
        "months_used": months,
        "based_on": STATE_NOTES[state].format(n=months),
        "method": method,
    }


def predict_spending(user_id):
    """Forecast next month's spending per category for one user."""
    real, estimates = _split(user_id)
    by_category = monthly_totals_by_category(real or estimates)

    all_months = sorted({m for series in by_category.values() for m in series})
    if not all_months:
        return {
            "error": "no dated expense transactions yet — add some spending "
                     "before a forecast can be made"
        }

    months = _month_range(all_months[0], all_months[-1])[-CATEGORY_LOOKBACK_MONTHS:]
    next_month = _next_month(months[-1])

    categories = {}
    for category, series in by_category.items():
        values = [series.get(m, 0.0) for m in months]
        if not any(values):
            continue
        predicted = forecast_series(values)
        last = values[-1]
        categories[category] = {
            "history": dict(zip(months, values)),
            "last_month": round(last, 2),
            "predicted": predicted,
            "change": round(predicted - last, 2),
            "change_percent": round((predicted - last) / last * 100, 2) if last else None,
        }

    total_predicted = round(sum(c["predicted"] for c in categories.values()), 2)
    total_last = round(sum(c["last_month"] for c in categories.values()), 2)
    income = round(float(get_income(user_id)), 2)
    state, _ = data_state(user_id)

    return {
        "month": next_month,
        "based_on_months": months,
        "months_used": len(months),
        "data_state": state,
        "total_predicted": total_predicted,
        "total_last_month": total_last,
        "total_change": round(total_predicted - total_last, 2),
        "expected_income": income,
        "expected_saving": round(income - total_predicted, 2) if income else None,
        "by_category": dict(
            sorted(categories.items(), key=lambda kv: -kv[1]["predicted"])
        ),
        "method": (
            f"linear regression per category over the last {len(months)} month(s), "
            f"clipped to 0..{MAX_GROWTH}x the highest month"
            if len(months) > 1
            else "only one month of history: the forecast repeats that month"
        ),
    }
