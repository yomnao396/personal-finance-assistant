# app/finance/facts.py
# Exact, traceable financial facts for the assistant, read straight from the
# database for ONE user and ONE period.
#
# * Every query filters on user_id and on the months asked for, and reads only
#   the columns it needs (no whole-history dumps).
# * Sums use Decimal, so 0.1 + 0.2 is 0.3; rounding happens only when a number
#   is shown (money()), never in between.
# * The accounting rules are the app's own: expenses are the rows of the
#   transactions table outside the Income category (crud.get_expense_transactions),
#   income is the income table, and the monthly saving is income - expenses
#   (financial_engine). A negative amount is a refund or correction and is netted.
#
# The functions return plain dicts that also say how many records were used,
# so an answer can show where its number came from.

from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.database.connection import get_connection
from app.services import trace

ZERO = Decimal("0")
CENT = Decimal("0.01")
EXPENSE_ROWS = "user_id = ? AND COALESCE(category, '') != 'Income'"


def dec(value):
    """A stored amount as an exact Decimal (None for a missing or invalid amount)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def money(value):
    """A Decimal rounded to cents for display or JSON (half-up, like a receipt)."""
    return float(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def percent(part, whole):
    """part / whole x 100, or None when the whole is zero."""
    whole = Decimal(whole)
    if whole == 0:
        return None
    return money(Decimal(part) / whole * 100)


def _rows(sql, args):
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    trace.count_query(len(rows))        # processing diagnostics: counts only
    return rows


# ---------- periods ----------

def shift_month(month, k):
    """'2026-10' shifted by k months ('2026-08' for k = -2)."""
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + mon - 1 + k
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def months_back(month, n):
    """The n months ending with `month`, oldest first."""
    return [shift_month(month, -k) for k in range(n - 1, -1, -1)]


def expense_months(user_id):
    rows = _rows(f"SELECT DISTINCT substr(date, 1, 7) AS m FROM transactions WHERE {EXPENSE_ROWS}"
                 " AND COALESCE(date, '') != '' ORDER BY m", (user_id,))
    return [r["m"] for r in rows]


def current_month(user_id, today=None):
    """The month "this month" means: the calendar month when it has spending,
    otherwise the latest month with spending (the month the dashboard shows)."""
    months = expense_months(user_id)
    if not months:
        return None
    this = (today or date.today()).strftime("%Y-%m")
    return this if this in months else months[-1]


# ---------- spending ----------

def spending(user_id, months):
    """Expenses in the given months: total, per category, per subcategory, per
    month, and what was netted or flagged on the way."""
    months = list(months)
    out = {"months": months, "total": ZERO, "count": 0, "by_category": {}, "by_subcategory": {},
           "by_month": {m: ZERO for m in months}, "refunds": 0, "invalid_amounts": 0,
           "possible_duplicates": 0, "transactions": []}
    if not months:
        return out
    marks = ",".join("?" * len(months))
    rows = _rows(f"SELECT id, description, amount, category, subcategory, date FROM transactions"
                 f" WHERE {EXPENSE_ROWS} AND substr(date, 1, 7) IN ({marks})", (user_id, *months))
    seen = Counter()
    for r in rows:
        amount = dec(r["amount"])
        if amount is None:
            out["invalid_amounts"] += 1
            continue
        if amount < 0:
            out["refunds"] += 1
        category = r["category"] or "Others"
        sub = r["subcategory"] or ""
        month = (r["date"] or "")[:7]
        out["total"] += amount
        out["count"] += 1
        out["by_category"][category] = out["by_category"].get(category, ZERO) + amount
        subs = out["by_subcategory"].setdefault(category, {})
        subs[sub] = subs.get(sub, ZERO) + amount
        out["by_month"][month] = out["by_month"].get(month, ZERO) + amount
        out["transactions"].append({**r, "amount": amount, "category": category, "subcategory": sub})
        # Same day, amount, description and category: shown, never dropped (two
        # identical coffees are real), so the user can check it.
        seen[(r["date"], amount, (r["description"] or "").strip().lower(), category, sub)] += 1
    out["possible_duplicates"] = sum(n - 1 for n in seen.values() if n > 1)
    return out


def ranking(by_category, total):
    """Categories from the biggest down, with their share of the total."""
    items = sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"category": c, "amount": a, "percent": percent(a, total)} for c, a in items]


def subcategory_changes(before, after):
    """(category, subcategory, before, after, change) for every line that moved."""
    keys = {(c, s) for side in (before, after) for c, subs in side.items() for s in subs}
    out = []
    for c, s in keys:
        a = before.get(c, {}).get(s, ZERO)
        b = after.get(c, {}).get(s, ZERO)
        if a != b:
            out.append((c, s, a, b, b - a))
    return sorted(out, key=lambda x: (-abs(x[4]), x[0], x[1]))


# ---------- income and savings ----------

def income(user_id, month):
    """Income for one month. When nothing is dated in that month, the app's
    monthly income (the latest month with income) is used and the answer says so."""
    rows = _rows("SELECT amount, source FROM income WHERE user_id = ? AND substr(date, 1, 7) = ?",
                 (user_id, month))
    basis, used_month = "recorded in the month", month
    if not rows:
        latest = _rows("SELECT substr(COALESCE(date, ''), 1, 7) AS m FROM income WHERE user_id = ?"
                       " GROUP BY m ORDER BY m DESC LIMIT 1", (user_id,))
        if not latest:
            return {"total": None, "by_source": {}, "basis": "no income recorded", "month": None,
                    "count": 0}
        used_month = latest[0]["m"]
        rows = _rows("SELECT amount, source FROM income WHERE user_id = ?"
                     " AND substr(COALESCE(date, ''), 1, 7) = ?", (user_id, used_month))
        basis = "latest monthly income on record"
    total, by_source = ZERO, {}
    for r in rows:
        amount = dec(r["amount"]) or ZERO
        total += amount
        source = r["source"] or "Other"
        by_source[source] = by_source.get(source, ZERO) + amount
    return {"total": total, "by_source": by_source, "basis": basis, "month": used_month or None,
            "count": len(rows)}


def recorded_savings(user_id):
    """Money the user has actually put aside: the savings balance on their profile
    and the amounts saved toward goals. Not the same as what is left this month."""
    user = _rows("SELECT current_savings FROM users WHERE id = ?", (user_id,))
    goals = _rows("SELECT name, current_amount FROM financial_goals WHERE user_id = ?", (user_id,))
    balance = dec(user[0]["current_savings"]) if user else None
    in_goals = sum((dec(g["current_amount"]) or ZERO for g in goals), ZERO)
    return {"profile_balance": balance or ZERO, "saved_in_goals": in_goals,
            "goals": {g["name"]: dec(g["current_amount"]) or ZERO for g in goals}}


def month_snapshot(user_id, month):
    """Income, expenses and what is left in one month: remaining = income - expenses,
    saving rate = remaining / income x 100."""
    spent = spending(user_id, [month])
    earned = income(user_id, month)
    snap = {"month": month, "spending": spent, "income": earned, "expenses": spent["total"],
            "remaining": None, "saving_rate": None}
    if earned["total"] is not None:
        snap["remaining"] = earned["total"] - spent["total"]
        snap["saving_rate"] = percent(snap["remaining"], earned["total"])
    return snap
