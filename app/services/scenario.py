# app/services/scenario.py
# What-if scenarios and multi-goal ranking — deterministic and explainable
# (Intelligence Design §I and §J). No new model: a scenario is the goal
# assessment re-run with one input changed, so the forecast and the probability
# genuinely recompute.

from datetime import date

from app.database.crud import get_goals, get_income
from app.engines.goals_engine import _parse_date, months_between
from app.ml.goal_assessment import assess_goal
from app.ml.prediction import forecast_spending
from app.preferences.user_preferences import is_essential

# Importance weights for multi-goal ranking: the only assumption, kept visible.
# Matched against words in the goal's name; the first match wins.
GOAL_IMPORTANCE = [
    (("emergency", "طوارئ"), 3.0),
    (("debt", "loan", "دين", "قرض"), 2.8),
    (("rent", "health", "medical", "إيجار", "علاج"), 2.6),
    (("education", "course", "school", "university", "tuition", "تعليم", "دراسة", "جامعة"), 2.2),
    (("laptop", "computer", "phone", "لابتوب", "حاسوب", "هاتف"), 1.8),
    (("car", "house", "home", "سيارة", "منزل", "بيت"), 1.6),
    (("trip", "travel", "vacation", "holiday", "سفر", "رحلة"), 1.0),
]
DEFAULT_IMPORTANCE = 1.5
UNDATED_MONTHS = 12          # a goal without a date is treated as a year away


def simulate_scenario(user_id, target_amount=None, months=None, goal_name=None,
                      cut_category=None, cut_amount=0.0, extra_saving=0.0,
                      income_change_pct=0.0, extra_months=0):
    """Baseline vs scenario for one goal. Each override is optional:
    cut_category + cut_amount  — spend less in one category each month
    extra_saving               — save this much more each month
    income_change_pct          — e.g. -10 for a 10% pay cut
    extra_months               — move the deadline later
    """
    baseline = assess_goal(user_id, target_amount, months, goal_name)
    if "error" in baseline or baseline["label"] == "Reached":
        return baseline     # nothing to simulate for a goal that is already funded

    warnings = []
    if cut_category and cut_amount:
        if is_essential(user_id, cut_category):
            warnings.append(f"{cut_category} is marked essential — the plan never cuts it, "
                            f"so this scenario is shown for information only")

    scenario = assess_goal(
        user_id, baseline["target_amount"], baseline["months"] + int(extra_months or 0),
        baseline["goal_name"], current_savings=baseline["current_savings"],
        extra_monthly_saving=float(extra_saving or 0),
        income_change_pct=float(income_change_pct or 0),
        spending_cut=float(cut_amount or 0) if cut_category else 0.0,
    )
    if "error" in scenario:
        return scenario

    changes = {k: v for k, v in {
        "cut_category": cut_category, "cut_amount": cut_amount if cut_category else 0,
        "extra_saving": extra_saving, "income_change_pct": income_change_pct,
        "extra_months": extra_months,
    }.items() if v}

    return {
        "summary": _scenario_summary(changes, baseline, scenario),
        "changes": changes,
        "baseline": baseline,
        "scenario": scenario,
        "probability_change": round(scenario["probability"] - baseline["probability"], 3),
        "label_change": f"{baseline['label']} -> {scenario['label']}",
        "warnings": warnings,
    }


def _scenario_summary(changes, baseline, scenario):
    """One sentence comparing now with the what-if, for the assistant to repeat."""
    what = []
    if changes.get("extra_saving"):
        what.append(f"saving {changes['extra_saving']:,.0f} SDG more each month")
    if changes.get("cut_category"):
        what.append(f"spending {changes['cut_amount']:,.0f} SDG less on {changes['cut_category']}")
    if changes.get("income_change_pct"):
        what.append(f"income changing by {changes['income_change_pct']:+.0f}%")
    if changes.get("extra_months"):
        what.append(f"{changes['extra_months']} more month(s)")
    goal = f" for '{baseline['goal_name']}'" if baseline.get("goal_name") else ""
    text = (f"What-if{goal} with {' and '.join(what) or 'no change'}: the chance goes from "
            f"{baseline['probability']:.0%} ({baseline['label']}) to "
            f"{scenario['probability']:.0%} ({scenario['label']}); you would save about "
            f"{scenario['expected_monthly_saving']:,.0f} SDG a month instead of "
            f"{baseline['expected_monthly_saving']:,.0f} SDG.")
    if scenario.get("estimated_completion"):
        text += (f" At that rate the goal is reached in about "
                 f"{scenario['months_to_finish_at_this_rate']} month(s), around "
                 f"{scenario['estimated_completion']}.")
    if scenario["extra_monthly_saving_needed"]:
        text += (f" To be confident, about {scenario['extra_monthly_saving_needed']:,.0f} SDG "
                 f"more a month would still be needed.")
    return text + " This is hypothetical: nothing saved was changed."


def importance_of(goal_name):
    name = (goal_name or "").lower()
    for words, weight in GOAL_IMPORTANCE:
        if any(w in name for w in words):
            return weight
    return DEFAULT_IMPORTANCE


def rank_goals(user_id):
    """Rank unreached goals by importance x urgency, with a feasibility check."""
    goals = [g for g in get_goals(user_id) if g["current_amount"] < g["target_amount"]]
    if not goals:
        return {"error": "no unreached goals to rank"}

    forecast = forecast_spending(user_id)
    capacity = None
    if "error" not in forecast:
        capacity = round(float(get_income(user_id)) - forecast["mu"], 2)

    ranked = []
    for g in goals:
        months = months_between(date.today(), _parse_date(g.get("target_date")))
        months = max(months, 1) if months is not None else UNDATED_MONTHS
        importance = importance_of(g["name"])
        urgency = round(1 / months, 4)
        remaining = round(g["target_amount"] - g["current_amount"], 2)
        assessment = assess_goal(user_id, goal_name=g["name"])
        ranked.append({
            "goal_id": g["id"],
            "goal_name": g["name"],
            "importance": importance,
            "months_left": months,
            "urgency": urgency,
            "priority_score": round(importance * urgency, 4),
            "remaining": remaining,
            "required_monthly_saving": round(remaining / months, 2),
            "probability": assessment.get("probability"),
            "label": assessment.get("label"),
        })

    ranked.sort(key=lambda r: -r["priority_score"])
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i

    total_required = round(sum(r["required_monthly_saving"] for r in ranked), 2)
    competing = capacity is not None and total_required > capacity
    return {
        "ranked_goals": ranked,
        "monthly_saving_capacity": capacity,
        "total_required_monthly_saving": total_required,
        "goals_compete_for_money": competing,
        "note": (
            f"together the goals need {total_required:,.0f} a month but about "
            f"{capacity:,.0f} is available — fund them in this order"
            if competing else "the monthly saving covers all goals at once"
        ) if capacity is not None else "no spending forecast yet",
        "method": "priority = importance x urgency, urgency = 1 / months left; "
                  "importance weights are listed in app/services/scenario.py",
    }
