# app/services/direct_answers.py
# Questions about the user's own numbers, answered straight from the database
# and the finance engines, without the language model:
#
#   question -> intent (intent.py) -> exact facts for this user and period
#   (facts.py, goal_context, budget / plan engines) -> checked sentence
#
# This is the fast path: one or two small queries and no model call, so the
# answer arrives in milliseconds and every number in it is the engine's number.
# Each answer also carries
#   * "calculation": the formula and inputs, shown when the user asks
#     "how did you calculate that?";
#   * "context": the intent, period, category, goal and what-if, which the app
#     sends back with the next question so follow-ups ("what about last month?",
#     "how much sooner will I reach it?") continue from it.
# A question this module does not clearly understand returns None and goes to
# the model exactly as before.

import calendar
import math
import re
import time
from datetime import date
from decimal import Decimal

from app.finance import facts, verify
from app.finance.facts import ZERO, dec, money, percent
from app.services import intent as parse
from app.services import trace
from app.services.goal_context import STATUS, goal_facts

CURRENCY = {"en": "SDG", "ar": "ج.س"}
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر",
             "أكتوبر", "نوفمبر", "ديسمبر"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]
AR_NAMES = {
    "Food": "الطعام", "Utilities": "المرافق", "Transport": "المواصلات", "Entertainment": "الترفيه",
    "Shopping": "التسوق", "Bills": "الفواتير", "Education": "التعليم", "Healthcare": "الصحة",
    "Others": "أخرى", "Breakfast": "الفطور", "Lunch": "الغداء", "Dinner": "العشاء",
    "Restaurant": "المطاعم", "Fast Food": "الوجبات السريعة", "Other": "أخرى", "Movie": "السينما",
    "Clothes": "الملابس", "Electronics": "الإلكترونيات", "Furniture": "الأثاث", "Rent": "الإيجار",
    "Electricity": "الكهرباء", "Water": "المياه", "Internet": "الإنترنت", "Salary": "الراتب",
    "Gifts": "الهدايا", "From Dad": "من الأب", "Freelancer": "العمل الحر",
}
# A goal counts as "at risk" (not "not achievable") when the monthly gap is no
# bigger than the flexible spending the user could cut.
SCENARIO_MEMORY = 2          # a what-if is reused by the next 2 answers at most
# Intents a request for advice takes over ("how can I reduce my food spending?").
ADVICE_CAN_REPLACE = {None, "goal", "savings", "total", "category", "top", "budget"}
DATA_INTENTS = {"total", "category", "top", "income", "savings", "average", "compare", "why",
                "budget", "goal", "summary", "transactions"}
# Follow-ups that keep the month the conversation is about when they name none.
KEEPS_MONTH = {"total", "category", "top", "income", "savings", "summary", "transactions"}


# ---------- wording helpers ----------

def L(lang, en, ar):
    return ar if lang == "ar" else en


def num(value):
    """1800 -> "1,800"; 2873.333 -> "2,873.33" (rounded only for display)."""
    value = money(value)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def amt(value, lang):
    return f"{num(value)} {CURRENCY.get(lang, 'SDG')}"


def pct(value):
    return "—" if value is None else f"{value:.2f}%"


def month_label(month, lang):
    if not month:
        return ""
    names = AR_MONTHS if lang == "ar" else EN_MONTHS
    return f"{names[int(month[5:7]) - 1]} {month[:4]}"


def period_label(months, lang):
    if len(months) == 1:
        return month_label(months[0], lang)
    first, last = month_label(months[0], lang), month_label(months[-1], lang)
    return L(lang, f"the last {len(months)} months ({first} to {last})",
             f"آخر {len(months)} أشهر (من {first} إلى {last})")


def name(item, lang):
    return AR_NAMES.get(item, item) if lang == "ar" else item


def cat_label(category, sub, lang):
    if not sub:
        return name(category, lang)
    return f"{name(sub, lang)} ({name(category, lang)})"


def after_months(k):
    """The month k months from today, e.g. '2027-04'."""
    today = date.today()
    total = today.year * 12 + today.month - 1 + k
    return f"{total // 12}-{total % 12 + 1:02d}"


def months_to_reach(remaining, monthly):
    if remaining <= 0:
        return 0
    if monthly <= 0:
        return None
    return math.ceil(Decimal(remaining) / Decimal(monthly))


def _result(text, tool, data, steps, context, goal=None):
    return {"answer": text, "tool": tool, "data": data, "calculation": steps,
            "context": context, "goal": goal}


def _no_data(lang):
    return L(lang, "You have no spending recorded yet, so there is nothing to calculate. Add your "
                   "transactions on the Money page (or import a CSV) and ask again.",
             "لا توجد مصروفات مسجلة بعد، لذلك لا يوجد ما أحسبه. أضف معاملاتك من صفحة المال "
             "(أو استورد ملف CSV) ثم اسأل مرة أخرى.")


def _quality_notes(spent, lang):
    notes = []
    if spent["refunds"]:
        notes.append(L(lang, f"{spent['refunds']} refund(s) or correction(s) with a negative amount were subtracted.",
                       f"تم طرح {spent['refunds']} استرداد/تصحيح بمبلغ سالب."))
    if spent["possible_duplicates"]:
        notes.append(L(lang, f"{spent['possible_duplicates']} record(s) look like duplicates (same day, amount "
                             "and description); they are counted, so check them on the Money page.",
                       f"{spent['possible_duplicates']} سجل يبدو مكررًا (نفس اليوم والمبلغ والوصف)؛ "
                       "تم احتسابه، فراجعه في صفحة المال."))
    if spent["invalid_amounts"]:
        notes.append(L(lang, f"{spent['invalid_amounts']} record(s) without a valid amount were left out.",
                       f"تم استبعاد {spent['invalid_amounts']} سجل بدون مبلغ صحيح."))
    return notes


# ---------- period ----------

def _months(q, cur, user_id, default="current"):
    """The months a question is about: the period it names, else the current
    month (or the last three months for an average)."""
    if q.period:
        if q.period["kind"] == "all":
            return facts.expense_months(user_id)
        return q.period["months"]
    if default == "three":
        return facts.months_back(cur, 3)
    return [cur]


def _with_data(months, spent):
    """The months of a range that actually have records (an average over months
    before the user started would be wrong)."""
    return [m for m in months if spent["by_month"].get(m, ZERO) != 0] or months


# ---------- spending ----------

def _total(q, user_id, cur):
    lang, months = q.language, _months(q, cur, user_id)
    spent = facts.spending(user_id, months)
    label = period_label(months, lang)
    if spent["count"] == 0:
        text = L(lang, f"No spending is recorded for {label}. Your latest month with spending is "
                       f"{month_label(cur, lang)}.",
                 f"لا توجد مصروفات مسجلة في {label}. آخر شهر فيه مصروفات هو {month_label(cur, lang)}.")
        return _result(text, "get_monthly_spending", {"months": months, "total": 0}, [], {})
    total = spent["total"]
    if len(months) == 1:
        text = L(lang, f"You spent {amt(total, lang)} in {label} ({spent['count']} expenses).",
                 f"صرفت {amt(total, lang)} في {label} ({spent['count']} مصروفًا).")
        prev = facts.shift_month(months[0], -1)
        before = facts.spending(user_id, [prev])
        if before["count"]:
            diff = total - before["total"]
            if diff:
                more = diff > 0
                text += " " + L(lang, f"That is {amt(abs(diff), lang)} {'more' if more else 'less'} than "
                                      f"{month_label(prev, lang)} ({amt(before['total'], lang)}).",
                                f"هذا {'أكثر' if more else 'أقل'} بمقدار {amt(abs(diff), lang)} من "
                                f"{month_label(prev, lang)} ({amt(before['total'], lang)}).")
        steps = [L(lang, f"Total expenses = sum of the {spent['count']} expense records dated {label}",
                   f"إجمالي المصروفات = مجموع {spent['count']} سجل مصروفات بتاريخ {label}"),
                 f"= {amt(total, lang)}"]
    else:
        used = _with_data(months, spent)
        parts = " + ".join(num(spent["by_month"][m]) for m in used)
        avg = total / len(used)
        text = L(lang, f"Over {label} you spent {amt(total, lang)} in total ({parts}), "
                       f"{amt(avg, lang)} a month on average.",
                 f"خلال {label} صرفت {amt(total, lang)} إجمالًا ({parts})، بمتوسط {amt(avg, lang)} شهريًا.")
        steps = [L(lang, f"Total = {parts} = {amt(total, lang)} ({spent['count']} expense records)",
                   f"الإجمالي = {parts} = {amt(total, lang)} ({spent['count']} سجل)"),
                 L(lang, f"Average = {num(total)} ÷ {len(used)} months = {amt(avg, lang)}",
                   f"المتوسط = {num(total)} ÷ {len(used)} أشهر = {amt(avg, lang)}")]
    steps += _quality_notes(spent, lang)
    data = {"months": months, "total": money(total), "count": spent["count"],
            "by_month": {m: money(v) for m, v in spent["by_month"].items()}}
    return _result(text, "get_monthly_spending", data, steps, {"months": months})


def _categories_together(q, user_id, cur):
    """"How much did I spend on restaurants and fast food?": every named category,
    each from all of its records, and their sum."""
    lang, months = q.language, _months(q, cur, user_id)
    spent = facts.spending(user_id, months)
    label = period_label(months, lang)
    parts, steps = [], []
    for category, sub in dict.fromkeys(q.categories):
        rows = [t for t in spent["transactions"] if t["category"] == category and (not sub or t["subcategory"] == sub)]
        parts.append({"category": category, "subcategory": sub, "amount": sum((t["amount"] for t in rows), ZERO),
                      "count": len(rows)})
    total = sum((p["amount"] for p in parts), ZERO)
    lines = [f"- {cat_label(p['category'], p['subcategory'], lang)}: {amt(p['amount'], lang)} "
             + L(lang, f"({p['count']} record(s))", f"({p['count']} سجل)") for p in parts]
    sum_text = " + ".join(num(p["amount"]) for p in parts)
    text = "\n".join([L(lang, f"Your spending in {label}, from all {spent['count']} expense records:",
                        f"إنفاقك في {label}، من كل سجلات المصروفات ({spent['count']}):")] + lines + [
        L(lang, f"Together: {sum_text} = {amt(total, lang)}, {pct(percent(total, spent['total']))} of your "
                f"{amt(spent['total'], lang)} total spending.",
          f"معًا: {sum_text} = {amt(total, lang)}، أي {pct(percent(total, spent['total']))} من إجمالي إنفاقك "
          f"{amt(spent['total'], lang)}.")])
    data = {"months": months, "parts": [{**p, "amount": money(p["amount"])} for p in parts], "amount": money(total),
            "records_checked": spent["count"]}
    return _result(text, "get_category_spending", data, [f"{sum_text} = {num(total)}"],
                   {"months": months, "categories": [list(c) for c in q.categories]})


def _category(q, user_id, cur):
    if len(dict.fromkeys(q.categories)) > 1:
        return _categories_together(q, user_id, cur)
    lang, months = q.language, _months(q, cur, user_id)
    category, sub = q.categories[0]
    spent = facts.spending(user_id, months)
    label, what = period_label(months, lang), cat_label(category, sub, lang)
    rows = [t for t in spent["transactions"]
            if t["category"] == category and (not sub or t["subcategory"] == sub)]
    amount = sum((t["amount"] for t in rows), ZERO)
    share = percent(amount, spent["total"])
    if not rows:
        # Say what was checked, so "nothing recorded" is never a retrieval gap.
        same_main = sum((t["amount"] for t in spent["transactions"] if t["category"] == category), ZERO)
        extra = (L(lang, f" Your {name(category, lang)} spending that month was {amt(same_main, lang)}, all in other "
                         "subcategories.",
                   f" إنفاقك على {name(category, lang)} في ذلك الشهر كان {amt(same_main, lang)}، كله في فئات فرعية أخرى.")
                 if sub and same_main else "")
        text = L(lang, f"You recorded no spending on {what} in {label}: I checked all {spent['count']} expense "
                       f"records of that period and none is {what}.{extra}",
                 f"لم تسجل أي إنفاق على {what} في {label}: راجعت كل سجلات المصروفات في تلك الفترة "
                 f"({spent['count']}) ولا يوجد بينها {what}.{extra}")
        return _result(text, "get_category_spending", {"category": category, "subcategory": sub,
                                                       "months": months, "amount": 0}, [],
                       {"months": months, "category": [category, sub]})
    if len(months) == 1:
        text = L(lang, f"You spent {amt(amount, lang)} on {what} in {label}: {pct(share)} of your "
                       f"{amt(spent['total'], lang)} total spending that month.",
                 f"صرفت {amt(amount, lang)} على {what} في {label}، أي {pct(share)} من إجمالي "
                 f"إنفاقك في ذلك الشهر ({amt(spent['total'], lang)}).")
    else:
        per_month = {m: sum((t["amount"] for t in rows if (t["date"] or "")[:7] == m), ZERO) for m in months}
        used = _with_data(months, spent)
        parts = " + ".join(num(per_month[m]) for m in used)
        text = L(lang, f"You spent {amt(amount, lang)} on {what} over {label} ({parts}): "
                       f"{pct(share)} of the {amt(spent['total'], lang)} you spent in that time.",
                 f"صرفت {amt(amount, lang)} على {what} خلال {label} ({parts})، أي {pct(share)} من "
                 f"إجمالي {amt(spent['total'], lang)} صرفتها في تلك الفترة.")
    if not sub:
        subs = sorted(((s, v) for s, v in spent["by_subcategory"].get(category, {}).items() if s),
                      key=lambda kv: -kv[1])
        if len(subs) > 1:
            text += " " + L(lang, "Breakdown: ", "التفصيل: ") + ", ".join(
                f"{name(s, lang)} {amt(v, lang)}" for s, v in subs[:5]) + "."
    steps = [L(lang, f"{what} = sum of {len(rows)} record(s) in {label} = {amt(amount, lang)}",
               f"{what} = مجموع {len(rows)} سجل في {label} = {amt(amount, lang)}"),
             L(lang, f"Share = {num(amount)} ÷ {num(spent['total'])} × 100 = {pct(share)}",
               f"النسبة = {num(amount)} ÷ {num(spent['total'])} × 100 = {pct(share)}")]
    if parse.JUDGE.search(q.text) and len(months) == 1 and not sub:
        # "Am I spending too much on food?": the facts that answer it, not an opinion.
        text += " " + _category_judgement(user_id, category, amount, months[0], lang)
    if ANALYZE.search(q.text) and len(months) == 1 and not sub:
        text += "\n" + _category_analysis(user_id, category, amount, months[0], spent, lang)
    steps += _quality_notes(spent, lang)
    data = {"category": category, "subcategory": sub, "months": months, "amount": money(amount),
            "percent_of_spending": share, "total_spending": money(spent["total"])}
    return _result(text, "get_category_spending", data, steps,
                   {"months": months, "category": [category, sub]})


ANALYZE = parse.ANALYZE_WORD


def _category_analysis(user_id, category, amount, month, spent, lang):
    """"Analyze my food expenses": against the month before, line by line, which
    part is essential and which flexible, and where a saving would actually come from."""
    from app.preferences.user_preferences import is_essential, preference_map
    prefs = preference_map(user_id)
    prev_month = facts.shift_month(month, -1)
    before = facts.spending(user_id, [prev_month])
    lines = []
    old_total = before["by_category"].get(category)
    if before["count"] and old_total is not None:
        diff = amount - old_total
        lines.append(L(lang, f"- Against {month_label(prev_month, lang)}: {num(amount)} − {num(old_total)} = "
                             f"{'+' if diff >= 0 else '−'}{amt(abs(diff), lang)}",
                       f"- مقارنة بـ{month_label(prev_month, lang)}: {num(amount)} − {num(old_total)} = "
                       f"{'+' if diff >= 0 else '−'}{amt(abs(diff), lang)}"))
        moved = [c for c in facts.subcategory_changes({category: before["by_subcategory"].get(category, {})},
                                                      {category: spent["by_subcategory"].get(category, {})})]
        if moved:
            lines.append(L(lang, "- Where it changed: ", "- أين تغيّر: ") + L(lang, ", ", "، ").join(
                f"{name(sname or category, lang)} {num(a)} → {num(b)} ({'+' if d >= 0 else '−'}{num(abs(d))})"
                for _, sname, a, b, d in moved[:4]))
    subs = spent["by_subcategory"].get(category, {})
    flexible = {sname: v for sname, v in subs.items() if v > 0 and not is_essential(user_id, category, sname, prefs)}
    essential = sum((v for sname, v in subs.items() if sname not in flexible), ZERO)
    flex_total = sum(flexible.values(), ZERO)
    if flexible:
        lines.append(L(lang, f"- Essential: {amt(essential, lang)}; flexible (you can change it): "
                             + " + ".join(f"{name(sname, lang)} {num(v)}" for sname, v in flexible.items())
                             + f" = {amt(flex_total, lang)}",
                       f"- أساسي: {amt(essential, lang)}؛ مرن (يمكنك تغييره): "
                       + " + ".join(f"{name(sname, lang)} {num(v)}" for sname, v in flexible.items())
                       + f" = {amt(flex_total, lang)}"))
        biggest = max(flexible.items(), key=lambda kv: kv[1])
        prev = before["by_subcategory"].get(category, {}).get(biggest[0])
        if prev is not None and prev < biggest[1]:
            lines.append(L(lang, f"- Opportunity: {name(biggest[0], lang)} back to {month_label(prev_month, lang)}'s "
                                 f"{num(prev)} saves {num(biggest[1])} − {num(prev)} = {amt(biggest[1] - prev, lang)} a "
                                 "month, with the essentials untouched",
                           f"- فرصة: إعادة {name(biggest[0], lang)} إلى مستوى {month_label(prev_month, lang)} "
                           f"({num(prev)}) توفر {num(biggest[1])} − {num(prev)} = {amt(biggest[1] - prev, lang)} شهريًا "
                           "دون المساس بالأساسيات"))
    return "\n".join(lines)


def _top(q, user_id, cur):
    lang, months = q.language, _months(q, cur, user_id)
    spent = facts.spending(user_id, months)
    if not spent["count"]:
        return _total(q, user_id, cur)
    ranked = facts.ranking(spent["by_category"], spent["total"])
    top, rest = ranked[0], ranked[1:3]
    label = period_label(months, lang)
    text = L(lang, f"Your biggest expense in {label} is {top['category']}: {amt(top['amount'], lang)}, "
                   f"{pct(top['percent'])} of the {amt(spent['total'], lang)} you spent.",
             f"أكبر مصروفاتك في {label} هو {name(top['category'], lang)}: {amt(top['amount'], lang)}، "
             f"أي {pct(top['percent'])} من إجمالي {amt(spent['total'], lang)}.")
    if rest:
        text += " " + L(lang, "Next: ", "يليه: ") + ", ".join(
            f"{name(r['category'], lang)} {amt(r['amount'], lang)} ({pct(r['percent'])})" for r in rest) + "."
    subs = [(s, v) for s, v in spent["by_subcategory"].get(top["category"], {}).items() if s]
    if len(subs) > 1:
        s, v = max(subs, key=lambda kv: kv[1])
        text += " " + L(lang, f"Within {top['category']}, {s} is the largest part ({amt(v, lang)}).",
                        f"وداخل {name(top['category'], lang)}، أكبر جزء هو {name(s, lang)} ({amt(v, lang)}).")
    steps = [L(lang, f"Spending per category in {label}, from {spent['count']} records, sorted from the largest",
               f"الإنفاق لكل فئة في {label} من {spent['count']} سجل، مرتبًا من الأكبر")] + [
        f"{name(r['category'], lang)}: {num(r['amount'])} ÷ {num(spent['total'])} × 100 = {pct(r['percent'])}"
        for r in ranked]
    data = {"months": months, "total": money(spent["total"]),
            "ranking": [{**r, "amount": money(r["amount"])} for r in ranked]}
    return _result(text, "analyze_categories", data, steps, {"months": months})


def _average(q, user_id, cur):
    lang = q.language
    months = _months(q, cur, user_id, default="three")
    if len(months) == 1 and not q.period:
        months = facts.months_back(cur, 3)
    spent = facts.spending(user_id, months)
    used = _with_data(months, spent)
    if not spent["count"]:
        return _total(q, user_id, cur)
    avg = spent["total"] / len(used)
    parts = " + ".join(num(spent["by_month"][m]) for m in used)
    label = period_label(used, lang)
    text = L(lang, f"Your average monthly spending over {label} is {amt(avg, lang)} "
                   f"(({parts}) ÷ {len(used)} = {num(spent['total'])} ÷ {len(used)}).",
             f"متوسط إنفاقك الشهري خلال {label} هو {amt(avg, lang)} "
             f"(({parts}) ÷ {len(used)} = {num(spent['total'])} ÷ {len(used)}).")
    if len(used) < len(months):
        text += " " + L(lang, f"Only {len(used)} of those months have records, so the average uses those.",
                        f"فقط {len(used)} من هذه الأشهر فيها سجلات، لذلك حُسب المتوسط عليها.")
    steps = [L(lang, f"Average = total ÷ months with records = {num(spent['total'])} ÷ {len(used)} = {amt(avg, lang)}",
               f"المتوسط = الإجمالي ÷ عدد الأشهر = {num(spent['total'])} ÷ {len(used)} = {amt(avg, lang)}")]
    data = {"months": used, "average": money(avg), "total": money(spent["total"])}
    return _result(text, "analyze_spending_patterns", data, steps, {"months": used})


def _compare_months(q, cur):
    named = [m for m in q.months]
    if len(named) >= 2:
        return named[0], named[1]
    if len(named) == 1 and q.follow_up:
        return named[0], facts.shift_month(named[0], -1)   # "what about last month?"
    if len(named) == 1 and named[0] != cur:
        return cur, named[0]
    return cur, facts.shift_month(cur, -1)


def _change_lines(before, after, lang, limit=4, only=None):
    out = []
    for c, s, a, b, d in facts.subcategory_changes(before["by_subcategory"], after["by_subcategory"]):
        if only == "up" and d <= 0 or only == "down" and d >= 0:
            continue
        sign = "+" if d > 0 else "−"
        out.append(f"{cat_label(c, s, lang)} {sign}{num(abs(d))} ({num(a)} → {num(b)})")
        if len(out) == limit:
            break
    return out


def _compare_category(q, user_id, a_month, b_month):
    lang = q.language
    category, sub = q.categories[0]
    what = cat_label(category, sub, lang)

    def total(month):
        rows = [t for t in facts.spending(user_id, [month])["transactions"]
                if t["category"] == category and (not sub or t["subcategory"] == sub)]
        return sum((t["amount"] for t in rows), ZERO)
    la, lb = month_label(a_month, lang), month_label(b_month, lang)
    empty = [label for m, label in ((a_month, la), (b_month, lb)) if not facts.spending(user_id, [m])["count"]]
    if empty:
        # A month with no records at all is missing data, not a month of 0 spending.
        text = L(lang, f"There is no spending recorded for {empty[0]}, so I cannot compare {what} between the "
                       "two months.",
                 f"لا توجد مصروفات مسجلة في {empty[0]}، لذلك لا يمكنني مقارنة {what} بين الشهرين.")
        return _result(text, "get_category_spending", {}, [],
                       {"months": [a_month, b_month], "category": [category, sub]})
    a, b = total(a_month), total(b_month)
    diff = a - b
    change = percent(diff, b)
    trend = (L(lang, "the same", "المبلغ نفسه") if diff == 0 else
             L(lang, f"{amt(abs(diff), lang)} {'more' if diff > 0 else 'less'}",
               f"{'أكثر' if diff > 0 else 'أقل'} بمقدار {amt(abs(diff), lang)}"))
    text = L(lang, f"{what}: {amt(a, lang)} in {la} against {amt(b, lang)} in {lb}, {trend}"
                   + (f" ({'+' if diff > 0 else ''}{pct(change)})." if change is not None and diff else "."),
             f"{what}: {amt(a, lang)} في {la} مقابل {amt(b, lang)} في {lb}، {trend}"
             + (f" ({'+' if diff > 0 else ''}{pct(change)})." if change is not None and diff else "."))
    steps = [L(lang, f"Difference = {num(a)} − {num(b)} = {num(diff)}", f"الفرق = {num(a)} − {num(b)} = {num(diff)}")]
    if change is not None:
        steps.append(f"{num(diff)} ÷ {num(b)} × 100 = {pct(change)}")
    data = {"category": category, "subcategory": sub, "months": [a_month, b_month],
            "totals": [money(a), money(b)], "difference": money(diff), "percent_change": change}
    return _result(text, "get_category_spending", data, steps,
                   {"months": [a_month, b_month], "category": [category, sub]})


def _compare(q, user_id, cur):
    lang = q.language
    a_month, b_month = _compare_months(q, cur)
    if q.categories:
        return _compare_category(q, user_id, a_month, b_month)
    a, b = facts.spending(user_id, [a_month]), facts.spending(user_id, [b_month])
    la, lb = month_label(a_month, lang), month_label(b_month, lang)
    if not b["count"] or not a["count"]:
        missing = lb if not b["count"] else la
        text = L(lang, f"There is no spending recorded for {missing}, so I cannot compare the two months.",
                 f"لا توجد مصروفات مسجلة في {missing}، لذلك لا يمكنني المقارنة بين الشهرين.")
        return _result(text, "get_monthly_spending", {}, [], {"months": [a_month, b_month]})
    diff = a["total"] - b["total"]
    change = percent(diff, b["total"])
    if diff == 0:
        text = L(lang, f"You spent the same in {la} and {lb}: {amt(a['total'], lang)}.",
                 f"صرفت المبلغ نفسه في {la} و{lb}: {amt(a['total'], lang)}.")
    else:
        more = diff > 0
        text = L(lang, f"In {la} you spent {amt(a['total'], lang)}, which is {amt(abs(diff), lang)} "
                       f"{'more' if more else 'less'} than in {lb} ({amt(b['total'], lang)}), "
                       f"a change of {'+' if more else ''}{pct(change)}.",
                 f"في {la} صرفت {amt(a['total'], lang)}، أي {'أكثر' if more else 'أقل'} بمقدار "
                 f"{amt(abs(diff), lang)} مما صرفته في {lb} ({amt(b['total'], lang)})، بتغير "
                 f"{'+' if more else ''}{pct(change)}.")
    lines = _change_lines(b, a, lang)
    if lines:
        text += " " + L(lang, "Biggest changes: ", "أكبر التغيرات: ") + "; ".join(lines) + "."
    steps = [L(lang, f"Difference = {num(a['total'])} − {num(b['total'])} = {num(diff)}",
               f"الفرق = {num(a['total'])} − {num(b['total'])} = {num(diff)}"),
             L(lang, f"Change = {num(diff)} ÷ {num(b['total'])} × 100 = {pct(change)}",
               f"نسبة التغير = {num(diff)} ÷ {num(b['total'])} × 100 = {pct(change)}")]
    data = {"months": [a_month, b_month], "totals": [money(a["total"]), money(b["total"])],
            "difference": money(diff), "percent_change": change}
    return _result(text, "get_monthly_spending", data, steps, {"months": [a_month, b_month]})


def _why(q, user_id, cur):
    lang = q.language
    a_month, b_month = _compare_months(q, cur)
    a, b = facts.spending(user_id, [a_month]), facts.spending(user_id, [b_month])
    la, lb = month_label(a_month, lang), month_label(b_month, lang)
    if not a["count"] or not b["count"]:
        return _compare(q, user_id, cur)
    diff = a["total"] - b["total"]
    ups = _change_lines(b, a, lang, limit=5, only="up")
    downs = _change_lines(b, a, lang, limit=3, only="down")
    change = percent(diff, b["total"])
    if diff > 0:
        text = L(lang, f"Your spending rose by {amt(diff, lang)} ({num(a['total'])} − {num(b['total'])}), from "
                       f"{amt(b['total'], lang)} in {lb} to {amt(a['total'], lang)} in {la}: +{pct(change)}.",
                 f"ارتفع إنفاقك بمقدار {amt(diff, lang)} ({num(a['total'])} − {num(b['total'])})، من "
                 f"{amt(b['total'], lang)} في {lb} إلى {amt(a['total'], lang)} في {la}: +{pct(change)}.")
    else:
        text = L(lang, f"Your spending did not rise: {amt(a['total'], lang)} in {la} against "
                       f"{amt(b['total'], lang)} in {lb}.",
                 f"إنفاقك لم يرتفع: {amt(a['total'], lang)} في {la} مقابل {amt(b['total'], lang)} في {lb}.")
    if ups:
        text += " " + L(lang, "The increase comes from: ", "الزيادة جاءت من: ") + "; ".join(ups) + "."
    new_items = [t for t in a["transactions"]
                 if not any(u["category"] == t["category"] and u["subcategory"] == t["subcategory"]
                            for u in b["transactions"])]
    if new_items:
        text += " " + L(lang, "New this month: ", "جديد هذا الشهر: ") + ", ".join(
            f"{t['description'] or cat_label(t['category'], t['subcategory'], lang)} {amt(t['amount'], lang)}"
            for t in new_items[:3]) + "."
    if downs:
        text += " " + L(lang, "Partly offset by: ", "قابلها انخفاض في: ") + "; ".join(downs) + "."
    steps = [L(lang, f"Each subcategory in {la} minus the same subcategory in {lb}; the total change is "
                     f"{num(a['total'])} − {num(b['total'])} = {num(diff)}",
               f"كل فئة فرعية في {la} ناقص نفسها في {lb}؛ التغير الكلي = {num(a['total'])} − "
               f"{num(b['total'])} = {num(diff)}")]
    if change is not None:
        steps.append(L(lang, f"Change = {num(diff)} ÷ {num(b['total'])} × 100 = {pct(change)}",
                       f"نسبة التغير = {num(diff)} ÷ {num(b['total'])} × 100 = {pct(change)}"))
    data = {"months": [a_month, b_month], "totals": [money(a["total"]), money(b["total"])],
            "difference": money(diff), "percent_change": change}
    return _result(text, "analyze_spending_patterns", data, steps, {"months": [a_month, b_month]})


# ---------- income, savings, budget ----------

def _income(q, user_id, cur):
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    got = facts.income(user_id, month)
    if got["total"] is None:
        text = L(lang, "You have no income recorded yet. Add it on the Money page.",
                 "لا يوجد دخل مسجل بعد. أضفه من صفحة المال.")
        return _result(text, "analyze_income", {}, [], {})
    label = month_label(got["month"], lang)
    sources = ", ".join(f"{name(s, lang)} {amt(v, lang)}" for s, v in
                        sorted(got["by_source"].items(), key=lambda kv: -kv[1]))
    if got["basis"] == "recorded in the month":
        text = L(lang, f"Your income in {label} is {amt(got['total'], lang)}",
                 f"دخلك في {label} هو {amt(got['total'], lang)}")
    else:
        text = L(lang, f"No income is recorded for {month_label(month, lang)}; your latest monthly income "
                       f"on record ({label or 'undated'}) is {amt(got['total'], lang)}",
                 f"لا يوجد دخل مسجل في {month_label(month, lang)}؛ آخر دخل شهري مسجل ({label}) هو "
                 f"{amt(got['total'], lang)}")
    text += (f" ({sources})." if len(got["by_source"]) > 1 else ".")
    asked_source = next((src for src, pattern in INCOME_SOURCES.items()
                         if src in got["by_source"] and re.search(pattern, q.text)), None)
    if asked_source:
        # "كم بيجيني من الشغل الحر؟": that source, with its share
        value = got["by_source"][asked_source]
        share = percent(value, got["total"])
        text = L(lang, f"{name(asked_source, lang)} brought {amt(value, lang)} in {label}, {pct(share)} of your "
                       f"{amt(got['total'], lang)} income ({num(value)} ÷ {num(got['total'])} × 100).",
                 f"{name(asked_source, lang)} جلب لك {amt(value, lang)} في {label}، أي {pct(share)} من دخلك "
                 f"{amt(got['total'], lang)} ({num(value)} ÷ {num(got['total'])} × 100).")
    elif len(got["by_source"]) > 1 and got["total"]:
        shares = ", ".join(f"{name(src, lang)} {pct(percent(v, got['total']))}" for src, v in
                           sorted(got["by_source"].items(), key=lambda kv: -kv[1]))
        text += " " + L(lang, f"Shares: {shares}.", f"النسب: {shares}.")
    steps = [L(lang, f"Income = sum of {got['count']} income record(s): {sources} = {amt(got['total'], lang)}",
               f"الدخل = مجموع {got['count']} سجل دخل: {sources} = {amt(got['total'], lang)}")]
    data = {"month": got["month"], "income": money(got["total"]),
            "by_source": {s: money(v) for s, v in got["by_source"].items()}}
    return _result(text, "analyze_income", data, steps, {"months": [month]})


# Words for the income sources the app knows (income.source values).
INCOME_SOURCES = {
    "Salary": r"\b(?:salary|wages?|pay ?check)\b|راتب|مرتب|ماهيه|معاش",
    "Freelancer": r"\b(?:freelanc\w*|side (?:job|work|gig)s?|gigs?)\b|عمل حر|العمل الحر|شغل حر|الشغل الحر|فريلانس|"
                  r"شغل جانبي|عمل جانبي",
    "From Dad": r"\b(?:dad|father|parents?)\b|(?<![ء-ي])(?:ابوي|ابويا|الوالد|والدي|الاب|ابي|من ابوي|اهلي)(?![ء-ي])",
}


def _savings(q, user_id, cur):
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    snap = facts.month_snapshot(user_id, month)
    label = month_label(month, lang)
    earned = snap["income"]
    if earned["total"] is None:
        text = L(lang, "I cannot work out your savings: no income is recorded yet.",
                 "لا يمكنني حساب مدخراتك: لا يوجد دخل مسجل بعد.")
        return _result(text, "get_current_savings", {}, [], {})
    left, income, spent = snap["remaining"], earned["total"], snap["expenses"]
    if left >= 0:
        text = L(lang, f"In {label} you have {amt(left, lang)} left after spending: income "
                       f"{amt(income, lang)} − spending {amt(spent, lang)}. That is a saving rate of "
                       f"{pct(snap['saving_rate'])} of your income.",
                 f"في {label} تبقى لك {amt(left, lang)} بعد المصروفات: الدخل {amt(income, lang)} − "
                 f"المصروفات {amt(spent, lang)}. أي أن معدل ادخارك {pct(snap['saving_rate'])} من دخلك.")
    else:
        text = L(lang, f"In {label} you spent {amt(-left, lang)} more than your income: income "
                       f"{amt(income, lang)} − spending {amt(spent, lang)} = {num(left)} {CURRENCY['en']}. "
                       "Nothing was left to save this month.",
                 f"في {label} صرفت أكثر من دخلك بمقدار {amt(-left, lang)}: الدخل {amt(income, lang)} − "
                 f"المصروفات {amt(spent, lang)}. لم يتبقَّ شيء للادخار هذا الشهر.")
    if earned["basis"] != "recorded in the month":
        text += " " + L(lang, f"(No income is dated in {label}; your latest monthly income is used.)",
                        f"(لا يوجد دخل بتاريخ {label}؛ استُخدم آخر دخل شهري مسجل.)")
    kept = facts.recorded_savings(user_id)
    if kept["saved_in_goals"] or kept["profile_balance"]:
        parts = []
        if kept["saved_in_goals"]:
            parts.append(L(lang, f"{amt(kept['saved_in_goals'], lang)} saved toward your goals",
                           f"{amt(kept['saved_in_goals'], lang)} مدخرة لأهدافك"))
        if kept["profile_balance"]:
            parts.append(L(lang, f"a savings balance of {amt(kept['profile_balance'], lang)} on your profile",
                           f"رصيد ادخار {amt(kept['profile_balance'], lang)} في ملفك"))
        text += " " + L(lang, "Money actually put aside so far: ", "المبالغ المدخرة فعليًا حتى الآن: ") \
            + L(lang, " and ", " و").join(parts) + "."
    sources = []
    if parse.JUDGE.search(q.text) and snap["saving_rate"] is not None:
        guide = _guideline_source()
        rate = Decimal(str(snap["saving_rate"]))
        verdict = (L(lang, "above", "أعلى من") if rate > 20 else L(lang, "below", "أقل من") if rate < 20
                   else L(lang, "exactly at", "يساوي"))
        text += " " + L(lang, f"The 50/30/20 guideline (Duke University, Budgeting & Spending Plans) puts 20% of "
                              f"income toward savings; your {pct(snap['saving_rate'])} is {verdict} that.",
                        f"قاعدة 50/30/20 (جامعة ديوك، Budgeting & Spending Plans) تخصص 20% من الدخل للادخار؛ "
                        f"ونسبتك {pct(snap['saving_rate'])} {verdict} ذلك.")
        sources = [guide] if guide else []
    steps = [L(lang, f"Remaining = income − expenses = {num(income)} − {num(spent)} = {num(left)}",
               f"المتبقي = الدخل − المصروفات = {num(income)} − {num(spent)} = {num(left)}"),
             L(lang, f"Saving rate = remaining ÷ income × 100 = {num(left)} ÷ {num(income)} × 100 = "
                     f"{pct(snap['saving_rate'])}",
               f"معدل الادخار = المتبقي ÷ الدخل × 100 = {num(left)} ÷ {num(income)} × 100 = "
               f"{pct(snap['saving_rate'])}")] + _quality_notes(snap["spending"], lang)
    data = {"month": month, "income": money(income), "expenses": money(spent),
            "remaining": money(left), "saving_rate": snap["saving_rate"],
            "saved_in_goals": money(kept["saved_in_goals"])}
    out = _result(text, "get_current_savings", data, steps, {"months": [month]})
    out["sources"] = sources
    return out


def _guideline_source():
    """The knowledge-base entry behind the 20% savings guideline, as a source."""
    from app.rag.knowledge import list_entries
    entry = next((e for e in list_entries() if e.get("id") == "budget-503020"), None)
    return dict(entry["source"], entry_id=entry["id"], passage_id=f"{entry['id']}-1") if entry else None


# ---------- income, spending and what is left, together ----------

def _income_line(got, lang):
    sources = L(lang, ", ", "، ").join(f"{name(s, lang)} {num(v)}" for s, v in
                                       sorted(got["by_source"].items(), key=lambda kv: -kv[1]))
    return sources


def _summary(q, user_id, cur):
    """"How much did I earn, spend and have left?": every part from the same
    month's records. remaining = income − expenses; saving rate = remaining ÷ income × 100."""
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    snap = facts.month_snapshot(user_id, month)
    got, spent = snap["income"], snap["spending"]
    label = month_label(month, lang)
    lines = [L(lang, f"In {label}, from your saved records:", f"في {label}، من سجلاتك المحفوظة:")]
    steps = []
    if got["total"] is None:
        lines.append(L(lang, "- Income: none recorded, so what is left cannot be worked out. Add your income on the "
                             "Money page.",
                       "- الدخل: لا يوجد دخل مسجل، لذلك لا يمكن حساب المتبقي. أضف دخلك من صفحة المال."))
    else:
        income_note = "" if got["basis"] == "recorded in the month" else L(
            lang, f" (none is dated in {label}; your latest monthly income, {month_label(got['month'], lang)}, is used)",
            f" (لا يوجد دخل بتاريخ {label}؛ استُخدم آخر دخل شهري مسجل، {month_label(got['month'], lang)})")
        breakdown = f" ({_income_line(got, lang)})" if len(got["by_source"]) > 1 else ""
        lines.append(L(lang, f"- Income: {amt(got['total'], lang)}{breakdown}{income_note}",
                       f"- الدخل: {amt(got['total'], lang)}{breakdown}{income_note}"))
        steps.append(L(lang, f"Income = sum of {got['count']} income record(s) = {num(got['total'])}",
                       f"الدخل = مجموع {got['count']} سجل دخل = {num(got['total'])}"))
    if spent["count"]:
        lines.append(L(lang, f"- Expenses: {amt(spent['total'], lang)} ({spent['count']} transactions)",
                       f"- المصروفات: {amt(spent['total'], lang)} ({spent['count']} عملية)"))
    else:
        lines.append(L(lang, f"- Expenses: no spending is recorded for {label} (0)",
                       f"- المصروفات: لا توجد مصروفات مسجلة في {label} (0)"))
    steps.append(L(lang, f"Expenses = sum of {spent['count']} expense record(s) dated {label} = {num(spent['total'])}",
                   f"المصروفات = مجموع {spent['count']} سجل مصروفات بتاريخ {label} = {num(spent['total'])}"))
    left = snap["remaining"]
    if left is not None:
        formula = f"{num(got['total'])} − {num(spent['total'])} = {amt(left, lang)}"
        if left >= 0:
            lines.append(L(lang, f"- Left after expenses: {formula}", f"- المتبقي بعد المصروفات: {formula}"))
        else:
            lines.append(L(lang, f"- Left after expenses: {formula}: you spent {amt(-left, lang)} more than your income",
                           f"- المتبقي بعد المصروفات: {formula}: صرفت أكثر من دخلك بمقدار {amt(-left, lang)}"))
        steps.append(L(lang, f"Remaining = income − expenses = {formula}", f"المتبقي = الدخل − المصروفات = {formula}"))
        if snap["saving_rate"] is not None:
            lines.append(L(lang, f"- Saving rate: {num(left)} ÷ {num(got['total'])} × 100 = {pct(snap['saving_rate'])}",
                           f"- نسبة الادخار: {num(left)} ÷ {num(got['total'])} × 100 = {pct(snap['saving_rate'])}"))
            steps.append(f"{num(left)} ÷ {num(got['total'])} × 100 = {pct(snap['saving_rate'])}")
        else:
            lines.append(L(lang, "- Saving rate: not defined, because the income is 0",
                           "- نسبة الادخار: غير محددة لأن الدخل 0"))
    steps += _quality_notes(spent, lang)
    data = {"month": month, "income": None if got["total"] is None else money(got["total"]),
            "income_by_source": {s: money(v) for s, v in got["by_source"].items()},
            "expenses": money(spent["total"]), "expense_count": spent["count"],
            "remaining": None if left is None else money(left), "saving_rate": snap["saving_rate"]}
    return _result("\n".join(lines), "calculate_balance", data, steps, {"months": [month]})


# ---------- the records themselves ----------

COUNT_ASKED = re.compile(r"(?:عمليات|العمليات|معاملات|المعاملات|transactions|records|entries)\s+(?:ال)?(\d{1,4})(?!\d)|"
                         r"(?:ال)?(\d{1,4})\s+(?:عمليه|عمليات|معاملات|معامله|transactions?|records?|entries)")


def _claimed_numbers(text, known, count=None):
    """Amounts a question (or an earlier answer) states that the records do not
    contain: "where did 4,400 come from?". Years, days and the asked count are skipped."""
    out = []
    for value, is_pct, _ in parse.amounts(text or ""):
        v = dec(value)
        if is_pct or v < 100 or (count is not None and v == count) or any(v == dec(k) for k in known if k is not None):
            continue
        if v not in out:
            out.append(v)
    return out


def _transactions(q, user_id, cur, recent=None):
    lang = q.language
    months = _months(q, cur, user_id)
    spent = facts.spending(user_id, months)
    label = period_label(months, lang)
    category = sub = None
    if q.categories:
        category, sub = q.categories[0]
    elif recent and recent.get("category") and re.search(r"(?:ها|ه)(?![ء-ي])|\b(?:them|those|it)\b", q.text):
        category, sub = recent["category"]                     # "show me its details"
    rows = [t for t in spent["transactions"]
            if category is None or (t["category"] == category and (not sub or t["subcategory"] == sub))]
    rows.sort(key=lambda t: (t["date"] or "", t["id"]))
    if rows and parse.LARGEST.search(q.text):
        # "What was my biggest purchase in October?"
        biggest = sorted(rows, key=lambda t: (-t["amount"], t["date"] or ""))[:3]
        top = biggest[0]
        text = L(lang, f"Your largest transaction in {label}: {top['description'] or cat_label(top['category'], None, lang)}"
                       f" ({cat_label(top['category'], top['subcategory'] or None, lang)}), {amt(top['amount'], lang)} "
                       f"on {top['date']}.",
                 f"أكبر عملية في {label}: {top['description'] or cat_label(top['category'], None, lang)} "
                 f"({cat_label(top['category'], top['subcategory'] or None, lang)})، {amt(top['amount'], lang)} "
                 f"بتاريخ {top['date']}.")
        if len(biggest) > 1:
            text += " " + L(lang, "Next: ", "يليها: ") + ", ".join(
                f"{t['description']} {amt(t['amount'], lang)}" for t in biggest[1:]) + "."
        data = {"months": months, "largest": [{"date": t["date"], "description": t["description"],
                                                "category": t["category"], "amount": money(t["amount"])}
                                               for t in biggest], "count": len(rows)}
        return _result(text, "list_transactions", data,
                       [L(lang, f"The {len(rows)} transactions of {label}, sorted by amount",
                          f"عمليات {label} ({len(rows)}) مرتبة حسب المبلغ")],
                       {"months": months})
    total = sum((t["amount"] for t in rows), ZERO)
    if category is None and total != spent["total"]:
        raise ArithmeticError(f"transaction list {total} != monthly total {spent['total']}")
    what = cat_label(category, sub, lang) if category else L(lang, "expense", "المصروفات")
    if not rows:
        text = L(lang, f"No {what} transactions are recorded for {label}.",
                 f"لا توجد عمليات {what} مسجلة في {label}.")
        return _result(text, "list_transactions", {"months": months, "count": 0, "total": 0}, [],
                       {"months": months, "category": [category, sub] if category else None})
    head = L(lang, f"Your {what} transactions in {label}, from your saved records ({len(rows)}):",
             f"عمليات {what} المسجلة في {label}، من سجلاتك المحفوظة ({len(rows)}):")
    shown = rows[:60]
    lines = [head] + [f"- {t['date']} · {t['description'] or cat_label(t['category'], t['subcategory'], lang)} · "
                      f"{cat_label(t['category'], t['subcategory'] or None, lang)} · {amt(t['amount'], lang)}"
                      for t in shown]
    if len(rows) > len(shown):
        lines.append(L(lang, f"… and {len(rows) - len(shown)} more (all are counted in the total).",
                       f"… و{len(rows) - len(shown)} عملية أخرى (كلها محسوبة في المجموع)."))
    lines.append(L(lang, f"Total: {len(rows)} transactions = {amt(total, lang)}",
                   f"المجموع: {len(rows)} عملية = {amt(total, lang)}"))
    m = COUNT_ASKED.search(q.text)
    asked = int(next(g for g in m.groups() if g)) if m else None
    if asked is not None and asked != len(rows):
        lines.append(L(lang, f"You mentioned {asked} transactions; your records have {len(rows)} for {label}.",
                       f"ذكرت {asked} عملية؛ وسجلاتك فيها {len(rows)} عملية في {label}."))
    for v in _claimed_numbers(q.text, [total], asked):
        lines.append(L(lang, f"{num(v)} is not in your records for {label}: the recorded total is {amt(total, lang)}.",
                       f"الرقم {num(v)} غير موجود في سجلاتك لشهر {label}: المجموع المسجل هو {amt(total, lang)}."))
    if spent["possible_duplicates"] and category is None:
        lines.append(L(lang, f"{spent['possible_duplicates']} transaction(s) look like duplicates (same day, amount and "
                             "description); they are counted as recorded, so please check them.",
                       f"{spent['possible_duplicates']} عملية تبدو مكررة (نفس اليوم والمبلغ والوصف)؛ وهي محسوبة كما "
                       "سُجلت، فيُرجى مراجعتها."))
    steps = [L(lang, f"Expense records = transactions dated {label}, outside the Income category, for your profile only",
               f"سجلات المصروفات = المعاملات بتاريخ {label}، خارج فئة الدخل، لملفك فقط"),
             " + ".join(num(t["amount"]) for t in rows) + f" = {num(total)}"]
    data = {"months": months, "count": len(rows), "total": money(total),
            "transactions": [{"date": t["date"], "description": t["description"], "category": t["category"],
                              "subcategory": t["subcategory"], "amount": money(t["amount"])} for t in rows]}
    return _result("\n".join(lines), "list_transactions", data, steps,
                   {"months": months, "category": [category, sub] if category else None})


def _explain_source(q, user_id, cur, previous, last_answer):
    """"Where did that number come from?" after an answer without a recorded
    calculation (for example one the model wrote): the number is worked out again
    from the records, and any figure the records do not support is named."""
    lang = q.language
    month = ((previous or {}).get("months") or [None])[0] or _months(q, cur, user_id)[-1]
    snap = facts.month_snapshot(user_id, month)
    got, spent = snap["income"], snap["spending"]
    label = month_label(month, lang)
    lines = [L(lang, f"Your numbers for {label}, worked out again from your saved records:",
               f"أرقامك لشهر {label}، محسوبة من جديد من سجلاتك المحفوظة:"),
             L(lang, f"- Expenses: the {spent['count']} expense transactions dated {label} (Income category excluded), "
                     f"added up = {amt(spent['total'], lang)}",
               f"- المصروفات: مجموع {spent['count']} عملية مصروفات بتاريخ {label} (بدون فئة الدخل) = "
               f"{amt(spent['total'], lang)}")]
    if got["total"] is not None:
        lines.append(L(lang, f"- Income: the {got['count']} income record(s) ({_income_line(got, lang)}) = "
                             f"{amt(got['total'], lang)}",
                       f"- الدخل: {got['count']} سجل دخل ({_income_line(got, lang)}) = {amt(got['total'], lang)}"))
        lines.append(L(lang, f"- Left: {num(got['total'])} − {num(spent['total'])} = {amt(snap['remaining'], lang)}",
                       f"- المتبقي: {num(got['total'])} − {num(spent['total'])} = {amt(snap['remaining'], lang)}"))
    known = [spent["total"], got["total"], snap["remaining"]]
    wrong = _claimed_numbers(q.text, known) + [v for v in _claimed_numbers(last_answer, known)
                                               if v not in _claimed_numbers(q.text, known)]
    for v in wrong:
        lines.append(L(lang, f"{num(v)} does not come from your records for {label}; it was not a verified figure. "
                             "The figures above are the correct ones.",
                       f"الرقم {num(v)} لا يأتي من سجلاتك لشهر {label}، ولم يكن رقمًا موثقًا. الأرقام أعلاه هي الصحيحة."))
    steps = [line.lstrip("- ") for line in lines[1:]]
    data = {"month": month, "expenses": money(spent["total"]), "expense_count": spent["count"],
            "income": None if got["total"] is None else money(got["total"]),
            "remaining": None if snap["remaining"] is None else money(snap["remaining"]),
            "unsupported_numbers": [money(v) for v in wrong]}
    return _result("\n".join(lines), "explain_calculation", data, steps, {"months": [month]})


# ---------- daily and weekly averages ----------

def _days_counted(month):
    """Days a month's spending is spread over: the whole month, or the days so far
    when it is the current calendar month."""
    year, mon = int(month[:4]), int(month[5:7])
    today = date.today()
    if (year, mon) == (today.year, today.month):
        return today.day
    return calendar.monthrange(year, mon)[1]


def _per_day(q, user_id, cur):
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    spent = facts.spending(user_id, [month])
    label = month_label(month, lang)
    if not spent["count"]:
        return _total(q, user_id, cur)
    days = _days_counted(month)
    daily = spent["total"] / days
    weekly = daily * 7
    so_far = days != calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    span = L(lang, f"{days} days so far" if so_far else f"{days} days",
             f"{days} يومًا حتى الآن" if so_far else f"{days} يومًا")
    if parse.WEEKLY.search(q.text):
        text = L(lang, f"In {label} you spent {amt(spent['total'], lang)} over {span}: {num(spent['total'])} ÷ {days} "
                       f"= {amt(daily, lang)} a day, × 7 = {amt(weekly, lang)} a week on average.",
                 f"في {label} صرفت {amt(spent['total'], lang)} خلال {span}: {num(spent['total'])} ÷ {days} = "
                 f"{amt(daily, lang)} في اليوم، × 7 = {amt(weekly, lang)} في الأسبوع في المتوسط.")
    else:
        text = L(lang, f"In {label} you spent {amt(spent['total'], lang)} over {span}: {num(spent['total'])} ÷ {days} "
                       f"= {amt(daily, lang)} a day on average.",
                 f"في {label} صرفت {amt(spent['total'], lang)} خلال {span}: {num(spent['total'])} ÷ {days} = "
                 f"{amt(daily, lang)} في اليوم في المتوسط.")
    steps = [f"{num(spent['total'])} ÷ {days} = {num(daily)}", f"{num(daily)} × 7 = {num(weekly)}"]
    data = {"month": month, "total": money(spent["total"]), "days": days, "daily_average": money(daily),
            "weekly_average": money(weekly)}
    return _result(text, "analyze_spending_patterns", data, steps, {"months": [month]})


# ---------- trend ----------

def _trend(q, user_id, cur):
    lang = q.language
    months = _months(q, cur, user_id, default="three")
    if len(months) == 1:
        months = facts.months_back(cur, 6)
    spent = facts.spending(user_id, months)
    used = [m for m in months if spent["by_month"].get(m, ZERO) != 0]
    if len(used) < 2:
        text = L(lang, "There are not enough months with spending to show a trend (at least two are needed).",
                 "لا توجد أشهر كافية فيها مصروفات لعرض اتجاه (يلزم شهران على الأقل).")
        return _result(text, "analyze_spending_patterns", {"months": used}, [], {"months": used})
    lines = [L(lang, f"Your monthly spending, {period_label(used, lang)}:", f"إنفاقك الشهري، {period_label(used, lang)}:")]
    steps = []
    for prev, m in zip([None] + used[:-1], used):
        value = spent["by_month"][m]
        if prev is None:
            lines.append(f"- {month_label(m, lang)}: {amt(value, lang)}")
            continue
        diff = value - spent["by_month"][prev]
        change = percent(diff, spent["by_month"][prev])
        lines.append(f"- {month_label(m, lang)}: {amt(value, lang)} ({'+' if diff >= 0 else '−'}{num(abs(diff))}, "
                     f"{'+' if diff >= 0 else ''}{pct(change)})")
        steps.append(f"{num(value)} − {num(spent['by_month'][prev])} = {num(diff)}")
    first, last = spent["by_month"][used[0]], spent["by_month"][used[-1]]
    overall = last - first
    rises = sum(1 for a, b in zip(used, used[1:]) if spent["by_month"][b] > spent["by_month"][a])
    if rises == len(used) - 1:
        word = L(lang, "rising every month", "في ارتفاع كل شهر")
    elif rises == 0:
        word = L(lang, "falling every month", "في انخفاض كل شهر")
    else:
        word = L(lang, "up and down", "متذبذب")
    lines.append(L(lang, f"Overall {word}: {num(last)} − {num(first)} = {'+' if overall >= 0 else ''}{num(overall)} "
                         f"from {month_label(used[0], lang)} to {month_label(used[-1], lang)}.",
                   f"الاتجاه العام {word}: {num(last)} − {num(first)} = {'+' if overall >= 0 else ''}{num(overall)} "
                   f"من {month_label(used[0], lang)} إلى {month_label(used[-1], lang)}."))
    data = {"months": used, "totals": [money(spent["by_month"][m]) for m in used], "change": money(overall)}
    return _result("\n".join(lines), "analyze_spending_patterns", data, steps, {"months": used})


# ---------- unusual spending ----------

UNUSUAL_RISE = Decimal("0.25")       # 25% above the line's own average ...
UNUSUAL_MIN = Decimal("100")         # ... and at least 100 SDG more


def _anomalies(q, user_id, cur):
    """Lines of this month that stand out against the same lines in earlier
    months (only what the records support: at least one earlier month is needed)."""
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    now = facts.spending(user_id, [month])
    earlier = [m for m in facts.months_back(facts.shift_month(month, -1), 3)
               if facts.spending(user_id, [m])["count"]]
    label = month_label(month, lang)
    if not now["count"]:
        return _total(q, user_id, cur)
    if not earlier:
        text = L(lang, f"There is no earlier month to compare {label} with, so I cannot say what is unusual yet.",
                 f"لا يوجد شهر سابق للمقارنة مع {label}، لذلك لا يمكنني تحديد ما هو غير طبيعي بعد.")
        return _result(text, "analyze_spending_patterns", {"month": month, "flags": []}, [], {"months": [month]})
    past = facts.spending(user_id, earlier)
    flags = []
    for category, subs in now["by_subcategory"].items():
        for sub, value in subs.items():
            before = past["by_subcategory"].get(category, {}).get(sub, ZERO) / len(earlier)
            if before == 0 and value >= UNUSUAL_MIN:
                flags.append((value, category, sub, value, before, "new"))
            elif before > 0 and value - before >= UNUSUAL_MIN and value >= before * (1 + UNUSUAL_RISE):
                flags.append((value - before, category, sub, value, before, "rise"))
    flags.sort(key=lambda f: -f[0])
    lines = [L(lang, f"Compared with your average for the same items over {period_label(earlier, lang)}:",
               f"مقارنة بمتوسطك لنفس البنود خلال {period_label(earlier, lang)}:")]
    for _, category, sub, value, before, kind in flags:
        what = cat_label(category, sub or None, lang)
        if kind == "new":
            lines.append(L(lang, f"- {what}: {amt(value, lang)}, new (nothing in the earlier months)",
                           f"- {what}: {amt(value, lang)}، جديد (لا شيء في الأشهر السابقة)"))
        else:
            lines.append(L(lang, f"- {what}: {amt(value, lang)} against {amt(before, lang)} on average "
                                 f"(+{num(value - before)}, +{pct(percent(value - before, before))})",
                           f"- {what}: {amt(value, lang)} مقابل متوسط {amt(before, lang)} "
                           f"(+{num(value - before)}، +{pct(percent(value - before, before))})"))
    if not flags:
        lines = [L(lang, f"Nothing in {label} stands out: no item is both {int(UNUSUAL_RISE * 100)}% and "
                         f"{num(UNUSUAL_MIN)} SDG above its average over {period_label(earlier, lang)}.",
                   f"لا شيء غير عادي في {label}: لا يوجد بند زاد {int(UNUSUAL_RISE * 100)}% و{num(UNUSUAL_MIN)} ج.س "
                   f"فوق متوسطه خلال {period_label(earlier, lang)}.")]
    if now["possible_duplicates"]:
        lines.append(L(lang, f"Also, {now['possible_duplicates']} transaction(s) look like duplicates (same day, "
                             "amount and description); please check them.",
                       f"أيضًا، {now['possible_duplicates']} عملية تبدو مكررة (نفس اليوم والمبلغ والوصف)؛ يُرجى "
                       "مراجعتها."))
    steps = [L(lang, f"Rule: an item counts as unusual when it is at least {int(UNUSUAL_RISE * 100)}% and "
                     f"{num(UNUSUAL_MIN)} above its own monthly average, or new and at least {num(UNUSUAL_MIN)}",
               f"القاعدة: البند غير عادي إذا زاد {int(UNUSUAL_RISE * 100)}% و{num(UNUSUAL_MIN)} على الأقل عن متوسطه "
               f"الشهري، أو كان جديدًا وقيمته {num(UNUSUAL_MIN)} على الأقل")]
    data = {"month": month, "compared_with": earlier,
            "flags": [{"category": c, "subcategory": s, "amount": money(v), "average_before": money(b), "kind": k}
                      for _, c, s, v, b, k in flags]}
    return _result("\n".join(lines), "analyze_spending_patterns", data, steps, {"months": [month]})


# ---------- "what is 15% of my income?" ----------

def _percent_of(q, user_id, cur):
    lang = q.language
    m = parse.PERCENT_OF.search(q.text)
    rate, what = Decimal(m.group(1)), m.group(2)
    month = _months(q, cur, user_id)[-1] if cur else None
    snap = facts.month_snapshot(user_id, month) if month else None
    label = month_label(month, lang) if month else ""
    cats = parse.find_categories(what)
    if cats and snap:
        _, category, sub = cats[0]
        base = sum((t["amount"] for t in snap["spending"]["transactions"]
                    if t["category"] == category and (not sub or t["subcategory"] == sub)), ZERO)
        base_name = cat_label(category, sub, lang)
    elif parse.ASKS_LEFT.search(what) and snap and snap["remaining"] is not None:
        base, base_name = snap["remaining"], L(lang, "what is left after expenses", "المتبقي بعد المصروفات")
    elif parse.ASKS_INCOME.search(what) and snap and snap["income"]["total"] is not None:
        base, base_name = snap["income"]["total"], L(lang, "your income", "دخلك")
    elif parse.ASKS_SPENT.search(what) and snap:
        base, base_name = snap["expenses"], L(lang, "your expenses", "مصروفاتك")
    else:
        return None                  # "15% of a 20,000 loan": the model (or the interest tool) handles it
    value = base * rate / 100
    text = L(lang, f"{num(rate)}% of {base_name} in {label} ({amt(base, lang)}) is {num(base)} × {num(rate)}% = "
                   f"{amt(value, lang)}.",
             f"{num(rate)}% من {base_name} في {label} ({amt(base, lang)}) = {num(base)} × {num(rate)}% = "
             f"{amt(value, lang)}.")
    steps = [f"{num(base)} × {num(rate)} ÷ 100 = {num(value)}"]
    return _result(text, "calculate_percentage", {"month": month, "base": money(base), "percent": float(rate),
                                                  "value": money(value)}, steps, {"months": [month]})


# ---------- next month ----------

def _forecast(q, user_id, cur):
    """"How much will I save next month?": a projection, labelled as one. Spending
    comes from the forecast model; income is the latest recorded monthly income."""
    from app.ml.prediction import predict_spending
    lang = q.language
    p = predict_spending(user_id)
    if "error" in p:
        return None
    snap = facts.month_snapshot(user_id, cur)
    income = snap["income"]["total"]
    nxt, label_now = month_label(p["month"], lang), month_label(cur, lang)
    spend = dec(p["total_predicted"])
    lines = [L(lang, f"A forecast for {nxt} (not recorded data):", f"توقع لشهر {nxt} (ليس بيانات مسجلة):"),
             L(lang, f"- Expected spending: about {amt(spend, lang)} (from your last {p['months_used']} month(s) of "
                     "records)",
               f"- المصروفات المتوقعة: حوالي {amt(spend, lang)} (من آخر {p['months_used']} شهر من سجلاتك)")]
    data = {"month": p["month"], "forecast_spending": money(spend), "hypothetical": True}
    steps = [L(lang, "Spending: the app's forecast model on your monthly records", "المصروفات: نموذج التوقع على سجلاتك الشهرية")]
    if income is None:
        lines.append(L(lang, "- No income is recorded, so the saving cannot be projected.",
                       "- لا يوجد دخل مسجل، لذلك لا يمكن توقع الادخار."))
    else:
        left = income - spend
        lines.append(L(lang, f"- Assuming your income stays {amt(income, lang)}: {num(income)} − {num(spend)} = "
                             f"{amt(left, lang)} left to save",
                       f"- بافتراض أن دخلك يبقى {amt(income, lang)}: {num(income)} − {num(spend)} = {amt(left, lang)} "
                       "متبقية للادخار"))
        lines.append(L(lang, f"- If you spend exactly what you spent in {label_now} ({amt(snap['expenses'], lang)}), "
                             f"{amt(snap['remaining'], lang)} would be left, as now.",
                       f"- إذا صرفت مثل {label_now} تمامًا ({amt(snap['expenses'], lang)})، سيتبقى "
                       f"{amt(snap['remaining'], lang)} كما الآن."))
        steps.append(f"{num(income)} − {num(spend)} = {num(left)}")
        data.update(income=money(income), forecast_left=money(left), left_if_unchanged=money(snap["remaining"]))
    if parse.ASKS_SPENT.search(q.text) and not parse.ASKS_LEFT.search(q.text) and not parse.EXTRA_SAVING.search(q.text):
        lines = lines[:2]
    return _result("\n".join(lines), "predict_spending", data, steps, {"months": [cur]})


# ---------- "how am I doing?" ----------

def _overview(q, user_id, cur):
    """A picture of the month built only from the records: income, spending and
    what is left, the change from last month, the biggest category, the budget
    and the goals. Facts first, then what they mean, each with its numbers."""
    lang = q.language
    month = _months(q, cur, user_id)[-1]
    summary = _summary(q, user_id, cur)
    lines = summary["answer"].split("\n")
    spent = facts.spending(user_id, [month])
    prev = facts.spending(user_id, [facts.shift_month(month, -1)])
    if prev["count"]:
        diff = spent["total"] - prev["total"]
        lines.append(L(lang, f"- Compared with {month_label(facts.shift_month(month, -1), lang)}: "
                             f"{'+' if diff >= 0 else '−'}{num(abs(diff))} ({num(spent['total'])} − {num(prev['total'])})",
                       f"- مقارنة بـ{month_label(facts.shift_month(month, -1), lang)}: "
                       f"{'+' if diff >= 0 else '−'}{num(abs(diff))} ({num(spent['total'])} − {num(prev['total'])})"))
    if spent["count"]:
        top = facts.ranking(spent["by_category"], spent["total"])[0]
        lines.append(L(lang, f"- Biggest category: {top['category']}, {amt(top['amount'], lang)} "
                             f"({pct(top['percent'])} of spending)",
                       f"- أكبر فئة: {name(top['category'], lang)}، {amt(top['amount'], lang)} "
                       f"({pct(top['percent'])} من المصروفات)"))
    budget = _budget(q, user_id, cur)
    if budget and budget["data"].get("over_budget") is not None:
        b = budget["data"]
        over = b["spent"] - b["allocated"]
        lines.append(L(lang, f"- Budget: {num(b['spent'])} spent of {num(b['allocated'])} planned"
                             + (f", {num(over)} over" if over > 0 else f", {num(-over)} left"),
                       f"- الميزانية: صرفت {num(b['spent'])} من {num(b['allocated'])} مخططة"
                       + (f"، بتجاوز {num(over)}" if over > 0 else f"، ويتبقى {num(-over)}")))
    goals = _goals(q, user_id, cur)
    if goals["data"].get("goals"):
        lines.append(L(lang, "- Goals: ", "- الأهداف: ") + "; ".join(
            f'"{g["name"]}" {g["percent_complete"] or 0:.0f}%' for g in goals["data"]["goals"]))
    rate = summary["data"].get("saving_rate")
    sources = []
    if rate is not None:
        guide = _guideline_source()
        sources = [guide] if guide else []
        lines.append(L(lang, f"What it means: you keep {pct(rate)} of your income; the 50/30/20 guideline (Duke "
                             f"University) suggests 20% for savings, so you are {'above' if rate >= 20 else 'below'} it.",
                       f"المعنى: تحتفظ بـ{pct(rate)} من دخلك؛ وقاعدة 50/30/20 (جامعة ديوك) تقترح 20% للادخار، "
                       f"فأنت {'أعلى' if rate >= 20 else 'أقل'} منها."))
    out = _result("\n".join(lines), "analyze_finances", {**summary["data"], "month": month},
                  summary["calculation"], {"months": [month]})
    out["sources"] = sources
    return out


# ---------- all goals ----------

def _goals(q, user_id, cur):
    from app.database.crud import get_goals
    lang = q.language
    goals = [goal_facts(g) for g in get_goals(user_id)]
    if not goals:
        text = L(lang, "You have no saved goals yet. Add one on the Goals page.",
                 "لا توجد لديك أهداف محفوظة بعد. أضف هدفًا من صفحة الأهداف.")
        return _result(text, "list_goals", {"goals": []}, [], {})
    lines = [L(lang, f"Your {len(goals)} saved goal(s):", f"أهدافك المحفوظة ({len(goals)}):")]
    steps = []
    for f in goals:
        pc = f["percent_complete"] or 0
        line = L(lang, f'- "{f["name"]}": saved {num(f["saved_so_far"])} of {amt(f["target_amount"], lang)} '
                       f'({pc:.0f}%), {amt(f["remaining_amount"], lang)} left',
                 f'- "{f["name"]}": ادخرت {num(f["saved_so_far"])} من {amt(f["target_amount"], lang)} '
                 f'({pc:.0f}%)، يتبقى {amt(f["remaining_amount"], lang)}')
        status = f["deadline_status"]
        if status == "reached":
            line += L(lang, ", reached", "، تحقق")
        elif status == "upcoming":
            line += L(lang, f"; deadline {f['target_date']} ({f['months_left']} month(s)): "
                            f"{amt(f['required_monthly_saving'], lang)} a month needed",
                      f"؛ الموعد {f['target_date']} ({f['months_left']} شهر): يلزم {amt(f['required_monthly_saving'], lang)} "
                      "شهريًا")
            steps.append(f"{f['name']}: {num(f['remaining_amount'])} ÷ {f['months_left']} = "
                         f"{num(f['required_monthly_saving'])}")
        elif status == "deadline passed":
            line += L(lang, f"; its deadline {f['target_date']} has passed", f"؛ موعده {f['target_date']} مضى")
        else:
            line += L(lang, "; no deadline saved, so no monthly amount can be worked out",
                      "؛ لا يوجد موعد محفوظ، لذلك لا يمكن حساب المبلغ الشهري")
        lines.append(line)
        steps.append(f"{f['name']}: {num(f['saved_so_far'])} ÷ {num(f['target_amount'])} × 100 = {pc:.2f}%")
    open_goals = [f for f in goals if f["deadline_status"] != "reached"]
    if len(open_goals) > 1:
        closest = max(open_goals, key=lambda f: (f["percent_complete"] or 0, -f["remaining_amount"]))
        lines.append(L(lang, f'Closest to completion: "{closest["name"]}" ({closest["percent_complete"]:.0f}% done).',
                       f'الأقرب للاكتمال: "{closest["name"]}" (اكتمل {closest["percent_complete"]:.0f}%).'))
    total_saved = sum(dec(f["saved_so_far"]) for f in goals)
    lines.append(L(lang, f"Saved toward all goals: {amt(total_saved, lang)}.",
                   f"المدخر لكل الأهداف: {amt(total_saved, lang)}."))
    return _result("\n".join(lines), "list_goals", {"goals": goals, "total_saved": money(total_saved)}, steps, {})


def _category_judgement(user_id, category, amount, month, lang):
    """How one category's spending compares with its budget line and the month
    before: the evidence for "is that too much?"."""
    from app.engines.budget_engine import calculate_budget
    what = name(category, lang)
    parts = []
    plan = calculate_budget(user_id)
    if "error" not in plan and plan.get(category) is not None:
        limit = dec(plan[category])
        diff = amount - limit
        parts.append(L(lang, f"Your budget gives {what} {amt(limit, lang)}: you are "
                             + (f"{amt(diff, lang)} over it ({num(amount)} − {num(limit)})." if diff > 0 else
                                f"within it, with {amt(-diff, lang)} to spare."),
                       f"ميزانيتك تخصص لـ{what} {amt(limit, lang)}: "
                       + (f"تجاوزتها بمقدار {amt(diff, lang)} ({num(amount)} − {num(limit)})." if diff > 0 else
                          f"أنت ضمنها ويتبقى {amt(-diff, lang)}.")))
    prev_month = facts.shift_month(month, -1)
    prev = facts.spending(user_id, [prev_month])
    if prev["count"]:
        before = prev["by_category"].get(category, ZERO)
        d = amount - before
        parts.append(L(lang, f"In {month_label(prev_month, lang)} it was {amt(before, lang)} "
                             f"({'+' if d >= 0 else '−'}{num(abs(d))}).",
                       f"في {month_label(prev_month, lang)} كان {amt(before, lang)} "
                       f"({'+' if d >= 0 else '−'}{num(abs(d))})."))
    return " ".join(parts)


def _budget(q, user_id, cur):
    from app.engines.budget_engine import calculate_budget
    lang = q.language
    plan = calculate_budget(user_id)
    if "error" in plan:
        text = L(lang, "I cannot build your budget yet: no income is recorded.",
                 "لا يمكنني إعداد ميزانيتك بعد: لا يوجد دخل مسجل.")
        return _result(text, "calculate_budget", plan, [], {})
    month = cur
    spent = facts.spending(user_id, [month])
    lines = {c: dec(v) for c, v in plan.items() if not c.startswith("_") and c != "Savings"}
    allocated = sum(lines.values(), ZERO)
    used = spent["total"]
    left = allocated - used
    label = month_label(month, lang)
    over = sorted(((c, spent["by_category"].get(c, ZERO) - v) for c, v in lines.items()
                   if spent["by_category"].get(c, ZERO) > v), key=lambda kv: -kv[1])
    unbudgeted = [c for c in spent["by_category"] if c not in lines and spent["by_category"][c] > 0]
    text = L(lang, f"Your budget for {label} gives {amt(allocated, lang)} to spending "
                   f"(and {amt(dec(plan.get('Savings', 0)), lang)} to saving). You have spent "
                   f"{amt(used, lang)}, so ",
             f"ميزانيتك لشهر {label} تخصص {amt(allocated, lang)} للإنفاق "
             f"(و{amt(dec(plan.get('Savings', 0)), lang)} للادخار). صرفت {amt(used, lang)}، لذلك ")
    text += (L(lang, f"{amt(left, lang)} is left.", f"تبقى {amt(left, lang)}.") if left >= 0 else
             L(lang, f"you are {amt(-left, lang)} over budget.", f"تجاوزت الميزانية بمقدار {amt(-left, lang)}."))
    if over:
        text += " " + L(lang, "Over budget: ", "تجاوزت الميزانية في: ") + "; ".join(
            L(lang, f"{c} {amt(spent['by_category'][c], lang)} of {amt(lines[c], lang)} ({num(d)} over)",
              f"{name(c, lang)} {amt(spent['by_category'][c], lang)} من {amt(lines[c], lang)} (بزيادة {num(d)})")
            for c, d in over[:4]) + "."
    under = sorted(((c, v - spent["by_category"].get(c, ZERO)) for c, v in lines.items()
                    if v - spent["by_category"].get(c, ZERO) > 0), key=lambda kv: -kv[1])
    if under:
        text += " " + L(lang, "Still available: ", "ما زال متاحًا: ") + ", ".join(
            f"{name(c, lang)} {amt(d, lang)}" for c, d in under[:4]) + "."
    if unbudgeted:
        text += " " + L(lang, f"Not in the budget: {', '.join(unbudgeted)}.",
                        f"خارج الميزانية: {'، '.join(name(c, lang) for c in unbudgeted)}.")
    steps = [L(lang, f"Budget remaining = allocated − spent = {num(allocated)} − {num(used)} = {num(left)}",
               f"المتبقي من الميزانية = المخصص − المصروف = {num(allocated)} − {num(used)} = {num(left)}")] + [
        f"{name(c, lang)}: {num(v)} − {num(spent['by_category'].get(c, ZERO))} = "
        f"{num(v - spent['by_category'].get(c, ZERO))}" for c, v in lines.items()]
    data = {"month": month, "allocated": money(allocated), "spent": money(used), "remaining": money(left),
            "over_budget": {c: money(d) for c, d in over}}
    return _result(text, "calculate_budget", data, steps, {"months": [month]})


# ---------- goals ----------

def _flexible_spending(user_id, spent):
    from app.preferences.user_preferences import is_essential, preference_map
    prefs = preference_map(user_id)
    return sum((t["amount"] for t in spent["transactions"]
                if not is_essential(user_id, t["category"], t["subcategory"], prefs)), ZERO)


def _capacity(user_id, cur):
    """What the user can save a month now: this month's income − spending."""
    snap = facts.month_snapshot(user_id, cur)
    return snap


def goal_numbers(user_id, goal, cur, extra=ZERO, snap=None):
    """The goal's stored facts with this month's saving capacity next to them."""
    f = goal_facts(goal)
    snap = snap or _capacity(user_id, cur)
    capacity = snap["remaining"]
    out = {"facts": f, "snap": snap, "capacity": capacity}
    if capacity is None:
        return out
    saving = capacity + extra
    remaining = dec(f["remaining_amount"])
    out.update(saving=saving, months_needed=months_to_reach(remaining, saving))
    if f.get("required_monthly_saving") is not None:
        required = dec(f["required_monthly_saving"])
        out.update(required=required, gap=required - saving)
    return out


def _verdict(n, flexible):
    gap = n.get("gap")
    if gap is None:
        return None
    if gap <= 0:
        return "achievable"
    if n["saving"] > 0 and gap <= flexible:
        return "at risk"
    return "not achievable"


def _open_goals(user_id):
    from app.database.crud import get_goals
    return [g for g in get_goals(user_id) if (g["current_amount"] or 0) < g["target_amount"]]


def _goal(q, user_id, cur, goal, how=None):
    lang = q.language
    n = goal_numbers(user_id, goal, cur)
    f, gname = n["facts"], goal["name"]
    head = L(lang, f'Goal "{gname}": {amt(f["remaining_amount"], lang)} still needed '
                   f'({amt(f["saved_so_far"], lang)} of {amt(f["target_amount"], lang)} saved, '
                   f'{f["percent_complete"]:.0f}%).',
             f'هدف "{gname}": يتبقى {amt(f["remaining_amount"], lang)} '
             f'(ادخرت {amt(f["saved_so_far"], lang)} من {amt(f["target_amount"], lang)}، '
             f'{f["percent_complete"]:.0f}%).')
    ctx = {"goal": gname, "months": [cur]}
    if f["deadline_status"] == "reached":
        text = L(lang, f'You have already reached "{gname}": {amt(f["saved_so_far"], lang)} saved of '
                       f'{amt(f["target_amount"], lang)}.',
                 f'لقد حققت هدف "{gname}": ادخرت {amt(f["saved_so_far"], lang)} من {amt(f["target_amount"], lang)}.')
        return _result(text, "assess_goal", f, [], ctx, gname)
    if n["capacity"] is None:
        text = head + " " + L(lang, "No income is recorded, so I cannot tell how much you can save each month.",
                              "لا يوجد دخل مسجل، لذلك لا يمكنني معرفة كم تستطيع أن تدخر شهريًا.")
        return _result(text, "assess_goal", f, [], ctx, gname)
    saving, label = n["saving"], month_label(cur, lang)
    pace = L(lang, f"In {label} you had {amt(saving, lang)} left after spending (income "
                   f"{amt(n['snap']['income']['total'], lang)} − spending {amt(n['snap']['expenses'], lang)}).",
             f"في {label} تبقى لك {amt(saving, lang)} بعد المصروفات (الدخل "
             f"{amt(n['snap']['income']['total'], lang)} − المصروفات {amt(n['snap']['expenses'], lang)}).")
    finish = n["months_needed"]
    eta = (L(lang, f"At {amt(saving, lang)} a month you would reach it in about {finish} month(s), around "
                   f"{after_months(finish)}.",
             f"بمعدل {amt(saving, lang)} شهريًا ستصل إليه خلال {finish} شهر تقريبًا، حوالي {after_months(finish)}.")
           if finish else L(lang, "At the current pace nothing is left to save, so it would not be reached.",
                            "بالوتيرة الحالية لا يتبقى شيء للادخار، لذلك لن يتحقق."))
    steps = [L(lang, f"Remaining = target − saved = {num(f['target_amount'])} − {num(f['saved_so_far'])} = "
                     f"{num(f['remaining_amount'])}",
               f"المتبقي = المستهدف − المدخر = {num(f['target_amount'])} − {num(f['saved_so_far'])} = "
               f"{num(f['remaining_amount'])}"),
             L(lang, f"Available to save each month = income − expenses ({label}) = {num(n['snap']['income']['total'])} − "
                     f"{num(n['snap']['expenses'])} = {num(saving)}",
               f"المتاح للادخار شهريًا = الدخل − المصروفات ({label}) = {num(n['snap']['income']['total'])} − "
               f"{num(n['snap']['expenses'])} = {num(saving)}")]
    if f["deadline_status"] == "no deadline saved":
        text = " ".join([head, L(lang, "No deadline is saved for it, so I cannot say whether it is on time. "
                                       "By what date do you want to reach it?",
                                 "لا يوجد موعد محفوظ لهذا الهدف، لذلك لا يمكنني الحكم إن كان في الوقت. "
                                 "في أي تاريخ تريد تحقيقه؟"), pace, eta])
        return _result(text, "assess_goal", f, steps, {**ctx, "awaiting": "deadline"}, gname)
    if f["deadline_status"] == "deadline passed":
        text = " ".join([head, L(lang, f"Its deadline ({f['target_date']}) has already passed. What new date "
                                       "do you want to aim for?",
                                 f"موعده ({f['target_date']}) قد مضى. ما التاريخ الجديد الذي تريده؟"), pace, eta])
        return _result(text, "assess_goal", f, steps, {**ctx, "awaiting": "deadline"}, gname)
    required, gap = n["required"], n["gap"]
    flexible = _flexible_spending(user_id, n["snap"]["spending"])
    verdict = _verdict(n, flexible)
    need = L(lang, f"The deadline is {f['target_date']} ({f['months_left']} month(s) left), so it needs "
                   f"{amt(required, lang)} a month ({num(f['remaining_amount'])} ÷ {f['months_left']}).",
             f"الموعد {f['target_date']} (يتبقى {f['months_left']} شهر)، لذلك يحتاج إلى {amt(required, lang)} "
             f"شهريًا ({num(f['remaining_amount'])} ÷ {f['months_left']}).")
    if verdict == "achievable":
        lead = L(lang, f'Yes, you can reach "{gname}" on time at your current pace.',
                 f'نعم، يمكنك تحقيق "{gname}" في الموعد بوتيرتك الحالية.')
        compare = L(lang, f"The {amt(saving, lang)} left each month after spending covers that, with "
                          f"{amt(-gap, lang)} to spare, if you put it toward this goal.",
                    f"المتبقي بعد المصروفات ({amt(saving, lang)} شهريًا) يغطي ذلك ويزيد بمقدار {amt(-gap, lang)}، "
                    "إذا خصصته لهذا الهدف.")
    elif verdict == "at risk":
        lead = L(lang, f'"{gname}" is at risk: at your current pace you will not reach it on time, but the gap '
                       f"can be closed by cutting flexible spending.",
                 f'هدف "{gname}" في خطر: بوتيرتك الحالية لن تصل إليه في الموعد، لكن يمكن سد الفجوة بتقليل '
                 f"الإنفاق غير الأساسي.")
        compare = L(lang, f"Even all of the {amt(saving, lang)} left each month after spending is "
                          f"{amt(gap, lang)} a month short "
                          f"({num(required)} − {num(saving)}). Your flexible (non-essential) spending in {label} was "
                          f"{amt(flexible, lang)}.",
                    f"حتى كل المتبقي بعد المصروفات ({amt(saving, lang)} شهريًا) أقل بمقدار {amt(gap, lang)} شهريًا "
                    f"({num(required)} − {num(saving)}). إنفاقك غير الأساسي في {label} كان {amt(flexible, lang)}.")
    else:
        lead = L(lang, f'"{gname}" is not achievable on time under your current finances.',
                 f'هدف "{gname}" غير قابل للتحقيق في موعده وفق وضعك المالي الحالي.')
        compare = L(lang, f"Even all of the {amt(saving, lang)} left each month after spending is "
                          f"{amt(gap, lang)} a month short "
                          f"({num(required)} − {num(saving)}), and that gap is larger than all your flexible "
                          f"spending in {label} ({amt(flexible, lang)}). A later date or extra income is needed.",
                    f"حتى كل المتبقي بعد المصروفات ({amt(saving, lang)} شهريًا) أقل بمقدار {amt(gap, lang)} شهريًا "
                    f"({num(required)} − {num(saving)})، وهذه الفجوة أكبر من كل إنفاقك غير الأساسي في {label} "
                    f"({amt(flexible, lang)}). يلزم موعد أبعد أو دخل إضافي.")
    text = " ".join([lead, head, need, pace, compare, eta])
    others = [goal_facts(g) for g in _open_goals(user_id) if g["name"] != gname]
    others = [o for o in others if o.get("required_monthly_saving")]
    if others and verdict == "achievable":
        other_need = sum((dec(o["required_monthly_saving"]) for o in others), ZERO)
        if required + other_need > saving:
            names = ", ".join(f'"{o["name"]}" {num(o["required_monthly_saving"])}' for o in others)
            text += " " + L(lang, f"But your other goals also need {amt(other_need, lang)} a month ({names}): together "
                                  f"{num(required)} + {num(other_need)} = {amt(required + other_need, lang)}, more than "
                                  f"the {amt(saving, lang)} left, so not every goal can be on time.",
                            f"لكن أهدافك الأخرى تحتاج أيضًا {amt(other_need, lang)} شهريًا ({names}): معًا "
                            f"{num(required)} + {num(other_need)} = {amt(required + other_need, lang)}، أكثر من "
                            f"المتبقي {amt(saving, lang)}، لذلك لا يمكن أن تكون كل الأهداف في موعدها.")
    if how == "only dated goal":
        text += " " + L(lang, f'(I used "{gname}" because it is your only open goal with a deadline; ask me '
                              "about another goal by name.)",
                        f'(استخدمت "{gname}" لأنه هدفك الوحيد المفتوح الذي له موعد؛ اسألني عن هدف آخر باسمه.)')
    steps += [L(lang, f"Needed each month = remaining ÷ months left = {num(f['remaining_amount'])} ÷ "
                      f"{f['months_left']} = {num(required)}",
                f"المطلوب شهريًا = المتبقي ÷ الأشهر المتبقية = {num(f['remaining_amount'])} ÷ "
                f"{f['months_left']} = {num(required)}"),
              L(lang, f"Monthly gap = needed − available = {num(required)} − {num(saving)} = {num(gap)}",
                f"الفجوة الشهرية = المطلوب − المتاح = {num(required)} − {num(saving)} = {num(gap)}")]
    if finish:
        steps.append(L(lang, f"Months at this pace = {num(f['remaining_amount'])} ÷ {num(saving)}, rounded up = {finish}",
                       f"الأشهر بهذه الوتيرة = {num(f['remaining_amount'])} ÷ {num(saving)}، مقربة لأعلى = {finish}"))
    data = {**f, "monthly_saving_now": money(saving), "monthly_gap": money(gap), "status": verdict,
            "flexible_spending": money(flexible), "months_at_current_pace": finish}
    return _result(text, "assess_goal", data, steps, ctx, gname)


# ---------- what-if ----------

def _apply(changes, snap):
    """Apply explicit changes to a copy of the month; everything else stays as it is.

    "expenses" changes spending as a whole (not one category): the amount, or the
    percentage of the total so far, is kept as its own line next to the categories."""
    by_cat = dict(snap["spending"]["by_category"])
    by_sub = {c: dict(s) for c, s in snap["spending"]["by_subcategory"].items()}
    income = snap["income"]["total"]
    applied, extra, warnings, overall, planned = [], ZERO, [], ZERO, None
    for ch in changes:
        value = Decimal(str(ch.value))
        if ch.target == "saving" and ch.mode in ("set", "share"):
            # "save 2,000 a month" / "save 10% of my income": the planned monthly saving
            planned = income * value / 100 if ch.mode == "share" else value
            applied.append({"what": "saving", "mode": ch.mode, "value": value, "old": None, "new": planned,
                            "delta": ZERO})
            continue
        if ch.target == "saving":
            delta = income * value / 100 if ch.mode == "pct" else value
            extra += delta
            applied.append({"what": "saving", "mode": ch.mode, "value": value, "old": None, "new": None,
                            "delta": delta})
            continue
        if ch.target == "income":
            old = income
        elif ch.target == "expenses":
            old = sum(by_cat.values(), ZERO) + overall
        elif ch.subcategory:
            old = by_sub.get(ch.category, {}).get(ch.subcategory, ZERO)
        else:
            old = by_cat.get(ch.category, ZERO)
        new = {"set": value, "add": old + value, "pct": old * (1 + value / 100)}[ch.mode]
        if new < 0:
            warnings.append(new)
            new = ZERO
        if ch.target == "income":
            income = new
        elif ch.target == "expenses":
            overall += new - old
        elif ch.subcategory:
            by_sub.setdefault(ch.category, {})[ch.subcategory] = new
            by_cat[ch.category] = by_cat.get(ch.category, ZERO) + (new - old)
        else:
            by_cat[ch.category] = new
        applied.append({"what": ch.target, "mode": ch.mode, "value": value, "category": ch.category,
                        "subcategory": ch.subcategory, "old": old, "new": new, "delta": new - old})
    expenses = sum(by_cat.values(), ZERO) + overall
    # Consistency check: the new total must equal the old total plus the changes.
    expected = snap["expenses"] + sum((a["delta"] for a in applied if a["what"] in ("category", "expenses")),
                                      ZERO)
    if expenses != expected:
        raise ArithmeticError(f"what-if totals disagree: {expenses} != {expected}")
    return {"by_category": by_cat, "income": income, "expenses": expenses, "extra_saving": extra,
            "planned_saving": planned,
            "remaining": None if income is None else income - expenses, "applied": applied,
            "clamped": warnings}


def _shared(changes, snap):
    """One amount shared by several categories in proportion to what each one costs
    ("cut restaurants and fast food by 300 together"): whole units, adding up exactly."""
    lines = []
    for c in changes:
        spent = (snap["spending"]["by_subcategory"].get(c.category, {}).get(c.subcategory, ZERO) if c.subcategory
                 else snap["spending"]["by_category"].get(c.category, ZERO))
        lines.append(((c.category, c.subcategory), spent))
    total = Decimal(str(abs(changes[0].value)))
    sign = -1 if changes[0].value < 0 else 1
    split = verify.allocate_cuts(total, lines) if sign < 0 else None
    if not split:                       # a rise, or more than they cost: equal parts
        each = total / len(changes)
        return [parse.Change("category", "add", float(sign * each), c.category, c.subcategory) for c in changes]
    return [parse.Change("category", "add", float(-cut), c, s) for (c, s), _, cut in split]


def _changes_from(context):
    return [parse.Change(**c) for c in (context or {}).get("changes", [])]


def _change_line(a, lang):
    """One applied change as a line that shows the arithmetic."""
    cur = CURRENCY.get(lang)
    if a["what"] == "income":
        what = L(lang, "Income", "الدخل")
    elif a["what"] == "expenses":
        what = L(lang, "Total spending", "إجمالي المصروفات")
    else:
        what = cat_label(a["category"], a["subcategory"], lang)
    if a["delta"] == 0:
        return f"- {what}: " + L(lang, f"stays {num(a['old'])} {cur}", f"يبقى {num(a['old'])} {cur}")
    sign = "+" if a["delta"] >= 0 else "−"
    if a["mode"] == "pct":
        change = L(lang, "increase" if a["delta"] > 0 else "decrease", "زيادة" if a["delta"] > 0 else "نقص")
        return (f"- {what}: {num(a['old'])} × {num(abs(a['value']))}% = {num(abs(a['delta']))} {change}"
                f"{L(lang, ';', '؛')} {num(a['old'])} {sign} {num(abs(a['delta']))} = {num(a['new'])} {cur}")
    if a["mode"] == "add":
        return f"- {what}: {num(a['old'])} {sign} {num(abs(a['delta']))} = {num(a['new'])} {cur}"
    return f"- {what}: {num(a['old'])} → {num(a['new'])} {cur} ({sign}{num(abs(a['delta']))})"


def _what_if(q, user_id, cur, goal, previous):
    lang = q.language
    # The month the user names ("15% more than October") is the baseline;
    # otherwise the current month.
    month = q.months[0] if q.months and facts.spending(user_id, [q.months[0]])["count"] else cur
    snap = facts.month_snapshot(user_id, month)
    changes = list(q.changes)
    stated_income = next((c for c in changes if c.target == "income" and c.mode == "set"), None)
    if snap["income"]["total"] is None:
        if stated_income is None:
            text = L(lang, "I need your income to run a what-if; none is recorded yet. How much is your "
                           "monthly income?",
                     "أحتاج إلى دخلك لإجراء سيناريو افتراضي؛ لا يوجد دخل مسجل بعد. كم دخلك الشهري؟")
            return _result(text, "simulate_scenario", {}, [], {"awaiting": "income"})
        snap = {**snap, "income": {**snap["income"], "total": ZERO}}
        snap["remaining"] = -snap["expenses"]
    stacked = []
    if previous and previous.get("scenario") and STACK.search(q.text) and not q.followup_note:
        stacked = _changes_from(previous["scenario"])
        changes = stacked + changes       # "and another 200": build on the last what-if
    if q.together:
        changes = _shared(changes, snap)
    s = _apply(changes, snap)
    label = month_label(month, lang)
    old_exp, new_exp = snap["expenses"], s["expenses"]
    lines = [_change_line(a, lang) for a in s["applied"] if a["what"] != "saving" and a["delta"]]
    cats = [a for a in s["applied"] if a["what"] in ("category", "expenses") and a["delta"]]
    if len(cats) > 1 or any(a["what"] == "category" for a in cats):
        formula = num(old_exp) + "".join(
            f" − {num(a['old'])} + {num(a['new'])}" if a["mode"] == "set" else
            f" {'+' if a['delta'] >= 0 else '−'} {num(abs(a['delta']))}" for a in cats)
        lines.append(L(lang, f"- New spending: {formula} = {amt(new_exp, lang)}",
                       f"- المصروفات الجديدة: {formula} = {amt(new_exp, lang)}"))
    elif not cats:
        lines.append(L(lang, f"- Spending stays {amt(new_exp, lang)}", f"- المصروفات تبقى {amt(new_exp, lang)}"))
    cat_changes = [a for a in s["applied"] if a["what"] == "category"]
    if len(cat_changes) > 1:
        # the named categories together, then everything that did not change
        names = " + ".join(cat_label(a["category"], a["subcategory"], lang) for a in cat_changes)
        parts = " + ".join(num(a["old"]) for a in cat_changes)
        old_sum = sum((a["old"] for a in cat_changes), ZERO)
        new_sum = sum((a["new"] for a in cat_changes), ZERO)
        delta = new_sum - old_sum
        pcts = {a["value"] for a in cat_changes}
        how = (f"{num(old_sum)} × {num(abs(next(iter(pcts))))}% = {num(abs(delta))}{L(lang, ';', '؛')} "
               if all(a["mode"] == "pct" for a in cat_changes) and len(pcts) == 1 else "")
        lines.insert(len(cat_changes), L(
            lang, f"- Together: {names} = {parts} = {num(old_sum)} SDG; {how}{num(old_sum)} "
                  f"{'−' if delta < 0 else '+'} {num(abs(delta))} = {amt(new_sum, lang)}",
            f"- معًا: {names} = {parts} = {num(old_sum)} ج.س؛ {how}{num(old_sum)} "
            f"{'−' if delta < 0 else '+'} {num(abs(delta))} = {amt(new_sum, lang)}"))
    if cat_changes:
        changed_old = sum((a["old"] for a in cat_changes), ZERO)
        lines.append(L(lang, f"- Everything else stays as recorded: {num(old_exp)} − {num(changed_old)} = "
                             f"{amt(old_exp - changed_old, lang)} of other spending, unchanged",
                       f"- كل ما عدا ذلك يبقى كما هو مسجل: {num(old_exp)} − {num(changed_old)} = "
                       f"{amt(old_exp - changed_old, lang)} من المصروفات الأخرى بدون تغيير"))
    for a in s["applied"]:
        if a["what"] == "category" and a["old"] == 0 and a["mode"] != "set":
            what = cat_label(a["category"], a["subcategory"], lang)
            lines.append(L(lang, f"- Note: no {what} spending is recorded in {label}, so the change starts from 0",
                           f"- ملاحظة: لا يوجد إنفاق مسجل على {what} في {label}، لذلك بدأ التغيير من 0"))
    if stated_income is not None and snap["income"]["total"] != dec(stated_income.value) \
            and snap["income"]["total"]:
        lines.append(L(lang, f"- You said your income is {amt(stated_income.value, lang)}; your records show "
                             f"{amt(snap['income']['total'], lang)}. I used {amt(stated_income.value, lang)} as "
                             "you said.",
                       f"- ذكرت أن دخلك {amt(stated_income.value, lang)}؛ وسجلاتك تُظهر "
                       f"{amt(snap['income']['total'], lang)}. استخدمت {amt(stated_income.value, lang)} كما ذكرت."))
    inc_changed = s["income"] != snap["income"]["total"]
    income_word = (L(lang, "New income", "الدخل الجديد") if inc_changed else L(lang, "Income stays", "الدخل يبقى"))
    if s["remaining"] >= 0:
        left = L(lang, f"would be left each month (now {amt(snap['remaining'], lang)})",
                 f"سيتبقى شهريًا (حاليًا {amt(snap['remaining'], lang)})")
    else:
        left = L(lang, f": you would spend {amt(-s['remaining'], lang)} more than you earn "
                       f"(now {amt(snap['remaining'], lang)} left)",
                 f": ستصرف أكثر من دخلك بمقدار {amt(-s['remaining'], lang)} (حاليًا يتبقى "
                 f"{amt(snap['remaining'], lang)})")
    lines.append(L(lang, f"- {income_word} {amt(s['income'], lang)}, so {num(s['income'])} − {num(new_exp)} = "
                         f"{amt(s['remaining'], lang)} {left}",
                   f"- {income_word} {amt(s['income'], lang)}، إذن {num(s['income'])} − {num(new_exp)} = "
                   f"{amt(s['remaining'], lang)} {left}"))
    # Putting more aside than what is left is only possible by spending less: the
    # answer says so instead of treating the whole remainder as already saved.
    saving = s["remaining"] + s["extra_saving"]
    if s["planned_saving"] is not None:
        plan_amount, left_now = s["planned_saving"], s["remaining"]
        saving = plan_amount
        if plan_amount <= left_now:
            lines.append(L(lang, f"- Saving {amt(plan_amount, lang)} a month out of the {amt(left_now, lang)} left: "
                                 f"{num(left_now)} − {num(plan_amount)} = {amt(left_now - plan_amount, lang)} stays "
                                 "unallocated",
                           f"- ادخار {amt(plan_amount, lang)} شهريًا من المتبقي ({amt(left_now, lang)}): "
                           f"{num(left_now)} − {num(plan_amount)} = {amt(left_now - plan_amount, lang)} تبقى غير مخصصة"))
        else:
            lines.append(L(lang, f"- Saving {amt(plan_amount, lang)} a month is more than the {amt(left_now, lang)} left, "
                                 f"so spending would have to fall by {num(plan_amount)} − {num(left_now)} = "
                                 f"{amt(plan_amount - left_now, lang)}",
                           f"- ادخار {amt(plan_amount, lang)} شهريًا أكثر من المتبقي ({amt(left_now, lang)})، لذلك "
                           f"يجب تقليل المصروفات بمقدار {num(plan_amount)} − {num(left_now)} = "
                           f"{amt(plan_amount - left_now, lang)}"))
    elif s["extra_saving"]:
        extra = s["extra_saving"]
        lines.append(L(lang, f"- Putting {amt(extra, lang)} more aside each month than the {amt(s['remaining'], lang)} "
                             f"left means spending {amt(extra, lang)} less ({num(new_exp)} − {num(extra)} = "
                             f"{num(new_exp - extra)}): {num(s['remaining'])} + {num(extra)} = {amt(saving, lang)} "
                             "a month available to save",
                       f"- ادخار {amt(extra, lang)} إضافية شهريًا فوق المتبقي ({amt(s['remaining'], lang)}) يعني أن "
                       f"تصرف أقل بمقدار {amt(extra, lang)} ({num(new_exp)} − {num(extra)} = {num(new_exp - extra)}): "
                       f"{num(s['remaining'])} + {num(extra)} = {amt(saving, lang)} شهريًا متاحة للادخار"))
    scenario = {"changes": [vars(c) for c in changes], "month": month, "saving": money(saving),
                "base_saving": money(snap["remaining"])}
    gname = None
    if goal:
        gname = goal["name"]
        base = goal_numbers(user_id, goal, month, snap=snap)
        new = goal_numbers(user_id, goal, month, extra=saving - snap["remaining"], snap=snap)
        if new.get("required") is not None:
            gap = new["gap"]
            lines.append(
                L(lang, f'- Your goal "{gname}" needs {amt(new["required"], lang)} a month: '
                        + (f"you would be {amt(gap, lang)} short ({num(new['required'])} − {num(saving)})"
                           if gap > 0 else f"that is covered, with {amt(-gap, lang)} to spare"),
                  f'- هدفك "{gname}" يحتاج إلى {amt(new["required"], lang)} شهريًا: '
                  + (f"سيكون لديك نقص بمقدار {amt(gap, lang)} ({num(new['required'])} − {num(saving)})"
                     if gap > 0 else f"وهذا مغطى ويزيد بمقدار {amt(-gap, lang)}")))
        b, k = base.get("months_needed"), new.get("months_needed")
        if k and b and k != b:
            sooner = b - k
            lines.append(L(lang, f"- You would reach it in about {k} month(s) (around {after_months(k)}) instead of "
                                 f"{b} (if all of the {amt(snap['remaining'], lang)} left now went to it): "
                                 f"{abs(sooner)} month(s) {'sooner' if sooner > 0 else 'later'}",
                           f"- ستصل إليه خلال {k} شهر تقريبًا (حوالي {after_months(k)}) بدلًا من {b} (لو خُصص كل "
                           f"المتبقي حاليًا {amt(snap['remaining'], lang)} له): "
                           f"{'أسرع' if sooner > 0 else 'أبطأ'} بـ {abs(sooner)} شهر"))
        elif k:
            lines.append(L(lang, f"- You would reach it in about {k} month(s), around {after_months(k)} "
                                 f"(the same as now)",
                           f"- ستصل إليه خلال {k} شهر تقريبًا، حوالي {after_months(k)} (كما هو الآن)"))
        elif k is None:
            lines.append(L(lang, "- Nothing would be left to save, so the goal would not be reached",
                           "- لن يتبقى شيء للادخار، لذلك لن يتحقق الهدف"))
        scenario.update(goal=gname, months_before=b, months_after=k)
    # Every goal, not only the one above, and what a deficit would mean.
    from app.finance.decisions import position as decision_position
    pos = decision_position(user_id, month)
    if goal and pos["goal_need"] > 0 and len(pos["goals"]) > 1:
        short = pos["goal_need"] - saving
        lines.append(L(lang, f"- All your goals together need {amt(pos['goal_need'], lang)} a month: "
                             + (f"{amt(short, lang)} short ({num(pos['goal_need'])} − {num(saving)})" if short > 0
                                else f"covered, with {amt(-short, lang)} to spare"),
                       f"- كل أهدافك معًا تحتاج {amt(pos['goal_need'], lang)} شهريًا: "
                       + (f"نقص {amt(short, lang)} ({num(pos['goal_need'])} − {num(saving)})" if short > 0
                          else f"مغطاة ويزيد {amt(-short, lang)}")))
    if s["remaining"] < 0:
        lines.append(L(lang, f"- Risk: the {amt(-s['remaining'], lang)} a month gap would have to come from savings "
                             f"(savings outside goals: {amt(pos['savings_balance'], lang)})",
                       f"- خطر: العجز {amt(-s['remaining'], lang)} شهريًا سيُغطى من المدخرات "
                       f"(مدخرات خارج الأهداف: {amt(pos['savings_balance'], lang)})"))
    if s["clamped"]:
        lines.append(L(lang, "- A change would make an amount negative, so it was set to 0",
                       "- أحد التغييرات يجعل المبلغ سالبًا، لذلك اعتُبر 0"))
    head = L(lang, f"Hypothetical, based on your actual {label} figures; everything you did not mention stays "
                   f"the same, and nothing saved was changed:",
             f"سيناريو افتراضي مبني على أرقامك الفعلية لشهر {label}؛ كل ما لم تذكره يبقى كما هو، ولم "
             f"يتغير أي شيء محفوظ:")
    if stacked:
        head += "\n" + L(lang, "(This builds on your previous scenario: its changes are kept and the new one is "
                               "added.)",
                         "(هذا مبني على السيناريو السابق: تغييراته باقية وأُضيف إليها التغيير الجديد.)")
    text = head + "\n" + "\n".join(lines)
    data = {"month": month, "builds_on_previous": bool(stacked),
            "baseline": {"income": money(snap["income"]["total"]), "expenses": money(old_exp),
                         "remaining": money(snap["remaining"])},
            "scenario": {"income": money(s["income"]), "expenses": money(new_exp),
                         "remaining": money(s["remaining"]), "monthly_saving": money(saving)},
            "changes": [vars(c) for c in changes], "hypothetical": True}
    steps = [line.lstrip("- ") for line in lines]
    return _result(text, "simulate_scenario", data, steps,
                   {"months": [month], "scenario": scenario, "goal": gname}, gname)


# ---------- "of my 6,600 income I save 1,500": the split ----------

def _split(q, user_id, cur):
    """Saving and spending as shares of income. Uses the income the user states,
    else the recorded income (and says which)."""
    lang = q.language
    stated, saving = parse.split_amounts(q.text)
    basis = L(lang, "the income you gave", "الدخل الذي ذكرته")
    income = dec(stated) if stated is not None else None
    if income is None:
        recorded = facts.income(user_id, cur) if cur else {"total": None}
        if recorded["total"] is None:
            text = L(lang, "What is your monthly income? I need it to work out the percentages.",
                     "كم دخلك الشهري؟ أحتاجه لحساب النسب.")
            return _result(text, "calculate_split", {}, [], {"awaiting": "income"})
        income = recorded["total"]
        basis = L(lang, "your recorded monthly income", "دخلك الشهري المسجل")
    saving = dec(saving)
    if income <= 0:
        text = L(lang, "With an income of 0 the percentages cannot be worked out.",
                 "مع دخل يساوي 0 لا يمكن حساب النسب.")
        return _result(text, "calculate_split", {}, [], {})
    if saving > income:
        text = L(lang, f"Saving {amt(saving, lang)} is more than the income of {amt(income, lang)}, so that split "
                       "is not possible. Which of the two amounts should I change?",
                 f"ادخار {amt(saving, lang)} أكثر من الدخل {amt(income, lang)}، لذلك هذا التقسيم غير ممكن. "
                 "أي المبلغين تريد تعديله؟")
        return _result(text, "calculate_split", {}, [], {"awaiting": "amounts"})
    spend = income - saving
    save_rate, spend_rate = percent(saving, income), percent(spend, income)
    text = L(lang, f"Out of {amt(income, lang)} ({basis}):\n"
                   f"- Saving: {amt(saving, lang)}, which is {num(saving)} ÷ {num(income)} × 100 = {pct(save_rate)}\n"
                   f"- Spending: {num(income)} − {num(saving)} = {amt(spend, lang)}, which is "
                   f"{num(spend)} ÷ {num(income)} × 100 = {pct(spend_rate)}\n"
                   f"Together {pct(save_rate)} + {pct(spend_rate)} = 100%.",
             f"من {amt(income, lang)} ({basis}):\n"
             f"- الادخار: {amt(saving, lang)}، أي {num(saving)} ÷ {num(income)} × 100 = {pct(save_rate)}\n"
             f"- الصرف: {num(income)} − {num(saving)} = {amt(spend, lang)}، أي "
             f"{num(spend)} ÷ {num(income)} × 100 = {pct(spend_rate)}\n"
             f"والمجموع {pct(save_rate)} + {pct(spend_rate)} = 100%.")
    steps = [f"{num(saving)} ÷ {num(income)} × 100 = {pct(save_rate)}",
             f"{num(income)} − {num(saving)} = {num(spend)}",
             f"{num(spend)} ÷ {num(income)} × 100 = {pct(spend_rate)}"]
    data = {"income": money(income), "saving": money(saving), "spending": money(spend),
            "saving_rate": save_rate, "spending_rate": spend_rate, "hypothetical": True}
    return _result(text, "calculate_split", data, steps, {})


def _clarify(text, awaiting, extra=None):
    return _result(text, "clarify", {}, [], {"awaiting": awaiting, **(extra or {})})


def _goal_timing(q, user_id, cur, goal, previous):
    """"How much sooner will I reach it?" — from the latest what-if when there is
    one for this goal, else at the current pace."""
    lang = q.language
    scenario = (previous or {}).get("scenario")
    if scenario and goal and scenario.get("goal") == goal["name"]:
        b, k = scenario.get("months_before"), scenario.get("months_after")
        gname = goal["name"]
        if k is None:
            text = L(lang, f'In that scenario nothing is left to save, so "{gname}" would not be reached.',
                     f'في ذلك السيناريو لا يتبقى شيء للادخار، لذلك لن يتحقق "{gname}".')
        elif b is None:
            text = L(lang, f'Right now nothing is left to save each month; with that change you would reach '
                           f'"{gname}" in about {k} month(s), around {after_months(k)}.',
                     f'حاليًا لا يتبقى شيء للادخار شهريًا؛ مع هذا التغيير ستصل إلى "{gname}" خلال {k} شهر '
                     f'تقريبًا، حوالي {after_months(k)}.')
        else:
            diff = b - k
            text = L(lang, f'With the what-if ({num(scenario["saving"])} {CURRENCY["en"]} a month instead of '
                           f'{num(scenario["base_saving"])}), you would reach "{gname}" in about {k} month(s), '
                           f'around {after_months(k)}, instead of {b} month(s), around {after_months(b)}: '
                           + (f"{diff} month(s) sooner." if diff > 0 else
                              "no sooner, because the months are rounded up." if diff == 0 else
                              f"{-diff} month(s) later."),
                     f'مع السيناريو ({num(scenario["saving"])} ج.س شهريًا بدلًا من {num(scenario["base_saving"])})، '
                     f'ستصل إلى "{gname}" خلال {k} شهر تقريبًا، حوالي {after_months(k)}، بدلًا من {b} شهر، '
                     f'حوالي {after_months(b)}: '
                     + (f"أسرع بـ {diff} شهر." if diff > 0 else
                        "ليس أسرع، لأن الأشهر تُقرَّب لأعلى." if diff == 0 else f"أبطأ بـ {-diff} شهر."))
        rest = num(goal_facts(goal)["remaining_amount"])
        steps = [L(lang, f"Months now = remaining ÷ monthly saving = {rest} ÷ {num(scenario['base_saving'])}, "
                         f"rounded up = {b}",
                   f"الأشهر حاليًا = المتبقي ÷ الادخار الشهري = {rest} ÷ {num(scenario['base_saving'])}، "
                   f"مقربة لأعلى = {b}"),
                 L(lang, f"Months in the what-if = {rest} ÷ {num(scenario['saving'])}, rounded up = {k}",
                   f"الأشهر في السيناريو = {rest} ÷ {num(scenario['saving'])}، مقربة لأعلى = {k}"),
                 L(lang, f"Difference = {b} − {k} = {(b or 0) - (k or 0)} month(s)",
                   f"الفرق = {b} − {k} = {(b or 0) - (k or 0)} شهر")]
        return _result(text, "simulate_scenario", {"months_before": b, "months_after": k}, steps,
                       {"goal": gname, "scenario": scenario, "months": [cur]}, gname)
    if goal:
        return _goal(q, user_id, cur, goal)
    return None


# ---------- advice ----------

def _advice_for_amount(q, user_id, cur, target):
    """"How can I save 500 more a month?": cuts from flexible spending that add up
    to exactly 500 (or, when flexible spending is smaller, exactly what it allows)."""
    from app.preferences.user_preferences import is_essential, preference_map
    lang = q.language
    spent = facts.spending(user_id, [cur])
    prefs = preference_map(user_id)
    lines = {}
    for t in spent["transactions"]:
        if t["amount"] > 0 and not is_essential(user_id, t["category"], t["subcategory"], prefs):
            key = (t["category"], t["subcategory"])
            lines[key] = lines.get(key, ZERO) + t["amount"]
    flexible = sum(lines.values(), ZERO)
    label = month_label(cur, lang)
    if not lines:
        text = L(lang, f"All your spending in {label} is marked essential, so there is nothing flexible to cut "
                       f"for {amt(target, lang)} a month. Extra income, or marking some spending as flexible on "
                       "the Settings page, would be needed.",
                 f"كل إنفاقك في {label} مصنف أساسيًا، لذلك لا يوجد إنفاق مرن يمكن تخفيضه لتوفير "
                 f"{amt(target, lang)} شهريًا. يلزم دخل إضافي أو تصنيف بعض الإنفاق كمرن من صفحة الإعدادات.")
        return _result(text, "plan_savings", {"target": money(target)}, [], {"months": [cur]})
    cuts = verify.allocate_cuts(min(target, flexible), list(lines.items()))
    total = sum((c for _, _, c in cuts), ZERO)
    if not verify.validate_allocation([c for _, _, c in cuts], total) or total > target:
        raise ArithmeticError("savings plan does not add up")
    out = [L(lang, f"To save {amt(target, lang)} more a month, these cuts from your flexible spending in {label} "
                   "(in proportion to what you spend on each; essentials are protected):",
             f"لتوفير {amt(target, lang)} إضافية شهريًا، هذه تخفيضات من إنفاقك المرن في {label} "
             "(بنسبة ما تصرفه على كل بند؛ المصروفات الأساسية محمية):")]
    for (c, sub), spent_on, cut in cuts:
        out.append(L(lang, f"- {cat_label(c, sub, lang)}: cut {amt(cut, lang)} (from {num(spent_on)} to "
                           f"{num(spent_on - cut)})",
                     f"- {cat_label(c, sub, lang)}: خفّض {amt(cut, lang)} (من {num(spent_on)} إلى "
                     f"{num(spent_on - cut)})"))
    sum_text = " + ".join(num(c) for _, _, c in cuts)
    if total == target:
        out.append(L(lang, f"Total: {sum_text} = {amt(total, lang)} a month, exactly the {amt(target, lang)} you asked "
                           "for.",
                     f"المجموع: {sum_text} = {amt(total, lang)} شهريًا، أي {amt(target, lang)} المطلوبة بالضبط."))
    else:
        out.append(L(lang, f"Total: {sum_text} = {amt(total, lang)}, which is all your flexible spending; "
                           f"{amt(target - total, lang)} a month is still missing ({num(target)} − {num(total)}).",
                     f"المجموع: {sum_text} = {amt(total, lang)}، وهو كل إنفاقك المرن؛ ويبقى نقص "
                     f"{amt(target - total, lang)} شهريًا ({num(target)} − {num(total)})."))
    steps = [f"{cat_label(c, sub, lang)}: {num(target)} × {num(spent_on)} ÷ {num(flexible)} ≈ {num(cut)}"
             for (c, sub), spent_on, cut in cuts] + [f"{sum_text} = {num(total)}"]
    data = {"target": money(target), "cuts": [{"category": c, "subcategory": sub, "spent": money(a),
                                               "cut": money(x)} for (c, sub), a, x in cuts],
            "total": money(total), "flexible_spending": money(flexible)}
    return _result("\n".join(out), "plan_savings", data, steps, {"months": [cur]})


def _advice(q, user_id, cur, goal):
    from app.llm.agent import _compact_plan
    from app.tools.financial_tools import get_recommendation_plan
    lang = q.language
    target = next((dec(v) for v, is_pct, _ in parse.amounts(q.text) if not is_pct and v > 0), None)
    if target is not None and re.search(r"\bsave\b|ادخر|اوفر|وفر|احوش", q.text):
        return _advice_for_amount(q, user_id, cur, target)
    plan = _compact_plan(get_recommendation_plan(user_id, goal["name"] if goal else None))
    if not isinstance(plan, dict) or "error" in plan:
        return None
    parts = []
    g = plan.get("goal")
    gname = (g or {}).get("goal_name")
    if g:
        req, now, gap = (g.get("required_monthly_saving") or 0, g.get("current_monthly_saving") or 0,
                         g.get("saving_gap") or 0)
        parts.append(L(lang, f'For "{gname}" you need {amt(req, lang)} a month and you have {amt(now, lang)} '
                             "left each month after spending, "
                             + (f"so you are {amt(gap, lang)} a month short." if gap > 0 else "so you are on track."),
                       f'هدف "{gname}" يحتاج إلى {amt(req, lang)} شهريًا والمتبقي لك بعد المصروفات {amt(now, lang)}، '
                       + (f"أي أقل بمقدار {amt(gap, lang)} شهريًا." if gap > 0 else "أي أنك على المسار الصحيح.")))
    cuts = plan.get("cuts_from_flexible_spending") or []
    if cuts:
        parts.append(L(lang, "Practical cuts from your flexible spending (your essentials are protected):",
                       "تخفيضات عملية من إنفاقك غير الأساسي (مصروفاتك الأساسية محمية):"))
        parts.extend(
            L(lang, f"- {c['subcategory'] or c['category']}: cut {amt(c['cut_per_month'], lang)} a month "
                    f"(from {num(c['spent_per_month'])} to {num(c['spent_per_month'] - c['cut_per_month'])})",
              f"- {name(c['subcategory'] or c['category'], lang)}: خفّض {amt(c['cut_per_month'], lang)} شهريًا "
              f"(من {num(c['spent_per_month'])} إلى {num(c['spent_per_month'] - c['cut_per_month'])})")
            for c in cuts)
        freed = sum(dec(c["cut_per_month"]) for c in cuts)
        missing = dec(plan.get("still_missing_per_month") or 0)
        if g and dec(g.get("saving_gap") or 0) > 0 and missing > 0 and \
                not verify.validate_allocation([freed, missing], g["saving_gap"], Decimal("1")):
            raise ArithmeticError("plan cuts and the missing amount do not add up to the gap")
        parts.append(L(lang, f"Together these free {amt(freed, lang)} a month"
                             + (f"; {amt(missing, lang)} a month is still missing, which needs extra income or "
                                f"a later deadline." if missing > 0 else ", which closes the gap."),
                       f"معًا توفر هذه {amt(freed, lang)} شهريًا"
                       + (f"؛ ويبقى نقص {amt(missing, lang)} شهريًا يحتاج إلى دخل إضافي أو موعد أبعد."
                          if missing > 0 else "، وهذا يسد الفجوة.")))
    tips = [t["action"] for t in plan.get("evidence_based_tips") or [] if t.get("action")]
    if tips and lang != "ar":
        parts.append(("Also: " if cuts else "What your records suggest: ") + " ".join(tips[:2 if cuts else 3]))
    if not cuts and (lang == "ar" or not tips):
        return None                 # nothing concrete to list: the model explains the plan
    text = "\n".join(parts)
    steps = [L(lang, "Cuts come from the recommendation plan: flexible subcategories only, sized from your "
                     "actual monthly spending; essential spending is never cut.",
               "التخفيضات من خطة التوصيات: الفئات غير الأساسية فقط، محسوبة من إنفاقك الشهري الفعلي؛ "
               "ولا يُخفَّض أي إنفاق أساسي.")] + [
        f"{c['subcategory'] or c['category']}: {num(c['spent_per_month'])} − {num(c['cut_per_month'])} = "
        f"{num(c['spent_per_month'] - c['cut_per_month'])}" for c in cuts]
    return _result(text, "get_recommendation_plan", plan, steps, {"goal": gname, "months": [cur]}, gname)


# ---------- interest ----------

def _interest(q):
    from app.finance.calculations import compound_interest, simple_interest
    text, lang = q.text, q.language
    nums = parse.amounts(text)
    rate = next((v for v, is_pct, _ in nums if is_pct), None)
    years_m = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|سنه|سنوات|سنين|اعوام|عام|اشهر|شهور)", text)
    principal = next((v for v, is_pct, p in nums if not is_pct and not (years_m and p == years_m.start(1))), None)
    if rate is None or principal is None or not years_m:
        return None
    years = float(years_m.group(1))
    if years_m.group(2).startswith(("month", "اشهر", "شهور")):
        years /= 12
    compound = bool(re.search(r"compound|مركب", text))
    if compound:
        n = (12 if re.search(r"monthly|شهري", text) else
             4 if re.search(r"quarter|ربع", text) else
             365 if re.search(r"daily|يومي", text) else 1)
        r = compound_interest(principal, rate, years, n)
        every = {1: L(lang, "once a year", "مرة في السنة"), 4: L(lang, "quarterly", "كل ربع سنة"),
                 12: L(lang, "monthly", "شهريًا"), 365: L(lang, "daily", "يوميًا")}[n]
        answer = L(lang, f"{amt(principal, lang)} at {rate:g}% a year, compounded {every}, grows to "
                         f"{amt(r['total'], lang)} in {years:g} year(s); the interest is {amt(r['interest'], lang)}.",
                   f"{amt(principal, lang)} بفائدة {rate:g}% سنويًا، تُضاف {every}، تصبح {amt(r['total'], lang)} "
                   f"بعد {years:g} سنة؛ والفائدة {amt(r['interest'], lang)}.")
        steps = [f"A = P × (1 + r/n)^(n × t) = {num(principal)} × (1 + {rate / 100:g}/{n})^({n} × {years:g}) = "
                 f"{num(r['total'])}"]
    else:
        r = simple_interest(principal, rate, years)
        answer = L(lang, f"Simple interest on {amt(principal, lang)} at {rate:g}% a year for {years:g} year(s) is "
                         f"{amt(r['interest'], lang)}, so the total is {amt(r['total'], lang)}.",
                   f"الفائدة البسيطة على {amt(principal, lang)} بنسبة {rate:g}% سنويًا لمدة {years:g} سنة هي "
                   f"{amt(r['interest'], lang)}، فيصبح الإجمالي {amt(r['total'], lang)}.")
        steps = [f"I = P × r × t = {num(principal)} × {rate / 100:g} × {years:g} = {num(r['interest'])}"]
    return _result(answer, "calculate_interest", r, steps, {})


# ---------- explain ----------

def _explain(q, previous):
    lang = q.language
    steps = (previous or {}).get("calculation")
    if not steps:
        text = L(lang, "My last answer did not involve a calculation. Ask me about a number (for example your "
                       "spending this month) and I can show how it is worked out.",
                 "إجابتي الأخيرة لم تتضمن عملية حسابية. اسألني عن رقم (مثل إنفاقك هذا الشهر) وسأوضح طريقة حسابه.")
        return _result(text, "explain_calculation", {}, [], {})
    text = L(lang, "Here is how I worked that out, from your saved records:",
             "هكذا حسبت ذلك من سجلاتك المحفوظة:") + "\n" + "\n".join(f"- {s}" for s in steps)
    return _result(text, "explain_calculation", {"steps": steps}, steps,
                   {k: v for k, v in previous.items() if k != "calculation"}, previous.get("goal"))


# ---------- entry point ----------

def last_context(history):
    """The context of the most recent answer that had one, and how many answers ago."""
    seen = 0
    for m in reversed(list(history or [])):
        if m.get("role") != "assistant":
            continue
        seen += 1
        if m.get("context"):
            return m["context"], seen
    return None, seen


def _previous_question(history):
    users = [m["content"] for m in history or [] if m.get("role") == "user"]
    return users[-1] if users else ""


def answer(question, user_id, history=None, language="en", goal_context=None):
    """The fast-path answer as {"answer", "tool", "data", "calculation", "context",
    "goal", "language"}, or None when the model should answer instead."""
    began = time.perf_counter()
    cur = facts.current_month(user_id)
    goal = (goal_context or {}).get("active")
    strip = [goal["name"]] if goal else []
    q = parse.detect(_strip_names(question, strip), cur, language)
    q.question = question
    previous, age = last_context(history)
    # Decisions (a purchase, an emergency, which goal first, a saving plan) and
    # confirmed changes to saved data: app/services/decision_answers.py.
    from app.services import decision_answers
    last_goal = decision_answers.last_goal(history)
    from app.services.plan_followups import last_plan
    plan = last_plan(history)
    kind = decision_answers.detect(question, user_id, previous, age, last_goal, plan)
    if kind:
        began = trace.mark("interpret", began)
        found = decision_answers.answer(question, user_id, cur, q.language, previous, age, kind, last_goal, plan)
        began = trace.mark("retrieve_and_calculate", began)
        if found:
            found = _validated(found, began)
            found["context"] = {"intent": kind, **found["context"], "calculation": found["calculation"]}
            found["language"] = q.language
            return found
    if q.intent is None and goal_context and goal_context.get("how") == "named":
        # "Laptop and course." answering "which goal do you mean?": the earlier question's intent.
        earlier = parse.detect(_previous_question(history), cur, language)
        if earlier.intent in ("goal", "goal_timing", "advice"):
            q.intent = earlier.intent
    if q.intent is None and goal_context and goal_context.get("how") == "named" \
            and STATUS.search(question):
        q.intent = "goal"           # "tell me about my emergency fund": that goal's own figures
    if q.intent is None and previous and q.follow_up and previous.get("intent") in DATA_INTENTS:
        # "What about last month?" / "and food?": the last question again, changed.
        q.intent = previous["intent"]
        if not q.categories and previous.get("category") and q.intent == "category":
            q.categories = [tuple(previous["category"])]
        if q.categories and q.intent in ("total", "top"):
            q.intent = "category"
    if q.intent is None and previous and age == 1 and previous.get("awaiting") == "what_if_amount" \
            and parse.amounts(q.text) and len(q.text.split()) <= 4:
        # "200" / "10%" answering "by how much?": the what-if that was asked about.
        value, is_pct, _ = parse.amounts(q.text)[0]
        target, category, sub = previous["target"]
        q.intent, q.from_context = "what_if", True
        mode = "set" if previous.get("set") else "pct" if is_pct else "add"
        q.changes = [parse.Change(target, mode, previous["direction"] * value, category, sub)]
    if q.intent is None and previous and age == 1 and previous.get("awaiting") == "what_if_mode" \
            and len(q.text.split()) <= 6:
        # "more" / "in total" answering "300 more, or 300 in total?"
        if MORE_REPLY.search(q.text):
            q.intent, q.changes = "what_if", [parse.Change("expenses", "add", previous["value"])]
        elif TOTAL_REPLY.search(q.text):
            q.intent, q.changes = "what_if", [parse.Change("expenses", "set", previous["value"])]
    if previous and age == 1 and previous.get("awaiting") == "what_if_share" and len(q.text.split()) <= 8 \
            and (EACH.search(q.text) or TOGETHER.search(q.text)):
        # "each" / "together" answering "300 from each, or 300 in total?"
        q.intent, q.from_context = "what_if", True
        q.changes = [parse.Change(**c) for c in previous["pending"]]
        q.together = bool(TOGETHER.search(q.text)) and not EACH.search(q.text)
        if previous.get("month"):
            q.months = [previous["month"]]
    named = None
    if q.intent in (None, "goal") and DEFINES.search(q.text) and parse.PERSONAL.search(q.text):
        # only when it is also about the user ("... and how much should I have?"):
        # "What is an emergency fund?" alone is a general question
        from app.services.goal_context import match_goals
        matches = match_goals((goal_context or {}).get("goals") or [], question)
        named = matches[0] if len(matches) == 1 else None
    if named is not None:
        # "What is an emergency fund and how much should I have?" with a saved
        # "Emergency fund" goal: the trusted note for the first part, the goal's
        # own figures for the second (a model mixed the saved amount up with the target).
        found = _define_with_goal(q, user_id, named)
        if found:
            found["language"] = q.language
            return found
    if q.intent in ADVICE_CAN_REPLACE and _is_advice(q.text):
        q.intent = "advice"         # "how can I improve my savings?" asks for advice, not a number
    recent = previous if previous and age <= SCENARIO_MEMORY else None
    # "what if I increase the reduction to 20%?", "keep everything else unchanged",
    # "I meant restaurants AND fast food": the last scenario, revised.
    revised = _scenario_follow_up(q, recent)
    if revised is not None and "answer" in revised:
        revised["language"] = q.language
        return revised
    if q.intent is None and recent and (recent.get("scenario") or {}).get("changes") and (
            parse.amounts(q.text) or CORRECTION.search(q.text) or KEEP_OTHERS.search(q.text)):
        # About the last scenario but not clear how: ask, never hand it to the model.
        found = _clarify(L(q.language, f"Do you want to change the last scenario ({_scenario_summary(recent, 'en')})? "
                                       "Tell me the new amount or percentage, or which categories to include.",
                           f"هل تريد تعديل السيناريو السابق ({_scenario_summary(recent, 'ar')})؟ أخبرني بالمبلغ أو "
                           "النسبة الجديدة، أو الفئات التي تريد تضمينها."), "what_if_target")
        found["language"] = q.language
        return found
    if q.intent is None:
        return None
    ref = _referent(recent)
    if q.intent == "compare" and not q.categories and ref and ref[0] == "category" \
            and REFERS_BACK.search(q.text):
        q.categories = [ref[1:]]                            # "compare it with last month"
    if q.period is None and q.intent in KEEPS_MONTH and recent and recent.get("months") \
            and not q.changes:
        # "and how much did I spend?" after a question about October: still October.
        q.period = {"kind": "month", "months": [recent["months"][0]]}
        q.months = [recent["months"][0]]
    if q.intent == "what_if":
        asked = _what_if_questions(q, recent)
        if asked:
            asked["language"] = q.language
            return asked
    if cur is None and q.intent in ("goal", "goal_timing", "advice"):
        cur = date.today().strftime("%Y-%m")
    began = trace.mark("interpret", began)
    if q.intent == "interest":
        out = _interest(q)
    elif q.intent == "explain":
        last = next((m for m in reversed(history or []) if m.get("role") == "assistant"), {})
        # Only figures the model wrote are checked; the app's own answers are computed.
        last_answer = last.get("content", "") if (last.get("context") or {}).get("intent") in (None, "model") else ""
        # "Where did the expense number come from?" names the figure: it is worked out
        # again from the records. A bare "how did you calculate that?" shows the last steps.
        if (previous or {}).get("calculation") and not _claimed_numbers(q.text, []) \
                and not parse.asked_parts(q.text):
            out = _explain(q, previous)
        elif cur is not None:
            out = _explain_source(q, user_id, cur, recent, last_answer)
        else:
            out = _explain(q, previous)
    elif cur is None and q.intent not in ("goal", "goal_timing", "advice", "income", "split", "goals"):
        out = _result(_no_data(q.language), "get_monthly_spending", {}, [], {})
    elif q.intent == "what_if":
        # The goal is part of the answer when the conversation is about it, the user
        # puts money aside, or the what-if is open ("what if transport goes up?").
        # A precise question ("how much would be left?") gets just its answer.
        # A goal is added only when the user brings it up (or the conversation is
        # about one, or money is put aside): "reduce restaurants by 15%" is answered
        # without an unrelated goal calculation.
        scenario_goal = goal
        if scenario_goal is None and ((recent or {}).get("goal") or any(c.target == "saving" for c in q.changes)
                                      or GOAL_WORD.search(q.text)):
            scenario_goal = _goal_named((recent or {}).get("goal"), user_id) or _main_goal(user_id)
        out = _what_if(q, user_id, cur, scenario_goal, recent)
        out = _checked_what_if(q, out)
    elif q.intent == "split":
        out = _split(q, user_id, cur)
    elif q.intent == "transactions":
        out = _transactions(q, user_id, cur, recent)
    elif q.intent == "unknown_category":
        m = parse.SPENT_ON.search(q.text)
        known = ", ".join(name(c, q.language) for c in sorted(facts.spending(user_id, [cur])["by_category"]))
        out = _clarify(L(q.language, f"I could not match \"{m.group(1)}\" to one of your spending categories, so I "
                                     f"will not guess. Your categories this month: {known}. Which one do you mean?",
                         f"لم أستطع مطابقة \"{m.group(1)}\" مع إحدى فئات مصروفاتك، لذلك لن أخمّن. فئاتك هذا الشهر: "
                         f"{known}. أيّها تقصد؟"), "category")
    elif q.intent == "percent_of":
        out = _percent_of(q, user_id, cur)
    elif q.intent == "forecast":
        out = _forecast(q, user_id, cur)
    elif q.intent == "average" and (parse.DAILY.search(q.text) or parse.WEEKLY.search(q.text)):
        out = _per_day(q, user_id, cur)
    elif q.intent == "goal_timing":
        out = _goal_timing(q, user_id, cur, goal, previous if age <= SCENARIO_MEMORY else None)
    elif q.intent in ("goal", "advice"):
        if q.intent == "goal" and goal is None:
            if (goal_context or {}).get("how") != "no goals":
                return None         # e.g. a hypothetical purchase: the model handles it
            out = _result(L(q.language, "You have no saved goals yet. Add one on the Goals page (only the target "
                                        "amount and the date are needed), or tell me the amount and the date.",
                            "لا توجد لديك أهداف محفوظة بعد. أضف هدفًا من صفحة الأهداف (يكفي المبلغ المستهدف "
                            "والتاريخ)، أو أخبرني بالمبلغ والتاريخ."), "list_goals", {}, [], {})
        elif q.intent == "goal":
            out = _goal(q, user_id, cur, goal, (goal_context or {}).get("how"))
        else:
            out = _advice(q, user_id, cur, goal)
    else:
        builder = {"total": _total, "category": _category, "top": _top, "average": _average,
                   "compare": _compare, "why": _why, "income": _income, "savings": _savings,
                   "budget": _budget, "summary": _summary, "trend": _trend, "anomalies": _anomalies,
                   "goals": _goals, "overview": _overview}[q.intent]
        out = builder(q, user_id, cur)
    if out is None:
        return None
    began = trace.mark("retrieve_and_calculate", began)
    out = _validated(out, began)
    out["context"] = {"intent": q.intent, **out["context"], "calculation": out["calculation"]}
    if q.intent == "explain":
        out["context"]["intent"] = (previous or {}).get("intent", "explain")
    out["language"] = q.language
    return out


def _validated(out, began):
    """Last check before an answer is shown: every "a + b = c" in it must be right.
    A wrong sum here is a bug, so it is corrected and logged, never shown."""
    fixed, problems = verify.check_arithmetic(out["answer"])
    if problems:
        trace.logger.warning("fast-path answer had %d wrong sum(s); corrected (tool %s)", len(problems), out["tool"])
        out["answer"] = fixed
    trace.mark("validate", began)
    return out


# ---------- follow-ups to a scenario ----------

KEEP_OTHERS = re.compile(r"\bkeep (?:all |the )?(?:other|rest|remaining|everything else)\b|\b(?:everything|all) else "
                         r"(?:the same|unchanged|as (?:it is|is))|\bothers? (?:unchanged|the same)\b|\bunchanged\b|"
                         r"(?:باقي|بقيه|الباقي|كل) (?:ال)?(?:مصروفات|مصاريف|الحاجات|شي)? ?(?:زي ما هي|كما هي|ثابت|ثابته|"
                         r"بدون تغيير)|خلي (?:ال)?باقي")
CORRECTION = re.compile(r"\bi meant\b|\bi mean\b|\bnot only\b|\byou (?:only |just )?(?:calculated|used|changed|forgot|"
                        r"missed|included)\b|\bthat'?s (?:wrong|not what)|\bwrong\b|\bactually\b|"
                        r"(?<![ء-ي])(?:قصدت|اقصد|بقصد|مش بس|مو بس|ليس فقط|غلط|خطا|نسيت|حسبت بس|انت حسبت)(?![ء-ي])")
MEANT = re.compile(r"\b(?:i meant|i mean|but|should be|instead)\b|(?<![ء-ي])(?:قصدت|اقصد|بقصد|لكن|بل)(?![ء-ي])")
TOGETHER = re.compile(r"\b(?:together|combined|in total|altogether|between them)\b|مع بعض|سوا|اجمالي|مجتمعين|معا")
EACH = re.compile(r"\beach\b|\bper (?:category|item)\b|(?<![ء-ي])(?:كل واحد|لكل|كل وحده|كل فئه)(?![ء-ي])")


def _names(changes, lang):
    labels = [cat_label(c.category, c.subcategory, lang) if c.target == "category" else
              {"income": L(lang, "income", "الدخل"), "expenses": L(lang, "total spending", "إجمالي المصروفات"),
               "saving": L(lang, "saving", "الادخار")}.get(c.target, c.target) for c in changes]
    return L(lang, " and ", " و").join(labels)


def _scenario_summary(recent, lang):
    changes = _changes_from(recent.get("scenario"))
    if not changes:
        return ""
    c = changes[0]
    how = (f"{num(abs(c.value))}%" if c.mode == "pct" else amt(abs(c.value), lang))
    return f"{_names(changes, lang)} {'−' if c.value < 0 else '+'}{how}"


PLURAL_REF = re.compile(r"\b(?:them|both|these|those|all of them|the two|both categories|these (?:expenses|categories))\b|"
                        r"(?<![ء-ي])(?:هم|كلهم|الاثنين|الاتنين|التنين|الاثنتين|ديل|دول|هذه|هؤلاء|البندين|الفئتين)(?![ء-ي])|"
                        r"(?<=[ء-ي])(?:هم|هما)(?![ء-ي])")


def _plural_follow_up(q, recent):
    """"reduce them by 15%" after an answer about restaurants AND fast food: the
    change applies to every category that answer was about."""
    cats = [tuple(c) for c in (recent or {}).get("categories") or []]
    if not cats:
        cats = [(c.category, c.subcategory) for c in _changes_from((recent or {}).get("scenario"))
                if c.target == "category"]
    if len(cats) < 2 or not PLURAL_REF.search(q.text) or parse.find_categories(q.text):
        return None
    found = parse.amounts(q.text)
    if not found:
        return None
    value, is_pct, _ = found[0]
    direction = parse._direction(q.text) or 1
    mode = "pct" if is_pct else "add"
    q.intent, q.from_context = "what_if", True
    q.changes = [parse.Change("category", mode, direction * value, c, s) for c, s in cats]
    q.together = bool(TOGETHER.search(q.text)) and mode == "add"
    names = _names(q.changes, q.language)
    q.followup_note = L(q.language, f"Applied to both: {names}.", f"طُبّق على الاثنين: {names}.")
    if recent.get("months"):
        q.months = [recent["months"][0]]
    return {}


def _scenario_follow_up(q, recent):
    """A scenario follow-up as a re-run of the last scenario with the change asked
    for (q.changes / q.intent are set), a clarifying question, or None."""
    plural = _plural_follow_up(q, recent)
    if plural is not None:
        return plural
    scenario = (recent or {}).get("scenario") or {}
    old = _changes_from(scenario)
    if not old:
        return None
    lang, text = q.language, q.text
    named = parse.find_categories(parse.without_rest_unchanged(parse.condition_part(text)))
    targets = parse.INCOME_WORD.search(text) or parse.EXTRA_SAVING.search(text)
    found = [(v, p) for v, p, _ in parse.amounts(text)]
    note = None
    if CORRECTION.search(text) and named:
        # "you calculated only restaurants, but I meant restaurants and fast food together"
        after = MEANT.search(text)
        meant = parse.find_categories(text[after.end():]) if after else []
        meant = meant or named
        base = old[0]
        value, is_pct = (found[-1][0], found[-1][1]) if found else (abs(base.value), base.mode == "pct")
        sign = -1 if base.value < 0 else 1
        mode = "pct" if is_pct else base.mode
        new = [parse.Change("category", mode, sign * value if mode in ("pct", "add") else value, c, s)
               for _, c, s in meant]
        before = {(c.category, c.subcategory) for c in old}
        if {(c.category, c.subcategory) for c in new} == before and value == abs(base.value):
            return None
        note = L(lang, f"You're right: the previous answer changed only {_names(old, lang)}. Here it is with "
                       f"{_names(new, lang)} together:",
                 f"معك حق: الإجابة السابقة غيّرت {_names(old, lang)} فقط. هذه النتيجة مع {_names(new, lang)} معًا:")
    elif named and found and (STACK.search(text) or q.follow_up) and not CORRECTION.search(text):
        # "and transport by 10% too": added to the last scenario, in the same direction
        # unless the words say otherwise
        value, is_pct = found[0]
        direction = parse._direction(parse.without_rest_unchanged(text)) or (-1 if old[0].value < 0 else 1)
        added = [parse.Change("category", "pct" if is_pct else "add", direction * value, c, s) for _, c, s in named]
        new = old + added
        note = L(lang, f"Added to the last scenario: {_names(added, lang)}; {_names(old, lang)} stay as before.",
                 f"أُضيف إلى السيناريو السابق: {_names(added, lang)}؛ و{_names(old, lang)} كما كانت.")
    elif q.intent is None and q.period and q.period.get("kind") == "month" and not found and not named and \
            (q.follow_up or len(text.split()) <= 6):
        # "what about last month?": the same scenario on that month's figures
        month = q.period["months"][0]
        q.intent, q.changes, q.from_context = "what_if", old, True
        q.followup_note = L(lang, f"The same scenario ({_names(old, lang)}), on {month_label(month, lang)}'s figures:",
                            f"نفس السيناريو ({_names(old, lang)})، على أرقام {month_label(month, lang)}:")
        q.months = [month]
        return {}
    elif KEEP_OTHERS.search(text) and not found:
        new = old                                     # "keep all other expenses unchanged": it already is
        note = L(lang, f"Yes: only {_names(old, lang)} change; every other expense stays exactly as recorded.",
                 f"نعم: يتغير {_names(old, lang)} فقط؛ وكل المصروفات الأخرى تبقى كما هي مسجلة.")
    elif len(found) == 1 and not named and not targets and not (q.changes and any(c.target for c in q.changes)) \
            and not STACK.search(text):
        # "what if I increase the reduction to 20%?", "لو خليتها 20%؟": same changes, new size
        value, is_pct = found[0]
        if len({(c.mode, c.value) for c in old}) > 1:
            return _clarify(L(lang, f"Your last scenario changed {_names(old, lang)} by different amounts. Which one "
                                    f"should become {num(value)}{'%' if is_pct else ''}?",
                              f"السيناريو السابق غيّر {_names(old, lang)} بمقادير مختلفة. أيها يصبح "
                              f"{num(value)}{'%' if is_pct else ''}؟"), "what_if_value")
        base = old[0]
        sign = -1 if base.value < 0 else 1
        mode = "pct" if is_pct else ("set" if base.mode == "set" else "add")
        new = [parse.Change(c.target, mode, value if mode == "set" else sign * value, c.category, c.subcategory)
               for c in old]
        was = f"{num(abs(base.value))}{'%' if base.mode == 'pct' else ''}"
        now = f"{num(value)}{'%' if is_pct else ''}"
        note = L(lang, f"Same scenario ({_names(old, lang)}), now {now} instead of {was}:",
                 f"نفس السيناريو ({_names(old, lang)})، الآن {now} بدلًا من {was}:")
        if CORRECTION.search(text):
            note = L(lang, f"You're right, {now} instead of {was}. ", f"معك حق، {now} بدلًا من {was}. ") + note
    else:
        return None
    q.intent, q.changes, q.from_context, q.followup_note = "what_if", new, True, note
    if scenario.get("month"):
        q.months = [scenario["month"]]
    return {}


def _checked_what_if(q, out):
    """The scenario answer checked against the request before it is shown: every
    category the user named must be in it, and every sum in the text must be right."""
    if out["tool"] == "clarify" or "changes" not in out["data"]:
        return out                      # a question back ("what is your income?"), not a result
    if not q.from_context:
        named = {(c, s) for _, c, s in parse.find_categories(parse.without_rest_unchanged(parse.condition_part(q.text)))}
        changed = {(a["category"], a["subcategory"] or None) for a in out["data"].get("changes", [])
                   if a.get("target") == "category"}
        changed |= {(c, None) for c, _ in changed}
        missing = [n for n in named if n not in changed and (n[0], None) not in changed]
        if missing:
            what = ", ".join(cat_label(c, s, q.language) for c, s in missing)
            return _clarify(L(q.language, f"I could not apply your change to {what}. Please say how it should change.",
                              f"لم أتمكن من تطبيق التغيير على {what}. وضّح كيف يتغير من فضلك."), "what_if_amount")
    fixed, problems = verify.check_arithmetic(out["answer"])
    if problems:
        raise ArithmeticError(f"what-if text has wrong sums: {problems}")
    if getattr(q, "followup_note", None):
        out["answer"] = q.followup_note + "\n" + out["answer"]
    return out


# "and another 200": the new change is added to the last what-if
STACK = re.compile(r"\b(?:also|too|another|again|on top of (?:that|it)|in addition|further)\b|"
                   r"(?<![ء-ي])(?:ايضا|كمان|تاني|ثاني|كذلك|فوق (?:كده|ذلك|دا)|زياده علي (?:كده|ذلك))(?![ء-ي])")
MORE_REPLY = re.compile(r"\b(?:more|extra|on top|additional)\b|زياده|اكتر|اكثر|فوق|اضافي")
TOTAL_REPLY = re.compile(r"\b(?:total|in total|altogether|only)\b|اجمالي|الاجمالي|بس|فقط|كله|كلها")
REFERS_BACK = re.compile(r"\b(?:it|that|this|them|those)\b|^قارن(?:ها|ه|هم)|^(?:و|طيب )?(?:ماذا عن|نفس)")
SPECIFIC = re.compile(r"\bhow much\b|\bwhat (?:will|would) (?:be|my)\b|\bwhat (?:is|are) (?:the|my) new\b|"
                      r"(?<![ء-ي])كم(?![ء-ي])")
GOAL_WORD = re.compile(r"\b(?:goals?|target)\b|هدف")


def _referent(previous):
    """What "it" means in "raise it by 15%": the variable the previous answer was about."""
    if not previous:
        return None
    for c in reversed((previous.get("scenario") or {}).get("changes", [])):
        if c.get("target") in ("category", "income", "expenses"):
            return c["target"], c.get("category"), c.get("subcategory")
    if previous.get("category"):
        category, sub = previous["category"]
        return "category", category, sub
    return {"total": ("expenses", None, None), "top": ("expenses", None, None),
            "compare": ("expenses", None, None), "average": ("expenses", None, None),
            "income": ("income", None, None)}.get(previous.get("intent"))


def _target_label(ref, lang):
    target, category, sub = ref
    if target == "income":
        return L(lang, "your income", "دخلك")
    if target == "expenses":
        return L(lang, "your total spending", "إجمالي مصروفاتك")
    return cat_label(category, sub, lang)


def _what_if_questions(q, previous):
    """Resolve "it" from the conversation, or ask one short question instead of
    guessing: which variable, by how much, or which of two values."""
    lang = q.language
    unresolved = [c for c in q.changes if c.target is None]
    if unresolved or q.needs_amount:
        ref = _referent(previous)
        if ref is None:
            return _clarify(L(lang, "Which amount should change: your income, your total spending, or one "
                                    "category (for example Transport)?",
                              "أي مبلغ تقصد: دخلك، أم إجمالي مصروفاتك، أم فئة معينة (مثل المواصلات)؟"),
                            "what_if_target")
        if q.needs_amount:
            down = bool(parse.DOWN.search(q.text)) and not parse.UP.search(q.text)
            verb = L(lang, "reduce" if down else "increase", "تخفيض" if down else "زيادة")
            return _clarify(L(lang, f"By how much should I {verb} {_target_label(ref, lang)}: an amount or a "
                                    "percentage?",
                              f"بكم تريد {verb} {_target_label(ref, lang)}؟ مبلغ أم نسبة مئوية؟"),
                            "what_if_amount", {"scenario": (previous or {}).get("scenario"), "target": list(ref),
                                               "direction": -1 if down else 1,
                                               "category": list(ref[1:]) if ref[0] == "category" else None})
        for c in unresolved:
            c.target, c.category, c.subcategory = ref
        q.from_context = True
    for c in q.changes:
        if c.mode == "ask":
            ref = (c.target, c.category, c.subcategory)
            down = c.value < 0
            verb = L(lang, "reduce" if down else "increase", "تخفيض" if down else "زيادة")
            return _clarify(L(lang, f"By how much should I {verb} {_target_label(ref, lang)}: an amount or a "
                                    "percentage?",
                              f"بكم تريد {verb} {_target_label(ref, lang)}؟ مبلغ أم نسبة مئوية؟"),
                            "what_if_amount", {"target": list(ref), "direction": -1 if down else 1,
                                               "category": list(ref[1:]) if c.target == "category" else None})
        if c.mode == "unclear" and c.target == "expenses":
            return _clarify(L(lang, f"Do you mean spending {num(c.value)} more than now, or total spending of "
                                    f"{num(c.value)}?",
                              f"هل تقصد أن تصرف {num(c.value)} زيادة على مصروفاتك الحالية، أم أن يكون إجمالي "
                              f"مصروفاتك {num(c.value)}؟"),
                            "what_if_mode", {"value": c.value})
        if c.mode == "unclear":
            return _clarify(L(lang, "How much do you want to save each month (an amount)?",
                              "كم تريد أن تدخر شهريًا (مبلغ)؟"), "what_if_amount",
                            {"target": ["saving", None, None], "direction": 1, "set": True})
    cats = [c for c in q.changes if c.target == "category"]
    if len(cats) > 1 and not q.from_context and all(c.mode == "add" for c in cats) \
            and len({c.value for c in cats}) == 1 and not EACH.search(q.text):
        if TOGETHER.search(q.text):
            q.together = True               # one amount, shared in proportion to each line
        else:
            value = abs(cats[0].value)
            return _clarify(L(lang, f"Do you mean {num(value)} from each of {_names(cats, lang)} "
                                    f"({num(value * len(cats))} in total), or {num(value)} in total shared between them?",
                              f"هل تقصد {num(value)} من كل واحد من {_names(cats, lang)} ({num(value * len(cats))} "
                              f"إجمالًا)، أم {num(value)} إجمالًا موزعة بينها؟"), "what_if_share",
                            {"pending": [vars(c) for c in q.changes], "month": (q.months or [None])[0]})
    clash = parse.conflicting(q.changes)
    if clash:
        values = [num(c.value) for c in q.changes if (c.target, c.category, c.subcategory) == clash]
        what = _target_label(clash, lang)
        return _clarify(L(lang, f"You gave two values for {what} ({' and '.join(values)}). Which one should I use?",
                          f"ذكرت قيمتين لـ{what} ({' و'.join(values)}). أيهما أستخدم؟"), "what_if_value")
    return None


DEFINES = re.compile(r"\bwhat (?:is|are) (?:an? |the )?(?!my\b)\w+|\bdefine\b|\bmeaning of\b|"
                     r"(?:ما|ماهو|ما هو|ما هي|ايش|شنو) (?:هو |هي )?(?!دخلي|مصروفاتي)[^\s؟?]+|يعني ايه|معني")


def _define_with_goal(q, user_id, goal):
    from app.rag.knowledge import list_entries, search
    lang = q.language
    found = search(q.question, lang=lang if lang in ("en", "ar") else None)
    if not found["found"] or found["results"][0]["score"] < 0.2:
        return None
    best = found["results"][0]
    entry = next((e for e in list_entries() if e["id"] == best["entry_id"]), None)
    if entry is None:
        return None
    key = "ar" if lang == "ar" else "en"
    passages = [p[key] for p in entry["passages"][:2] if p.get(key)]
    f = goal_facts(goal)
    goal_line = L(lang, f'Your goal "{f["name"]}": {num(f["saved_so_far"])} saved of the {amt(f["target_amount"], lang)} '
                        f'you set ({f["percent_complete"]:.0f}%), {amt(f["remaining_amount"], lang)} still to go',
                  f'هدفك "{f["name"]}": ادخرت {num(f["saved_so_far"])} من {amt(f["target_amount"], lang)} التي حددتها '
                  f'({f["percent_complete"]:.0f}%)، ويتبقى {amt(f["remaining_amount"], lang)}')
    if f.get("required_monthly_saving") is not None:
        goal_line += L(lang, f"; by {f['target_date']} that is {amt(f['required_monthly_saving'], lang)} a month "
                             f"({num(f['remaining_amount'])} ÷ {f['months_left']}).",
                       f"؛ وحتى {f['target_date']} يلزم {amt(f['required_monthly_saving'], lang)} شهريًا "
                       f"({num(f['remaining_amount'])} ÷ {f['months_left']}).")
    else:
        goal_line += "."
    source = dict(entry["source"], entry_id=entry["id"], passage_id=best["passage_id"])
    text = (" ".join(passages) + f" ({source['publisher']})\n" + goal_line)
    out = _result(text, "assess_goal", f, [], {"intent": "goal", "goal": f["name"]}, f["name"])
    out["sources"] = [source]
    return out


def _goal_named(goal_name, user_id):
    if not goal_name:
        return None
    from app.database.crud import get_goals
    return next((g for g in get_goals(user_id) if g["name"] == goal_name), None)


def _strip_names(question, names):
    """Remove a goal's own name before parsing ("Rent deposit" is not the Rent category)."""
    for n in names:
        question = re.sub(re.escape(n), " ", question, flags=re.IGNORECASE)
    return question


def _main_goal(user_id):
    from app.database.crud import get_goals
    from app.engines.financial_engine import _pick_primary_goal
    goal = _pick_primary_goal(get_goals(user_id))
    return goal if goal and goal["current_amount"] < goal["target_amount"] else None


def _is_advice(text):
    from app.services.goal_context import PLAN
    return bool(PLAN.search(text) or re.search(
        r"\bwhat should i (?:do|cut|change|reduce)\b|\bhow (?:can|do|could) i (?:save|cut|reduce|spend less)\b|"
        r"\bwhere can i (?:save|cut)\b|\bhow (?:can|do|could|should) i (?:\w+ ){0,2}(?:save|cut|reduce|lower|"
        r"spend less)\b|\b(?:tips|ways) to (?:save|cut|reduce)\b|ماذا افعل|ماذا يجب ان افعل|"
        r"كيف (?:\S+ ){0,2}(?:اوفر|ادخر|اقلل|اخفض|انقص|احوش|اقلص)|(?:طرق|نصائح) (?:ل)?(?:التوفير|الادخار|تقليل)", text))
