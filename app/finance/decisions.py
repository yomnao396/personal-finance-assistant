# app/finance/decisions.py
# Decision support: can I afford it, how do I pay for an emergency, which goal
# comes first, where can I really save. Deterministic: every figure is read from
# the user's records (facts.py, saved goals, essential/flexible preferences),
# the arithmetic is Decimal, and the few rules are named constants below.
#
# The money buckets are kept apart, so total savings are never mistaken for
# money that is free to spend:
#   surplus          income − expenses in the month (what is left after spending)
#   goal_need        what the dated goals need each month to stay on time
#   free_monthly     surplus − goal_need: money no plan is counting on yet
#   savings_balance  the savings balance on the profile (outside any goal)
#   safety_floor     SAFETY_FLOOR_MONTHS x essential monthly spending, kept for
#                    emergencies; the emergency goal's savings count toward it
#   free_savings     the part of savings_balance above what the floor still needs
#   in_goals         money saved inside goals: earmarked, spending it delays them
#
# A cost is covered from these buckets in a fixed order (funding()), and the
# verdict says which buckets it had to touch:
#   affordable                   unallocated money and free savings only
#   affordable_with_adjustments  also this month's goal contributions or cuts to
#                                flexible spending (never essential spending)
#   risky                        also the safety floor / emergency savings
#   not_affordable               even all of that is not enough
#   insufficient_information     no income recorded, or no price given
#
# Nothing here writes to the database: scenarios are calculated, never saved.

import calendar
import math
from decimal import ROUND_HALF_UP, Decimal

from app.database import crud
from app.finance import facts, verify
from app.finance.facts import ZERO, dec
from app.ml.recommendation_rules import MAX_CUT_SHARE
from app.preferences.user_preferences import is_essential, preference_map
from app.services.goal_context import goal_facts

SAFETY_FLOOR_MONTHS = Decimal("1")      # keep at least one month of essential spending
EMERGENCY_TARGET_MONTHS = Decimal("3")  # the usual emergency-fund guideline (shown, never imposed)
CUT_SHARE = Decimal(str(MAX_CUT_SHARE))  # at most 30% of a flexible line, as in the plan
HISTORY_MONTHS = 3                      # earlier months a saving plan compares with
EMERGENCY_WORDS = ("emergency", "طوارئ", "طارئ")

VERDICTS = ("affordable", "affordable_with_adjustments", "risky", "not_affordable",
            "insufficient_information")

# Funding order for something the user chooses to buy, and for an emergency
# (an emergency is what the safety money is for, so it comes before the goals).
PURCHASE_ORDER = ("unallocated", "free_savings", "goal_contributions", "flexible_cuts", "safety")
EMERGENCY_ORDER = ("unallocated", "free_savings", "safety", "goal_contributions", "flexible_cuts")
ADJUSTMENTS = {"goal_contributions", "flexible_cuts"}


def as_json(value):
    """Decimals as floats (rounded to cents) and tuple keys as text, for JSON."""
    if isinstance(value, Decimal):
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, dict):
        return {str(k) if isinstance(k, tuple) else k: as_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_json(v) for v in value]
    return value


def _pos(x):
    return x if x > 0 else ZERO


def months_needed(amount, per_month):
    """Whole months to put `amount` aside at `per_month` (None if it never gets there)."""
    amount, per_month = Decimal(amount), Decimal(per_month)
    if amount <= 0:
        return 0
    if per_month <= 0:
        return None
    return math.ceil(amount / per_month)


def is_emergency_goal(goal_name):
    name = (goal_name or "").lower()
    return any(w in name for w in EMERGENCY_WORDS)


# ---------- the verified picture ----------

def position(user_id, month=None):
    """Everything a decision needs, for one user and the month the figures come from."""
    month = month or facts.current_month(user_id)
    snap = facts.month_snapshot(user_id, month) if month else None
    income = snap["income"]["total"] if snap else None
    expenses = snap["expenses"] if snap else ZERO
    prefs = preference_map(user_id)
    lines, essential, flexible = {}, ZERO, ZERO
    for t in (snap["spending"]["transactions"] if snap else []):
        key = (t["category"], t["subcategory"])
        lines[key] = lines.get(key, ZERO) + t["amount"]
    flexible_lines = {}
    for (c, s), a in lines.items():
        if is_essential(user_id, c, s, prefs):
            essential += a
        else:
            flexible += a
            if a > 0:
                flexible_lines[(c, s)] = a
    user = crud.get_user(user_id) or {}
    balance = dec(user.get("current_savings")) or ZERO
    goals = []
    for g in crud.get_goals(user_id):
        f = goal_facts(g)
        if f["deadline_status"] == "reached":
            continue
        f["emergency"] = is_emergency_goal(g["name"])
        goals.append(f)
    goal_need = sum((dec(f["required_monthly_saving"]) for f in goals
                     if f.get("required_monthly_saving") is not None), ZERO)
    emergency_saved = sum((dec(f["saved_so_far"]) for f in goals if f["emergency"]), ZERO)
    in_goals = sum((dec(f["saved_so_far"]) for f in goals), ZERO)
    floor = SAFETY_FLOOR_MONTHS * essential
    floor_from_balance = min(balance, _pos(floor - emergency_saved))
    surplus = None if income is None else income - expenses
    missing = []
    if income is None:
        missing.append("income")
    if not lines:
        missing.append("expenses")
    pos = {
        "user_id": user_id, "month": month, "income": income, "expenses": expenses,
        "income_basis": snap["income"]["basis"] if snap else None,
        "surplus": surplus, "essential": essential, "flexible": flexible,
        "flexible_lines": flexible_lines, "cuttable": sum((a * CUT_SHARE for a in flexible_lines.values()),
                                                          ZERO).quantize(Decimal(1)),
        "savings_balance": balance, "in_goals": in_goals, "emergency_saved": emergency_saved,
        "safety_floor": floor, "free_savings": balance - floor_from_balance,
        "safety_money": floor_from_balance + emergency_saved,
        "goals": goals, "goal_need": goal_need,
        "free_monthly": None if surplus is None else surplus - goal_need,
        "recurring": [{"description": r["description"], "amount": dec(r["amount"]) or ZERO,
                       "frequency": r.get("frequency")} for r in crud.get_recurring_expenses(user_id)],
        "missing": missing,
        # Past their date and not reached: no monthly need can be worked out, so they
        # are listed (and their remaining amount shown) instead of silently left out.
        "overdue": [g for g in goals if g["deadline_status"] == "deadline passed"],
    }
    pos["emergency_months"] = (None if essential <= 0 else
                               ((balance + emergency_saved) / essential).quantize(Decimal("0.1")))
    return pos


def without_goal(pos, name):
    """The position once the goal `name` is spent on what it was saved for: its
    savings pay first, and its monthly need stops."""
    goal = next((g for g in pos["goals"] if g["name"] == name), None)
    if goal is None:
        return pos, ZERO
    need = dec(goal.get("required_monthly_saving") or 0) or ZERO
    saved = dec(goal["saved_so_far"]) or ZERO
    goal_need = pos["goal_need"] - need
    return {**pos, "goals": [g for g in pos["goals"] if g["name"] != name], "goal_need": goal_need,
            "in_goals": pos["in_goals"] - saved,
            "free_monthly": None if pos["surplus"] is None else pos["surplus"] - goal_need}, saved


# ---------- covering a cost ----------

def _buckets(pos):
    surplus = pos["surplus"] or ZERO
    contributions = min(pos["goal_need"], _pos(surplus))
    return {"unallocated": _pos(pos["free_monthly"] or ZERO), "free_savings": _pos(pos["free_savings"]),
            "goal_contributions": contributions, "flexible_cuts": pos["cuttable"],
            "safety": _pos(pos["safety_money"])}


def funding(pos, cost, order=PURCHASE_ORDER):
    """Cover `cost` from the buckets in `order`: [{"source", "amount"}], the
    shortfall, and the verdict (see the top of this file)."""
    cost = Decimal(cost)
    if pos["income"] is None or "expenses" in pos["missing"]:
        return {"parts": [], "shortfall": cost, "verdict": "insufficient_information"}
    left, parts, room = cost, [], _buckets(pos)
    for source in order:
        if left <= 0:
            break
        take = min(left, room[source])
        if take > 0:
            parts.append({"source": source, "amount": take})
            left -= take
    if not verify.validate_allocation([p["amount"] for p in parts] + [left], cost):
        raise ArithmeticError("funding parts do not add up to the cost")
    used = {p["source"] for p in parts}
    verdict = ("not_affordable" if left > 0 else "risky" if "safety" in used
               else "affordable_with_adjustments" if used & ADJUSTMENTS else "affordable")
    return {"parts": parts, "shortfall": left, "verdict": verdict}


def goal_impact(pos, amount):
    """Which goals fall behind when `amount` of this month's goal contributions is
    used: shared in proportion to what each goal needs a month."""
    amount = Decimal(amount)
    dated = [g for g in pos["goals"] if g.get("required_monthly_saving")]
    need = sum((dec(g["required_monthly_saving"]) for g in dated), ZERO)
    if amount <= 0 or need <= 0:
        return []
    out, given = [], ZERO
    for i, g in enumerate(dated):
        req = dec(g["required_monthly_saving"])
        share = (amount - given if i == len(dated) - 1 else
                 (amount * req / need).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        given += share
        out.append({"goal": g["name"], "behind_by": share, "monthly_need": req,
                    "months_of_contributions": math.ceil(share / req) if share > 0 else 0})
    return out


def cuts_for(pos, amount):
    """Flexible-spending cuts adding up to `amount` (each line at most CUT_SHARE)."""
    amount = Decimal(amount).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    lines = [(k, (a * CUT_SHARE).quantize(Decimal(1))) for k, a in pos["flexible_lines"].items()]
    cuts = verify.allocate_cuts(min(amount, sum((c for _, c in lines), ZERO)), lines) or []
    return [{"category": c, "subcategory": s, "spent": pos["flexible_lines"][(c, s)], "cut": cut}
            for (c, s), _, cut in cuts]


# ---------- purchases ----------

def max_safe_price(pos):
    """The most a one-off purchase can cost without touching goals, flexible
    spending or the safety money."""
    if pos["income"] is None:
        return None
    b = _buckets(pos)
    return b["unallocated"] + b["free_savings"]


def _plan_monthly(pos, per_month, months, label, total=None):
    """A monthly commitment (saving toward it, or an installment) of `total` in all."""
    per_month = Decimal(per_month)
    total = per_month * months if total is None else Decimal(total)
    free = _pos(pos["free_monthly"] or ZERO)
    surplus = pos["surplus"] or ZERO
    from_goals = min(_pos(per_month - free), _pos(min(pos["goal_need"], _pos(surplus))))
    from_cuts = _pos(per_month - free - from_goals)
    short = _pos(from_cuts - pos["cuttable"])
    from_cuts -= short
    verdict = ("not_affordable" if short > 0 else
               "risky" if from_cuts > 0 and from_goals > 0 else
               "affordable_with_adjustments" if from_cuts > 0 or from_goals > 0 else "affordable")
    return {"kind": label, "per_month": per_month, "months": months, "from_unallocated": min(per_month, free),
            "from_goal_contributions": from_goals, "from_flexible_cuts": from_cuts, "monthly_shortfall": short,
            "goals_behind_total": (total * from_goals / per_month).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if per_month else ZERO, "verdict": verdict}


def assess_purchase(pos, price, delay_months=None, installments=None, monthly_payment=None,
                    cheaper_price=None, urgent=None, goal_first=None):
    """Buying now vs waiting vs a cheaper version vs installments, with a
    recommendation and the reason for it."""
    out = {"price": None if price is None else Decimal(price), "urgent": urgent, "options": [],
           "missing": list(pos["missing"]), "assumptions": [], "risks": []}
    if price is None:
        out["missing"].append("price")
    if price is None or pos["income"] is None or "expenses" in pos["missing"]:
        out["verdict"] = "insufficient_information"
        out["recommended"] = None
        return out
    price = Decimal(price)
    free = _pos(pos["free_monthly"])
    now = funding(pos, price)
    now.update(kind="buy_now", cost=price, goal_impact=goal_impact(pos, sum(
        (p["amount"] for p in now["parts"] if p["source"] == "goal_contributions"), ZERO)),
        cuts=cuts_for(pos, sum((p["amount"] for p in now["parts"] if p["source"] == "flexible_cuts"), ZERO)))
    out["options"].append(now)
    safe = max_safe_price(pos)
    out["max_safe_price"] = safe

    # Waiting: how long saving only unallocated money takes, or the user's own wait.
    wait = delay_months or months_needed(price, free) or months_needed(price, _pos(pos["surplus"]))
    if wait:
        delay = _plan_monthly(pos, (price / wait).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), wait, "delay",
                              total=price)
        delay["asked"] = bool(delay_months)
        out["options"].append(delay)
    else:
        out["options"].append({"kind": "delay", "per_month": None, "months": None, "verdict": "not_affordable",
                               "reason": "no_unallocated_money"})
    if cheaper_price is not None:
        cheap = funding(pos, Decimal(cheaper_price))
        cheap.update(kind="cheaper", cost=Decimal(cheaper_price), saving_vs_price=price - Decimal(cheaper_price))
        out["options"].append(cheap)
    elif safe is not None and 0 < safe < price:
        out["options"].append({"kind": "cheaper", "cost": None, "max_price": safe, "verdict": "affordable"})
    if installments or monthly_payment:
        n = installments or math.ceil(price / Decimal(monthly_payment))
        pay = Decimal(monthly_payment) if monthly_payment else (price / n).quantize(Decimal("0.01"),
                                                                                     rounding=ROUND_HALF_UP)
        if monthly_payment and pay * n < price:
            # n payments of `pay` cannot cover the price: keep the payment, add months
            n = math.ceil(price / pay)
            out["assumptions"].append("installments_extended_to_cover_price")
        total = pay * n if monthly_payment else price     # equal parts of the price: no markup
        plan = _plan_monthly(pos, pay, n, "installments", total=total)
        plan.update(total_paid=total, extra_cost=total - price)
        out["options"].append(plan)

    rec, why = _recommend(out, now, urgent, goal_first)
    out.update(verdict=now["verdict"], recommended=rec, reason=why)
    if pos["income_basis"] != "recorded in the month":
        out["assumptions"].append("income_from_latest_month")
    out["assumptions"].append("future_months_like_this_one")
    if pos["free_monthly"] is not None and pos["free_monthly"] < 0:
        out["risks"].append("goals_already_need_more_than_surplus")
    if now["verdict"] == "risky":
        out["risks"].append("uses_safety_money")
    return out


def _recommend(out, now, urgent, goal_first):
    by_kind = {o["kind"]: o for o in out["options"]}
    delay, cheap, inst = by_kind.get("delay"), by_kind.get("cheaper"), by_kind.get("installments")
    if now["verdict"] == "affordable":
        return "buy_now", "covered_without_touching_goals"
    if urgent:
        if now["verdict"] != "not_affordable":
            return "buy_now", "urgent_least_harm"
        if inst and inst["verdict"] != "not_affordable":
            return "installments", "urgent_spread_cost"
        return "cheaper" if cheap else "buy_now", "urgent_not_covered"
    if cheap and cheap.get("cost") is not None and cheap["verdict"] == "affordable":
        return "cheaper", "cheaper_fits"
    if delay and delay.get("months") and delay["verdict"] == "affordable":
        return "delay", "goal_first" if goal_first else "waiting_avoids_harm"
    if inst and inst["verdict"] == "affordable" and inst["extra_cost"] <= 0:
        return "installments", "no_extra_cost_fits"
    if goal_first or now["verdict"] in ("risky", "not_affordable"):
        return "delay", "goal_first" if goal_first else "buying_now_too_costly"
    # Buying now needs adjustments, and so does waiting.
    now_cuts = any(p["source"] == "flexible_cuts" for p in now["parts"])
    if now_cuts and delay and delay.get("months") and not delay.get("from_flexible_cuts"):
        return "delay", "waiting_avoids_cuts"
    return "delay", "goals_pay_either_way"


# ---------- emergencies ----------

def plan_emergency(pos, amount):
    """Cover an unexpected cost: the funding order puts safety money before the
    goals, essential spending is never cut, and the plan says how long refilling
    what was used takes."""
    amount = Decimal(amount)
    if pos["income"] is None or "expenses" in pos["missing"]:
        return {"amount": amount, "verdict": "insufficient_information", "missing": list(pos["missing"])}
    f = funding(pos, amount, EMERGENCY_ORDER)
    used = {p["source"]: p["amount"] for p in f["parts"]}
    refill = used.get("free_savings", ZERO) + used.get("safety", ZERO)
    free = _pos(pos["free_monthly"])
    rate = free if free > 0 else _pos(pos["surplus"])
    return {"amount": amount, **f,
            "goal_impact": goal_impact(pos, used.get("goal_contributions", ZERO)),
            "cuts": cuts_for(pos, used.get("flexible_cuts", ZERO)),
            "refill": refill, "refill_rate": rate, "refill_from": "unallocated" if free > 0 else "surplus",
            "refill_months": months_needed(refill, rate) if refill else 0,
            "essential_protected": pos["essential"]}


# ---------- which goal first ----------

def _finish_months(remaining, per_month):
    return months_needed(remaining, per_month) if remaining > 0 else 0


def goal_order(pos, preferred=None):
    """(ordered goals, how the order was chosen). The user's own order wins: a goal
    named in the question, else the order saved on the Goals page; otherwise the
    suggestion is emergency savings first while cover is under SAFETY_FLOOR_MONTHS,
    then the earliest deadline."""
    goals = list(pos["goals"])
    if preferred:
        first = [g for g in goals if g["name"] == preferred]
        return first + [g for g in goals if g["name"] != preferred], "stated"
    if any(g.get("priority") for g in goals):
        return sorted(goals, key=lambda g: (g.get("priority") or 99, g["name"])), "saved"
    low_cover = pos["emergency_months"] is None or pos["emergency_months"] < SAFETY_FLOOR_MONTHS
    return sorted(goals, key=lambda g: (not (g["emergency"] and low_cover), g.get("target_date") or "9999",
                                        g["name"])), "suggested"


def prioritize_goals(pos, preferred=None):
    """Compare focusing on one goal at a time, splitting the monthly saving, and
    moving dates, for the money actually left each month."""
    goals = [g for g in pos["goals"] if dec(g["remaining_amount"]) > 0]
    if not goals:
        return {"goals": [], "verdict": "no_open_goals"}
    if pos["surplus"] is None:
        return {"goals": goals, "verdict": "insufficient_information", "missing": ["income"]}
    capacity = _pos(pos["surplus"])
    ordered, how = goal_order({**pos, "goals": goals}, preferred)
    # one at a time, in that order
    focus, done = [], 0
    for g in ordered:
        n = _finish_months(dec(g["remaining_amount"]), capacity)
        done = None if n is None or done is None else done + n
        focus.append({"goal": g["name"], "finish_in": done, "months_left": g.get("months_left"),
                      "on_time": _on_time(done, g)})
    # split: dated goals get their monthly need first (scaled down when it does not
    # all fit), then whatever is left is shared equally by the goals without a date
    dated = [g for g in goals if g.get("required_monthly_saving")]
    undated = [g for g in goals if not g.get("required_monthly_saving")]
    need = sum((dec(g["required_monthly_saving"]) for g in dated), ZERO)
    scale = min(Decimal(1), capacity / need) if need > 0 else ZERO
    shares = {g["name"]: (dec(g["required_monthly_saving"]) * scale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
              for g in dated}
    rest = capacity - sum(shares.values(), ZERO)
    for g in undated:
        shares[g["name"]] = (rest / len(undated)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
            if rest > 0 else ZERO
    split = []
    for g in goals:
        share = shares[g["name"]]
        n = _finish_months(dec(g["remaining_amount"]), share)
        split.append({"goal": g["name"], "per_month": share, "finish_in": n, "months_left": g.get("months_left"),
                      "on_time": _on_time(n, g)})
    # dates that fit when the split continues
    dates = [{"goal": s["goal"], "months_needed": s["finish_in"], "months_left": s["months_left"]}
             for s in split if s["on_time"] is False]
    strategies = {"focus": focus, "split": split}
    emergency = [g for g in goals if g["emergency"]]
    if emergency and ordered[0]["name"] != emergency[0]["name"]:
        e_first = [emergency[0]] + [g for g in ordered if g["name"] != emergency[0]["name"]]
        rows, done = [], 0
        for g in e_first:
            n = _finish_months(dec(g["remaining_amount"]), capacity)
            done = None if n is None or done is None else done + n
            rows.append({"goal": g["name"], "finish_in": done, "months_left": g.get("months_left"),
                         "on_time": _on_time(done, g)})
        strategies["emergency_first"] = rows
    on_time = {k: sum(1 for r in v if r["on_time"]) for k, v in strategies.items()}
    late = {k: sum(_months_late(r) for r in v) for k, v in strategies.items()}
    all_fit = pos["goal_need"] <= capacity
    if all_fit:
        best, reason = "split", "all_fit"
    else:
        # most goals on time; then the least total delay past the deadlines
        best = min(strategies, key=lambda k: (-on_time[k], late[k], k != "focus"))
        reason = "most_on_time" if on_time[best] else "none_on_time"
    return {"goals": ordered, "order_basis": how,
            "capacity": capacity, "total_need": pos["goal_need"], "all_fit": all_fit,
            "strategies": strategies, "on_time": on_time, "months_late": late, "adjust_dates": dates,
            "recommended": best, "reason": reason, "verdict": "ok",
            "emergency_months": pos["emergency_months"]}


def _months_late(row):
    if row["months_left"] is None:
        return 0
    if row["finish_in"] is None:
        return 999
    return max(row["finish_in"] - row["months_left"], 0)


def _on_time(months, goal):
    if goal.get("months_left") is None:
        return None
    return months is not None and months <= goal["months_left"]


# ---------- saving plan from the user's own history ----------

def saving_plan(user_id, month):
    """For each flexible line: this month against the user's own earlier months.
    A line that rose gets its own earlier average as the limit; a line above its
    own lowest earlier month gets that month as the limit. No generic percentages."""
    earlier = [m for m in facts.months_back(month, HISTORY_MONTHS + 1)[:-1]
               if facts.spending(user_id, [m])["count"]]
    now = facts.spending(user_id, [month])
    prefs = preference_map(user_id)
    history = {m: facts.spending(user_id, [m])["by_subcategory"] for m in earlier}
    days = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    lines = []
    for c, subs in now["by_subcategory"].items():
        for s, amount in subs.items():
            if amount <= 0 or is_essential(user_id, c, s, prefs):
                continue
            past = [history[m].get(c, {}).get(s, ZERO) for m in earlier]
            row = {"category": c, "subcategory": s, "now": amount, "history": dict(zip(earlier, past))}
            if past:
                avg = (sum(past, ZERO) / len(past)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                low = min(past)
                if amount > avg:
                    row.update(limit=avg, rule="back_to_average", basis=avg)
                elif amount > low:
                    row.update(limit=low, rule="lowest_month", basis=low)
            if "limit" in row:
                row["saving"] = amount - row["limit"]
                row["weekly_limit"] = (row["limit"] * 7 / days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                lines.append(row)
            else:
                row["rule"] = "steady" if past else "no_history"
                lines.append(row)
    suggested = sorted([r for r in lines if "limit" in r], key=lambda r: (-r["saving"], r["category"]))
    total = sum((r["saving"] for r in suggested), ZERO)
    return {"month": month, "earlier_months": earlier, "lines": lines, "suggested": suggested,
            "total_saving": total, "weekly_saving": (total * 7 / days).quantize(Decimal("0.01"),
                                                                                 rounding=ROUND_HALF_UP),
            "days": days}


# ---------- a purchase planned for a date ----------

def assess_plan(pos, price, months, extra_monthly=ZERO, own_saved=ZERO):
    """Saving for `price` over `months` (the user's own time frame). It keeps four
    answers apart:
      fits_money_left  the monthly amount fits in what is left after spending
                       (possible with the current budget, goals aside)
      fits_free_money  it fits without reducing what the goals receive now
      goals_lose       how much less the goals would receive each month
      adjustments      what closes the gap: realistic cuts to flexible spending,
                       extra income, or more months
    extra_monthly is money the user proposes to add each month (a cut or extra
    income); own_saved is money already saved for this very item."""
    if pos["income"] is None or "expenses" in pos["missing"]:
        return {"verdict": "insufficient_information", "missing": list(pos["missing"])}
    price, extra = Decimal(price), Decimal(extra_monthly)
    to_save = _pos(price - Decimal(own_saved))
    months = max(int(months), 1)
    per_month = (to_save / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    left = pos["surplus"] + extra                    # money left each month after spending
    goals_now = min(pos["goal_need"], _pos(pos["surplus"]))   # what the goals receive today
    free = left - goals_now                          # left over once the goals have their share
    goals_get = min(goals_now, _pos(left - per_month))
    goals_lose = goals_now - goals_get
    short = _pos(per_month - left)
    verdict = ("too_tight" if short > 0 else "goals_slow" if goals_lose > 0 else "fits")
    realistic_cuts = pos["cuttable"]
    # what it would take to keep the goals' current share untouched
    extra_needed = _pos(per_month - _pos(free))
    months_free = months_needed(to_save, free) if free > 0 else None
    months_left = months_needed(to_save, left) if left > 0 else None
    impact = goal_impact(pos, goals_lose * months)
    return {
        "price": price, "to_save": to_save, "months": months, "per_month": per_month,
        "money_left": left, "extra_monthly": extra, "goals_need": pos["goal_need"], "goals_receive_now": goals_now,
        "goals_receive_with_plan": goals_get, "goals_lose_monthly": goals_lose,
        "goals_lose_total": goals_lose * months, "goal_impact": impact, "monthly_shortfall": short,
        "fits_money_left": short == 0, "fits_free_money": goals_lose == 0 and short == 0,
        "extra_needed_to_keep_goals": extra_needed, "realistic_cuts": realistic_cuts,
        "cuts": cuts_for(pos, min(extra_needed, realistic_cuts)) if extra_needed > 0 else [],
        "months_using_money_left": months_left, "months_without_touching_goals": months_free,
        "verdict": verdict,
    }
