# app/services/plan_followups.py
# A purchase the user plans for a time frame ("a PlayStation for 15,000 in four
# months"), carried through the conversation:
#
#   - the plan (item, price, number of months) lives in the answer context and
#     every follow-up starts from it, so nothing has to be repeated;
#   - a correction ("actually the price is 10,000", "لا سعرها 10 الف") changes
#     only that value and keeps the rest;
#   - each follow-up gets a short answer to what was asked: is it possible, how
#     much a month, what a cut or extra income changes, what happens to the goals.
#     The full comparison (buy now / wait / cheaper / installments) is given only
#     when asked for.
#
# Every figure comes from decisions.assess_plan (Decimal arithmetic on the user's
# records); this module only reads the question and words the result.

import re
from decimal import ROUND_HALF_UP, Decimal

from app.finance import decisions as engine
from app.finance.facts import ZERO, dec
from app.services import intent as parse
from app.services.direct_answers import L, amt, cat_label, name, num

_A = parse.AR_LETTER
_N = parse._N
# "in four months", "within 4 months", "after 4 months", "خلال اربعة شهور", "بعد شهرين"
HORIZON = re.compile(
    rf"\b(?:in|within|after|over|by|for)\s+(?:the\s+next\s+|about\s+|around\s+|another\s+)?{_N}\s*months?\b|"
    rf"\b{_N}\s*months?\s+(?:from now|time)\b|"
    rf"(?<!{_A})(?:خلال|بعد|في|لمده|كمان|في خلال)\s*(?:حوالي\s*)?{_N}\s*(?:شهور|اشهر|شهر)(?!{_A})|"
    rf"(?<!{_A})(?:خلال|بعد|في)\s*(شهرين)(?!{_A})")
PRICE_WORD = re.compile(r"\b(?:price|prices|cost|costs|costing|actually|instead|not|only)\b|"
                        rf"(?<!{_A})(?:سعر|سعرها|سعره|بكم|طلع|طلعت|بقي|بقت|بقى|اصلا|في الحقيقه|بالحقيقه|قصدت|"
                        rf"اقصد|لا|بس)(?!{_A})")
MONTHLY = re.compile(r"\bhow much\b.*\b(?:save|saving|put aside|set aside|need|keep)\b.*\b(?:month|monthly)\b|"
                     r"\b(?:monthly|per month|each month|every month|a month)\b.*\b(?:save|saving|need)\b|"
                     rf"(?:كم|قد شنو|قديش).*(?:احوش|ادخر|اوفر|اشيل|اجنب|اخت|احتاج|محتاج|لازم).*(?:شهر|شهريا)|"
                     rf"(?:شهريا|في الشهر|كل شهر).*(?:كم|قد شنو)")
FEASIBLE = re.compile(r"\b(?:possible|can i|could i|able to|feasible|realistic|affordable|afford|doable|manage)\b|"
                      rf"(?<!{_A})(?:ممكن|اقدر|ينفع|بقدر|حقدر|بقدر|معقول|يمشي|ينفع)(?!{_A})")
INCOME_UP = re.compile(r"\b(?:more|extra|additional|side|second|increase|raise|boost)\b.{0,25}\b(?:income|money|earn\w*|job)\b|"
                       r"\b(?:income|salary)\b.{0,20}\b(?:up|more|increase\w*|rais\w*|higher)\b|"
                       r"\bearn\b.{0,20}\b(?:more|extra)\b|"
                       rf"(?:دخل|مرتب|راتب|قروش)\s*(?:ال)?(?:اضافي|زياده|اكتر|اكثر)|(?:زدت|زودت|زاد|زيادة|زياده)\s*(?:ال)?"
                       rf"(?:دخل|مرتب|راتب)|شغل اضافي|اشتغل زياده")
GOAL_IMPACT = re.compile(r"\b(?:goals?|emergency fund|laptop)\b.*\b(?:affect\w*|impact\w*|effect|happen\w*|mean|slow\w*|"
                         r"delay\w*)\b|\b(?:affect\w*|impact\w*|effect|happen\w*)\b.*\bgoals?\b|"
                         rf"(?:هدف|اهداف|اهدافي|هدفي).*(?:يأثر|ياثر|تاثير|اثر|يحصل|حيحصل|يتأثر|يتاثر)|"
                         rf"(?:يأثر|ياثر|تاثير|اثر|يحصل|حيحصل).*(?:هدف|اهداف|اهدافي)")
DETAILS = re.compile(r"\b(?:details|detailed|full (?:analysis|comparison|report)|all (?:the )?options|"
                     r"compare (?:the )?options|other options|alternatives)\b|تفاصيل|كل الخيارات|التحليل الكامل|خيارات تانيه|"
                     r"خيارات اخري|البدائل")
CUT = re.compile(r"\b(?:cut|reduce|lower|trim|spend less|less on|decrease)\w*\b|" + parse.DOWN.pattern)


def horizon(text):
    """The number of months a plan is for, or None."""
    m = HORIZON.search(text)
    if not m:
        return None
    token = next((g for g in m.groups() if g), None)
    if token == "شهرين":
        return 2
    value = int(token) if token and token.isdigit() else parse.NUMBER_WORDS.get(token or "")
    return value if value and 0 < value <= 120 else None


def _price_in(text, d):
    """A new price in a correction ("actually the price is 10,000"): an amount that
    is not the number of months and not a monthly figure."""
    skip = set()
    for regex in (HORIZON, re.compile(_N + r"\s*(?:months?|اشهر|شهور|شهر)")):
        for m in regex.finditer(text):
            skip.update(range(m.start(), m.end()))
    found = [dec(v) for v, is_pct, pos in parse.amounts(text) if not is_pct and v >= 10 and pos not in skip]
    return found[-1] if found else None


def _item_named(text):
    from app.services.decision_answers import _item
    return _item(text)


def continues(text, recent):
    """True when the message continues the purchase plan in `recent`."""
    d = (recent or {}).get("decision") or {}
    if (recent or {}).get("intent") != "purchase" or not d.get("price"):
        return False
    from app.services.decision_answers import INSTALL, PER_MONTH
    repayment = PER_MONTH.search(text) and not (INCOME_UP.search(text) or CUT.search(text))
    if INSTALL.search(text) or repayment or d.get("planned") or d.get("loan"):
        return False                             # installments / a loan repayment / a planned expense
    other = _item_named(text)
    if other and d.get("item") and other.lower() != str(d["item"]).lower() and \
            other.lower() not in ("it", "one", "this", "that"):
        return False                             # a new purchase, not this one
    if horizon(text) or MONTHLY.search(text) or GOAL_IMPACT.search(text) or DETAILS.search(text):
        return True
    if INCOME_UP.search(text) or (CUT.search(text) and parse.find_categories(parse.without_rest_unchanged(text))):
        return True
    if _price_in(text, d) is not None and (PRICE_WORD.search(text) or len(text.split()) <= 4):
        return True
    return bool(FEASIBLE.search(text) and len(text.split()) <= 14)


def last_plan(history, window=6):
    """The context of the latest purchase plan among the last `window` answers, so a
    plan survives an answer about something else in between."""
    seen = 0
    for m in reversed(list(history or [])):
        if m.get("role") != "assistant":
            continue
        seen += 1
        if seen > window:
            break
        ctx = m.get("context") or {}
        if ctx.get("intent") == "purchase" and (ctx.get("decision") or {}).get("price"):
            return ctx
    return None


def _plan_from(previous):
    d = dict((previous or {}).get("decision") or {})
    for k in ("price", "cheaper", "monthly"):
        if d.get(k) is not None:
            d[k] = dec(d[k])
    return d


def follow_up(question, text, user_id, cur, lang, previous):
    """The answer to a follow-up about the plan, or None (another kind of follow-up)."""
    d = _plan_from(previous)
    notes = []
    new_price = _price_in(text, d)
    if new_price is not None and new_price != d.get("price") and (PRICE_WORD.search(text) or len(text.split()) <= 4
                                                                  or (previous or {}).get("awaiting") == "purchase_price"):
        notes.append(L(lang, f"Updated: the price is now {amt(new_price, lang)} (was {amt(d['price'], lang)}).",
                       f"تم التحديث: السعر الآن {amt(new_price, lang)} (كان {amt(d['price'], lang)})."))
        d["price"] = new_price
    months = horizon(text)
    if months and months != d.get("horizon"):
        if d.get("horizon"):
            notes.append(L(lang, f"Updated: {months} months instead of {d['horizon']}.",
                           f"تم التحديث: {months} شهر بدلًا من {d['horizon']}."))
        d["horizon"] = months
    if DETAILS.search(text):
        from app.services.decision_answers import purchase
        d["delay"] = d.get("horizon") or d.get("delay")
        out = purchase(user_id, cur, lang, {**d, "details": True}, previous)
        return out
    if not d.get("horizon"):
        if MONTHLY.search(text) or FEASIBLE.search(text):
            return _ask_months(lang, d, cur, "monthly" if MONTHLY.search(text) else "verdict")
        return None
    pending = (previous or {}).get("pending_ask") if (previous or {}).get("awaiting") == "purchase_months" else None
    if pending:
        ask = pending                   # "4" answering "in how many months?": the question asked before
    elif MONTHLY.search(text):
        ask = "monthly"
    elif CUT.search(text) and parse.find_categories(parse.without_rest_unchanged(text)):
        ask = "cut"
    elif INCOME_UP.search(text):
        ask = "income"
    elif GOAL_IMPACT.search(text):
        ask = "goals"
    else:
        ask = "verdict"
    return plan_answer(user_id, cur, lang, d, ask=ask, text=text, notes=notes, previous=previous)


def _ask_months(lang, d, cur, pending="verdict"):
    from app.services.decision_answers import _out
    item = d.get("item") or L(lang, "item", "المشتريات")
    text = L(lang, f"In how many months do you want to buy the {item}? With that I can work out the monthly amount.",
             f"بعد كم شهر تريد شراء {item}؟ بها أحسب المبلغ الشهري.")
    context = {"intent": "purchase", "decision": _stored(d), "months": [cur], "awaiting": "purchase_months",
               "pending_ask": pending}
    return _out(text, "clarify", {"missing": ["months"]}, [], context)


def _stored(d):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items() if k != "details"}


# ---------- the answers ----------

def _goal_names(pos):
    return " + ".join(f"{g['name']} {num(g['required_monthly_saving'])}" for g in pos["goals"]
                      if g.get("required_monthly_saving"))


def _cut_amount(pos, text, cats, lang):
    """(monthly amount, description lines, flexible total) for "what if I reduce X?":
    the amount the user names, else a realistic cut of 30% of each flexible line."""
    from app.services.decision_answers import _lines_for
    lines = _lines_for(pos, cats)
    flexible = [x for x in lines if not x["essential"]]
    flex_total = sum((x["amount"] for x in flexible), ZERO)
    named = [dec(v) for v, is_pct, _ in parse.amounts(text) if v >= 1]
    pcts = [Decimal(str(v)) for v, is_pct, _ in parse.amounts(text) if is_pct]
    if pcts:
        amount, how = (flex_total * pcts[0] / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP), "pct"
    elif named:
        amount, how = min(named[0], flex_total), "named"
    else:
        amount = sum(((x["amount"] * engine.CUT_SHARE).quantize(Decimal(1)) for x in flexible), ZERO)
        how = "realistic"
    return amount, how, flexible, flex_total, sum((x["amount"] for x in lines), ZERO)


def plan_answer(user_id, cur, lang, d, ask="verdict", text="", notes=(), previous=None):
    from app.services.decision_answers import _out
    pos = engine.position(user_id, cur)
    item = d.get("item") or L(lang, "item", "المشتريات")
    price, months = Decimal(str(d["price"])), int(d["horizon"])
    own = next((g for g in pos["goals"] if g["name"] == d.get("own_goal")), None)
    own_saved = ZERO
    if own:
        pos, own_saved = engine.without_goal(pos, own["name"])
    context = {"intent": "purchase", "decision": _stored(d), "months": [cur]}
    if pos["income"] is None or "expenses" in pos["missing"]:
        from app.services.decision_answers import _missing_income
        return _missing_income(lang, "assess_purchase", context)
    extra, extra_lines = ZERO, []
    if ask == "cut":
        cats = list(dict.fromkeys((c, s) for _, c, s in parse.find_categories(parse.without_rest_unchanged(text))))
        extra, how, flexible, flex_total, total = _cut_amount(pos, text, cats, lang)
        names = L(lang, " and ", " و").join(cat_label(c, s, lang) for c, s in cats)
        if flex_total <= 0:
            body = L(lang, f"All of your {names} spending ({amt(total, lang)}) is marked essential, so I would not cut "
                           "it: that money pays for basics. The plan stays as it is.",
                     f"كل إنفاقك على {names} ({amt(total, lang)}) مصنف أساسيًا، لذلك لا أنصح بتخفيضه: هذا المال "
                     "لاحتياجات أساسية. الخطة تبقى كما هي.")
            return _out("\n".join([*notes, body]), "assess_purchase", {"decision": "plan", "cut": 0}, [], context)
        basis = {"realistic": L(lang, f"30% of its flexible {num(flex_total)}, a realistic cut",
                                f"30% من الجزء المرن {num(flex_total)}، تخفيض واقعي"),
                 "named": L(lang, "as you said", "كما ذكرت"),
                 "pct": L(lang, f"of its flexible {num(flex_total)}", f"من الجزء المرن {num(flex_total)}")}[how]
        parts = L(lang, ", ", "، ").join(f"{name(x['subcategory'] or x['category'], lang)} {num(x['amount'])}"
                                        for x in flexible)
        extra_lines.append(L(lang, f"Cutting {names} by {amt(extra, lang)} a month ({basis}; {parts}) adds "
                                   f"{num(extra)} to the {num(pos['surplus'])} left each month.",
                             f"تخفيض {names} بمقدار {amt(extra, lang)} شهريًا ({basis}؛ {parts}) يضيف {num(extra)} إلى "
                             f"المتبقي الشهري {num(pos['surplus'])}."))
    if ask == "income":
        named = [dec(v) for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 1]
        extra = named[0] if named else ZERO
    r = engine.assess_plan(pos, price, months, extra, own_saved)
    if ask == "income" and not extra:
        r0 = r
        body = [L(lang, f"Extra income of {amt(r0['extra_needed_to_keep_goals'], lang)} a month would pay for the {item} "
                        f"without taking anything from your goals' current {num(r0['goals_receive_now'])} a month "
                        f"({num(r0['per_month'])} needed − {num(r0['per_month'] - r0['extra_needed_to_keep_goals'])} "
                        "free now).",
                  f"دخل إضافي قدره {amt(r0['extra_needed_to_keep_goals'], lang)} شهريًا يغطي {item} دون أن يأخذ شيئًا من "
                  f"مبلغ أهدافك الحالي {num(r0['goals_receive_now'])} شهريًا ({num(r0['per_month'])} مطلوبة − "
                  f"{num(r0['per_month'] - r0['extra_needed_to_keep_goals'])} متاحة الآن)."),
                L(lang, "Tell me how much extra you could earn a month and I'll show exactly what it changes.",
                  "أخبرني كم يمكنك أن تكسب زيادة شهريًا وسأوضح بالضبط ما يتغير.")]
        return _out("\n".join([*notes, *body]), "assess_purchase", {"decision": "plan", **engine.as_json(r0)}, [],
                    {**context, "awaiting": "plan_income"})
    lines = [*notes]
    head_calc = f"{num(r['to_save'])} ÷ {months}"
    if own_saved:
        lines.append(L(lang, f'{amt(own_saved, lang)} is already saved for it in "{own["name"]}", so {amt(r["to_save"], lang)} is left to save.',
                       f'{amt(own_saved, lang)} مدخرة له بالفعل في "{own["name"]}"، فيبقى {amt(r["to_save"], lang)} للادخار.'))
    if ask == "monthly":
        lines.append(L(lang, f"**{amt(r['per_month'], lang)} a month** ({head_calc} months).",
                       f"**{amt(r['per_month'], lang)} شهريًا** ({head_calc} أشهر)."))
        lines.append(_where_from(r, lang, item))
    elif ask == "goals":
        lines.append(_goals_line(r, lang, item, months, full=True))
    elif ask in ("cut", "income"):
        lines.extend(extra_lines)
        if ask == "income":
            lines.append(L(lang, f"With {amt(extra, lang)} more income a month you would have {amt(r['money_left'], lang)} "
                                 "left after spending.",
                           f"مع دخل إضافي {amt(extra, lang)} شهريًا يصبح المتبقي بعد المصروفات {amt(r['money_left'], lang)}."))
        base = engine.assess_plan(pos, price, months, ZERO, own_saved)
        if r["verdict"] == "fits":
            lines.append(L(lang, f"**That is enough:** the {num(r['per_month'])} a month for the {item} then fits without "
                                 "taking anything from your goals.",
                           f"**هذا يكفي:** {num(r['per_month'])} شهريًا لـ{item} تصبح ممكنة دون أن تأخذ شيئًا من أهدافك."))
        elif base["monthly_shortfall"] > 0 and r["monthly_shortfall"] == 0:
            lines.append(L(lang, f"**It closes the gap:** buying in {months} months becomes possible, but your goals get "
                                 f"{num(r['goals_receive_with_plan'])} a month instead of {num(r['goals_receive_now'])}.",
                           f"**هذا يسد الفجوة:** الشراء خلال {months} شهر يصبح ممكنًا، لكن أهدافك تحصل على "
                           f"{num(r['goals_receive_with_plan'])} شهريًا بدلًا من {num(r['goals_receive_now'])}."))
        elif r["monthly_shortfall"] > 0:
            lines.append(L(lang, f"**It helps but is not enough:** you would still be {amt(r['monthly_shortfall'], lang)} a "
                                 f"month short of the {num(r['per_month'])} needed (it was {num(base['monthly_shortfall'])}).",
                           f"**يساعد لكنه لا يكفي:** يبقى نقص {amt(r['monthly_shortfall'], lang)} شهريًا من "
                           f"{num(r['per_month'])} المطلوبة (كان {num(base['monthly_shortfall'])})."))
        else:
            covered = (extra / r["per_month"] * 100).quantize(Decimal("0.1")) if r["per_month"] else ZERO
            lines.append(L(lang, f"**It helps a little:** it covers {covered}% of the {num(r['per_month'])} a month. Your goals "
                                 f"would get {num(r['goals_receive_with_plan'])} a month instead of "
                                 f"{num(base['goals_receive_with_plan'])}; keeping them whole would still need "
                                 f"{amt(r['extra_needed_to_keep_goals'], lang)} more a month.",
                           f"**يساعد قليلًا:** يغطي {covered}% من {num(r['per_month'])} الشهرية. تحصل أهدافك على "
                           f"{num(r['goals_receive_with_plan'])} شهريًا بدلًا من {num(base['goals_receive_with_plan'])}؛ "
                           f"وللحفاظ عليها كاملة يلزم {amt(r['extra_needed_to_keep_goals'], lang)} إضافية شهريًا."))
    else:
        same = (previous or {}).get("summary") or {}
        if not notes and same.get("price") == str(price) and same.get("horizon") == months:
            lines.extend(_verdict_lines(r, lang, item, months, pos)[:1])     # asked again: the conclusion only
            if r["goals_lose_monthly"] > 0:
                lines.append(L(lang, f"The cost is {num(r['goals_lose_monthly'])} a month taken from your goals for "
                                     f"{months} months ({amt(r['goals_lose_total'], lang)} in total).",
                               f"الثمن {num(r['goals_lose_monthly'])} شهريًا تُؤخذ من أهدافك لمدة {months} شهر "
                               f"({amt(r['goals_lose_total'], lang)} إجمالًا)."))
        else:
            lines.extend(_verdict_lines(r, lang, item, months, pos))
    data = {"decision": "plan", "item": item, **engine.as_json(r), "asked": ask, "hypothetical": True}
    steps = [f"{num(r['to_save'])} ÷ {months} = {num(r['per_month'])}",
             f"{num(pos['income'])} − {num(pos['expenses'])} = {num(pos['surplus'])}"]
    context["summary"] = {"price": str(price), "horizon": months, "verdict": r["verdict"],
                          "per_month": str(r["per_month"]), "money_left": str(r["money_left"]),
                          "goals_need": str(r["goals_need"]), "goals_receive_now": str(r["goals_receive_now"]),
                          "goals_lose_monthly": str(r["goals_lose_monthly"]),
                          "goals_lose_total": str(r["goals_lose_total"])}
    return _out("\n".join(x for x in lines if x), "assess_purchase", data, steps, context, d.get("goal"))


def _where_from(r, lang, item):
    if r["verdict"] == "fits":
        return L(lang, f"That fits in money your goals are not using, so they are not affected.",
                 f"هذا يناسب مالًا لا تستخدمه أهدافك، فلا تتأثر.")
    if r["verdict"] == "goals_slow":
        return L(lang, f"It fits in the {num(r['money_left'])} you have left after spending, but your goals would then get "
                       f"{num(r['goals_receive_with_plan'])} a month instead of {num(r['goals_receive_now'])}.",
                 f"يناسب المتبقي بعد المصروفات ({num(r['money_left'])})، لكن أهدافك ستحصل حينها على "
                 f"{num(r['goals_receive_with_plan'])} شهريًا بدلًا من {num(r['goals_receive_now'])}.")
    return L(lang, f"That is more than the {num(r['money_left'])} you have left after spending: {amt(r['monthly_shortfall'], lang)} "
                   "a month short even if your goals got nothing.",
             f"هذا أكثر من المتبقي بعد المصروفات ({num(r['money_left'])}): نقص {amt(r['monthly_shortfall'], lang)} شهريًا "
             "حتى لو لم تحصل أهدافك على شيء.")


def _goals_line(r, lang, item, months, full=False):
    if r["goals_lose_monthly"] <= 0:
        return L(lang, "Your goals are not affected: they keep getting what they get now.",
                 "أهدافك لا تتأثر: تبقى تحصل على ما تحصل عليه الآن.")
    per_goal = L(lang, ", ", "، ").join(f'"{g["goal"]}" {num(g["behind_by"])}' for g in r["goal_impact"]
                                         if g["behind_by"] > 0)
    out = L(lang, f"Your goals would get {num(r['goals_lose_monthly'])} a month less for {months} months "
                  f"({num(r['goals_lose_monthly'])} × {months} = {amt(r['goals_lose_total'], lang)} behind: {per_goal}).",
            f"ستحصل أهدافك على أقل بـ {num(r['goals_lose_monthly'])} شهريًا لمدة {months} شهر "
            f"({num(r['goals_lose_monthly'])} × {months} = {amt(r['goals_lose_total'], lang)} تأخير: {per_goal}).")
    if full and r["goals_need"] > r["goals_receive_now"]:
        out += " " + L(lang, f"They already need {num(r['goals_need'])} a month, more than the "
                             f"{num(r['goals_receive_now'])} you have, so the {item} adds to a gap that is already there.",
                       f"وهي تحتاج أصلًا {num(r['goals_need'])} شهريًا، أكثر من {num(r['goals_receive_now'])} المتاحة لك، "
                       f"فـ{item} يزيد فجوة موجودة أصلًا.")
    return out


def _verdict_lines(r, lang, item, months, pos):
    calc = f"{num(r['to_save'])} ÷ {months}"
    if r["verdict"] == "fits":
        head = L(lang, f"**Yes, you can buy the {item} in {months} months without touching your goals.** It needs "
                       f"{amt(r['per_month'], lang)} a month ({calc}), and that fits in money no goal is counting on.",
                 f"**نعم، يمكنك شراء {item} خلال {months} شهر دون المساس بأهدافك.** يحتاج {amt(r['per_month'], lang)} "
                 f"شهريًا ({calc})، وهذا يناسب مالًا لا يعتمد عليه أي هدف.")
        return [head]
    if r["verdict"] == "goals_slow":
        head = L(lang, f"**Yes, it is possible in {months} months, but only by slowing your goals.** It needs "
                       f"{amt(r['per_month'], lang)} a month ({calc}); that fits in the {num(r['money_left'])} you have "
                       "left after spending.",
                 f"**نعم، ممكن خلال {months} شهر، لكن فقط على حساب أهدافك.** يحتاج {amt(r['per_month'], lang)} شهريًا "
                 f"({calc})؛ وهذا يناسب المتبقي بعد المصروفات ({num(r['money_left'])}).")
    else:
        head = L(lang, f"**Not in {months} months with your current spending.** It needs {amt(r['per_month'], lang)} a month "
                       f"({calc}), and you have {num(r['money_left'])} left after spending: "
                       f"{amt(r['monthly_shortfall'], lang)} a month short even if your goals got nothing.",
                 f"**غير ممكن خلال {months} شهر بمصروفاتك الحالية.** يحتاج {amt(r['per_month'], lang)} شهريًا ({calc})، "
                 f"والمتبقي بعد المصروفات {num(r['money_left'])}: نقص {amt(r['monthly_shortfall'], lang)} شهريًا حتى لو "
                 "لم تحصل أهدافك على شيء.")
    lines = [head, "- " + _goals_line(r, lang, item, months, full=True)]
    options = []
    gap = r["monthly_shortfall"] if r["verdict"] == "too_tight" else r["extra_needed_to_keep_goals"]
    what = (L(lang, "close the gap", "سد الفجوة") if r["verdict"] == "too_tight"
            else L(lang, "keep your goals whole", "الحفاظ على أهدافك كاملة"))
    if r["cuts"] or r["realistic_cuts"] > 0:
        cuts = engine.cuts_for(pos, min(gap, r["realistic_cuts"]))
        listed = L(lang, ", ", "، ").join(f"{name(c['subcategory'] or c['category'], lang)} −{num(c['cut'])}"
                                         for c in cuts[:4])
        enough = r["realistic_cuts"] >= gap
        options.append(L(lang, f"cut flexible spending: realistic cuts free up to {num(r['realistic_cuts'])} a month "
                               f"({listed}{', …' if len(cuts) > 4 else ''})"
                               + ("" if enough else f", not the full {num(gap)}"),
                         f"تخفيض الإنفاق المرن: التخفيضات الواقعية توفر حتى {num(r['realistic_cuts'])} شهريًا "
                         f"({listed}{'، …' if len(cuts) > 4 else ''})" + ("" if enough else f"، وليس كامل {num(gap)}")))
    options.append(L(lang, f"extra income of {num(gap)} a month", f"دخل إضافي {num(gap)} شهريًا"))
    longer = r["months_without_touching_goals"] if r["verdict"] == "goals_slow" else r["months_using_money_left"]
    if longer and longer > months:
        options.append(L(lang, f"{longer} months instead of {months}", f"{longer} شهر بدلًا من {months}"))
    elif r["verdict"] == "goals_slow" and not r["months_without_touching_goals"]:
        options.append(L(lang, "waiting longer does not help while your goals take all that is left",
                         "الانتظار أطول لا يفيد ما دامت أهدافك تأخذ كل المتبقي"))
    lines.append(L(lang, f"- To {what}: ", f"- لـ{what}: ") + L(lang, "; or ", "؛ أو ").join(options) + ".")
    lines.append(L(lang, "Ask me for the full comparison (buy now, wait, cheaper, installments) if you want it.",
                   "اطلب مني المقارنة الكاملة (الشراء الآن، الانتظار، الأرخص، التقسيط) إن أردت."))
    return lines
