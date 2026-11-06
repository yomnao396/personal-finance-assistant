# app/engines/recommendation_engine.py
# REWRITTEN — the Strategic Recommendation Engine.
#
# Flow:
#   Financial Analysis -> Feature Engineering -> Random Forest -> ACTIONS
#   -> User Essential Preferences (runtime constraint) -> THIS ENGINE
#   -> a strategic multi-category plan with real amounts.
#
# Division of responsibility:
#   * the Random Forest decides WHICH actions apply (multi-label flags only);
#   * this engine decides HOW MUCH, using the user's real spending, the real
#     saving gap and the user's protected categories.
# No amount in this file is hard-coded: every figure is derived from the data.

import math

from app.engines.financial_engine import analyze_finances
from app.ml.feature_engineering import (
    ACTION_NAMES,
    LABEL_TO_CATEGORY,
    build_features,
)
from app.ml.recommendation_rules import (
    INCOME_UPLIFT_SHARE,
    MAX_CUT_SHARE,
    recommend_labels,
)
from app.preferences.user_preferences import (
    preference_map,
    split_category_spending,
)

# Policy constants (shares/targets, never amounts).
MAX_REDUCTION_SHARE = MAX_CUT_SHARE
TARGET_SAVING_RATE = 20.0
MIN_ACTIONABLE_AMOUNT = 1.0


# ---------------------------------------------------------------------------
# step 1 — get the predicted actions
# ---------------------------------------------------------------------------

def predict_actions(features):
    try:
        from app.ml.recommendation_model import predict_recommendations

        return predict_recommendations(features)

    except Exception as exc:
        labels, reasons = recommend_labels(features)
        actions = [name for name, on in labels.items() if on]

        return {
            "actions": actions,
            "action_names": [ACTION_NAMES[a] for a in actions],
            "labels": labels,
            "probabilities": {},
            "source": "rule_fallback",
            "note": (
                f"Random Forest unavailable ({exc.__class__.__name__}); "
                f"used the explicit financial rules instead"
            ),
            "rule_reasons": reasons,
        }


# ---------------------------------------------------------------------------
# step 2 — remove contradictory / impossible actions
# ---------------------------------------------------------------------------

def filter_actions(actions, analysis, gap, category_totals):
    """Returns (kept_actions, [{'action':..., 'reason':...}])."""

    kept, removed = list(actions), []

    def drop(action, reason):
        if action in kept:
            kept.remove(action)
            removed.append({
                "action": action,
                "action_name": ACTION_NAMES[action],
                "reason": reason,
            })

    needs_action = gap > 0 or analysis["savings"] < 0

    # Maintain Current Plan can never sit next to a corrective action.
    if "Maintain_Current_Plan" in kept and len(kept) > 1:
        if needs_action:
            drop(
                "Maintain_Current_Plan",
                f"a monthly gap of {gap:.2f} still has to be closed, "
                f"so the current plan cannot simply be maintained",
            )
        else:
            for other in list(kept):
                if other != "Maintain_Current_Plan":
                    drop(
                        other,
                        "the user is already on track, so no corrective action "
                        "is combined with Maintain Current Plan",
                    )

    # Nothing to extend if there is no goal.
    if "Extend_Goal_Timeline" in kept and not analysis.get("goal_analysis"):
        drop(
            "Extend_Goal_Timeline",
            "the user has no financial goal to extend",
        )

    # Do not ask to cut a category with no spending in it.
    for action, category in LABEL_TO_CATEGORY.items():
        if action in kept and category_totals.get(category, 0) <= 0:
            drop(
                action,
                f"there is no {category} spending to reduce",
            )

    # An Increase Saving instruction on a user already at/above target is noise.
    if (
        "Increase_Saving" in kept
        and not needs_action
        and analysis["savings_rate"] >= TARGET_SAVING_RATE
    ):
        drop(
            "Increase_Saving",
            f"the saving rate is already {analysis['savings_rate']:.1f}% "
            f"(target {TARGET_SAVING_RATE:.0f}%)",
        )

    if not kept:
        kept = (
            ["Maintain_Current_Plan"]
            if not needs_action
            else ["Increase_Income"]
        )

    return kept, removed


# ---------------------------------------------------------------------------
# step 3 — turn reduction actions into real amounts
# ---------------------------------------------------------------------------

def _reduction_capacity(user_id, category, subcategory_totals, prefs):
    split = split_category_spending(
        user_id,
        category,
        subcategory_totals,
        prefs,
    )

    split["max_reduction"] = round(
        split["eligible_amount"] * MAX_REDUCTION_SHARE,
        2,
    )

    return split


def _distribute_over_subcategories(split, amount):
    """Spread one category's reduction across flexible subcategories."""

    eligible = split["eligible_amount"]
    lines = []

    if eligible <= 0 or amount <= 0:
        return lines

    for item in split["eligible_subcategories"]:
        share = item["amount"] / eligible
        cut = round(amount * share, 2)

        if cut >= 0.01:
            lines.append({
                "subcategory": item["subcategory"],
                "current_spending": item["amount"],
                "reduce_by": cut,
                "new_spending": round(
                    item["amount"] - cut,
                    2,
                ),
            })

    return lines


def generate_recommendation_plan(
    user_id,
    reference_date=None,
    analysis=None,
):
    """The complete strategic plan for one user."""

    analysis = analysis or analyze_finances(
        user_id,
        reference_date=reference_date,
    )

    features = build_features(analysis)
    prediction = predict_actions(features)

    goal = analysis.get("goal_analysis")
    savings = float(analysis["savings"])
    income = float(analysis["income"])

    gap = (
        float(goal["saving_gap"])
        if goal
        else round(max(0.0, -savings), 2)
    )

    category_totals = analysis["category_analysis"]["by_category"]
    subcategory_totals = analysis["category_analysis"]["by_subcategory"]

    prefs = preference_map(user_id)

    actions, filtered_out = filter_actions(
        prediction["actions"],
        analysis,
        gap,
        category_totals,
    )

    # ---- how much money do we actually need to find this month? ----

    if gap > 0:
        target_amount = round(gap, 2)

        target_reason = (
            f"monthly saving gap for goal '{goal['goal_name']}': "
            f"needs {goal['required_monthly_saving']:.2f}/month, "
            f"currently saving {goal['current_monthly_saving']:.2f}"
        )

    elif "Increase_Saving" in actions and income > 0:
        target_amount = round(
            max(
                income * TARGET_SAVING_RATE / 100.0 - savings,
                0,
            ),
            2,
        )

        target_reason = (
            f"no goal gap; aiming to lift the saving rate from "
            f"{analysis['savings_rate']:.1f}% to "
            f"{TARGET_SAVING_RATE:.0f}%"
        )

    else:
        target_amount = 0.0
        target_reason = (
            "no funding gap — nothing has to be freed up this month"
        )

    # ---- reduction capacity per predicted category ----

    reduction_actions = [
        a for a in actions
        if a in LABEL_TO_CATEGORY
    ]

    splits = {}
    blocked = []

    for action in list(reduction_actions):
        category = LABEL_TO_CATEGORY[action]

        split = _reduction_capacity(
            user_id,
            category,
            subcategory_totals.get(category, {}),
            prefs,
        )

        if split["eligible_amount"] <= 0:
            blocked.append({
                "action": action,
                "action_name": ACTION_NAMES[action],
                "category": category,
                "reason": (
                    f"every {category} subcategory the user spends on "
                    f"is marked essential — protected expenses are never reduced"
                ),
                "protected_amount": split["protected_amount"],
                "protected_subcategories": (
                    split["protected_subcategories"]
                ),
            })

            reduction_actions.remove(action)

        else:
            splits[category] = split

    # If protection removed a predicted category, look for another eligible one.
    substitutions = []

    if blocked and target_amount > 0:
        current_capacity = sum(
            s["max_reduction"]
            for s in splits.values()
        )

        for category in sorted(
            category_totals,
            key=lambda c: -category_totals[c],
        ):
            action = f"Reduce_{category}"

            if action not in LABEL_TO_CATEGORY or category in splits:
                continue

            if current_capacity >= target_amount:
                break

            candidate = _reduction_capacity(
                user_id,
                category,
                subcategory_totals.get(category, {}),
                prefs,
            )

            if candidate["eligible_amount"] > 0:
                splits[category] = candidate
                reduction_actions.append(action)

                current_capacity += candidate["max_reduction"]

                substitutions.append({
                    "action": action,
                    "action_name": ACTION_NAMES[action],
                    "category": category,
                    "reason": (
                        "substituted because a predicted category was "
                        "fully protected; this category still has "
                        "flexible spending"
                    ),
                })

    total_capacity = round(
        sum(
            s["max_reduction"]
            for s in splits.values()
        ),
        2,
    )

    # ---- allocate the needed money across eligible categories ----

    reductions = []
    allocated = 0.0

    if target_amount > 0 and total_capacity > 0:
        take = min(
            target_amount,
            total_capacity,
        )

        for category, split in sorted(
            splits.items(),
            key=lambda kv: -kv[1]["max_reduction"],
        ):
            share = (
                split["max_reduction"]
                / total_capacity
            )

            amount = round(
                take * share,
                2,
            )

            if amount < MIN_ACTIONABLE_AMOUNT:
                continue

            allocated += amount

            reductions.append({
                "action": f"Reduce_{category}",
                "action_name": ACTION_NAMES[
                    f"Reduce_{category}"
                ],
                "category": category,
                "current_spending": split["total"],
                "protected_spending": split["protected_amount"],
                "eligible_spending": split["eligible_amount"],
                "max_reduction_allowed": split["max_reduction"],
                "reduce_by": amount,
                "reduction_percent_of_category": (
                    round(
                        amount / split["total"] * 100,
                        2,
                    )
                    if split["total"]
                    else 0.0
                ),
                "new_category_spending": round(
                    split["total"] - amount,
                    2,
                ),
                "protected_subcategories": (
                    split["protected_subcategories"]
                ),
                "subcategory_plan": _distribute_over_subcategories(
                    split,
                    amount,
                ),
            })

    allocated = round(allocated, 2)

    remaining_gap = round(
        max(
            target_amount - allocated,
            0,
        ),
        2,
    )

    # ---- feasibility rule ----

    derived_actions = []

    if (
        remaining_gap > 0
        and "Increase_Income" not in actions
        and "Extend_Goal_Timeline" not in actions
    ):
        actions.append("Increase_Income")

        derived_actions.append({
            "action": "Increase_Income",
            "action_name": "Increase Income",
            "reason": (
                f"the planned reductions free {allocated:.2f} but "
                f"{remaining_gap:.2f} of the monthly gap is still open, "
                f"so the money has to come from the income side"
            ),
        })

        if remaining_gap > INCOME_UPLIFT_SHARE * income:
            actions.append("Extend_Goal_Timeline")

            derived_actions.append({
                "action": "Extend_Goal_Timeline",
                "action_name": "Extend Goal Timeline",
                "reason": (
                    f"the remaining gap ({remaining_gap:.2f}) is larger "
                    f"than a realistic income uplift "
                    f"({INCOME_UPLIFT_SHARE * income:.2f}), "
                    f"so the timeline has to move as well"
                ),
            })

    # ---- income / saving / timeline actions ----

    income_actions = []
    saving_actions = []
    timeline_actions = []

    if "Increase_Income" in actions:
        needed = (
            remaining_gap
            if remaining_gap > 0
            else 0.0
        )

        income_actions.append({
            "action": "Increase_Income",
            "action_name": "Increase Income",
            "required_extra_monthly_income": needed,
            "current_income": round(income, 2),
            "num_income_sources": (
                analysis["income_analysis"]["num_sources"]
            ),
            "primary_source": (
                analysis["income_analysis"]["primary_source"]
            ),
            "explanation": (
                f"after the planned reductions, {needed:.2f} per month "
                f"is still missing; extra income of that size would close it"
                if needed > 0
                else
                "the reductions already cover the gap; extra income "
                "would build a buffer"
            ),
        })

    if "Increase_Saving" in actions:
        saving_actions.append({
            "action": "Increase_Saving",
            "action_name": "Increase Saving",
            "current_monthly_saving": round(savings, 2),
            "current_saving_rate": analysis["savings_rate"],
            "target_saving_rate": TARGET_SAVING_RATE,
            "suggested_monthly_saving": round(
                max(savings + allocated, 0),
                2,
            ),
            "explanation": (
                "move the freed money to savings at the START of the month, "
                "before spending"
            ),
        })

    if "Extend_Goal_Timeline" in actions and goal:
        achievable = round(
            savings + allocated,
            2,
        )

        if achievable > 0:
            months_needed = math.ceil(
                goal["remaining"] / achievable
            )

            extra_months = max(
                months_needed - goal["months_remaining"],
                0,
            )

            explanation = (
                f"saving {achievable:.2f}/month reaches "
                f"{goal['remaining']:.2f} in {months_needed} month(s) "
                f"instead of {goal['months_remaining']}"
            )

        else:
            months_needed = None
            extra_months = None

            explanation = (
                "with no positive monthly saving the goal cannot be dated yet — "
                "expenses must come below income first"
            )

        timeline_actions.append({
            "action": "Extend_Goal_Timeline",
            "action_name": "Extend Goal Timeline",
            "goal_name": goal["goal_name"],
            "current_months_remaining": goal["months_remaining"],
            "achievable_monthly_saving": achievable,
            "months_needed_at_that_rate": months_needed,
            "extra_months_required": extra_months,
            "explanation": explanation,
        })

    maintain = "Maintain_Current_Plan" in actions

    gap_closed = remaining_gap <= 0

    if maintain:
        summary = (
            f"On track: saving {savings:.2f} per month "
            f"({analysis['savings_rate']:.1f}% of income). "
            f"Keep the current plan."
        )

    elif target_amount <= 0:
        summary = (
            "No funding gap this month; the actions below are "
            "improvements, not fixes."
        )

    elif gap_closed:
        summary = (
            f"Freeing {allocated:.2f} per month across "
            f"{len(reductions)} category/categories fully covers "
            f"the {target_amount:.2f} monthly gap."
        )

    else:
        summary = (
            f"Freeing {allocated:.2f} per month covers part of the "
            f"{target_amount:.2f} monthly gap; {remaining_gap:.2f} "
            f"is still open and must come from extra income or a "
            f"longer timeline."
        )

    return {
        "user_id": user_id,
        "summary": summary,

        "predicted_actions": prediction["actions"],
        "predicted_action_names": prediction.get(
            "action_names",
            [],
        ),
        "prediction_source": prediction.get("source"),
        "prediction_probabilities": prediction.get(
            "probabilities",
            {},
        ),

        "applied_actions": sorted(
            set(
                [r["action"] for r in reductions]
                + [a["action"] for a in income_actions]
                + [a["action"] for a in saving_actions]
                + [a["action"] for a in timeline_actions]
                + (
                    ["Maintain_Current_Plan"]
                    if maintain
                    else []
                )
            )
        ),

        "filtered_out": filtered_out,
        "derived_actions": derived_actions,
        "blocked_by_preferences": blocked,
        "substituted_actions": substitutions,

        "financial_gap": {
            "goal": goal,
            "target_amount": target_amount,
            "target_reason": target_reason,
            "total_reduction_capacity": total_capacity,
            "total_planned_reduction": allocated,
            "remaining_gap": remaining_gap,
            "gap_closed": gap_closed,
        },

        "reductions": reductions,
        "income_actions": income_actions,
        "saving_actions": saving_actions,
        "timeline_actions": timeline_actions,
        "maintain_current_plan": maintain,

        "constraints": {
            "max_reduction_share_of_flexible_spending": (
                MAX_REDUCTION_SHARE
            ),
            "protected_categories_are_never_reduced": True,
            "preferences_used": [
                {
                    "main_category": c,
                    "subcategory": s,
                    "is_essential": v,
                }
                for (c, s), v in sorted(prefs.items())
            ],
        },

        "analysis_snapshot": {
            "income": round(income, 2),
            "expenses": analysis["expenses"],
            "savings": round(savings, 2),
            "savings_rate": analysis["savings_rate"],
            "current_month": analysis.get("current_month"),
            "highest_category": (
                analysis["category_analysis"]["highest_category"]
            ),
            "increasing_categories": (
                analysis["spending_patterns"]["increasing_categories"]
            ),
        },
    }


# ---------------------------------------------------------------------------
# backwards-compatible text tips
# ---------------------------------------------------------------------------

def generate_recommendations(
    user_id,
    reference_date=None,
):
    """Short sentences built from the plan above."""

    plan = generate_recommendation_plan(
        user_id,
        reference_date=reference_date,
    )

    lines = [plan["summary"]]

    for r in plan["reductions"]:
        subs = ", ".join(
            f"{s['subcategory']} -{s['reduce_by']:.0f}"
            for s in r["subcategory_plan"]
        ) or "flexible items"

        lines.append(
            f"{r['action_name']}: cut {r['reduce_by']:.2f} "
            f"from {r['category']} "
            f"(currently {r['current_spending']:.2f}, "
            f"of which {r['protected_spending']:.2f} is protected) — "
            f"{subs}."
        )

    for b in plan["blocked_by_preferences"]:
        lines.append(
            f"{b['action_name']} was skipped: {b['reason']}."
        )

    for a in plan["income_actions"]:
        lines.append(
            f"{a['action_name']}: {a['explanation']}."
        )

    for a in plan["saving_actions"]:
        lines.append(
            f"{a['action_name']}: {a['explanation']} "
            f"(target rate {a['target_saving_rate']:.0f}%)."
        )

    for a in plan["timeline_actions"]:
        lines.append(
            f"{a['action_name']}: {a['explanation']}."
        )

    if (
        not plan["financial_gap"]["gap_closed"]
        and plan["financial_gap"]["target_amount"] > 0
    ):
        lines.append(
            f"Remaining monthly gap after this plan: "
            f"{plan['financial_gap']['remaining_gap']:.2f}."
        )

    return lines