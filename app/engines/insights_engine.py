# app/engines/insights_engine.py
# Evidence-based advice cards for the "Your plan" page. Each card says what was
# found, the numbers behind it, one concrete action, the calculated effect, how
# it helps the goal, and a difficulty/priority from the rules below.
#
# Nothing here invents a figure: every number comes from analyze_finances(),
# the user's transactions and preferences, or the existing goal maths. When the
# data cannot support a claim (too little history, no income, no essentials
# marked), the card says what is missing instead.
#
# Transparent rules (all shares, never amounts):
#   money leak      = a flexible subcategory bought >= LEAK_MIN_PER_MONTH times a
#                     month, each purchase <= SMALL_PURCHASE_SHARE of income, seen in
#                     >= LEAK_MIN_WEEKS different weeks of the last LEAK_WINDOW_DAYS
#   rising spending = recent vs earlier monthly average (analysis.compute_trend),
#                     >= INCREASE_MIN_MONTHS months and >= INCREASE_MIN_PCT higher
#   every proposed cut <= MAX_CUT_SHARE of that flexible spending (same rule as
#   the plan); essential (protected) spending is never proposed as a cut
#   difficulty      = cut <= 10% of the spending: easy, <= 20%: medium, else difficult
#   priority        = high: deficit, < 1 month of safety, goal deadline missed or
#                     <= 2 months away and off track; medium: off-track goal, cuts worth
#                     >= MEDIUM_IMPACT_SHARE of income; low: the rest. When the user is
#                     already on track every cut is marked optional and low.

import calendar
import math
from datetime import date, timedelta

from app.database import crud
from app.engines.financial_engine import analyze_finances
from app.engines.goals_engine import _parse_date
from app.finance.analysis import compute_trend
from app.ml.recommendation_rules import MAX_CUT_SHARE
from app.preferences.user_preferences import is_essential, preference_map
from app.services.goal_context import goal_facts, months_to_finish

SMALL_PURCHASE_SHARE = 0.03     # a "small" purchase is at most 3% of monthly income
SMALL_SPENDING_SHARE = 0.05     # ...or 5% of average monthly spending when there is no income
LEAK_WINDOW_DAYS = 90
LEAK_MIN_PER_MONTH = 4          # about once a week or more
LEAK_MIN_WEEKS = 3
INCREASE_MIN_MONTHS = 3
INCREASE_MIN_PCT = 20.0
MIN_HISTORY_MONTHS = 2
MEDIUM_IMPACT_SHARE = 0.02
DEADLINE_SOON_MONTHS = 2
SAFETY_LEVELS = (1, 3)          # months: < 1, 1 to 3, 3 or more
EMERGENCY_WORDS = ("emergency", "طوارئ")
STATUSES = ("accepted", "completed", "dismissed")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CUT_KINDS = ("money_leak", "spending_increase", "deficit")


def _r(x):
    return round(float(x), 2)


def _month(d):
    return f"{d.year:04d}-{d.month:02d}"


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _label(category, sub):
    return f"{category}/{sub}" if sub else category


def cut_difficulty(cut, spending):
    """easy / medium / difficult from the share of the spending that is cut."""
    if spending <= 0:
        return "difficult"
    share = cut / spending
    return "easy" if share <= 0.10 else "medium" if share <= 0.20 else "difficult"


def safety_label(months):
    low, high = SAFETY_LEVELS
    if months is None:
        return "unknown"
    return "under_1" if months < low else "1_to_3" if months < high else "3_plus"


# ---------------------------------------------------------------------------
# shared numbers
# ---------------------------------------------------------------------------

def _context(user_id, reference_date=None, analysis=None):
    today = _parse_date(reference_date) or date.today()
    analysis = analysis or analyze_finances(user_id, reference_date=reference_date)
    prefs = preference_map(user_id)
    expenses = crud.get_expense_transactions(user_id)
    real = [t for t in expenses if not t.get("is_estimate") and _parse_date(t.get("date"))]

    def essential(t):
        return is_essential(user_id, t["category"], t.get("subcategory"), prefs)

    by_month = {}
    for t in real:
        by_month.setdefault(t["date"][:7], []).append(t)
    months = sorted(by_month)

    essential_by_month = {m: _r(sum(t["amount"] for t in ts if essential(t)))
                          for m, ts in by_month.items()}
    current = analysis.get("current_month")
    month_txns = by_month.get(current, [])

    recurring = crud.get_recurring_expenses(user_id)
    seen = {(t["category"], t.get("subcategory") or "") for t in real}
    # A recurring bill that also appears as a transaction is already counted there.
    unrecorded = [r for r in recurring if (r["category"], r.get("subcategory") or "") not in seen]
    essential_avg = _r(sum(essential_by_month.values()) / len(months)) if months else 0.0
    essential_avg += _r(sum(r["amount"] for r in unrecorded
                            if is_essential(user_id, r["category"], r.get("subcategory"), prefs)))

    goal = analysis.get("goal_analysis")
    primary = None
    if goal:
        primary = next((g for g in analysis["goals"] if g["name"] == goal["goal_name"]), None)

    return {
        "user_id": user_id, "today": today, "analysis": analysis, "prefs": prefs,
        "real": real, "by_month": by_month, "months": months, "essential": essential,
        "essential_by_month": essential_by_month, "essential_avg": _r(essential_avg),
        "current_month": current, "month_txns": month_txns, "recurring": recurring,
        "income": float(analysis["income"]), "savings": float(analysis["savings"]),
        "goal": goal, "primary_goal": primary,
        "has_estimates": any(t.get("is_estimate") for t in expenses),
    }


def _goal_link(ctx, monthly_saving):
    """How a monthly saving helps the primary goal: months sooner, or share of the gap."""
    goal, primary = ctx["goal"], ctx["primary_goal"]
    if not goal or not primary or goal["reached"] or monthly_saving <= 0:
        return None
    now = max(ctx["savings"], 0.0)
    before = months_to_finish(goal["remaining"], now)
    after = months_to_finish(goal["remaining"], now + monthly_saving)
    link = {"goal_name": goal["goal_name"], "monthly_saving": _r(monthly_saving),
            "months_before": before, "months_after": after}
    if goal["saving_gap"] > 0:
        link["gap"] = goal["saving_gap"]
        link["share_of_gap_pct"] = round(min(monthly_saving / goal["saving_gap"], 1) * 100, 1)
    if before is None:
        link["type"] = "rate"
        link["text"] = (f"puts {monthly_saving:,.2f} a month toward '{goal['goal_name']}', "
                        f"which is reached in {after} month(s) at that rate")
    elif after < before:
        link["type"] = "sooner"
        link["text"] = (f"'{goal['goal_name']}' is reached in {after} month(s) instead of "
                        f"{before} at the current saving rate")
    elif "gap" in link:
        link["type"] = "gap"
        link["text"] = (f"covers {link['share_of_gap_pct']:.0f}% of the {link['gap']:,.2f} a month "
                        f"still missing for '{goal['goal_name']}'")
    else:
        link["type"] = "toward"
        link["text"] = f"adds {monthly_saving:,.2f} a month toward '{goal['goal_name']}'"
    return link


def _card(key, kind, title, summary, **fields):
    card = {"key": key, "kind": kind, "title": title, "summary": summary,
            "evidence": {}, "action": None, "impact": None, "goal_connection": None,
            "difficulty": None, "priority": "low", "optional": False, "tracking": None}
    card.update(fields)
    return card


# ---------------------------------------------------------------------------
# the cards
# ---------------------------------------------------------------------------

def _data_cards(ctx):
    """No income / not enough history: say what is missing instead of guessing."""
    cards = []
    a = ctx["analysis"]
    if ctx["income"] <= 0 and a["expenses"] > 0:
        cards.append(_card(
            "no_income", "no_income", "No income recorded",
            f"{a['expenses']:,.2f} of spending is recorded for {ctx['current_month']} but no "
            "income, so saving, budget and goal figures cannot be calculated.",
            evidence={"month": ctx["current_month"], "spent": a["expenses"]},
            action="Add your income on the Income & bills page.", priority="high"))
    if len(ctx["months"]) < MIN_HISTORY_MONTHS:
        cards.append(_card(
            "history", "insufficient_history", "Not enough history yet",
            f"Only {len(ctx['months'])} month(s) of recorded spending"
            + (" (plus the estimates you entered)" if ctx["has_estimates"] else "")
            + f". Rising categories and repeated purchases need at least "
            f"{MIN_HISTORY_MONTHS} months.",
            evidence={"months": len(ctx["months"]), "needed": MIN_HISTORY_MONTHS,
                      "estimates_only": ctx["has_estimates"] and not ctx["months"]},
            action="Keep recording spending; these insights appear by themselves."))
    return cards


def _deficit_card(ctx):
    a = ctx["analysis"]
    if ctx["income"] <= 0 or ctx["savings"] >= 0:
        return None
    short = _r(-ctx["savings"])
    flexible = {}
    for t in ctx["month_txns"]:
        if not ctx["essential"](t):
            key = _label(t["category"], t.get("subcategory"))
            flexible[key] = flexible.get(key, 0.0) + t["amount"]
    capacity = _r(sum(flexible.values()) * MAX_CUT_SHARE)
    cut = _r(min(short, capacity))
    left = _r(short - cut)
    action = (f"Cut {cut:,.2f} from flexible spending this month"
              + (f"; the other {left:,.2f} has to come from extra income or a review of "
                 "essential bills" if left > 0 else "") + ".")
    return _card(
        "deficit", "deficit", "Spending is above income this month",
        f"In {ctx['current_month']} you spent {a['expenses']:,.2f} against {ctx['income']:,.2f} "
        f"of income: {short:,.2f} more than came in.",
        evidence={"month": ctx["current_month"], "income": ctx["income"],
                  "spent": a["expenses"], "shortfall": short,
                  "flexible_spending": _r(sum(flexible.values())),
                  "largest_flexible": sorted(flexible, key=lambda k: -flexible[k])[:3]},
        action=action,
        impact={"monthly_saving": cut, "remaining": left,
                "assumption": f"at most {MAX_CUT_SHARE:.0%} of flexible spending can be cut; "
                              "essential spending is never cut"},
        difficulty="difficult" if left > 0 else "medium", priority="high")


def find_money_leaks(ctx):
    """Repeated small purchases in one flexible subcategory (see the rules at the top)."""
    if not ctx["real"]:
        return []
    last = max(_parse_date(t["date"]) for t in ctx["real"])
    start = last - timedelta(days=LEAK_WINDOW_DAYS - 1)
    window = [t for t in ctx["real"] if _parse_date(t["date"]) >= start]
    months_in_window = len({t["date"][:7] for t in window}) or 1
    if ctx["income"] > 0:
        small = ctx["income"] * SMALL_PURCHASE_SHARE
    else:
        small = ctx["analysis"]["spending_patterns"]["average_monthly_spending"] * SMALL_SPENDING_SHARE
    recurring = {(r["category"], r.get("subcategory") or "") for r in ctx["recurring"]}

    groups = {}
    for t in window:
        key = (t["category"], t.get("subcategory") or "")
        if key in recurring or ctx["essential"](t) or not (0 < t["amount"] <= small):
            continue
        groups.setdefault(key, []).append(t)

    leaks = []
    for (category, sub), ts in groups.items():
        per_month = len(ts) / months_in_window
        weeks = {_parse_date(t["date"]).isocalendar()[:2] for t in ts}
        if per_month < LEAK_MIN_PER_MONTH or len(weeks) < LEAK_MIN_WEEKS:
            continue
        total = sum(t["amount"] for t in ts)
        average = total / len(ts)
        monthly_cost = total / months_in_window
        fewer = max(1, math.floor(per_month * MAX_CUT_SHARE))
        dates = sorted(t["date"][:10] for t in ts)
        leaks.append({
            "category": category, "subcategory": sub or None, "count": len(ts),
            "total": _r(total), "average_price": _r(average),
            "months": months_in_window, "first_date": dates[0], "last_date": dates[-1],
            "per_month": round(per_month, 1), "monthly_cost": _r(monthly_cost),
            "suggested_fewer_per_month": fewer,
            "suggested_fewer_per_week": round(fewer / (52 / 12), 1),
            "monthly_saving": _r(fewer * average),
            "small_purchase_limit": _r(small),
        })
    return sorted(leaks, key=lambda x: -x["monthly_saving"])


def _leak_cards(ctx):
    cards = []
    for leak in find_money_leaks(ctx):
        name = leak["subcategory"] or leak["category"]
        saving = leak["monthly_saving"]
        cards.append(_card(
            f"leak:{_label(leak['category'], leak['subcategory'])}", "money_leak",
            f"Repeated {name} purchases",
            f"{leak['count']} {name} purchases from {leak['first_date']} to {leak['last_date']}, "
            f"{leak['total']:,.2f} in total: about {leak['per_month']} a month at "
            f"{leak['average_price']:,.2f} each.",
            evidence=leak,
            action=(f"Make {leak['suggested_fewer_per_month']} fewer {name} purchase(s) a month "
                    f"(about {leak['suggested_fewer_per_week']} a week)."),
            impact={"monthly_saving": saving,
                    "assumption": f"{leak['suggested_fewer_per_month']} x the average price of "
                                  f"{leak['average_price']:,.2f}; prices may change"},
            goal_connection=_goal_link(ctx, saving),
            difficulty=cut_difficulty(saving, leak["monthly_cost"]),
            tracking={"metric": "monthly_spending", "category": leak["category"],
                      "subcategory": leak["subcategory"]}))
    return cards


def _increase_cards(ctx, skip):
    """Subcategories whose monthly spending rose (flexible: a cut; essential: a check)."""
    series = {}
    for m in ctx["months"]:
        for t in ctx["by_month"][m]:
            key = (t["category"], t.get("subcategory") or "")
            series.setdefault(key, {m2: 0.0 for m2 in ctx["months"]})
            series[key][m] += t["amount"]
    cards = []
    if len(ctx["months"]) < INCREASE_MIN_MONTHS:
        return cards
    for (category, sub), values in series.items():
        label = _label(category, sub)
        if label in skip:
            continue
        trend = compute_trend(values)
        # A first purchase (nothing earlier) is not a trend.
        if (trend["direction"] != "increasing" or not trend["earlier_average"]
                or trend["change_percent"] < INCREASE_MIN_PCT):
            continue
        name = sub or category
        now = _r(values.get(ctx["current_month"], 0.0))
        evidence = {"category": category, "subcategory": sub or None,
                    "monthly": {m: _r(v) for m, v in values.items()},
                    "earlier_average": trend["earlier_average"],
                    "recent_average": trend["recent_average"],
                    "change_percent": trend["change_percent"], "this_month": now}
        insight = (f"{name} went from {trend['earlier_average']:,.2f} a month on average to "
                   f"{trend['recent_average']:,.2f} ({trend['change_percent']:+.0f}%).")
        tracking = {"metric": "monthly_spending", "category": category,
                    "subcategory": sub or None}
        if is_essential(ctx["user_id"], category, sub or None, ctx["prefs"]):
            cards.append(_card(
                f"rise:{label}", "essential_increase", f"{name} (essential) is rising", insight,
                evidence=evidence, tracking=tracking,
                action="It is marked essential, so it is never in the cut plan. Check whether "
                       "a price or usage change explains it."))
            continue
        saving = _r(min(trend["recent_average"] - trend["earlier_average"], now * MAX_CUT_SHARE))
        if saving <= 0:
            continue
        target = _r(now - saving)
        evidence["target"] = target
        cards.append(_card(
            f"rise:{label}", "spending_increase", f"{name} spending is rising", insight,
            evidence=evidence,
            action=f"Bring {name} down to about {target:,.2f} a month (this month: {now:,.2f}).",
            impact={"monthly_saving": saving,
                    "assumption": "back toward the earlier average, never more than "
                                  f"{MAX_CUT_SHARE:.0%} of this month's {name} spending"},
            goal_connection=_goal_link(ctx, saving),
            difficulty=cut_difficulty(saving, now), tracking=tracking))
    return cards


def weekly_allowance(ctx):
    """Flexible money left this month -> a weekly figure for the rest of the month.
    Needs real spending: from estimates alone the essentials are unknown."""
    if ctx["income"] <= 0 or not ctx["months"] or ctx["current_month"] not in ctx["by_month"]:
        return None
    goal = ctx["goal"]
    month_txns = ctx["month_txns"]
    recurring = {}
    for r in ctx["recurring"]:
        key = (r["category"], r.get("subcategory") or "")
        if not is_essential(ctx["user_id"], r["category"], r.get("subcategory"), ctx["prefs"]):
            recurring[key] = recurring.get(key, 0.0) + r["amount"]

    essential_now = sum(t["amount"] for t in month_txns if ctx["essential"](t))
    essential = _r(max(essential_now, ctx["essential_avg"]))
    committed = _r(sum(recurring.values()))
    paid = {}
    flexible_spent = 0.0
    for t in month_txns:
        if ctx["essential"](t):
            continue
        key = (t["category"], t.get("subcategory") or "")
        if key in recurring:   # a fixed payment, already reserved as committed
            covered = min(t["amount"], recurring[key] - paid.get(key, 0.0))
            paid[key] = paid.get(key, 0.0) + covered
            flexible_spent += t["amount"] - covered
        else:
            flexible_spent += t["amount"]
    flexible_spent = _r(flexible_spent)

    room = max(ctx["income"] - essential - committed, 0.0)
    saving = _r(min(goal["required_monthly_saving"], room)) if goal and not goal["reached"] else 0.0
    allowance = _r(room - saving)
    left = _r(allowance - flexible_spent)

    year, month = map(int, ctx["current_month"].split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    is_now = _month(ctx["today"]) == ctx["current_month"]
    if is_now:
        days_left = days_in_month - ctx["today"].day + 1
        weekly = _r(max(left, 0) / days_left * 7)
        assumption = f"{days_left} day(s) left in {ctx['current_month']}"
    else:
        days_left = None
        weekly = _r(allowance / days_in_month * 7)
        assumption = (f"{ctx['current_month']} is not the current month, so this is a typical "
                      f"week: the month's allowance split over {days_in_month} days")
    return {"month": ctx["current_month"], "income": ctx["income"],
            "essential": essential, "essential_this_month": _r(essential_now),
            "essential_average": ctx["essential_avg"], "committed": committed,
            "goal_saving": saving, "goal_name": goal["goal_name"] if goal else None,
            "flexible_allowance": allowance, "flexible_spent": flexible_spent,
            "left": left, "days_left": days_left, "weekly": weekly,
            "is_current_month": is_now, "assumption": assumption}


def _weekly_card(ctx):
    w = weekly_allowance(ctx)
    if not w:
        return None
    if w["left"] < 0 and w["is_current_month"]:
        action = (f"Flexible spending is already {-w['left']:,.2f} over this month's allowance; "
                  "keep further flexible purchases to a minimum until the month ends.")
        difficulty, priority = "difficult", "medium"
    else:
        action = f"Keep flexible spending to about {w['weekly']:,.2f} a week."
        difficulty, priority = "medium", "low"
    return _card(
        "weekly", "weekly_allowance", "Weekly spending allowance",
        f"From {w['income']:,.2f} of income, after essentials ({w['essential']:,.2f}), fixed "
        f"payments ({w['committed']:,.2f}) and goal saving ({w['goal_saving']:,.2f}), "
        f"{w['flexible_allowance']:,.2f} is left for flexible spending in {w['month']}; "
        f"{w['flexible_spent']:,.2f} of it is spent.",
        evidence=w, action=action,
        impact={"monthly_saving": None,
                "assumption": "essentials use this month or your average month, whichever is "
                              "higher; " + w["assumption"]},
        difficulty=difficulty, priority=priority)


def goal_plan(goal_row, monthly_saving, today):
    """Remaining amount, required contribution, ETA, milestones and alternatives."""
    facts = goal_facts(goal_row, today=today)
    if facts["deadline_status"] == "reached":
        return {**facts, "reachable_on_time": True}
    saving = max(float(monthly_saving), 0.0)
    remaining = facts["remaining_amount"]
    months = months_to_finish(remaining, saving)
    start = today.replace(day=1)
    eta = _month(_add_months(start, months)) if months is not None else None
    required = facts.get("required_monthly_saving")
    on_time = None
    if facts["deadline_status"] == "upcoming":
        on_time = saving >= required
    elif facts["deadline_status"] == "deadline passed":
        on_time = False

    milestones = []
    target, saved = facts["target_amount"], facts["saved_so_far"]
    for pct in (25, 50, 75, 100):
        amount = _r(target * pct / 100)
        if amount <= saved:
            continue
        need = months_to_finish(amount - saved, saving)
        milestones.append({"percent": pct, "amount": amount,
                           "month": _month(_add_months(start, need)) if need is not None else None})

    alternatives = []
    if on_time is False or (on_time is None and months is None):
        if required:
            alternatives.append({"option": "save_more", "monthly": required,
                                 "extra_per_month": _r(required - saving)})
        if eta:
            alternatives.append({"option": "move_date", "new_month": eta, "monthly": _r(saving)})
    return {**facts, "monthly_saving": _r(saving), "months_at_current_rate": months,
            "estimated_completion": eta, "reachable_on_time": on_time,
            "milestones": milestones, "alternatives": alternatives}


def _goal_cards(ctx):
    cards = []
    primary = ctx["primary_goal"]
    for g in ctx["analysis"]["goals"]:
        if g["current_amount"] >= g["target_amount"] or g["target_amount"] <= 0:
            continue
        p = goal_plan(g, ctx["savings"], ctx["today"])
        is_primary = primary is not None and g["id"] == primary["id"]
        soon = p.get("months_left") is not None and p["months_left"] <= DEADLINE_SOON_MONTHS
        if not is_primary and not soon and p["deadline_status"] != "deadline passed":
            continue
        if p["deadline_status"] == "deadline passed":
            priority = "high"
        elif p["reachable_on_time"] is False:
            priority = "high" if soon else "medium"
        else:
            priority = "low"
        when = (f"reaching it by {p['target_date']} needs {p['required_monthly_saving']:,.2f} a month"
                if p["deadline_status"] == "upcoming"
                else "its date has passed" if p["deadline_status"] == "deadline passed"
                else "no deadline is saved")
        eta = (f"at the current {p['monthly_saving']:,.2f} a month it is reached in "
               f"{p['months_at_current_rate']} month(s), around {p['estimated_completion']}"
               if p["months_at_current_rate"] is not None
               else "nothing is left over each month, so it cannot be dated yet")
        if p["reachable_on_time"]:
            action = f"Keep putting at least {p['required_monthly_saving']:,.2f} a month toward it."
            difficulty = "easy"
        elif p["alternatives"]:
            parts = [f"save {a['extra_per_month']:,.2f} more a month" if a["option"] == "save_more"
                     else f"move the date to about {a['new_month']}" for a in p["alternatives"]]
            action = "Either " + " or ".join(parts) + ". The goal is not changed automatically."
            extra = next((a["extra_per_month"] for a in p["alternatives"]
                          if a["option"] == "save_more"), None)
            difficulty = ("difficult" if extra is None or extra > ctx["income"] * 0.10
                          else "medium")
        else:
            action = "Set a target date to see what each month needs."
            difficulty = "easy"
        cards.append(_card(
            f"goal:{g['id']}", "goal_plan", f"Goal: {g['name']}",
            f"{p['remaining_amount']:,.2f} left of {p['target_amount']:,.2f} "
            f"({p['percent_complete']:.0f}% saved); {when}; {eta}.",
            evidence=p, action=action, difficulty=difficulty, priority=priority,
            tracking={"metric": "goal_saved", "goal_id": g["id"]}))
    return cards


def _surplus_card(ctx):
    goal, primary = ctx["goal"], ctx["primary_goal"]
    if ctx["savings"] <= 0 or not goal or not primary or goal["reached"]:
        return None
    move = _r(min(ctx["savings"], goal["remaining"]))
    before = goal["percent_complete"]
    after = round(min((primary["current_amount"] + move) / primary["target_amount"] * 100, 100), 1)
    return _card(
        f"surplus:{primary['id']}", "surplus", "Put this month's surplus to work",
        f"You kept {ctx['savings']:,.2f} in {ctx['current_month']}.",
        evidence={"kept": _r(ctx["savings"]), "month": ctx["current_month"],
                  "goal_name": goal["goal_name"], "goal_id": primary["id"],
                  "percent_before": before, "percent_after": after, "amount": move},
        action=(f"If it is not already in '{goal['goal_name']}', add {move:,.2f} on the Goals "
                f"page: progress goes from {before:.0f}% to {after:.0f}%."),
        difficulty="easy")


def emergency_safety(ctx):
    """Months of essential spending that genuinely available savings cover."""
    user = crud.get_user(ctx["user_id"]) or {}
    liquid = _r(user.get("current_savings") or 0.0)
    emergency_goals = [g for g in ctx["analysis"]["goals"]
                       if any(w in g["name"].lower() for w in EMERGENCY_WORDS)]
    in_emergency_goals = _r(sum(g["current_amount"] or 0 for g in emergency_goals))
    available = _r(liquid + in_emergency_goals)
    essential = ctx["essential_avg"]
    months = round(available / essential, 1) if essential > 0 else None
    result = {"liquid_savings": liquid, "emergency_goal_savings": in_emergency_goals,
              "emergency_goals": [g["name"] for g in emergency_goals],
              "available": available, "essential_monthly": essential,
              "months_covered": months, "label": safety_label(months),
              "missing": None if essential > 0 else "essential_spending"}
    if months is not None:
        nxt = next((lvl for lvl in SAFETY_LEVELS if months < lvl), None)
        result["next_level_months"] = nxt
        result["needed_for_next_level"] = _r(nxt * essential - available) if nxt else 0.0
    return result


def _safety_card(ctx):
    s = emergency_safety(ctx)
    if s["missing"]:
        return _card(
            "safety", "safety", "Emergency safety",
            "Essential spending is unknown, so emergency cover cannot be measured.",
            evidence=s, action="Mark your essential categories on the Essential spending page "
                               "and record your bills.")
    words = {"under_1": "less than one month", "1_to_3": "one to three months",
             "3_plus": "three months or more"}
    if s["next_level_months"]:
        action = (f"Building it to {s['next_level_months']} month(s) needs "
                  f"{s['needed_for_next_level']:,.2f} more.")
        monthly = max(ctx["savings"], 0)
        need = months_to_finish(s["needed_for_next_level"], monthly)
        difficulty = ("difficult" if need is None
                      else "easy" if need <= 3 else "medium" if need <= 12 else "difficult")
    else:
        action, difficulty = "Your cover meets the 3-month level; keep it separate from goals.", "easy"
    return _card(
        "safety", "safety", "Emergency safety",
        f"Available savings of {s['available']:,.2f} cover {s['months_covered']} month(s) of "
        f"essential spending ({s['essential_monthly']:,.2f} a month): {words[s['label']]}.",
        evidence=s, action=action, difficulty=difficulty,
        priority="high" if s["label"] == "under_1" else "low",
        impact={"monthly_saving": None,
                "assumption": "counts the savings entered in Setup and goals named for "
                              "emergencies; money in other goals is not counted. This is an "
                              "indicator, not a guarantee"})


def _income_card(ctx):
    income = ctx["analysis"]["income_analysis"]
    values = list((income.get("monthly_income") or {}).values())
    spread = income.get("stability", {}).get("month_to_month_spread_percent")
    if len(values) < 3 or spread is None or spread <= 15:
        return None
    low, high = min(values), max(values)
    return _card(
        "income", "irregular_income", "Your income changes month to month",
        f"Monthly income ranged from {low:,.2f} to {high:,.2f} over {len(values)} months "
        f"({spread:.0f}% spread).",
        evidence={"lowest": _r(low), "highest": _r(high), "months": len(values),
                  "spread_percent": spread, "latest": _r(values[-1])},
        action=f"Plan the month on your lowest income ({low:,.2f}) and send anything above it "
               "to savings.",
        impact={"monthly_saving": None,
                "assumption": f"in a month like the latest one that leaves {values[-1] - low:,.2f} "
                              "as a buffer"},
        difficulty="medium", priority="medium")


# ---------------------------------------------------------------------------
# tracking: one status row per (user, card key); refreshing never duplicates
# ---------------------------------------------------------------------------

def _tracked_value(ctx, tracking):
    if not tracking:
        return None
    if tracking["metric"] == "goal_saved":
        goal = next((g for g in ctx["analysis"]["goals"] if g["id"] == tracking["goal_id"]), None)
        return _r(goal["current_amount"]) if goal else None
    return _r(sum(t["amount"] for t in ctx["month_txns"]
                  if t["category"] == tracking["category"]
                  and (t.get("subcategory") or None) == tracking["subcategory"]))


def _attach_status(ctx, cards):
    rows = {r["rec_key"]: r for r in crud.get_recommendation_statuses(ctx["user_id"])}
    for card in cards:
        row = rows.get(card["key"])
        card["status"] = row["status"] if row else "new"
        card["tracking_value"] = _tracked_value(ctx, card["tracking"])
        if row and row["baseline"] is not None and card["tracking_value"] is not None:
            card["progress"] = {"baseline": row["baseline"], "since": row["month"],
                                "now": card["tracking_value"], "month": ctx["current_month"],
                                "change": _r(card["tracking_value"] - row["baseline"])}


def set_status(user_id, key, status, reference_date=None):
    """Mark one of this user's current cards accepted / completed / dismissed, or
    'new' to clear it. Returns the card, or None when the key is not one of theirs."""
    if status not in STATUSES + ("new",):
        raise ValueError(f"status must be one of {STATUSES + ('new',)}")
    result = build_insights(user_id, reference_date=reference_date)
    card = next((c for c in result["insights"] + result["dismissed"] if c["key"] == key), None)
    if card is None:
        return None
    if status == "new":
        crud.delete_recommendation_status(user_id, key)
    else:
        crud.set_recommendation_status(user_id, key, status, card.get("tracking_value"),
                                       result["month"])
    return {**card, "status": status}


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def build_insights(user_id, reference_date=None, analysis=None, plan=None):
    """All cards for one user, ranked; dismissed ones are returned separately."""
    ctx = _context(user_id, reference_date, analysis)
    a = ctx["analysis"]
    if not a["income"] and not a["expenses"] and not a["goals"]:
        return {"user_id": user_id, "month": None, "insights": [], "dismissed": [],
                "on_track": None, "empty": True}

    cards = _data_cards(ctx)
    cards += [c for c in (_deficit_card(ctx),) if c]
    leaks = _leak_cards(ctx)
    cards += leaks
    cards += _increase_cards(ctx, skip={c["key"].split(":", 1)[1] for c in leaks})
    cards += [c for c in (_weekly_card(ctx), _surplus_card(ctx), _safety_card(ctx),
                          _income_card(ctx)) if c]
    cards += _goal_cards(ctx)

    goal = ctx["goal"]
    maintain = plan["maintain_current_plan"] if plan else None
    on_track = (maintain if maintain is not None
                else ctx["savings"] >= 0 and (not goal or goal["on_track"]))
    for card in cards:
        saving = (card.get("impact") or {}).get("monthly_saving")
        if card["kind"] in CUT_KINDS and card["kind"] != "deficit":
            if on_track:
                # Never a required cut next to "keep your current plan".
                card["optional"], card["priority"] = True, "low"
            elif saving and ctx["income"] > 0 and saving >= ctx["income"] * MEDIUM_IMPACT_SHARE:
                card["priority"] = "medium"

    _attach_status(ctx, cards)
    cards.sort(key=lambda c: (PRIORITY_ORDER[c["priority"]],
                              -((c.get("impact") or {}).get("monthly_saving") or 0)))
    return {"user_id": user_id, "month": ctx["current_month"], "on_track": on_track,
            "insights": [c for c in cards if c["status"] != "dismissed"],
            "dismissed": [c for c in cards if c["status"] == "dismissed"], "empty": False}


def preview_change(user_id, spend_less=0.0, to_goal=None, reference_date=None, analysis=None):
    """What-if: spend less each month and/or put a monthly amount toward the main goal.
    Read-only — nothing is written; the numbers are an estimate, not a promise."""
    ctx = _context(user_id, reference_date, analysis)
    a = ctx["analysis"]
    flexible = _r(sum(t["amount"] for t in ctx["month_txns"] if not ctx["essential"](t)))
    asked = max(float(spend_less or 0), 0.0)
    cut = _r(min(asked, flexible))
    balance = _r(ctx["income"] - a["expenses"] + cut)
    contribution = _r(max(balance, 0.0) if to_goal is None else max(float(to_goal), 0.0))
    result = {
        "month": ctx["current_month"], "income": ctx["income"], "expenses": a["expenses"],
        "flexible_spending": flexible, "spend_less": cut, "spend_less_capped": cut < asked,
        "new_expenses": _r(a["expenses"] - cut), "balance_before": _r(ctx["savings"]),
        "balance_after": balance, "to_goal": contribution,
        "left_after_goal": _r(balance - contribution),
        "assumptions": ["income and other spending stay as in " + str(ctx["current_month"]),
                        "only flexible spending can be cut; essentials are protected",
                        "a preview only: no balance, budget, goal or transaction is changed"],
        "goal": None,
    }
    primary = ctx["primary_goal"]
    if primary and primary["current_amount"] < primary["target_amount"]:
        p = goal_plan(primary, contribution, ctx["today"])
        result["goal"] = {k: p.get(k) for k in (
            "goal_id", "name", "remaining_amount", "target_date", "deadline_status",
            "required_monthly_saving", "months_at_current_rate", "estimated_completion",
            "reachable_on_time", "milestones")}
    return result
