# app/ml/feature_engineering.py

import math


FEATURE_COLUMNS = [
    "income",
    "total_expenses",
    "savings",
    "saving_rate",
    "expense_ratio",
    "food_pct",
    "shopping_pct",
    "entertainment_pct",
    "transport_pct",
    "bills_pct",
    "utilities_pct",
    "education_pct",
    "healthcare_pct",
    "others_pct",
    "avg_monthly_spending",
    "spending_trend",
    "food_trend",
    "shopping_trend",
    "entertainment_trend",
    "transport_trend",
    "num_income_sources",
    "primary_income_pct",
    "has_goal",
    "goal_remaining",
    "goal_months_remaining",
    "required_monthly_saving",
    "saving_gap",
    "saving_gap_ratio",
]


LABEL_COLUMNS = [
    "Reduce_Food",
    "Reduce_Shopping",
    "Reduce_Entertainment",
    "Reduce_Transport",
    "Increase_Income",
    "Increase_Saving",
    "Maintain_Current_Plan",
    "Extend_Goal_Timeline",
]


ACTION_NAMES = {
    "Reduce_Food": "Reduce Food",
    "Reduce_Shopping": "Reduce Shopping",
    "Reduce_Entertainment": "Reduce Entertainment",
    "Reduce_Transport": "Reduce Transport",
    "Increase_Income": "Increase Income",
    "Increase_Saving": "Increase Saving",
    "Maintain_Current_Plan": "Maintain Current Plan",
    "Extend_Goal_Timeline": "Extend Goal Timeline",
}


LABEL_TO_CATEGORY = {
    "Reduce_Food": "Food",
    "Reduce_Shopping": "Shopping",
    "Reduce_Entertainment": "Entertainment",
    "Reduce_Transport": "Transport",
}


TREND_ENCODING = {
    "increasing": 1.0,
    "stable": 0.0,
    "decreasing": -1.0,
    "unknown": 0.0,
}


def encode_trend(direction):
    return TREND_ENCODING.get(direction, 0.0)


def _safe_div(a, b):
    return round(a / b, 4) if b else 0.0


def build_features(analysis):
    income = float(analysis.get("income") or 0.0)
    expenses = float(analysis.get("expenses") or 0.0)
    savings = float(analysis.get("savings") or 0.0)
    saving_rate = float(analysis.get("savings_rate") or 0.0)

    cat = analysis.get("category_analysis") or {}
    pct = cat.get("percent_of_expenses") or {}

    patterns = analysis.get("spending_patterns") or {}
    cat_trends = patterns.get("category_trends") or {}
    recent = patterns.get("recent_trend") or {}

    inc = analysis.get("income_analysis") or {}
    primary = inc.get("primary_source") or {}

    goal = analysis.get("goal_analysis") or None

    has_goal = 1.0 if goal else 0.0

    goal_remaining = (
        float(goal["remaining"])
        if goal
        else 0.0
    )

    months_remaining = (
        float(goal["months_remaining"])
        if goal
        else 0.0
    )

    required = (
        float(goal["required_monthly_saving"])
        if goal
        else 0.0
    )

    gap = (
        float(goal["saving_gap"])
        if goal
        else round(max(0.0, -savings), 2)
    )

    features = {
        "income": round(income, 2),

        "total_expenses": round(expenses, 2),

        "savings": round(savings, 2),

        "saving_rate": round(saving_rate, 2),

        "expense_ratio": _safe_div(expenses, income),

        "food_pct": float(
            pct.get("Food", 0.0)
        ),

        "shopping_pct": float(
            pct.get("Shopping", 0.0)
        ),

        "entertainment_pct": float(
            pct.get("Entertainment", 0.0)
        ),

        "transport_pct": float(
            pct.get("Transport", 0.0)
        ),

        "bills_pct": float(
            pct.get("Bills", 0.0)
        ),

        "utilities_pct": float(
            pct.get("Utilities", 0.0)
        ),

        "education_pct": float(
            pct.get("Education", 0.0)
        ),

        "healthcare_pct": float(
            pct.get("Healthcare", 0.0)
        ),

        "others_pct": float(
            pct.get("Others", 0.0)
        ),

        "avg_monthly_spending": float(
            patterns.get(
                "average_monthly_spending"
            )
            or expenses
        ),

        "spending_trend": encode_trend(
            recent.get("direction")
        ),

        "food_trend": encode_trend(
            (cat_trends.get("Food") or {}).get(
                "direction"
            )
        ),

        "shopping_trend": encode_trend(
            (cat_trends.get("Shopping") or {}).get(
                "direction"
            )
        ),

        "entertainment_trend": encode_trend(
            (
                cat_trends.get("Entertainment")
                or {}
            ).get("direction")
        ),

        "transport_trend": encode_trend(
            (cat_trends.get("Transport") or {}).get(
                "direction"
            )
        ),

        "num_income_sources": float(
            inc.get("num_sources") or 0
        ),

        "primary_income_pct": float(
            primary.get("percent") or 0.0
        ),

        "has_goal": has_goal,

        "goal_remaining": round(
            goal_remaining,
            2
        ),

        "goal_months_remaining": months_remaining,

        "required_monthly_saving": round(
            required,
            2
        ),

        "saving_gap": round(
            gap,
            2
        ),

        "saving_gap_ratio": _safe_div(
            gap,
            income
        ),
    }

    return validate_features(features)


def validate_features(features):
    missing = [
        c
        for c in FEATURE_COLUMNS
        if c not in features
    ]

    if missing:
        raise ValueError(
            f"missing features: {missing}"
        )

    extra = [
        k
        for k in features
        if k not in FEATURE_COLUMNS
    ]

    if extra:
        raise ValueError(
            "unexpected feature keys "
            f"(possible leakage): {extra}"
        )

    clean = {}

    for name in FEATURE_COLUMNS:
        value = float(features[name])

        if math.isnan(value) or math.isinf(value):
            value = 0.0

        clean[name] = value

    return clean


def features_to_row(features):
    clean = validate_features(features)

    return [
        clean[name]
        for name in FEATURE_COLUMNS
    ]


def assert_no_leakage():
    overlap = (
        set(FEATURE_COLUMNS)
        & set(LABEL_COLUMNS)
    )

    if overlap:
        raise AssertionError(
            "data leakage: columns in both X "
            f"and Y -> {sorted(overlap)}"
        )

    if len(set(FEATURE_COLUMNS)) != len(
        FEATURE_COLUMNS
    ):
        raise AssertionError(
            "duplicate feature columns"
        )

    if len(set(LABEL_COLUMNS)) != len(
        LABEL_COLUMNS
    ):
        raise AssertionError(
            "duplicate label columns"
        )

    return True


def split_x_y(df):
    assert_no_leakage()

    missing_x = [
        c
        for c in FEATURE_COLUMNS
        if c not in df.columns
    ]

    missing_y = [
        c
        for c in LABEL_COLUMNS
        if c not in df.columns
    ]

    if missing_x:
        raise ValueError(
            "dataset is missing feature "
            f"columns: {missing_x}"
        )

    if missing_y:
        raise ValueError(
            "dataset is missing label "
            f"columns: {missing_y}"
        )

    X = df[FEATURE_COLUMNS].astype(float)

    Y = df[LABEL_COLUMNS].astype(int)

    return X, Y


def build_features_for_user(
    user_id,
    reference_date=None
):
    from app.engines.financial_engine import (
        analyze_finances
    )

    return build_features(
        analyze_finances(
            user_id,
            reference_date=reference_date
        )
    )