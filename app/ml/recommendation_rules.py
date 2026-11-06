# app/ml/recommendation_rules.py
# NEW FILE — the EXPLICIT financial criteria that turn a financial scenario into
# multi-label recommendation targets.
#
# These rules are used in three places:
#   1. to LABEL the synthetic training scenarios (Option 2 dataset)
#   2. to LABEL scenarios derived from a Kaggle dataset (Option 1)
#   3. as a transparent FALLBACK when the trained model file is missing
#
# Labels are therefore never random: every row's Y is a deterministic function
# of that row's financial state, and the reason is recorded next to it.

from app.ml.feature_engineering import LABEL_COLUMNS

# --- thresholds (percent of total expenses) ---------------------------------

CATEGORY_THRESHOLDS = {
    "Food": 30.0,
    "Shopping": 15.0,
    "Entertainment": 8.0,
    "Transport": 15.0,
}

RISING_FACTOR = 0.60        # a rising category is flagged from 60% of its threshold
MAX_CUT_SHARE = 0.30        # at most 30% of a flexible category can realistically be cut
INCOME_UPLIFT_SHARE = 0.20  # a realistic extra-income effort is ~20% of current income

LOW_SAVING_RATE = 10.0      # below this the user is told to increase saving
GOOD_SAVING_RATE = 20.0     # at or above this, and on track, the plan can be maintained

DIVERSIFIED_SOURCES = 3     # already 3+ income sources -> asking for more is unrealistic


def flagged_categories(features):
    """Which flexible categories are over-weight or rising. Returns
    [(category, percent_of_expenses, is_over_threshold, is_rising)]."""
    out = []

    for category, threshold in CATEGORY_THRESHOLDS.items():
        key = category.lower() + "_pct"
        trend_key = category.lower() + "_trend"

        pct = float(features.get(key, 0.0))
        trend = float(features.get(trend_key, 0.0))

        over = pct >= threshold
        rising = trend > 0 and pct >= threshold * RISING_FACTOR

        if over or rising:
            out.append((category, pct, over, rising))

    return sorted(out, key=lambda item: -item[1])


def cuttable_amount(features, flags):
    """The money that could realistically be freed by trimming the flagged
    categories (MAX_CUT_SHARE of each). Used only to decide WHICH labels apply;
    the Recommendation Engine computes the real amounts later."""
    expenses = float(features.get("total_expenses", 0.0))

    return round(
        sum(
            expenses * pct / 100.0
            for _, pct, _, _ in flags
        ) * MAX_CUT_SHARE,
        2,
    )


def recommend_labels(features):
    """features -> ({label: 0/1}, [reasons]) using explicit financial criteria."""
    labels = {name: 0 for name in LABEL_COLUMNS}
    reasons = []

    income = float(features.get("income", 0.0))
    savings = float(features.get("savings", 0.0))
    rate = float(features.get("saving_rate", 0.0))

    has_goal = float(features.get("has_goal", 0.0)) >= 1.0

    gap = (
        float(features.get("saving_gap", 0.0))
        if has_goal
        else max(0.0, -savings)
    )

    spending_trend = float(features.get("spending_trend", 0.0))
    sources = float(features.get("num_income_sources", 0.0))

    flags = flagged_categories(features)

    needs_action = (
        gap > 0
        or savings < 0
        or rate < LOW_SAVING_RATE
    )

    if needs_action:
        for category, pct, over, rising in flags:
            labels["Reduce_" + category] = 1

            reasons.append(
                f"{category} is {pct:.1f}% of expenses"
                + (
                    f" (threshold {CATEGORY_THRESHOLDS[category]:.0f}%)"
                    if over
                    else ""
                )
                + (" and rising" if rising else "")
            )

        possible = cuttable_amount(features, flags)

        if gap > 0 and flags and possible < gap:
            labels["Increase_Income"] = 1

            reasons.append(
                f"trimming the flagged categories frees about {possible:.0f}, "
                f"which is less than the {gap:.0f} monthly gap"
            )

            if possible + INCOME_UPLIFT_SHARE * income < gap:
                labels["Extend_Goal_Timeline"] = 1

                reasons.append(
                    "even with extra income the gap stays open — the timeline "
                    "has to move"
                )

        if gap > 0 and not flags:
            if sources >= DIVERSIFIED_SOURCES and rate >= 15.0:
                labels["Extend_Goal_Timeline"] = 1

                reasons.append(
                    "no over-weight flexible category and income is already "
                    "diversified — extending the timeline is the realistic move"
                )
            else:
                labels["Increase_Income"] = 1

                reasons.append(
                    "no over-weight flexible category to cut, so the gap must be "
                    "closed from the income side"
                )

                if gap > INCOME_UPLIFT_SHARE * income:
                    labels["Extend_Goal_Timeline"] = 1

                    reasons.append(
                        f"the gap ({gap:.0f}) exceeds a realistic income uplift "
                        f"({INCOME_UPLIFT_SHARE * income:.0f})"
                    )

        if rate < LOW_SAVING_RATE:
            labels["Increase_Saving"] = 1

            reasons.append(
                f"saving rate {rate:.1f}% is below {LOW_SAVING_RATE:.0f}%"
            )

    else:
        if rate >= GOOD_SAVING_RATE and spending_trend <= 0 and not flags:
            labels["Maintain_Current_Plan"] = 1

            reasons.append(
                f"on track for the goal, saving rate {rate:.1f}% and spending is "
                f"not rising"
            )

        elif flags and spending_trend > 0:
            for category, pct, _, _ in flags:
                labels["Reduce_" + category] = 1

                reasons.append(
                    f"on track, but {category} ({pct:.1f}% of expenses) is rising"
                )

        else:
            labels["Increase_Saving"] = 1

            reasons.append(
                f"on track but the saving rate ({rate:.1f}%) can still be improved"
            )

    if sum(labels.values()) == 0:
        labels["Maintain_Current_Plan"] = 1

        reasons.append(
            "no criterion triggered — keep the current plan"
        )

    # Maintain Current Plan can never be combined with another action.
    if labels["Maintain_Current_Plan"] and sum(labels.values()) > 1:
        labels["Maintain_Current_Plan"] = 0

    return labels, reasons


def labels_to_actions(labels):
    """{'Reduce_Food': 1, ...} -> ['Reduce_Food', ...] (only the active ones)."""
    return [
        name
        for name in LABEL_COLUMNS
        if labels.get(name)
    ]