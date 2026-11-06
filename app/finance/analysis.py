# app/finance/analysis.py
#
# NEW FILE — the three expanded analyses required by the updated design:
#
# 1. Category Spending Analysis (totals, %, ranking, subcategories, dominance)
# 2. Income Analysis (sources, %, primary source, history, trend)
# 3. Spending Pattern Analysis (monthly totals, per-category trends)
#
# Everything here is a PURE function: it takes plain lists of dictionaries and
# returns plain dictionaries. No database, no ML, no LLM -> fully testable.
#
# Trend method (deliberately simple and explainable, as required):
# the observed months are split into an EARLIER half and a RECENT half;
# we compare the two averages. A change of less than TREND_THRESHOLD_PCT
# counts as "stable". A least-squares slope is reported alongside so the
# direction can be double-checked. No ARIMA / Prophet / LSTM is used.

from app.finance.calculations import (
    calculate_total_expenses,
    calculate_category_spending,
    calculate_subcategory_spending,
    monthly_totals,
    monthly_totals_by_category,
    percent_of,
)


TREND_THRESHOLD_PCT = 10.0      # +/-10% around the earlier average = "stable"
MIN_MONTHS_FOR_TREND = 2        # below this we honestly say "unknown"
MIN_MONTHS_FOR_RECURRING = 3    # below this we never claim income is recurring


# ---------------------------------------------------------------------------
# shared trend helper
# ---------------------------------------------------------------------------

def _slope(values):
    """Least-squares slope of y against x = 1..n. Returns 0.0 for < 2 points."""

    n = len(values)

    if n < 2:
        return 0.0

    xs = list(range(1, n + 1))

    mean_x = sum(xs) / n
    mean_y = sum(values) / n

    denom = sum(
        (x - mean_x) ** 2
        for x in xs
    )

    if denom == 0:
        return 0.0

    num = sum(
        (xs[i] - mean_x) * (values[i] - mean_y)
        for i in range(n)
    )

    return round(num / denom, 2)


def compute_trend(series_by_month):
    """
    series_by_month: {"2026-06": 900.0, ...} -> a trend dictionary.

    direction is one of:
    increasing / decreasing / stable / unknown.
    """

    months = sorted(series_by_month)

    values = [
        float(series_by_month[m])
        for m in months
    ]

    n = len(values)

    if n < MIN_MONTHS_FOR_TREND:
        return {
            "direction": "unknown",
            "months_observed": n,
            "earlier_average": None,
            "recent_average": None,
            "change_percent": None,
            "slope": 0.0,
            "note": (
                f"need at least {MIN_MONTHS_FOR_TREND} months of data"
            ),
        }

    half = n // 2

    earlier = (
        values[:half]
        if half
        else values[:1]
    )

    recent = values[half:]

    earlier_avg = round(
        sum(earlier) / len(earlier),
        2
    )

    recent_avg = round(
        sum(recent) / len(recent),
        2
    )

    if earlier_avg == 0:
        change = (
            100.0
            if recent_avg > 0
            else 0.0
        )
    else:
        change = round(
            (recent_avg - earlier_avg)
            / earlier_avg
            * 100,
            2
        )

    if abs(change) < TREND_THRESHOLD_PCT:
        direction = "stable"
    elif change > 0:
        direction = "increasing"
    else:
        direction = "decreasing"

    return {
        "direction": direction,
        "months_observed": n,
        "earlier_average": earlier_avg,
        "recent_average": recent_avg,
        "change_percent": change,
        "slope": _slope(values),
        "note": (
            f"recent {len(recent)} month(s) average vs earlier "
            f"{len(earlier)} month(s) average; "
            f"+/-{TREND_THRESHOLD_PCT:.0f}% counts as stable"
        ),
    }


# ---------------------------------------------------------------------------
# 1. Category Spending Analysis
# ---------------------------------------------------------------------------

def analyze_categories(
    transactions,
    total_income=0.0,
    history_transactions=None
):
    """
    Total spending per category with percentages, ranking and subcategories.

    `transactions` is the window being analysed (normally the current month).
    `history_transactions`, if given, adds all-time totals for the same
    categories.
    """

    total_expenses = calculate_total_expenses(
        transactions
    )

    by_category = calculate_category_spending(
        transactions
    )

    by_subcategory = calculate_subcategory_spending(
        transactions
    )

    ranking = []

    for rank, (cat, amount) in enumerate(
        sorted(
            by_category.items(),
            key=lambda kv: (-kv[1], kv[0])
        ),
        start=1
    ):
        ranking.append({
            "rank": rank,
            "category": cat,
            "amount": round(amount, 2),
            "percent_of_expenses": percent_of(
                amount,
                total_expenses
            ),
            "percent_of_income": percent_of(
                amount,
                total_income
            ),
        })

    # "category contribution" = each category's share of total expenses.
    contribution = {
        r["category"]: r["percent_of_expenses"]
        for r in ranking
    }

    dominant = {}

    for cat, subs in by_subcategory.items():

        cat_total = sum(subs.values())

        best_sub, best_amount = max(
            subs.items(),
            key=lambda kv: (kv[1], kv[0])
        )

        dominant[cat] = {
            "subcategory": best_sub,
            "amount": round(best_amount, 2),
            "percent_of_category": percent_of(
                best_amount,
                cat_total
            ),
            "is_dominant": (
                percent_of(
                    best_amount,
                    cat_total
                ) >= 50.0
            ),
        }

    subcategory_ranking = {
        cat: [
            {
                "subcategory": s,
                "amount": round(a, 2),
                "percent_of_category": percent_of(
                    a,
                    sum(subs.values())
                ),
            }
            for s, a in sorted(
                subs.items(),
                key=lambda kv: (-kv[1], kv[0])
            )
        ]
        for cat, subs in by_subcategory.items()
    }

    history = (
        history_transactions
        if history_transactions is not None
        else transactions
    )

    by_category_all_time = calculate_category_spending(
        history
    )

    by_subcategory_all_time = calculate_subcategory_spending(
        history
    )

    return {
        "total_expenses": total_expenses,
        "total_income": round(
            float(total_income),
            2
        ),
        "by_category": by_category,
        "percent_of_expenses": {
            r["category"]: r["percent_of_expenses"]
            for r in ranking
        },
        "percent_of_income": {
            r["category"]: r["percent_of_income"]
            for r in ranking
        },
        "category_contribution": contribution,
        "ranking": ranking,
        "highest_category": (
            ranking[0]
            if ranking
            else None
        ),
        "lowest_category": (
            ranking[-1]
            if ranking
            else None
        ),
        "by_subcategory": by_subcategory,
        "subcategory_ranking": subcategory_ranking,
        "dominant_subcategory": dominant,
        "by_category_all_time": by_category_all_time,
        "by_subcategory_all_time": by_subcategory_all_time,
    }


# ---------------------------------------------------------------------------
# 2. Income Analysis
# ---------------------------------------------------------------------------

def analyze_income(income_records):
    """
    income_records:
    [{'amount': 5000, 'source': 'Salary', 'date': '2026-06-01'}, ...]
    """

    total_income = round(
        sum(
            float(r["amount"])
            for r in income_records
        ),
        2
    )

    by_source = {}
    months_per_source = {}

    for r in income_records:

        src = r.get("source") or "Other"

        by_source[src] = round(
            by_source.get(src, 0)
            + float(r["amount"]),
            2
        )

        month = (
            (r.get("date") or "")[:7]
        )

        if month:
            months_per_source.setdefault(
                src,
                set()
            ).add(month)

    source_percentages = {
        s: percent_of(a, total_income)
        for s, a in by_source.items()
    }

    ranking = [
        {
            "rank": i,
            "source": s,
            "amount": round(a, 2),
            "percent": percent_of(
                a,
                total_income
            ),
        }
        for i, (s, a) in enumerate(
            sorted(
                by_source.items(),
                key=lambda kv: (-kv[1], kv[0])
            ),
            start=1
        )
    ]

    primary = (
        ranking[0]
        if ranking
        else None
    )

    monthly_income = {}

    for r in income_records:

        month = (
            (r.get("date") or "")[:7]
        )

        if month:

            monthly_income[month] = round(
                monthly_income.get(month, 0)
                + float(r["amount"]),
                2
            )

    monthly_income = {
        m: monthly_income[m]
        for m in sorted(monthly_income)
    }

    trend = compute_trend(
        monthly_income
    )

    # Honesty rule: only call a source recurring when it really appears in
    # MIN_MONTHS_FOR_RECURRING or more distinct months.
    recurring_sources = sorted(
        s
        for s, ms in months_per_source.items()
        if len(ms) >= MIN_MONTHS_FOR_RECURRING
    )

    months_observed = len(
        monthly_income
    )

    if months_observed < MIN_MONTHS_FOR_RECURRING:

        stability = {
            "is_stable_claim_supported": False,
            "recurring_sources": recurring_sources,
            "reason": (
                f"only {months_observed} month(s) of income data "
                "— not enough to claim the income is stable or recurring"
            ),
        }

    else:

        values = list(
            monthly_income.values()
        )

        avg = sum(values) / len(values)

        spread = (
            (max(values) - min(values))
            / avg
            * 100
            if avg
            else 0.0
        )

        supported = (
            spread <= 15.0
            and bool(recurring_sources)
        )

        stability = {
            "is_stable_claim_supported": supported,
            "recurring_sources": recurring_sources,
            "month_to_month_spread_percent": round(
                spread,
                2
            ),
            "reason": (
                "monthly income varies by "
                f"{spread:.1f}% across {months_observed} months"
                + (
                    "; sources seen in 3+ months: "
                    + ", ".join(recurring_sources)
                    if recurring_sources
                    else "; no source seen in 3+ months"
                )
            ),
        }

    return {
        "total_income": total_income,
        "by_source": by_source,
        "source_percentages": source_percentages,
        "ranking": ranking,
        "primary_source": primary,
        "num_sources": len(by_source),
        "monthly_income": monthly_income,
        "months_observed": months_observed,
        "average_monthly_income": (
            round(
                sum(monthly_income.values())
                / months_observed,
                2
            )
            if months_observed
            else 0.0
        ),
        "latest_month_income": (
            list(monthly_income.values())[-1]
            if monthly_income
            else total_income
        ),
        "trend": trend,
        "stability": stability,
    }


# ---------------------------------------------------------------------------
# 3. Spending Pattern Analysis
# ---------------------------------------------------------------------------

def analyze_spending_patterns(transactions):
    """Monthly spending, per-category monthly spending, and simple trends."""

    per_month = monthly_totals(
        transactions
    )

    per_category_month = monthly_totals_by_category(
        transactions
    )

    months = list(
        per_month
    )

    values = list(
        per_month.values()
    )

    average_monthly = (
        round(
            sum(values) / len(values),
            2
        )
        if values
        else 0.0
    )

    highest = (
        {
            "month": max(
                per_month,
                key=lambda m: per_month[m]
            ),
            "amount": max(values),
        }
        if values
        else None
    )

    lowest = (
        {
            "month": min(
                per_month,
                key=lambda m: per_month[m]
            ),
            "amount": min(values),
        }
        if values
        else None
    )

    category_trends = {
        c: compute_trend(series)
        for c, series in per_category_month.items()
    }

    increasing = sorted(
        c
        for c, t in category_trends.items()
        if t["direction"] == "increasing"
    )

    decreasing = sorted(
        c
        for c, t in category_trends.items()
        if t["direction"] == "decreasing"
    )

    stable = sorted(
        c
        for c, t in category_trends.items()
        if t["direction"] == "stable"
    )

    unknown = sorted(
        c
        for c, t in category_trends.items()
        if t["direction"] == "unknown"
    )

    return {
        "monthly_spending": per_month,
        "months_observed": len(months),
        "category_monthly_spending": per_category_month,
        "average_monthly_spending": average_monthly,
        "highest_month": highest,
        "lowest_month": lowest,
        "category_trends": category_trends,
        "increasing_categories": increasing,
        "decreasing_categories": decreasing,
        "stable_categories": stable,
        "unknown_trend_categories": unknown,
        "recent_trend": compute_trend(
            per_month
        ),
        "method": (
            "recent-vs-earlier monthly averages with a "
            "least-squares slope; no time-series model is used"
        ),
    }