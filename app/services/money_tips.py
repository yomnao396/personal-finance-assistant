# app/services/money_tips.py
# The Money Tips page: a fixed set of practical, actionable tips. The wording
# (title, why, steps, example) lives in ui/i18n.py in English and Arabic; this
# file only adds the user's own numbers to a tip when their data supports it.
#
# A tip with personal == None is shown with its general example; nothing is
# guessed. Every figure comes from the user's transactions, recurring payments,
# income records and goals, through the same helpers as the Plan page
# (insights_engine), so the two pages agree.
# Suggested amounts are only ever a share of money the user already has left
# over or already spends on flexible items, so a tip never asks for more than
# the data shows is available.

from collections import Counter
from datetime import timedelta

from app.database.crud import get_income_records
from app.engines.goals_engine import _parse_date
from app.engines.insights_engine import (
    _add_months,
    _context,
    _month,
    emergency_safety,
    find_money_leaks,
)
from app.services.goal_context import months_to_finish

GROUPS = ["quick_wins", "smart_spending", "goals", "recurring", "habits"]
TIPS = [  # (tip id, group) in display order
    ("leak_check", "quick_wins"),
    ("wait_rule", "quick_wins"),
    ("adaptive_limits", "smart_spending"),
    ("needs_wants", "smart_spending"),
    ("unit_price", "smart_spending"),
    ("goal_boost", "goals"),
    ("pay_first", "goals"),
    ("opportunity_cost", "goals"),
    ("subscription_audit", "recurring"),
    ("bill_calendar", "recurring"),
    ("weekly_review", "habits"),
    ("emergency_ladder", "habits"),
]
PER_MONTH = {"monthly": 1.0, "weekly": 52 / 12, "yearly": 1 / 12}
GOAL_TRIM_SHARE = 0.10      # goal boost: trim 10% of flexible spending
EMERGENCY_SHARE = 0.20      # emergency ladder: 20% of what is left each month
OPPORTUNITY_TRIM = 0.30     # never more than the plan's 30% cut limit
GOOD_SAVING_RATE = 20.0


def _r(x):
    return round(float(x), 2)


def _flexible(ctx, transactions):
    return [t for t in transactions if not ctx["essential"](t)]


def _name(t):
    return t.get("subcategory") or t["category"]


def _monthly_flexible_by_item(ctx):
    """{subcategory or category: {month: amount}} over the months with records."""
    out = {}
    for m in ctx["months"]:
        for t in _flexible(ctx, ctx["by_month"][m]):
            out.setdefault(_name(t), {m2: 0.0 for m2 in ctx["months"]})[m] += t["amount"]
    return out


# ---------------------------------------------------------------------------
# one function per tip: the user's numbers, or None (general version)
# ---------------------------------------------------------------------------

def _leak_check(ctx):
    leaks = find_money_leaks(ctx)
    if leaks:
        x = leaks[0]
        return {"kind": "leak", "name": x["subcategory"] or x["category"], "count": x["count"],
                "first": x["first_date"], "last": x["last_date"], "average": x["average_price"],
                "per_month": x["per_month"]}
    top = sorted(_flexible(ctx, ctx["month_txns"]), key=lambda t: -t["amount"])[:3]
    if not top:
        return None
    return {"kind": "top", "month": ctx["current_month"],
            "items": [{"description": t.get("description") or _name(t), "name": _name(t),
                       "amount": _r(t["amount"])} for t in top]}


def _wait_rule(ctx):
    flexible = _flexible(ctx, ctx["real"])
    if len(flexible) < 5:
        return None
    average = sum(t["amount"] for t in flexible) / len(flexible)
    above = [t for t in _flexible(ctx, ctx["month_txns"]) if t["amount"] > average]
    return {"threshold": _r(average), "count": len(above),
            "total": _r(sum(t["amount"] for t in above)), "month": ctx["current_month"]}


def _adaptive_limits(ctx):
    if len(ctx["months"]) < 2:
        return None
    items = []
    for name, months in _monthly_flexible_by_item(ctx).items():
        now = months.get(ctx["current_month"], 0.0)
        spent = {m: v for m, v in months.items() if v > 0 and m != ctx["current_month"]}
        if not spent or now <= 0:
            continue
        best_month = min(spent, key=spent.get)
        if spent[best_month] < now:
            items.append({"name": name, "now": _r(now), "limit": _r(spent[best_month]),
                          "limit_month": best_month})
    items.sort(key=lambda i: -(i["now"] - i["limit"]))
    return {"items": items[:3], "month": ctx["current_month"]} if items else None


def _needs_wants(ctx):
    month = ctx["month_txns"]
    total = sum(t["amount"] for t in month)
    if total <= 0:
        return None
    flexible = sum(t["amount"] for t in _flexible(ctx, month))
    return {"month": ctx["current_month"], "essential": _r(total - flexible),
            "flexible": _r(flexible), "flexible_pct": round(flexible / total * 100)}


def _goal_boost(ctx):
    goal, primary = ctx["goal"], ctx["primary_goal"]
    if not goal or not primary or goal["reached"] or ctx["savings"] <= 0 or not ctx["months"]:
        return None
    flexible_avg = sum(sum(v.values()) for v in _monthly_flexible_by_item(ctx).values()) \
        / len(ctx["months"])
    extra = round(flexible_avg * GOAL_TRIM_SHARE / 10) * 10
    if extra <= 0:
        return None
    before = months_to_finish(goal["remaining"], ctx["savings"])
    after = months_to_finish(goal["remaining"], ctx["savings"] + extra)
    start = ctx["today"].replace(day=1)
    return {"goal": goal["goal_name"], "remaining": goal["remaining"], "saving": _r(ctx["savings"]),
            "extra": _r(extra), "flexible_average": _r(flexible_avg),
            "months_before": before, "months_after": after, "added": _r(extra * before),
            "date_before": _month(_add_months(start, before)),
            "date_after": _month(_add_months(start, after))}


def _pay_first(ctx):
    if ctx["income"] <= 0 or ctx["savings"] <= 0:
        return None
    records = ctx["analysis"]["income_analysis"]
    days = [d.day for d in (_parse_date(r.get("date")) for r in get_income_records(ctx["user_id"]))
            if d]
    day = Counter(days).most_common(1)[0][0] if days else None
    spread = records.get("stability", {}).get("month_to_month_spread_percent")
    goal = ctx["goal"]
    amount = ctx["savings"]
    if goal and not goal["reached"]:
        amount = min(amount, goal["required_monthly_saving"])
    rate = ctx["savings"] / ctx["income"] * 100
    return {"payday": day, "transfer_day": min(day + 1, 28) if day else None,
            "amount": _r(amount), "rate": round(rate), "irregular": bool(spread and spread > 15),
            "goal": goal["goal_name"] if goal and not goal["reached"] else None,
            "doing_well": rate >= GOOD_SAVING_RATE and bool(not goal or goal["on_track"])}


def _opportunity_cost(ctx):
    goal = ctx["goal"]
    items = _monthly_flexible_by_item(ctx)
    if not items or not goal or goal["reached"]:
        return None
    name = max(items, key=lambda n: sum(items[n].values()))
    monthly = sum(items[name].values()) / len(ctx["months"])
    if monthly <= 0:
        return None
    trimmed_year = monthly * OPPORTUNITY_TRIM * 12
    return {"name": name, "monthly": _r(monthly), "yearly": _r(monthly * 12),
            "goal": goal["goal_name"], "remaining": goal["remaining"],
            "trim_pct": round(OPPORTUNITY_TRIM * 100), "trimmed_yearly": _r(trimmed_year),
            "goal_pct": round(min(trimmed_year / goal["remaining"], 1) * 100)
            if goal["remaining"] else 100}


def _subscription_audit(ctx):
    items = [r for r in ctx["recurring"]
             if not ctx["essential"]({"category": r["category"], "subcategory": r.get("subcategory")})]
    if not items:
        return None
    rows = [{"name": r.get("description") or _name(r), "frequency": r.get("frequency") or "monthly",
             "amount": _r(r["amount"]),
             "monthly": _r(r["amount"] * PER_MONTH.get(r.get("frequency") or "monthly", 1.0))}
            for r in items]
    monthly = sum(r["monthly"] for r in rows)
    return {"items": rows, "monthly": _r(monthly), "yearly": _r(monthly * 12)}


def _bill_calendar(ctx):
    if not ctx["recurring"]:
        return None
    days = {}
    for t in ctx["real"]:
        days.setdefault((t["category"], t.get("subcategory") or ""), []).append(
            _parse_date(t["date"]).day)
    paid_now = {(t["category"], t.get("subcategory") or "") for t in ctx["month_txns"]}
    is_now = ctx["current_month"] == _month(ctx["today"])
    rows, upcoming = [], []
    for r in ctx["recurring"]:
        key = (r["category"], r.get("subcategory") or "")
        day = Counter(days[key]).most_common(1)[0][0] if key in days else None
        monthly = _r(r["amount"] * PER_MONTH.get(r.get("frequency") or "monthly", 1.0))
        rows.append({"name": r.get("description") or _name(r), "monthly": monthly, "day": day})
        if is_now and key not in paid_now:
            upcoming.append(rows[-1]["name"])
    rows.sort(key=lambda x: (x["day"] is None, x["day"] or 0))
    return {"items": rows, "total": _r(sum(x["monthly"] for x in rows)), "upcoming": upcoming}


def _weekly_review(ctx):
    if not ctx["real"]:
        return None
    last = max(_parse_date(t["date"]) for t in ctx["real"])
    week = [t for t in ctx["real"] if _parse_date(t["date"]) > last - timedelta(days=7)]
    by_name = Counter()
    for t in week:
        by_name[_name(t)] += t["amount"]
    top, top_amount = by_name.most_common(1)[0]
    return {"start": (last - timedelta(days=6)).isoformat(), "end": last.isoformat(),
            "count": len(week), "total": _r(sum(t["amount"] for t in week)),
            "top": top, "top_amount": _r(top_amount)}


def _emergency_ladder(ctx):
    s = emergency_safety(ctx)
    if s["missing"]:
        return None
    result = {"available": s["available"], "essential": s["essential_monthly"],
              "months": s["months_covered"], "doing_well": not s.get("next_level_months")}
    if s.get("next_level_months"):
        step = _r(s["needed_for_next_level"])
        monthly = _r(ctx["savings"] * EMERGENCY_SHARE) if ctx["savings"] > 0 else 0.0
        result.update(next_months=s["next_level_months"], step=step, monthly=monthly,
                      share_pct=round(EMERGENCY_SHARE * 100),
                      months_to_step=months_to_finish(step, monthly) if monthly > 0 else None)
    return result


BUILDERS = {
    "leak_check": _leak_check, "wait_rule": _wait_rule, "adaptive_limits": _adaptive_limits,
    "needs_wants": _needs_wants, "unit_price": lambda ctx: None, "goal_boost": _goal_boost,
    "pay_first": _pay_first, "opportunity_cost": _opportunity_cost,
    "subscription_audit": _subscription_audit, "bill_calendar": _bill_calendar,
    "weekly_review": _weekly_review, "emergency_ladder": _emergency_ladder,
}


def money_tips(user_id, reference_date=None):
    """Every tip with the user's numbers where available. Tips using the user's
    data come first; tips for something the user already does well come last."""
    ctx = _context(user_id, reference_date)
    tips = []
    for order, (tip_id, group) in enumerate(TIPS):
        try:
            personal = BUILDERS[tip_id](ctx)
        except Exception:  # noqa: BLE001 — one tip must never break the page
            personal = None
        doing_well = bool(personal and personal.pop("doing_well", False))
        tips.append({"id": tip_id, "group": group, "personal": personal,
                     "doing_well": doing_well, "order": order})
    tips.sort(key=lambda tp: (tp["doing_well"], tp["personal"] is None, tp["order"]))
    return {"user_id": user_id, "month": ctx["current_month"], "groups": GROUPS, "tips": tips}
