from app.finance.analysis import analyze_spending_patterns, compute_trend


def _txn(month, amount, category):
    return {
        "amount": amount,
        "category": category,
        "date": f"{month}-10",
    }


TXNS = (
    [
        _txn("2026-06", 1000, "Food"),
        _txn("2026-07", 1200, "Food"),
        _txn("2026-08", 1500, "Food"),
    ]
    + [
        _txn("2026-06", 600, "Transport"),
        _txn("2026-07", 400, "Transport"),
        _txn("2026-08", 200, "Transport"),
    ]
    + [
        _txn("2026-06", 300, "Bills"),
        _txn("2026-07", 300, "Bills"),
        _txn("2026-08", 300, "Bills"),
    ]
)


def test_monthly_spending():
    p = analyze_spending_patterns(TXNS)

    assert p["monthly_spending"] == {
        "2026-06": 1900,
        "2026-07": 1900,
        "2026-08": 2000,
    }

    assert p["months_observed"] == 3


def test_category_spending_by_month():
    p = analyze_spending_patterns(TXNS)

    assert p["category_monthly_spending"]["Food"] == {
        "2026-06": 1000,
        "2026-07": 1200,
        "2026-08": 1500,
    }

    assert p["category_monthly_spending"]["Transport"]["2026-08"] == 200


def test_increasing_category_detected():
    p = analyze_spending_patterns(TXNS)

    assert "Food" in p["increasing_categories"]
    assert p["category_trends"]["Food"]["direction"] == "increasing"
    assert p["category_trends"]["Food"]["slope"] > 0


def test_decreasing_category_detected():
    p = analyze_spending_patterns(TXNS)

    assert "Transport" in p["decreasing_categories"]
    assert p["category_trends"]["Transport"]["slope"] < 0


def test_stable_category_detected():
    p = analyze_spending_patterns(TXNS)

    assert "Bills" in p["stable_categories"]
    assert p["category_trends"]["Bills"]["change_percent"] == 0.0


def test_average_monthly_spending():
    assert (
        analyze_spending_patterns(TXNS)["average_monthly_spending"]
        == 1933.33
    )


def test_highest_and_lowest_month():
    p = analyze_spending_patterns(TXNS)

    assert p["highest_month"] == {
        "month": "2026-08",
        "amount": 2000,
    }

    assert p["lowest_month"]["amount"] == 1900


def test_recent_trend_uses_recent_vs_earlier_averages():
    trend = analyze_spending_patterns(TXNS)["recent_trend"]

    assert trend["earlier_average"] == 1900
    assert trend["recent_average"] == 1950
    assert trend["direction"] == "stable"


def test_trend_needs_at_least_two_months():
    trend = compute_trend({"2026-08": 1000})

    assert trend["direction"] == "unknown"
    assert trend["change_percent"] is None


def test_undated_transactions_are_ignored_by_the_pattern_analysis():
    p = analyze_spending_patterns(
        TXNS + [
            {
                "amount": 999,
                "category": "Food",
            }
        ]
    )

    assert p["monthly_spending"]["2026-08"] == 2000


def test_no_transactions_is_safe():
    p = analyze_spending_patterns([])

    assert p["months_observed"] == 0
    assert p["highest_month"] is None
    assert p["average_monthly_spending"] == 0.0


def test_method_is_documented_in_the_output():
    assert (
        "no time-series model"
        in analyze_spending_patterns(TXNS)["method"]
    )
