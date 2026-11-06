# app/finance/calculations.py
#
# Pure calculation functions.
# They take plain Python data (no database, no LLM)
# so they are easy to test and always give the exact same answer.
#
# UPDATED:
# added subcategory and per-month helpers used by the expanded analysis.

def calculate_total_expenses(transactions):
    # Sum of all transaction amounts.
    return round(
        sum(
            t["amount"]
            for t in transactions
        ),
        2
    )


def calculate_category_spending(transactions):
    # Returns a dict like:
    # {"Food": 1000.0, "Transport": 500.0}

    totals = {}

    for t in transactions:
        cat = t.get("category") or "Others"

        totals[cat] = round(
            totals.get(cat, 0)
            + t["amount"],
            2
        )

    return totals


def calculate_subcategory_spending(transactions):
    """
    Nested totals:
    {
        "Food": {
            "Restaurant": 800.0,
            "Fast Food": 400.0
        }
    }

    Transactions with no subcategory are grouped
    under "(unspecified)".
    """

    totals = {}

    for t in transactions:
        cat = t.get("category") or "Others"
        sub = t.get("subcategory") or "(unspecified)"

        bucket = totals.setdefault(
            cat,
            {}
        )

        bucket[sub] = round(
            bucket.get(sub, 0)
            + t["amount"],
            2
        )

    return totals


def calculate_monthly_spending(transactions, month=None):
    """
    Total spending for a given month string "YYYY-MM".

    When month is provided:
        return spending for that month only.

    When month is None:
        return totals for all months.

    The optional form is kept for compatibility with
    the existing project code.
    """

    if month is not None:
        return round(
            sum(
                t["amount"]
                for t in transactions
                if (t.get("date") or "")[:7] == month
            ),
            2
        )

    result = {}

    for t in transactions:
        transaction_month = (
            t.get("date") or ""
        )[:7]

        if transaction_month:
            result[transaction_month] = round(
                result.get(transaction_month, 0)
                + t["amount"],
                2
            )

    return {
        m: result[m]
        for m in sorted(result)
    }


def monthly_totals(transactions):
    """
    {
        "2026-06": 3000.0,
        "2026-07": 3200.0
    }

    Ordered oldest -> newest.
    Transactions with no date are ignored.
    """

    totals = {}

    for t in transactions:
        month = (
            t.get("date") or ""
        )[:7]

        if month:
            totals[month] = round(
                totals.get(month, 0)
                + t["amount"],
                2
            )

    return {
        m: totals[m]
        for m in sorted(totals)
    }


def monthly_totals_by_category(transactions):
    """
    {
        "Food": {
            "2026-06": 900.0,
            "2026-07": 1100.0
        },
        ...
    }

    Ordered by month within each category.
    """

    out = {}

    for t in transactions:
        month = (
            t.get("date") or ""
        )[:7]

        if not month:
            continue

        cat = t.get("category") or "Others"

        bucket = out.setdefault(
            cat,
            {}
        )

        bucket[month] = round(
            bucket.get(month, 0)
            + t["amount"],
            2
        )

    return {
        category: {
            month: values[month]
            for month in sorted(values)
        }
        for category, values in out.items()
    }


def calculate_monthly_totals_by_category(transactions):
    """
    Compatibility alias kept from the existing project.
    """

    return monthly_totals_by_category(
        transactions
    )


def calculate_savings(income, total_expenses):
    # Savings = income minus what was spent.

    return round(
        income - total_expenses,
        2
    )


def calculate_savings_rate(income, savings):
    # Savings rate as a percentage.
    # Guard against dividing by zero.

    if income <= 0:
        return 0.0

    return round(
        (savings / income) * 100,
        2
    )


def percent_of(part, whole):
    """
    Safe percentage helper used everywhere in the analysis modules.
    """

    if not whole:
        return 0.0

    return round(
        (part / whole) * 100,
        2
    )


def get_spending_summary(transactions):
    """
    One dictionary combining the numbers above.
    """

    total = calculate_total_expenses(
        transactions
    )

    return {
        "total_expenses": total,
        "by_category": calculate_category_spending(
            transactions
        ),
        "by_subcategory": calculate_subcategory_spending(
            transactions
        ),
    }

def simple_interest(principal, annual_rate_pct, years):
    """Interest on the principal only: I = P x r x t."""
    if principal < 0 or annual_rate_pct < 0 or years < 0:
        raise ValueError("principal, rate and years cannot be negative")
    interest = principal * annual_rate_pct / 100 * years
    return {
        "principal": round(principal, 2),
        "annual_rate_pct": annual_rate_pct,
        "years": years,
        "interest": round(interest, 2),
        "total": round(principal + interest, 2),
        "formula": "I = P x r x t",
    }


def compound_interest(principal, annual_rate_pct, years, times_per_year=12):
    """Interest on the principal AND on interest already earned:
    A = P x (1 + r/n)^(n x t)."""
    if principal < 0 or annual_rate_pct < 0 or years < 0 or times_per_year <= 0:
        raise ValueError("principal, rate and years cannot be negative; "
                         "times_per_year must be at least 1")
    total = principal * (1 + annual_rate_pct / 100 / times_per_year) ** (times_per_year * years)
    return {
        "principal": round(principal, 2),
        "annual_rate_pct": annual_rate_pct,
        "years": years,
        "times_per_year": times_per_year,
        "interest": round(total - principal, 2),
        "total": round(total, 2),
        "formula": "A = P x (1 + r/n)^(n x t)",
    }
