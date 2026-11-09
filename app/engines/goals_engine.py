# app/engines/goals_engine.py
# Deterministic logic for financial goals (no ML, no LLM).
# Goal calculations also provide the remaining months, required monthly
# savings, and saving gap used by feature engineering and recommendations.

import math
from datetime import date

from app.database.crud import add_goal, get_goal, get_goals


DEFAULT_GOAL_HORIZON_MONTHS = 6


def calculate_goal_progress(goal):
    # How far the user is toward the goal.
    target = goal["target_amount"]
    current = goal["current_amount"]

    if target <= 0:
        return {
            "error": "target_amount must be greater than 0"
        }

    remaining = round(
        max(target - current, 0),
        2
    )

    percent = round(
        min(current / target * 100, 100),
        2
    )

    reached = current >= target

    return {
        "target": target,
        "current": current,
        "remaining": remaining,
        "percent_complete": percent,
        "reached": reached
    }


def calculate_goal_time(goal, monthly_saving):
    # How many months to reach the goal at a given monthly saving amount.
    progress = calculate_goal_progress(goal)

    if "error" in progress:
        return progress

    if progress["reached"]:
        return {
            "months_needed": 0,
            "message": "Goal already reached."
        }

    if monthly_saving <= 0:
        return {
            "error": (
                "monthly_saving must be greater than 0 "
                "to reach the goal."
            )
        }

    months = math.ceil(
        progress["remaining"] / monthly_saving
    )

    return {
        "remaining": progress["remaining"],
        "monthly_saving": monthly_saving,
        "months_needed": months
    }


def _parse_date(value):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM'; return a date or None."""

    if not value:
        return None

    text = str(value)[:10]

    try:
        if len(text) == 7:
            y, m = text.split("-")
            return date(int(y), int(m), 1)

        y, m, d = text.split("-")
        return date(int(y), int(m), int(d))

    except (ValueError, TypeError):
        return None


def months_between(start, end):
    """Whole months from start to end (at least 0)."""

    if start is None or end is None:
        return None

    return max(
        (end.year - start.year) * 12
        + (end.month - start.month),
        0
    )


def calculate_goal_gap(goal, monthly_saving, reference_date=None):
    """The core numbers the Recommendation Engine works from.

    required_monthly_saving = remaining / months_remaining
    saving_gap = required_monthly_saving - current monthly saving
    A gap <= 0 means the user is already on track.
    """

    progress = calculate_goal_progress(goal)

    if "error" in progress:
        return progress

    ref = _parse_date(reference_date) or date.today()

    target_date = _parse_date(
        goal.get("target_date")
    )

    months_remaining = months_between(
        ref,
        target_date
    )

    if months_remaining is None:
        months_remaining = DEFAULT_GOAL_HORIZON_MONTHS

        horizon_note = (
            f"no target date on this goal — using the default "
            f"{DEFAULT_GOAL_HORIZON_MONTHS}-month horizon"
        )

    elif months_remaining == 0:
        months_remaining = 1

        horizon_note = (
            "the target date is this month or has passed — "
            "treated as 1 month left"
        )

    else:
        horizon_note = (
            f"{months_remaining} month(s) left until "
            f"{goal.get('target_date')}"
        )

    remaining = progress["remaining"]

    required = (
        round(
            remaining / months_remaining,
            2
        )
        if months_remaining
        else remaining
    )

    gap = round(
        required - float(monthly_saving),
        2
    )

    return {
        "goal_name": goal.get("name"),
        "target_amount": round(
            float(goal["target_amount"]),
            2
        ),
        "current_amount": round(
            float(goal["current_amount"]),
            2
        ),
        "remaining": remaining,
        "percent_complete": progress["percent_complete"],
        "reached": progress["reached"],
        "months_remaining": months_remaining,
        "required_monthly_saving": required,
        "current_monthly_saving": round(
            float(monthly_saving),
            2
        ),
        "saving_gap": gap,
        "on_track": gap <= 0,
        "horizon_note": horizon_note,
    }
   