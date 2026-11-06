# app/services/decision_answers.py
# Decision questions in the chat, answered from the decision engine
# (app/finance/decisions.py) without the language model:
#
#   purchase   "can I afford a new phone?", "buy this laptop now or wait?",
#              "in 6 installments?", "how much can I safely spend on it?",
#              "a 5,000 phone or my emergency fund first?" (a trade-off)
#   emergency  "I have an unexpected 2,000 SDG medical bill"
#   priority   "which goal should I focus on first?"
#   saving     "give me a saving plan based on my spending"
#   action     "add 500 to my emergency fund" — a real change, made only after
#              the user confirms it (app/services/actions.py)
#
# Every answer has the same parts: the recommendation and why, the verified
# figures it rests on, the options compared, risks, assumptions, what is still
# missing, and the next step. Hypothetical answers say that nothing was saved.
# Follow-ups ("what if I wait 3 months?", "and in 6 installments?", "a cheaper
# one for 3,000?", "yes") continue from the decision kept in the answer context.

import calendar
import re
from datetime import date
from decimal import Decimal

from app.database import crud
from app.finance import decisions as engine
from app.finance import facts, verify
from app.finance.facts import ZERO, dec
from app.services import actions
from app.services import intent as parse
from app.services.direct_answers import L, after_months, amt, cat_label, month_label, name, num
from app.services.goal_context import goal_facts, match_goals

AMT = r"(\d[\d,]*(?:\.\d+)?)"
_A = parse.AR_LETTER

# ---------- what the question is about ----------
BOUGHT = re.compile(r"\b(?:did i buy|have i bought|i bought|i purchased)\b|اشتريت")
PURCHASE = re.compile(
    r"\bafford\b|\b(?:buy|buying|purchase|purchasing)\b|\binstal{1,2}ments?\b|"
    r"\b(?:take|get|taking|getting)(?: out)? an? (?:loan|credit)\b|\b(?:loan|financing|credit) (?:for|to buy)\b|"
    rf"(?<!{_A})(?:قرض|سلفيه|سلفه|تمويل)(?!{_A})|"
    r"\bpay(?:ing)? (?:for )?(?:it |this |that )?(?:in|over) \d+ (?:months|payments)\b|"
    r"\bhow much can i (?:safely )?(?:spend|afford) (?:on|for) (?:this|a|an|the|my) (?:purchase|item|thing)\b|"
    rf"(?<!{_A})(?:اشتري|نشتري|بشتري|حاشتري|ساشتري|هشتري|شراء|اشتراء|اجيب|اقتني|تقسيط|بالتقسيط|اقساط|قسط)(?!{_A})|"
    r"كم (?:اقدر|يمكنني ان|ممكن) (?:اصرف|انفق) (?:بامان )?(?:علي|في) (?:الشراء|المشتري|الحاجه)")
SAFE_PRICE = re.compile(r"\bhow much can i (?:safely )?(?:spend|afford)\b|\bsafely spend\b|\bmax(?:imum)? (?:price|budget)\b|"
                        r"كم (?:اقدر|يمكنني ان|ممكن) (?:اصرف|انفق)|اقصي (?:سعر|مبلغ)|بامان")
EMERGENCY = re.compile(
    r"\b(?:emergency|unexpected|unplanned|sudden|surprise|urgent)\s+(?:\w+\s+)?(?:expense|cost|bill|repair|payment|"
    r"spending)s?\b|\bmedical (?:bill|emergency)\b|\b(?:had|have|got|facing) an emergency\b|"
    r"\b(?:car|phone|laptop|fridge) (?:broke|broken|needs? (?:a )?repair)\b|"
    r"(?:مصروف|مصاريف|صرفه|فاتوره|ظرف|دفعه)\s+(?:طارئ|طارئه|طاريء|مفاجئ|مفاجئه|مستعجل|مستعجله)|"
    rf"(?<!{_A})(?:حاله|ظروف) طارئه|عندي (?:ظرف|حاجه) طارئ|مستشفي|عمليه جراحيه")
PRIORITY = re.compile(
    r"\bwhich (?:goal|one) (?:should|to|do|first|comes)|\bprioriti[sz]\w*|\bpriorit(?:y|ies)\b|"
    r"\bone goal at a time\b|\ball (?:my )?goals at once\b|\bsplit (?:my )?(?:savings|money) (?:between|across)\b|"
    r"\bfocus on (?:one|which) goal\b|\bwhich goals? (?:first|matters)\b|"
    rf"اولويه|اولويات|(?:باي|اي) هدف (?:ابدا|اركز|اول|اهم)|ترتيب (?:ال)?اهداف|ارتب (?:ال)?اهداف|"
    r"هدف واحد (?:في|ف) المره|اقسم (?:ال)?(?:ادخار|مدخرات|فلوس|قروش) (?:علي|بين)")
SAVING_PLAN = re.compile(
    r"\bsavings? (?:plan|strateg\w*)\b|\bwhere (?:can|could|should) i (?:save|cut)\b|\bspending limits?\b|"
    r"\bweekly (?:target|limit|budget)s?\b|\bsave based on my spending\b|\bpersonali[sz]ed saving|"
    r"خطه (?:ادخار|توفير|للادخار|للتوفير)|استراتيجيه (?:ادخار|توفير)|(?:وين|اين) (?:اقدر|ممكن|يمكنني) "
    r"(?:اوفر|اقلل|التوفير|ان اوفر)|حدود (?:ل)?(?:ال)?صرف|حد (?:ل)?(?:ل)?صرف")
# Words that set the purchase's urgency (None when neither is said).
URGENT = re.compile(r"\b(?:urgent\w*|essential|necessary|broke|broken|stolen|lost it|for work|i need it|need it now|"
                    r"it'?s needed|must have)\b|ضروري|مستعجل|محتاجه|محتاج|اتكسر|انكسر|خربان|خرب|اتسرق|ضاع|للشغل|للعمل")
OPTIONAL = re.compile(r"\b(?:not urgent|not essential|optional|nice to have|upgrade|luxury|can wait|just want)\b|"
                      r"مش ضروري|ما ضروري|غير ضروري|مو ضروري|كمالي|ممكن استني|ممكن انتظر")
GOAL_FIRST = re.compile(r"\b(?:more important|priority|comes first|first)\b|اهم|اولويه|الاول")
# Follow-ups to a purchase.
DELAY = re.compile(r"\b(?:delay|wait|postpone|later|put off|hold off)\w*\b|"
                   rf"(?<!{_A})(?:و|ف)?(?:اجل|ااجل|اوجل|اجلت|اجلته|اجلتها|انتظر|انتظرت|استني|اصبر)(?!{_A})|"
                   r"بعد " + parse._N + r" (?:اشهر|شهور)")
# A question about what was already spent is about the records, never a decision.
SPENT_QUESTION = re.compile(r"\b(?:did i spend|have i spent|i spent|spent on|did i pay|price of my last|did .{1,20} cost)\b|"
                            rf"(?<!{_A})(?:صرفت|انفقت|دفعت)(?!{_A})")
MONTHS_N = re.compile(parse._N + r"\s*(?:more\s+)?(?:months?|اشهر|شهور|شهر)")
INSTALL_N = re.compile(parse._N + r"\s*(?:installments?|instalments?|payments|monthly payments|اقساط|قسط|دفعات)|"
                       r"(?:installments?|instalments?|اقساط|قسط|بالتقسيط|تقسيط)\D{0,12}?" + parse._N +
                       r"\s*(?:months?|اشهر|شهور|شهر)|(?:over|in|علي)\s+" + parse._N + r"\s*(?:months?|اشهر|شهور)")
INSTALL = re.compile(r"\binstal{1,2}ments?\b|\bmonthly payments?\b|\bpay (?:it )?(?:monthly|over)\b|اقساط|قسط|تقسيط")
PER_MONTH = re.compile(AMT + r"\s*(?:sdg\s*)?(?:a|per|each|every)\s+month|" + AMT + r"\s*(?:ج\s*)?(?:شهريا|في الشهر|كل شهر)")
CHEAPER = re.compile(r"\bcheaper\b|\bless expensive\b|\bbudget (?:one|model|version)\b|\bused one\b|ارخص|اقل سعر|مستعمل")
PRICE_IS = re.compile(r"\b(?:costs?|price is|priced at|it'?s|for)\s+" + AMT + r"|(?:سعره|سعرها|بسعر|بـ|ب)\s*" + AMT)
STATED_INCOME = re.compile(r"\b(?:my (?:monthly )?(?:income|salary|pay) is|i earn|i make|i get paid)\s+(?:about\s+)?" + AMT +
                           r"|(?:دخلي|مرتبي|راتبي|ماهيتي)\s*(?:هو\s*|الشهري\s*)?" + AMT)
HALF = re.compile(r"\bhalf (?:of )?(?:that|it)\b|نص(?:ف)? (?:ده|دا|ذلك|المبلغ|هذا)")
COMPARE_PREV = re.compile(r"\bcompare\b.*\b(?:previous|last|other|before|earlier)\b|\bprevious option\b|"
                          r"قارن.*(?:السابق|الاول|القبل|اللي قبل)")
# A confirmation is the whole reply ("yes", "yes please", "نعم، احفظ"): "sure, but make it
# 100" or "ok, how much is in it now?" is not a yes.
_POLITE_TAIL = r"(?:[\s,،.!]*(?:please|thanks|thank you|save it|do it|go ahead|cancel|من فضلك|لو سمحت|شكرا|احفظ|نفذ|الغ|الغي))*"
YES = re.compile(r"^(?:yes|y|yeah|yep|sure|ok|okay|confirm|confirmed|do it|go ahead|please do|correct|"
                 r"نعم|ايوه|ايوا|ايه|اكيد|تمام|موافق|اوكي|اوك|يلا|نفذ|اكد)" + _POLITE_TAIL + r"[\s.!?؟]*$")
NO = re.compile(r"^(?:no|nope|cancel|stop|don'?t|never ?mind|لا|لأ|الغاء|الغي|ما تعمل|ما تنفذ|خلاص لا|بلاش)"
                + _POLITE_TAIL + r"[\s.!?؟]*$")

# ---------- real changes ----------
_POLITE = r"^(?:please\s+|can you\s+|could you\s+|لو سمحت\s+|ممكن\s+|من فضلك\s+)?"
CONTRIBUTE = re.compile(_POLITE + r"(?:add|put|deposit|transfer|move|allocate|contribute|save)\s+" + AMT +
                        r"\s*(?:sdg|pounds?)?\s+(?:to|into|in|toward|towards|for)\s+(.+)$|" +
                        _POLITE + r"(?:اضف|ضيف|اضيف|حط|حول|ودع|خصص|دخل|زود)\s*(?:لي\s+)?" + AMT +
                        r"\s*(?:ج|جنيه|ج\.س)?\s*(?:الي\s*|علي\s*|في\s*|ل)(.+)$")
SET_DATE = re.compile(_POLITE + r"(?:change|move|set|update|push|extend|postpone|shift)\b.*\b(?:date|deadline)\b|" +
                      _POLITE + r"(?:غير|عدل|خلي|اجل|مدد|حدث|حول)\s.*(?:موعد|تاريخ|لشهر|الي شهر)")
SET_TARGET = re.compile(_POLITE + r"(?:change|set|update|raise|lower|increase|decrease|make)\b.*\b(?:target|goal amount)\b"
                        r".*\bto\s+" + AMT + "|" + _POLITE + r"(?:غير|عدل|خلي|زود|قلل)\s.*(?:المبلغ المستهدف|مبلغ "
                        r"(?:ال)?هدف|المستهدف|الهدف)\s.*?(?:الي|ل|يكون|يبقي)\s*" + AMT)
RECORD = re.compile(_POLITE + r"(?:record|log|add|enter|save)\s+(?:a |an |the |this |that |it)?\s*(?:new )?"
                    r"(?:expense|transaction|spending|purchase|payment)?\b|\b(?:record|log) (?:it|this|that)\b|" +
                    _POLITE + r"(?:سجل|سجلي|دخل|ضيف|اضف)\s+(?:لي\s+)?(?:مصروف|مصاريف|صرف|معامله|عمليه|دفع|"
                              r"مشتريات)|(?<!" + _A + ")سجل(?:ها|ه)(?!" + _A + ")")
EXPENSE_NOUN = re.compile(r"\b(?:expense|transaction|spending|spent|paid|purchase|payment|bought)\b|"
                          r"مصروف|صرف|دفعت|معامله|اشتريت")
PRIORITY_SET = re.compile(_POLITE + r"(?:make|set|put|mark)\s+(.+?)\s+(?:as\s+)?(?:my\s+|the\s+)?(?:top|first|main|"
                          r"highest|number one)\s+priority|" + _POLITE + r"(?:خلي|اجعل|حط)\s+(.+?)\s+(?:هو\s+|هي\s+)?"
                          r"(?:اول|اعلي|ال)?(?:اولويه|أولوية)")
SAME_GOAL = re.compile(r"\b(?:same goal|that goal|this goal|it|its)\b|نفس الهدف|الهدف نفسه|الهدف ده|هذا الهدف|"
                       r"موعده|موعدها")
GOAL_WORD = re.compile(r"\bgoals?\b|\bfund\b|هدف|صندوق")

SOURCE = {
    "unallocated": ("money left after your goals' monthly amounts", "المال المتبقي بعد مبالغ أهدافك الشهرية"),
    "free_savings": ("savings outside your goals (above the safety floor)", "مدخرات خارج أهدافك (فوق حد الأمان)"),
    "goal_contributions": ("this month's goal contributions", "مساهمات أهدافك لهذا الشهر"),
    "flexible_cuts": ("cuts to flexible spending", "تخفيض الإنفاق المرن"),
    "safety": ("emergency savings", "مدخرات الطوارئ"),
}
VERDICT = {
    "affordable": ("Affordable", "في المتناول"),
    "affordable_with_adjustments": ("Affordable with adjustments", "ممكن مع تعديلات"),
    "risky": ("Risky", "مخاطرة"),
    "not_affordable": ("Not currently affordable", "غير ممكن حاليًا"),
    "insufficient_information": ("Insufficient information", "معلومات غير كافية"),
}
# A planned expense is paid when it falls due, and a loan is repaid: their own words.
PLANNED_OPTION = {"buy_now": ("Pay it all from this month's money", "الدفع كاملًا من مال هذا الشهر"),
                  "delay": ("Save toward it each month", "الادخار له شهريًا")}
LOAN_OPTION = ("Loan repayments", "سداد القرض")
OPTION = {"buy_now": ("Buy now", "الشراء الآن"), "delay": ("Wait and save", "الانتظار والادخار"),
          "cheaper": ("A cheaper option", "خيار أرخص"), "installments": ("Installments", "التقسيط"),
          "both": ("Save for both gradually", "الادخار للاثنين تدريجيًا")}


def V(kind, lang):
    en, ar = VERDICT[kind]
    return L(lang, en, ar)


def _out(text, tool, data, steps, context, goal=None):
    return {"answer": text, "tool": tool, "data": engine.as_json(data), "calculation": steps, "context": context,
            "goal": goal}


def _section(title, lines):
    lines = [x for x in lines if x]
    return f"**{title}**\n" + "\n".join(lines) if lines else ""


def _join(*sections):
    return "\n\n".join(s for s in sections if s)


def _n(token):
    """'3' or 'three' / 'ثلاثه' as an int."""
    token = (token or "").strip()
    return int(token) if token.isdigit() else parse.NUMBER_WORDS.get(token)


def _first_int(regex, text):
    m = regex.search(text)
    if not m:
        return None
    return next((_n(g) for g in m.groups() if g), None)


# ---------- reading a purchase question ----------
ITEM_STOP = (r"a|an|sdg|by|for|now|at|or|with|in|on|and|but|if|costing|worth|that|which|today|this|next|going|to|will|would|"
             r"is|be|affect\w*|without|before|after|right|then|so|because|cost\w*|price\w*")
ITEM_EN = re.compile(r"\b(?:buy(?:ing)?|purchas\w*|afford|get(?:ting)?)\s+(?:to buy\s+)?"
                     r"(?:(?:a|an|the|this|that|my|new|another|some|me)\s+|\d[\d,.]*\s*(?:sdg\s+)?)*"
                     rf"((?!(?:{ITEM_STOP})\b)[a-z][a-z-]*(?:\s+(?!(?:{ITEM_STOP})\b)[a-z][a-z-]*)?)")
ITEM_AR = re.compile(rf"(?<!{_A})(?:اشتري|نشتري|بشتري|حاشتري|ساشتري|هشتري|شراء|اشتراء|اجيب|اقتني)\s+(?:لي\s+)?"
                     rf"((?!(?:الان|هسه|هسي|دلوقتي|بعدين|ولا|او|ب)(?!{_A})){_A}+(?:\s+(?:جديد|جديده|مستعمل|"
                     rf"مستعمله))?)")
# A known expense coming up ("prepare for Ramadan expenses", "استعد لمصاريف العيد"):
# planned like a purchase, saved for by the date it falls due.
EVENTS = r"ramadan|eid|wedding|school|holidays?|trip|travel|vacation|christmas|new year"
PLANNED = re.compile(
    rf"\b(?:prepare|plan|budget|save|get ready)\s+for\s+(?:the\s+|my\s+|a\s+|an\s+|upcoming\s+|next\s+)*"
    rf"((?:{EVENTS})(?:\s+(?:expenses|costs|spending|shopping))?|[a-z]+\s+(?:expenses|costs))\b|"
    rf"(?:استعد|اجهز|اتجهز|اخطط|اوفر|احوش|اجهز نفسي)\s+(?:ل|لل|عشان\s+|علشان\s+|الي\s+)?"
    rf"((?:مصاريف|مصروفات)\s+(?:ال)?{_A}+|رمضان|العيد|عيد\s+{_A}+|الزواج|العرس|المدارس|السفر|الرحله)")
LOAN = re.compile(rf"\b(?:loan|financing|credit|borrow\w*)\b|(?<!{_A})(?:قرض|سلفيه|سلفه|تمويل|استلف|اتسلف)(?!{_A})")
ITEM_LOAN = re.compile(r"\b(?:loan|financing|credit) (?:for|to buy) (?:a |an |the |my |new )*([a-z]+)|"
                       rf"(?:قرض|سلفيه|سلفه|تمويل)\s+(?:عشان|ل|لشراء|علشان)\s*(?:اشتري\s+)?(?:ال)?({_A}+)")
GENERIC_ITEM = {"it", "item", "something", "thing", "stuff", "one", "this", "that"}


def _item(text):
    for regex in (ITEM_EN, ITEM_AR, ITEM_LOAN):
        m = regex.search(text)
        found = m and next((g for g in m.groups() if g), None)
        if found and found not in GENERIC_ITEM:
            return found.strip()
    return None


def _numbers(text):
    """(price, months to wait, installments, monthly payment, every price candidate)."""
    months = _first_int(MONTHS_N, text)
    count = _first_int(INSTALL_N, text) if INSTALL.search(text) else None
    per = PER_MONTH.search(text)
    monthly = dec((per.group(1) or per.group(2)).replace(",", "")) if per else None
    skip = set()
    for regex in (MONTHS_N, INSTALL_N, PER_MONTH, STATED_INCOME):
        for m in regex.finditer(text):
            skip.update(range(m.start(), m.end()))
    candidates = []
    for value, is_pct, pos in parse.amounts(text):
        if is_pct or pos in skip or value < 10:
            continue
        if dec(value) not in candidates:
            candidates.append(dec(value))
    price = max(candidates) if candidates else None
    return price, months, count, monthly, candidates


def _urgency(text):
    if OPTIONAL.search(text):
        return False
    if URGENT.search(text):
        return True
    return None


def _competing_goal(goals, question, item):
    """A saved goal the question weighs the purchase against ("... but I also want
    to complete my emergency fund"), or the goal the item itself is saved for."""
    found = match_goals(goals, question)
    if len(found) != 1:
        return None, None
    g = found[0]
    if item and match_goals([g], item):
        return None, g                       # "buy this laptop": the laptop goal is the item's own
    return g, None


# ---------- the shared "your numbers" block ----------

def _numbers_block(pos, lang, extra=()):
    label = month_label(pos["month"], lang)
    lines = []
    if pos["income"] is not None:
        lines.append(L(lang, f"- Income {num(pos['income'])} − spending {num(pos['expenses'])} = "
                             f"{amt(pos['surplus'], lang)} left in {label}",
                       f"- الدخل {num(pos['income'])} − المصروفات {num(pos['expenses'])} = "
                       f"{amt(pos['surplus'], lang)} متبقية في {label}"))
    dated = [g for g in pos["goals"] if g.get("required_monthly_saving")]
    if dated:
        parts = " + ".join(f"{g['name']} {num(g['required_monthly_saving'])}" for g in dated)
        need = pos["goal_need"]
        tail = ""
        if pos["surplus"] is not None:
            diff = pos["surplus"] - need
            tail = (L(lang, f": {num(need)} − {num(pos['surplus'])} = {amt(-diff, lang)} more than is left",
                      f": {num(need)} − {num(pos['surplus'])} = {amt(-diff, lang)} أكثر من المتبقي")
                    if diff < 0 else L(lang, f": {num(pos['surplus'])} − {num(need)} = {amt(diff, lang)} stays unallocated",
                                       f": {num(pos['surplus'])} − {num(need)} = {amt(diff, lang)} تبقى غير مخصصة"))
        lines.append(L(lang, f"- Your goals need {amt(need, lang)} a month to stay on time ({parts}){tail}",
                       f"- أهدافك تحتاج {amt(need, lang)} شهريًا لتبقى في موعدها ({parts}){tail}"))
    for g in pos.get("overdue", []):
        lines.append(L(lang, f'- "{g["name"]}" is past its date ({g["target_date"]}) with {amt(g["remaining_amount"], lang)} '
                             "still to go, so it is not counted in the monthly needs: set a new date to plan for it",
                       f'- "{g["name"]}" تجاوز موعده ({g["target_date"]}) ويتبقى له {amt(g["remaining_amount"], lang)}، '
                       "لذلك لم يُحسب في الاحتياجات الشهرية: حدد موعدًا جديدًا للتخطيط له"))
    lines.append(L(lang, f"- Savings outside goals: {amt(pos['savings_balance'], lang)}; saved inside goals: "
                         f"{amt(pos['in_goals'], lang)} (earmarked, not free to spend)",
                   f"- مدخرات خارج الأهداف: {amt(pos['savings_balance'], lang)}؛ مدخرات داخل الأهداف: "
                   f"{amt(pos['in_goals'], lang)} (مخصصة وليست متاحة للصرف)"))
    if pos["essential"] > 0:
        cover = pos["emergency_months"]
        lines.append(L(lang, f"- Essential spending {amt(pos['essential'], lang)} a month; emergency savings "
                             f"{amt(pos['savings_balance'] + pos['emergency_saved'], lang)} cover about {cover} month(s)",
                       f"- الإنفاق الأساسي {amt(pos['essential'], lang)} شهريًا؛ مدخرات الطوارئ "
                       f"{amt(pos['savings_balance'] + pos['emergency_saved'], lang)} تغطي حوالي {cover} شهر"))
    lines.extend(extra)
    return _section(L(lang, f"Your verified numbers ({label})", f"أرقامك الموثقة ({label})"), lines)


def _missing_income(lang, tool, context):
    text = L(lang, "I can't judge this yet: no income is recorded, and I won't guess it. How much is your monthly "
                   "income? (You can also add it on the Money page.)",
             "لا أستطيع الحكم بعد: لا يوجد دخل مسجل، ولن أخمّنه. كم دخلك الشهري؟ (يمكنك أيضًا إضافته من صفحة المال.)")
    return _out(text, tool, {"verdict": "insufficient_information", "missing": ["income"]}, [],
                {**context, "awaiting": "income"})


def _funding_line(parts, lang):
    pieces = [f"{num(p['amount'])} {L(lang, 'from', 'من')} {L(lang, *SOURCE[p['source']])}" for p in parts]
    return " + ".join(pieces)


def _impact_lines(impact, lang, focus=None):
    out = []
    for g in impact:
        if g["behind_by"] <= 0:
            continue
        mark = " ←" if focus and g["goal"] == focus else ""
        out.append(L(lang, f'   - "{g["goal"]}" falls {amt(g["behind_by"], lang)} behind (about '
                           f'{g["months_of_contributions"]} month(s) of its {num(g["monthly_need"])} a month){mark}',
                     f'   - "{g["goal"]}" يتأخر بمقدار {amt(g["behind_by"], lang)} (حوالي '
                     f'{g["months_of_contributions"]} شهر من مساهمته {num(g["monthly_need"])} شهريًا){mark}'))
    return out


# ---------- purchases ----------

def _purchase_options(r, pos, lang, item, focus=None, both=None, d=None):
    d = d or {}
    lines, table = [], []
    for i, o in enumerate(r["options"], start=1):
        kind = o["kind"]
        label = (PLANNED_OPTION.get(kind) if d.get("planned") else None) or \
            (LOAN_OPTION if kind == "installments" and d.get("loan") else None) or OPTION[kind]
        head = L(lang, *label)
        if kind == "buy_now":
            fund = _funding_line(o["parts"], lang)
            text = f"{i}. {head} ({amt(o['cost'], lang)}) — {V(o['verdict'], lang)}"
            if o["parts"]:
                text += f": {fund} = {num(o['cost'] - o['shortfall'])}"
            if o["shortfall"] > 0:
                text += L(lang, f"; still {amt(o['shortfall'], lang)} short", f"؛ ويبقى نقص {amt(o['shortfall'], lang)}")
            lines.append(text)
            lines.extend(_impact_lines(o["goal_impact"], lang, focus))
            if o["cuts"]:
                cut_text = L(lang, ", ", "، ").join(f"{cat_label(c['category'], c['subcategory'], lang)} −{num(c['cut'])}"
                                                   for c in o["cuts"])
                lines.append(L(lang, f"   - Flexible cuts: {cut_text} (essential spending untouched)",
                               f"   - تخفيضات مرنة: {cut_text} (الإنفاق الأساسي لا يُمس)"))
            effect = fund or "—"
        elif kind == "delay":
            if not o.get("months"):
                text = L(lang, f"{i}. {head} — nothing is left each month to save toward it",
                         f"{i}. {head} — لا يتبقى شيء شهريًا للادخار له")
                lines.append(text)
                table.append({"option": head, "verdict": V(o["verdict"], lang), "detail": "—"})
                continue
            text = L(lang, f"{i}. {head}: {o['months']} month(s), putting {amt(o['per_month'], lang)} a month aside "
                           f"({'ready' if d.get('planned') else 'buy'} around {after_months(o['months'])}) — "
                           f"{V(o['verdict'], lang)}",
                     f"{i}. {head}: {o['months']} شهر، بادخار {amt(o['per_month'], lang)} شهريًا "
                     f"({'جاهز' if d.get('planned') else 'الشراء'} حوالي {after_months(o['months'])}) — "
                     f"{V(o['verdict'], lang)}")
            lines.append(text)
            lines.extend(_monthly_effect(o, lang))
            effect = L(lang, f"{num(o['per_month'])} × {o['months']}", f"{num(o['per_month'])} × {o['months']}")
        elif kind == "cheaper":
            if o.get("cost") is not None:
                text = (f"{i}. {head} ({amt(o['cost'], lang)}, " + L(lang, f"{num(o['saving_vs_price'])} less", f"أقل بـ {num(o['saving_vs_price'])}")
                        + f") — {V(o['verdict'], lang)}")
                if o["parts"]:
                    text += ": " + _funding_line(o["parts"], lang)
                effect = amt(o["cost"], lang)
            else:
                text = L(lang, f"{i}. {head}: a version costing at most {amt(o['max_price'], lang)} is covered without "
                               "touching your goals or emergency savings",
                         f"{i}. {head}: نسخة لا يزيد سعرها عن {amt(o['max_price'], lang)} مغطاة دون المساس بأهدافك أو "
                         "مدخرات الطوارئ")
                effect = "≤ " + amt(o["max_price"], lang)
            lines.append(text)
        else:
            text = L(lang, f"{i}. {head}: {o['months']} × {amt(o['per_month'], lang)} = {amt(o['total_paid'], lang)}",
                     f"{i}. {head}: {o['months']} × {amt(o['per_month'], lang)} = {amt(o['total_paid'], lang)}")
            if o["extra_cost"] > 0:
                text += L(lang, f" ({amt(o['extra_cost'], lang)} more than paying at once)",
                          f" (أكثر بـ {amt(o['extra_cost'], lang)} من الدفع مرة واحدة)")
            text += f" — {V(o['verdict'], lang)}"
            lines.append(text)
            lines.extend(_monthly_effect(o, lang))
            lines.append(L(lang, f"   - A fixed payment for {o['months']} months: if income drops, it still has to be paid",
                           f"   - دفعة ثابتة لمدة {o['months']} شهر: إذا قل الدخل تبقى واجبة السداد"))
            effect = f"{o['months']} × {num(o['per_month'])}"
        table.append({"option": head, "verdict": V(o["verdict"], lang), "detail": effect})
    if both:
        head = L(lang, *OPTION["both"])
        lines.append(L(lang, f"{len(r['options']) + 1}. {head}: {amt(both['item_per_month'], lang)} a month toward the "
                             f"{item} ({both['item_months']} month(s)) and {amt(both['goal_per_month'], lang)} a month to "
                             f'"{both["goal"]}" (needs {num(both["goal_need"])})',
                       f"{len(r['options']) + 1}. {head}: {amt(both['item_per_month'], lang)} شهريًا لـ{item} "
                       f"({both['item_months']} شهر) و{amt(both['goal_per_month'], lang)} شهريًا لهدف "
                       f'"{both["goal"]}" (يحتاج {num(both["goal_need"])})'))
        if both["others_paused"]:
            lines.append(L(lang, f"   - Your other goals ({', '.join(both['others_paused'])}) get nothing during those months",
                           f"   - أهدافك الأخرى ({'، '.join(both['others_paused'])}) لا تحصل على شيء خلال هذه الأشهر"))
        table.append({"option": head, "verdict": "—", "detail": f"{num(both['item_per_month'])} + {num(both['goal_per_month'])}"})
    return lines, table


def _monthly_effect(o, lang):
    out = []
    if o["from_goal_contributions"] > 0:
        out.append(L(lang, f"   - Your goals get {amt(o['from_goal_contributions'], lang)} a month less for {o['months']} "
                           f"month(s) ({amt(o['goals_behind_total'], lang)} in total)",
                     f"   - أهدافك تحصل على أقل بـ {amt(o['from_goal_contributions'], lang)} شهريًا لمدة {o['months']} "
                     f"شهر ({amt(o['goals_behind_total'], lang)} إجمالًا)"))
    if o["from_flexible_cuts"] > 0:
        out.append(L(lang, f"   - And {amt(o['from_flexible_cuts'], lang)} a month must come from cutting flexible spending",
                     f"   - و{amt(o['from_flexible_cuts'], lang)} شهريًا يجب أن تأتي من تخفيض الإنفاق المرن"))
    if o["monthly_shortfall"] > 0:
        out.append(L(lang, f"   - Still {amt(o['monthly_shortfall'], lang)} a month short",
                     f"   - ويبقى نقص {amt(o['monthly_shortfall'], lang)} شهريًا"))
    return out


def _both(pos, price, goal):
    """Saving for the purchase and one goal at the same time."""
    surplus = pos["surplus"] or ZERO
    if surplus <= 0:
        return None
    need = dec(goal.get("required_monthly_saving") or 0) or ZERO
    others = [g["name"] for g in pos["goals"] if g["name"] != goal["name"] and g.get("required_monthly_saving")]
    goal_part = min(need, surplus / 2) if need else (surplus / 2)
    goal_part = goal_part.quantize(Decimal("0.01"))
    item_part = surplus - goal_part
    return {"goal": goal["name"], "goal_need": need, "goal_per_month": goal_part, "item_per_month": item_part,
            "item_months": engine.months_needed(price, item_part), "others_paused": others,
            "goal_on_time": need == 0 or goal_part >= need}


REASON = {
    "covered_without_touching_goals": (
        "it is covered by money no goal is counting on, so nothing else changes.",
        "لأنه مغطى بمال لا يعتمد عليه أي هدف، فلا يتغير شيء آخر."),
    "urgent_least_harm": (
        "you said it is urgent, so buying now makes sense; pay for it in the order shown, which never touches "
        "essential spending.",
        "ذكرت أنه ضروري، لذلك الشراء الآن منطقي؛ ادفع بالترتيب الموضح، وهو لا يمس الإنفاق الأساسي."),
    "urgent_spread_cost": (
        "it is urgent but paying all at once is not covered; installments spread the cost.",
        "إنه ضروري لكن الدفع مرة واحدة غير مغطى؛ التقسيط يوزع التكلفة."),
    "urgent_not_covered": (
        "it is urgent, but even with adjustments the full price is not covered now; look at the cheapest version "
        "that does the job.",
        "إنه ضروري، لكن السعر الكامل غير مغطى الآن حتى مع التعديلات؛ ابحث عن أرخص نسخة تؤدي الغرض."),
    "cheaper_fits": ("the cheaper option is covered without touching your goals.",
                     "الخيار الأرخص مغطى دون المساس بأهدافك."),
    "waiting_avoids_harm": ("waiting lets you pay for it without touching your goals or emergency savings.",
                            "الانتظار يتيح لك الدفع دون المساس بأهدافك أو مدخرات الطوارئ."),
    "goal_first": ("you said your goal comes first, and waiting keeps it on track.",
                   "ذكرت أن هدفك أولًا، والانتظار يبقيه على المسار."),
    "buying_now_too_costly": ("buying it now would take money your goals or emergency savings depend on.",
                              "الشراء الآن سيأخذ مالًا تعتمد عليه أهدافك أو مدخرات الطوارئ."),
    "waiting_avoids_cuts": ("buying now also needs cuts to your flexible spending; waiting only slows your goals.",
                            "الشراء الآن يحتاج أيضًا إلى تخفيض إنفاقك المرن؛ أما الانتظار فيبطئ أهدافك فقط."),
    "goals_pay_either_way": (
        "your goals pay for it either way (the same total), so waiting does not make it cheaper. If you don't need it "
        "now, waiting keeps this month's goal money on track; if you do, buying now costs the same.",
        "أهدافك تدفع ثمنه في الحالتين (بنفس المجموع)، فالانتظار لا يجعله أرخص. إن لم تكن تحتاجه الآن فالانتظار يبقي "
        "مال أهدافك لهذا الشهر على المسار؛ وإن كنت تحتاجه فالشراء الآن بنفس التكلفة."),
    "no_extra_cost_fits": ("the installments cost nothing extra and fit in the money left each month.",
                           "الأقساط بلا تكلفة إضافية وتناسب المتبقي الشهري."),
}


def purchase(user_id, cur, lang, d, previous=None):
    """d: {"item", "price", "delay", "installments", "monthly", "cheaper", "urgent", "goal", "own_goal",
    "goal_first", "safe_price"}."""
    item = d.get("item") or L(lang, "item", "المشتريات")
    context = {"intent": "purchase", "decision": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()},
               "months": [cur]}
    pos = engine.position(user_id, cur)
    if pos["income"] is None:
        return _missing_income(lang, "assess_purchase", context)
    if "expenses" in pos["missing"]:
        text = L(lang, "I can't judge this yet: no spending is recorded, so I can't tell how much of your income is "
                       "really free. Add your expenses on the Money page (or import a CSV) and ask again.",
                 "لا أستطيع الحكم بعد: لا توجد مصروفات مسجلة، لذلك لا أعرف كم من دخلك متاح فعلًا. أضف مصروفاتك من "
                 "صفحة المال (أو استورد ملف CSV) ثم اسأل مرة أخرى.")
        return _out(text, "assess_purchase", {"verdict": "insufficient_information", "missing": ["expenses"]}, [],
                    context)
    if d.get("safe_price") and d.get("price") is None:
        return _safe_price(pos, lang, item, context)
    if d.get("price_conflict"):
        options = L(lang, " or ", " أم ").join(amt(dec(c), lang) for c in d["price_conflict"])
        text = L(lang, f"You mentioned more than one price: is the {item} {options}? I'll use the one you confirm.",
                 f"ذكرت أكثر من سعر: هل سعر {item} {options}؟ سأستخدم السعر الذي تؤكده.")
        context["decision"].pop("price", None)
        context["decision"].pop("price_conflict", None)
        return _out(text, "clarify", {"missing": ["price"], "conflict": d["price_conflict"]}, [],
                    {**context, "awaiting": "purchase_price"})
    if d.get("price") is None:
        own = d.get("own_goal")
        hint = ""
        if own:
            f = next((g for g in pos["goals"] if g["name"] == own), None)
            if f:
                hint = L(lang, f' (Your goal "{own}" has a target of {amt(f["target_amount"], lang)} with '
                               f'{amt(f["saved_so_far"], lang)} saved; if that is the price, reply with that amount.)',
                         f' (هدفك "{own}" مبلغه المستهدف {amt(f["target_amount"], lang)} وادخرت منه '
                         f'{amt(f["saved_so_far"], lang)}؛ إذا كان هذا هو السعر فأرسل المبلغ.)')
        if d.get("planned"):
            text = L(lang, f"How much do you expect the {item} to cost, and in how many months is it? I need both to "
                           "plan it against your money, and I won't guess them.",
                     f"كم تتوقع أن تكلف {item}، وبعد كم شهر؟ أحتاج الاثنين لأخطط لها مقابل أموالك، ولن أخمّنهما.")
            return _out(text, "clarify", {"missing": ["price", "months"]}, [], {**context, "awaiting": "purchase_price"})
        loan = L(lang, " If it is a loan, also tell me the monthly repayment and for how many months.",
                 " وإذا كان بقرض فأخبرني أيضًا بالقسط الشهري ولكم شهر.") if d.get("loan") else ""
        text = L(lang, f"How much does the {item} cost? I need the price to check it against your money, and I "
                       f"won't guess it.{hint}{loan}",
                 f"كم سعر {item}؟ أحتاج السعر لأقارنه بأموالك، ولن أخمّنه.{hint}{loan}")
        return _out(text, "clarify", {"missing": ["price"]}, [], {**context, "awaiting": "purchase_price"})
    if d.get("horizon") and not d.get("details") and not (d.get("installments") or d.get("monthly")
                                                          or d.get("cheaper") or d.get("planned")):
        # "a PlayStation for 15,000 in four months": the user's own plan, answered
        # directly; the full comparison of options is given when asked for.
        from app.services.plan_followups import plan_answer
        return plan_answer(user_id, cur, lang, d, ask="verdict", previous=previous)
    d.pop("details", None)
    if d.get("horizon") and not d.get("delay"):
        d["delay"] = d["horizon"]
    price = Decimal(str(d["price"]))
    own_goal = next((g for g in pos["goals"] if g["name"] == d.get("own_goal")), None)
    covered_by_own = ZERO
    if own_goal:
        # Money already saved for this very item pays for part of it first, and
        # that goal's monthly need ends once it is bought.
        pos, saved = engine.without_goal(pos, own_goal["name"])
        covered_by_own = min(price, saved)
    rest = price - covered_by_own
    r = engine.assess_purchase(pos, rest, delay_months=d.get("delay"), installments=d.get("installments"),
                               monthly_payment=d.get("monthly"), cheaper_price=d.get("cheaper"),
                               urgent=d.get("urgent"), goal_first=d.get("goal_first"))
    focus = d.get("goal")
    goal_row = next((g for g in pos["goals"] if g["name"] == focus), None)
    both = _both(pos, rest, goal_row) if goal_row else None
    options, table = _purchase_options(r, pos, lang, item, focus, both, d)
    rec = r["recommended"]
    rec_option = next((o for o in r["options"] if o["kind"] == rec), None)
    verdict = r["verdict"]
    why_text = L(lang, *REASON[r["reason"]])
    if both and both["item_months"] and both["goal_on_time"] and not d.get("urgent") and rec != "buy_now" \
            and not (rec == "cheaper" and r["reason"] == "cheaper_fits"):
        # A trade-off with a named goal: saving for both keeps that goal on time.
        rec = "both"
        why_text = L(lang, f'it keeps "{both["goal"]}" on its {num(both["goal_per_month"])} a month and still gets you '
                           f"the {item} in about {both['item_months']} month(s)"
                           + (f"; the cost is that {', '.join(both['others_paused'])} gets nothing meanwhile."
                              if both["others_paused"] else "."),
                     f'يبقي "{both["goal"]}" على {num(both["goal_per_month"])} شهريًا ويوصلك لـ{item} خلال '
                     f"{both['item_months']} شهر تقريبًا"
                     + (f"؛ والثمن أن {'، '.join(both['others_paused'])} لا يحصل على شيء خلال ذلك."
                        if both["others_paused"] else "."))
    elif r["reason"] == "buying_now_too_costly" and rec_option and rec_option.get("months"):
        now_uses = [SOURCE[p["source"]] for p in r["options"][0]["parts"]]
        slows = rec_option["from_goal_contributions"] > 0
        why_text = L(lang, "buying now needs " + ", ".join(x[0] for x in now_uses) + "; waiting "
                     + (f"only slows your goals ({num(rec_option['goals_behind_total'])} in total)." if slows
                        else "touches none of that."),
                     "الشراء الآن يحتاج إلى " + "، ".join(x[1] for x in now_uses) + "؛ أما الانتظار "
                     + (f"فيبطئ أهدافك فقط ({num(rec_option['goals_behind_total'])} إجمالًا)." if slows
                        else "فلا يمس أيًا من ذلك."))
    delay_opt = next((o for o in r["options"] if o["kind"] == "delay" and o.get("months")), None)
    if d.get("planned") and delay_opt:
        rec, rec_option = "delay", delay_opt
        verdict = delay_opt["verdict"]
        free = engine._pos(pos["free_monthly"] or ZERO)
        if verdict == "affordable":
            why_text = L(lang, f"it fits in the money no goal is counting on ({amt(free, lang)} a month).",
                         f"يناسب المال الذي لا يعتمد عليه أي هدف ({amt(free, lang)} شهريًا).")
        elif verdict == "not_affordable":
            why_text = L(lang, f"even with goal money and flexible cuts it is {amt(delay_opt['monthly_shortfall'], lang)} "
                               "a month short: plan a smaller amount or start earlier.",
                         f"حتى مع مال الأهداف والتخفيضات المرنة ينقص {amt(delay_opt['monthly_shortfall'], lang)} "
                         "شهريًا: خطط لمبلغ أصغر أو ابدأ أبكر.")
        else:
            why_text = L(lang, f"it fits only if your goals get {amt(delay_opt['from_goal_contributions'], lang)} a month "
                               "less" + (f" and flexible spending falls by {amt(delay_opt['from_flexible_cuts'], lang)} a "
                                         "month" if delay_opt["from_flexible_cuts"] else "") + "; the options below "
                               "show the trade-off.",
                         f"يناسب فقط إذا حصلت أهدافك على أقل بـ {amt(delay_opt['from_goal_contributions'], lang)} "
                         "شهريًا" + (f" وانخفض الإنفاق المرن بـ {amt(delay_opt['from_flexible_cuts'], lang)} شهريًا"
                                     if delay_opt["from_flexible_cuts"] else "") + "؛ الخيارات أدناه توضح المقايضة.")
        head = L(lang, f"Recommendation: set aside {amt(delay_opt['per_month'], lang)} a month for the {item} "
                       f"({delay_opt['months']} month(s))",
                 f"التوصية: خصص {amt(delay_opt['per_month'], lang)} شهريًا لـ{item} ({delay_opt['months']} شهر)")
    elif rec == "delay" and rec_option and rec_option.get("months"):
        head = L(lang, f"Recommendation: wait about {rec_option['months']} month(s) before buying the {item}",
                 f"التوصية: انتظر حوالي {rec_option['months']} شهر قبل شراء {item}")
    elif rec == "delay":
        head = L(lang, f"Recommendation: don't buy the {item} yet", f"التوصية: لا تشترِ {item} الآن")
    elif rec == "cheaper":
        head = L(lang, "Recommendation: choose the cheaper option", "التوصية: اختر الخيار الأرخص")
    elif rec == "installments":
        head = L(lang, "Recommendation: pay in installments", "التوصية: ادفع بالتقسيط")
    elif rec == "both":
        head = L(lang, f'Recommendation: save for both — the {item} in about {both["item_months"]} month(s) while '
                       f'"{both["goal"]}" stays on track',
                 f'التوصية: ادخر للاثنين — {item} خلال {both["item_months"]} شهر تقريبًا مع بقاء "{both["goal"]}" '
                 "على المسار")
    else:
        head = L(lang, f"Recommendation: you can buy the {item} now", f"التوصية: يمكنك شراء {item} الآن")
    why = L(lang, "Why: ", "السبب: ") + why_text
    lead = (f"**{head}** ({L(lang, 'this plan', 'هذه الخطة')}: {V(verdict, lang)})\n{why}" if d.get("planned") else
            f"**{head}** ({L(lang, 'buying it now', 'الشراء الآن')}: {V(verdict, lang)})\n{why}")
    extra = []
    if covered_by_own:
        extra.append(L(lang, f'- {amt(covered_by_own, lang)} is already saved for it in "{own_goal["name"]}", so '
                             f"{num(price)} − {num(covered_by_own)} = {amt(rest, lang)} is still to pay",
                       f'- {amt(covered_by_own, lang)} مدخرة له بالفعل في "{own_goal["name"]}"، إذن '
                       f"{num(price)} − {num(covered_by_own)} = {amt(rest, lang)} متبقية للدفع"))
    risks = []
    if "goals_already_need_more_than_surplus" in r["risks"]:
        risks.append(L(lang, "- Your goals already need more each month than you have left, so any purchase now "
                             "delays a goal",
                       "- أهدافك تحتاج شهريًا أكثر مما يتبقى لك، لذلك أي شراء الآن يؤخر هدفًا"))
    if "uses_safety_money" in r["risks"]:
        risks.append(L(lang, "- Buying now uses emergency savings: an unexpected bill afterwards would have less cover",
                       "- الشراء الآن يستخدم مدخرات الطوارئ: أي فاتورة مفاجئة بعدها ستجد تغطية أقل"))
    if goal_row and both and not both["goal_on_time"]:
        risks.append(L(lang, f'- Saving for both means "{goal_row["name"]}" gets less than its '
                             f"{num(both['goal_need'])} a month and will be late",
                       f'- الادخار للاثنين يعني أن "{goal_row["name"]}" يحصل على أقل من {num(both["goal_need"])} '
                       "شهريًا وسيتأخر"))
    assumptions = [L(lang, f"- Future months look like {month_label(cur, lang)}; recurring bills are already in that "
                           "month's spending",
                     f"- الأشهر القادمة مثل {month_label(cur, lang)}؛ الفواتير المتكررة ضمن مصروفات ذلك الشهر"),
                   L(lang, "- This is an analysis only: nothing was bought or saved",
                     "- هذا تحليل فقط: لم يُشترَ أو يُحفظ أي شيء")]
    if d.get("stated_income") and dec(d["stated_income"]) != pos["income"]:
        assumptions.insert(0, L(lang, f"- You said your income is {amt(dec(d['stated_income']), lang)}, but your records "
                                      f"show {amt(pos['income'], lang)} for {month_label(cur, lang)}. I used the recorded "
                                      "figure; if your income changed, add it on the Money page and ask again",
                                f"- ذكرت أن دخلك {amt(dec(d['stated_income']), lang)}، لكن سجلاتك تُظهر "
                                f"{amt(pos['income'], lang)} في {month_label(cur, lang)}. استخدمت الرقم المسجل؛ إذا "
                                "تغير دخلك فأضفه في صفحة المال واسأل مرة أخرى"))
    if "income_from_latest_month" in r["assumptions"]:
        assumptions.insert(0, L(lang, "- Income is the latest month recorded", "- الدخل هو آخر شهر مسجل"))
    missing = []
    awaiting = None
    if d.get("urgent") is None and verdict not in ("affordable",) and not d.get("planned"):
        missing.append(L(lang, f"- Is the {item} urgent or essential (for example, needed for work)? That changes "
                               "the advice.",
                         f"- هل {item} ضروري أو مستعجل (مثلًا للعمل)؟ هذا يغير النصيحة."))
        awaiting = "purchase_urgency"
    if d.get("loan") and not (d.get("installments") and d.get("monthly")):
        missing.insert(0, L(lang, "- For the loan: the monthly repayment and the number of months (with them I can "
                                  "show the total cost, including interest, against your money)",
                            "- للقرض: القسط الشهري وعدد الأشهر (بهما أوضح التكلفة الكلية مع الفائدة مقارنة "
                            "بأموالك)"))
    if d.get("planned") and not d.get("delay"):
        missing.append(L(lang, f"- When the {item} falls due (in how many months): I assumed the earliest date your "
                               "money allows",
                         f"- متى تستحق {item} (بعد كم شهر): افترضت أقرب موعد تسمح به أموالك"))
    if d.get("cheaper") is None and verdict != "affordable" and not d.get("planned"):
        missing.append(L(lang, "- A cheaper model's price, if you have one in mind",
                         "- سعر موديل أرخص إن كان لديك واحد في بالك"))
    if goal_row:
        goal_pref = L(lang, f'- Which matters more to you now: the {item} or "{goal_row["name"]}"?',
                      f'- أيهما أهم لك الآن: {item} أم "{goal_row["name"]}"؟') if d.get("goal_first") is None \
            and d.get("urgent") is None else ""
        missing.append(goal_pref)
    nxt = {"both": L(lang, f'- Each month put {num(both["item_per_month"]) if both else ""} toward the {item} and '
                           f'{num(both["goal_per_month"]) if both else ""} into "{d.get("goal")}"',
                     f'- كل شهر ضع {num(both["item_per_month"]) if both else ""} لـ{item} و'
                     f'{num(both["goal_per_month"]) if both else ""} في "{d.get("goal")}"'),
           "delay": L(lang, "- Set the monthly amount aside as soon as income arrives, or add the item as a goal on the "
                            "Goals page so it is tracked",
                      "- خصص المبلغ الشهري فور وصول الدخل، أو أضف الشيء كهدف في صفحة الأهداف ليُتابع"),
           "buy_now": L(lang, "- If you buy it, record the expense so your figures stay right",
                        "- إذا اشتريته، سجّل المصروف لتبقى أرقامك صحيحة"),
           "cheaper": L(lang, "- Compare models up to that price before deciding",
                        "- قارن الموديلات حتى ذلك السعر قبل القرار"),
           "installments": L(lang, "- Check the seller's total price for installments before signing",
                             "- تأكد من السعر الإجمالي للتقسيط عند البائع قبل التوقيع")}[rec]
    if d.get("planned"):
        nxt = L(lang, f"- Add the {item} as a goal on the Goals page with its date, so the monthly amount is tracked",
                f"- أضف {item} كهدف في صفحة الأهداف مع موعدها، لتُتابع المبلغ الشهري")
    text = _join(lead, _numbers_block(pos, lang, extra),
                 _section(L(lang, "Options compared", "مقارنة الخيارات"), options),
                 _section(L(lang, "Risks", "المخاطر"), risks),
                 _section(L(lang, "Assumptions", "الافتراضات"), assumptions),
                 _section(L(lang, "Still missing", "معلومات ناقصة"), missing),
                 _section(L(lang, "Next step", "الخطوة التالية"), [nxt]))
    steps = [L(lang, f"Left each month = income − spending = {num(pos['income'])} − {num(pos['expenses'])} = "
                     f"{num(pos['surplus'])}",
               f"المتبقي شهريًا = الدخل − المصروفات = {num(pos['income'])} − {num(pos['expenses'])} = "
               f"{num(pos['surplus'])}"),
             L(lang, f"Unallocated = left − goals' monthly need = {num(pos['surplus'])} − {num(pos['goal_need'])} = "
                     f"{num(pos['free_monthly'])}",
               f"غير المخصص = المتبقي − حاجة الأهداف الشهرية = {num(pos['surplus'])} − {num(pos['goal_need'])} = "
               f"{num(pos['free_monthly'])}")]
    now = r["options"][0]
    if now["parts"]:
        steps.append(L(lang, "Buying now: ", "الشراء الآن: ") + " + ".join(num(p["amount"]) for p in now["parts"])
                     + f" = {num(rest - now['shortfall'])}")
    data = {"decision": "purchase", "item": item, "price": price, "to_pay": rest, "verdict": verdict,
            "verdict_label": V(verdict, lang), "recommended": rec, "reason": r["reason"] if rec == r["recommended"]
            else "both_keeps_goal", "options": r["options"],
            "max_safe_price": r.get("max_safe_price"), "both": both, "table": table, "hypothetical": True,
            "missing": [m for m in ("price",) if d.get("price") is None]}
    context["decision"]["price"] = str(price)
    if previous and previous.get("intent") == "purchase":
        context["before"] = previous.get("summary")
    context["summary"] = {"price": str(price), "verdict": verdict, "recommended": rec,
                          "delay": d.get("delay"), "installments": d.get("installments"), "cheaper":
                          str(d["cheaper"]) if d.get("cheaper") is not None else None,
                          "monthly": str(next((o["per_month"] for o in r["options"] if o["kind"] == "delay"
                                               and o.get("per_month")), "")) or None}
    if awaiting:
        context["awaiting"] = awaiting
    return _out(text, "assess_purchase", data, steps, context, d.get("goal"))


def _safe_price(pos, lang, item, context):
    safe = engine.max_safe_price(pos)
    b = engine._buckets(pos)
    with_goals = safe + b["goal_contributions"]
    with_cuts = with_goals + b["flexible_cuts"]
    lines = [L(lang, f"- Without touching goals or emergency savings: up to {amt(safe, lang)} "
                     f"({num(b['unallocated'])} unallocated + {num(b['free_savings'])} free savings)",
               f"- دون المساس بالأهداف أو مدخرات الطوارئ: حتى {amt(safe, lang)} "
               f"({num(b['unallocated'])} غير مخصص + {num(b['free_savings'])} مدخرات حرة)"),
             L(lang, f"- If your goals get less this month: up to {amt(with_goals, lang)} (your goals fall behind by "
                     "the difference)",
               f"- إذا حصلت أهدافك على أقل هذا الشهر: حتى {amt(with_goals, lang)} (تتأخر أهدافك بمقدار الفرق)"),
             L(lang, f"- With cuts to flexible spending as well: up to {amt(with_cuts, lang)}",
               f"- مع تخفيض الإنفاق المرن أيضًا: حتى {amt(with_cuts, lang)}")]
    if safe > 0:
        head = L(lang, f"**You can safely spend up to {amt(safe, lang)} on {item}.**",
                 f"**يمكنك أن تصرف بأمان حتى {amt(safe, lang)} على {item}.**")
    else:
        head = L(lang, f"**Right now no amount is free for {item} without delaying a goal.**",
                 f"**حاليًا لا يوجد مبلغ متاح لـ{item} دون تأخير هدف.**")
    text = _join(head, _numbers_block(pos, lang), _section(L(lang, "Limits", "الحدود"), lines),
                 L(lang, "Tell me the price and I'll compare buying now, waiting and cheaper options.",
                   "أخبرني بالسعر وسأقارن الشراء الآن والانتظار والخيارات الأرخص."))
    data = {"decision": "safe_price", "max_safe_price": safe, "with_goal_contributions": with_goals,
            "with_flexible_cuts": with_cuts, "hypothetical": True}
    return _out(text, "assess_purchase", data, [], {**context, "awaiting": "purchase_price"})


# ---------- emergencies ----------

def emergency(user_id, cur, lang, amount, what=None):
    context = {"intent": "emergency", "decision": {"amount": None if amount is None else str(amount)},
               "months": [cur]}
    pos = engine.position(user_id, cur)
    if amount is None:
        text = L(lang, "I'm sorry you're dealing with that. How much is the expense? With the amount I can show you "
                       "the safest way to cover it from your own money.",
                 "آسف لما تمر به. كم مبلغ المصروف؟ بالمبلغ أستطيع أن أوضح لك أأمن طريقة لتغطيته من أموالك.")
        return _out(text, "clarify", {"missing": ["amount"]}, [], {**context, "awaiting": "emergency_amount"})
    if pos["income"] is None:
        return _missing_income(lang, "plan_emergency", context)
    p = engine.plan_emergency(pos, amount)
    used = {x["source"]: x["amount"] for x in p["parts"]}
    order = [L(lang, f"{i}. {amt(x['amount'], lang)} from {SOURCE[x['source']][0]}",
               f"{i}. {amt(x['amount'], lang)} من {SOURCE[x['source']][1]}") for i, x in enumerate(p["parts"], 1)]
    if p["shortfall"] > 0:
        order.append(L(lang, f"- Still {amt(p['shortfall'], lang)} not covered by your own money",
                       f"- ويبقى {amt(p['shortfall'], lang)} غير مغطى من أموالك"))
    impact = _impact_lines(p["goal_impact"], lang)
    if p["cuts"]:
        impact.append(L(lang, "   - Flexible cuts next month: " + ", ".join(
            f"{cat_label(c['category'], c['subcategory'], lang)} −{num(c['cut'])}" for c in p["cuts"]),
            "   - تخفيضات مرنة الشهر القادم: " + "، ".join(
                f"{cat_label(c['category'], c['subcategory'], lang)} −{num(c['cut'])}" for c in p["cuts"])))
    if p["shortfall"] > 0:
        head = L(lang, f"**Your own money covers {amt(amount - p['shortfall'], lang)} of the {amt(amount, lang)}.**",
                 f"**أموالك تغطي {amt(amount - p['shortfall'], lang)} من {amt(amount, lang)}.**")
    else:
        head = L(lang, f"**You can cover the {amt(amount, lang)} expense; here is the order that does the least harm.**",
                 f"**يمكنك تغطية مصروف {amt(amount, lang)}؛ هذا الترتيب الأقل ضررًا.**")
    why = L(lang, "Why this order: money nothing depends on goes first, then the savings kept for exactly this kind of "
                  "emergency, and only then your goals and flexible spending.",
            "لماذا هذا الترتيب: المال الذي لا يعتمد عليه شيء أولًا، ثم المدخرات المخصصة لمثل هذا الطارئ، وبعدها فقط "
            "أهدافك والإنفاق المرن.")
    recovery = []
    if p["refill"]:
        if p["refill_months"]:
            src = L(lang, "the money left after your goals" if p["refill_from"] == "unallocated" else
                    "the money left each month (goals paused meanwhile)",
                    "المال المتبقي بعد أهدافك" if p["refill_from"] == "unallocated" else
                    "المتبقي الشهري (مع إيقاف الأهداف مؤقتًا)")
            recovery.append(L(lang, f"- Refill the {amt(p['refill'], lang)} taken from savings at "
                                    f"{amt(p['refill_rate'], lang)} a month from {src}: {num(p['refill'])} ÷ "
                                    f"{num(p['refill_rate'])} = about {p['refill_months']} month(s)",
                              f"- أعد ملء {amt(p['refill'], lang)} المأخوذة من المدخرات بمعدل "
                              f"{amt(p['refill_rate'], lang)} شهريًا من {src}: {num(p['refill'])} ÷ "
                              f"{num(p['refill_rate'])} = حوالي {p['refill_months']} شهر"))
        else:
            recovery.append(L(lang, "- Nothing is left each month to refill savings: cutting flexible spending is "
                                    "needed first", "- لا يتبقى شيء شهريًا لإعادة ملء المدخرات: يلزم أولًا تخفيض "
                                                    "الإنفاق المرن"))
    if used.get("goal_contributions"):
        recovery.append(L(lang, "- Resume the goal contributions the month after", "- استأنف مساهمات الأهداف في الشهر التالي"))
    risks = [L(lang, f"- Essential spending ({amt(pos['essential'], lang)} a month) is not cut: cutting it would mean "
                     "going without food, bills or health care",
               f"- الإنفاق الأساسي ({amt(pos['essential'], lang)} شهريًا) لا يُخفض: تخفيضه يعني الاستغناء عن الطعام "
               "أو الفواتير أو العلاج")]
    if used.get("safety"):
        left = pos["safety_money"] - used["safety"]
        risks.append(L(lang, f"- Emergency savings drop to {amt(left, lang)} until refilled",
                       f"- مدخرات الطوارئ تنخفض إلى {amt(left, lang)} حتى إعادة ملئها"))
    if p["shortfall"] > 0:
        risks.append(L(lang, "- Borrowing for the rest adds a monthly repayment; if you must, choose the smallest amount "
                             "and shortest term",
                       "- الاقتراض للباقي يضيف قسطًا شهريًا؛ إن اضطررت فاختر أقل مبلغ وأقصر مدة"))
    text = _join(head + "\n" + why, _numbers_block(pos, lang),
                 _section(L(lang, "How to cover it", "كيف تغطيه"), order + impact),
                 _section(L(lang, "Recovery plan", "خطة التعافي"), recovery),
                 _section(L(lang, "Risks", "المخاطر"), risks),
                 _section(L(lang, "Next step", "الخطوة التالية"),
                          [L(lang, "- Once it's paid, record the expense so your numbers stay right",
                             "- بعد الدفع سجّل المصروف لتبقى أرقامك صحيحة")]))
    steps = [" + ".join(num(x["amount"]) for x in p["parts"]) + f" = {num(amount - p['shortfall'])}"]
    data = {"decision": "emergency", "amount": amount, **{k: p[k] for k in ("parts", "shortfall", "verdict",
                                                                            "goal_impact", "cuts", "refill",
                                                                            "refill_months", "refill_rate")},
            "table": [{"option": L(lang, *SOURCE[x["source"]]), "verdict": "", "detail": amt(x["amount"], lang)}
                      for x in p["parts"]], "hypothetical": True}
    return _out(text, "plan_emergency", data, steps, context)


# ---------- goal priority ----------

def priority(user_id, cur, lang, preferred=None):
    pos = engine.position(user_id, cur)
    r = engine.prioritize_goals(pos, preferred)
    context = {"intent": "priority", "months": [cur], "decision": {"preferred": preferred}}
    if r["verdict"] == "no_open_goals":
        return _out(L(lang, "You have no open goals to prioritize. Add one on the Goals page.",
                      "لا توجد أهداف مفتوحة لترتيبها. أضف هدفًا من صفحة الأهداف."), "rank_goals", {}, [], context)
    if r["verdict"] == "insufficient_information":
        return _missing_income(lang, "rank_goals", context)
    cap = r["capacity"]
    basis = {"stated": L(lang, f'"{preferred}" first, as you asked', f'"{preferred}" أولًا كما طلبت'),
             "saved": L(lang, "the order you saved on the Goals page", "الترتيب الذي حفظته في صفحة الأهداف"),
             "suggested": L(lang, "a suggestion (emergency savings first while they cover under a month, then the "
                                  "nearest deadline); tell me if your priorities differ",
                            "اقتراح (مدخرات الطوارئ أولًا إن كانت تغطي أقل من شهر، ثم الأقرب موعدًا)؛ أخبرني إن "
                            "كانت أولوياتك مختلفة")}[r["order_basis"]]
    factors = []
    for g in r["goals"]:
        when = (L(lang, f"{g['months_left']} month(s) left, needs {num(g['required_monthly_saving'])} a month",
                  f"يتبقى {g['months_left']} شهر، يحتاج {num(g['required_monthly_saving'])} شهريًا")
                if g.get("required_monthly_saving") else L(lang, "no deadline", "بدون موعد"))
        delay = (L(lang, "delaying it leaves you less protected against surprises",
                   "تأخيره يتركك أقل حماية أمام المفاجآت") if g["emergency"]
                 else L(lang, "delaying it moves the purchase or plan later", "تأخيره يؤجل الشراء أو الخطة"))
        factors.append(f'- "{g["name"]}": ' + L(lang, f"{num(g['remaining_amount'])} to go of {num(g['target_amount'])} "
                                                      f"({g['percent_complete']:.0f}% done), {when}; {delay}",
                                                f"يتبقى {num(g['remaining_amount'])} من {num(g['target_amount'])} "
                                                f"({g['percent_complete']:.0f}% منجز)، {when}؛ {delay}"))
    names = {"focus": L(lang, "One goal at a time (in this order)", "هدف واحد في كل مرة (بهذا الترتيب)"),
             "split": L(lang, "Split the monthly saving across goals", "تقسيم الادخار الشهري على الأهداف"),
             "emergency_first": L(lang, "Emergency savings first, then the rest", "مدخرات الطوارئ أولًا ثم الباقي")}
    strat, table = [], []
    for key, rows in r["strategies"].items():
        parts = []
        for row in rows:
            fin = (L(lang, f"in {row['finish_in']} month(s)", f"خلال {row['finish_in']} شهر") if row["finish_in"]
                   is not None else L(lang, "never at this pace", "لن يتحقق بهذه الوتيرة"))
            share = f" ({num(row['per_month'])}/{L(lang, 'month', 'شهر')})" if "per_month" in row else ""
            ok = "" if row["on_time"] is None else (" ✓" if row["on_time"] else L(lang, " (late)", " (متأخر)"))
            parts.append(f'"{row["goal"]}"{share} {fin}{ok}')
        strat.append(f"- {names[key]}: " + "; ".join(parts) + L(lang, f" — {r['on_time'][key]} on time",
                                                                f" — {r['on_time'][key]} في الموعد"))
        table.append({"option": names[key], "verdict": L(lang, f"{r['on_time'][key]} on time",
                                                         f"{r['on_time'][key]} في الموعد"), "detail": "; ".join(parts)})
    if r["adjust_dates"]:
        strat.append(L(lang, "- Adjusting dates: with the split, " + "; ".join(
            f'"{a["goal"]}" needs about {a["months_needed"]} month(s) instead of {a["months_left"]} (around '
            f'{after_months(a["months_needed"])})' for a in r["adjust_dates"] if a["months_needed"]),
            "- تعديل المواعيد: مع التقسيم، " + "؛ ".join(
                f'"{a["goal"]}" يحتاج حوالي {a["months_needed"]} شهر بدلًا من {a["months_left"]} (حوالي '
                f'{after_months(a["months_needed"])})' for a in r["adjust_dates"] if a["months_needed"])))
    best = names[r["recommended"]]
    if r["reason"] == "all_fit":
        why = L(lang, f"the {amt(cap, lang)} left each month covers all goals' needs ({amt(r['total_need'], lang)}), "
                      "so every goal can be funded at once and stay on time.",
                f"المتبقي الشهري {amt(cap, lang)} يغطي حاجة كل الأهداف ({amt(r['total_need'], lang)})، لذلك يمكن "
                "تمويلها كلها معًا في موعدها.")
    elif r["reason"] == "most_on_time":
        best_late = r["months_late"][r["recommended"]]
        why = L(lang, f"your goals need {amt(r['total_need'], lang)} a month but {amt(cap, lang)} is left, so not "
                      f"all can be on time; this choice keeps the most goals on time ({r['on_time'][r['recommended']]}) "
                      f"with the least delay past the deadlines ({best_late} month(s) in total).",
                f"أهدافك تحتاج {amt(r['total_need'], lang)} شهريًا والمتبقي {amt(cap, lang)}، لذلك لا يمكن أن "
                f"تكون كلها في موعدها؛ هذا الخيار يبقي أكبر عدد في الموعد ({r['on_time'][r['recommended']]}) "
                f"بأقل تأخير بعد المواعيد ({best_late} شهر إجمالًا).")
    else:
        why = L(lang, "no strategy keeps a goal on time with what is left each month; moving dates or cutting "
                      "flexible spending is needed.",
                "لا توجد استراتيجية تبقي أي هدف في موعده بالمتبقي الشهري؛ يلزم تعديل المواعيد أو تخفيض الإنفاق المرن.")
    head = L(lang, f"**Recommendation: {best}**\nWhy: {why}", f"**التوصية: {best}**\nالسبب: {why}")
    nxt = L(lang, '- To save an order, say "make <goal> my top priority" (I will ask you to confirm), or drag the goals '
                  "on the Goals page", '- لحفظ ترتيب قل "خلي <الهدف> أولوية" (سأطلب تأكيدك)، أو رتب الأهداف في صفحة الأهداف')
    text = _join(head, _numbers_block(pos, lang),
                 _section(L(lang, f"Order used: {basis}", f"الترتيب المستخدم: {basis}"), factors),
                 _section(L(lang, "Strategies compared", "مقارنة الاستراتيجيات"), strat),
                 _section(L(lang, "Assumptions", "الافتراضات"),
                          [L(lang, f"- {amt(cap, lang)} is saved every month, as in {month_label(cur, lang)}",
                             f"- يُدخر {amt(cap, lang)} كل شهر كما في {month_label(cur, lang)}")]),
                 _section(L(lang, "Next step", "الخطوة التالية"), [nxt]))
    data = {"decision": "priority", **{k: r[k] for k in ("order_basis", "capacity", "total_need", "all_fit",
                                                          "strategies", "on_time", "months_late", "adjust_dates",
                                                          "recommended",
                                                          "reason")},
            "order": [g["name"] for g in r["goals"]], "table": table}
    return _out(text, "rank_goals", data, [], context)


# ---------- saving plan ----------

TRADE_OFF = {
    "Restaurant": ("fewer meals out; cooking at home takes more time", "وجبات خارجية أقل؛ الطبخ في البيت يأخذ وقتًا أكثر"),
    "Fast Food": ("fewer quick meals; plan simple food ahead", "وجبات سريعة أقل؛ خطط لأكل بسيط مسبقًا"),
    "Movie": ("fewer outings; free activities instead", "خروجات أقل؛ أنشطة مجانية بدلًا منها"),
    "Clothes": ("buy only what is needed this season", "اشترِ فقط ما تحتاجه هذا الموسم"),
    "Electronics": ("postpone gadgets that are not needed", "أجّل الأجهزة غير الضرورية"),
    "Transport": ("more shared rides or walking where safe", "مشاوير مشتركة أكثر أو المشي حيث يكون آمنًا"),
    "Internet": ("a smaller package if you use less", "باقة أصغر إن كان استخدامك أقل"),
}


def saving_plan(user_id, cur, lang):
    plan = engine.saving_plan(user_id, cur)
    pos = engine.position(user_id, cur)
    context = {"intent": "saving_plan", "months": [cur]}
    label = month_label(cur, lang)
    if not pos["flexible_lines"] and "expenses" in pos["missing"]:
        return _out(L(lang, "You have no spending recorded yet, so there is nothing to build a saving plan from. Add "
                            "your transactions on the Money page and ask again.",
                      "لا توجد مصروفات مسجلة بعد، لذلك لا يوجد ما أبني عليه خطة ادخار. أضف معاملاتك من صفحة المال "
                      "ثم اسأل مرة أخرى."), "plan_savings", {"decision": "saving_plan", "missing": ["expenses"]},
                    [], context)
    if not plan["earlier_months"]:
        text = L(lang, f"I only have {label} on record, so I can't tell which spending is higher than usual for you, "
                       "and I won't use generic percentages. Tell me how much you'd like to save a month (for example "
                       '"how can I save 500 a month?") and I\'ll split it over your flexible spending.',
                 f"لدي {label} فقط في السجلات، لذلك لا أستطيع معرفة أي إنفاق أعلى من المعتاد لك، ولن أستخدم نسبًا "
                 'عامة. أخبرني كم تريد أن تدخر شهريًا (مثلًا "كيف أوفر 500 شهريًا؟") وسأوزعها على إنفاقك المرن.')
        return _out(text, "plan_savings", {"decision": "saving_plan", "missing": ["history"]}, [], context)
    earlier = ", ".join(month_label(m, lang) for m in plan["earlier_months"])
    lines, table = [], []
    for r in plan["suggested"]:
        what = cat_label(r["category"], r["subcategory"], lang)
        if r["rule"] == "back_to_average" and r["limit"] == 0:
            rule = L(lang, f"new this month (nothing in {earlier}); if it doesn't repeat, {num(r['saving'])} is saved",
                     f"جديد هذا الشهر (لا شيء في {earlier})؛ إن لم يتكرر يُوفَّر {num(r['saving'])}")
        elif r["rule"] == "back_to_average":
            rule = L(lang, f"back to your usual {num(r['limit'])} ({earlier})", f"العودة إلى معدلك {num(r['limit'])} ({earlier})")
        else:
            rule = L(lang, f"your own lowest month, {num(r['limit'])}", f"أقل شهر لديك، {num(r['limit'])}")
        trade = TRADE_OFF.get(r["subcategory"] or r["category"])
        trade_text = f" — {L(lang, *trade)}" if trade else ""
        weekly = L(lang, f", about {num(r['weekly_limit'])} a week", f"، حوالي {num(r['weekly_limit'])} أسبوعيًا") \
            if r["limit"] > 0 else ""
        lines.append(L(lang, f"- {what}: {num(r['now'])} → limit {num(r['limit'])}{weekly}: saves {amt(r['saving'], lang)} "
                             f"({rule}){trade_text}",
                       f"- {what}: {num(r['now'])} ← حد {num(r['limit'])}{weekly}: يوفر {amt(r['saving'], lang)} "
                       f"({rule}){trade_text}"))
        table.append({"option": what, "verdict": amt(r["saving"], lang), "detail": f"{num(r['now'])} → {num(r['limit'])}"})
    steady = [cat_label(r["category"], r["subcategory"], lang) for r in plan["lines"] if r["rule"] == "steady"]
    total = plan["total_saving"]
    if not plan["suggested"]:
        head = L(lang, f"**Your flexible spending in {label} is not above your own earlier months ({earlier}), so your "
                       "records show no easy saving.**",
                 f"**إنفاقك المرن في {label} ليس أعلى من أشهرك السابقة ({earlier})، لذلك لا تظهر سجلاتك توفيرًا سهلًا.**")
    else:
        sum_text = " + ".join(num(r["saving"]) for r in plan["suggested"])
        head = L(lang, f"**A saving plan from your own spending: {amt(total, lang)} a month "
                       f"(about {amt(plan['weekly_saving'], lang)} a week).**\n{sum_text} = {num(total)}",
                 f"**خطة ادخار من إنفاقك الفعلي: {amt(total, lang)} شهريًا "
                 f"(حوالي {amt(plan['weekly_saving'], lang)} أسبوعيًا).**\n{sum_text} = {num(total)}")
    effect = []
    goal = next((g for g in pos["goals"] if g.get("required_monthly_saving")), None)
    if total > 0:
        effect.append(L(lang, f"- {amt(total, lang)} a month is {amt(total * 12, lang)} over a year ({num(total)} × 12)",
                        f"- {amt(total, lang)} شهريًا تساوي {amt(total * 12, lang)} في السنة ({num(total)} × 12)"))
    if goal and total > 0 and pos["surplus"] is not None and pos["surplus"] > 0:
        before = engine.months_needed(dec(goal["remaining_amount"]), pos["surplus"])
        after = engine.months_needed(dec(goal["remaining_amount"]), pos["surplus"] + total)
        calc = f"{num(goal['remaining_amount'])} ÷ ({num(pos['surplus'])} + {num(total)})"
        if after < before:
            effect.append(L(lang, f'- If all the money left went to "{goal["name"]}", it would be reached in about '
                                  f"{after} month(s) instead of {before} ({calc})",
                            f'- لو ذهب كل المتبقي إلى "{goal["name"]}" ستصل إليه خلال {after} شهر تقريبًا بدلًا من '
                            f"{before} ({calc})"))
        else:
            effect.append(L(lang, f'- "{goal["name"]}" would still take about {after} month(s) ({calc}, rounded up), '
                                  f"but its last month needs less",
                            f'- "{goal["name"]}" سيحتاج حوالي {after} شهر ({calc}، مقربة لأعلى)، لكن شهره الأخير '
                            "يحتاج مبلغًا أقل"))
    notes = [L(lang, "- Essential spending is never included", "- الإنفاق الأساسي غير مشمول أبدًا")]
    if steady:
        notes.append(L(lang, f"- Steady, so no limit suggested: {', '.join(steady)}",
                       f"- ثابت، لذلك لا يُقترح حد: {'، '.join(steady)}"))
    text = _join(head, _section(L(lang, f"Limits for next month (compared with {earlier})",
                                  f"حدود الشهر القادم (مقارنة بـ {earlier})"), lines),
                 _section(L(lang, "Effect", "الأثر"), effect), _section(L(lang, "Notes", "ملاحظات"), notes),
                 _section(L(lang, "Next step", "الخطوة التالية"),
                          [L(lang, "- Check these limits weekly; ask me \"how much did I spend on restaurants this "
                                   "month?\" any time", "- راجع هذه الحدود أسبوعيًا؛ اسألني \"كم صرفت على المطاعم "
                                                        "هذا الشهر؟\" في أي وقت")]))
    data = {"decision": "saving_plan", "suggested": plan["suggested"], "total_saving": total,
            "weekly_saving": plan["weekly_saving"], "earlier_months": plan["earlier_months"], "table": table}
    steps = [f"{cat_label(r['category'], r['subcategory'], lang)}: {num(r['now'])} − {num(r['limit'])} = {num(r['saving'])}"
             for r in plan["suggested"]]
    return _out(text, "plan_savings", data, steps, context)


# ---------- which spending to cut, and is a target realistic ----------

CUT_WORD = re.compile(r"\b(?:cut|reduce|trim|lower|save|spend less)\b|(?<![ء-ي])(?:قلل|اقلل|اخفض|خفض|اوفر|وفر|اقطع)(?![ء-ي])")
CHOICE = re.compile(r"\b(?:or|which|first|vs\.?|versus|better)\b|(?<![ء-ي])(?:ولا|او|ايهما|انهو|اي واحد|اول|احسن)(?![ء-ي])")
ONLY = re.compile(r"\b(?:only|just|by cutting|from)\b|(?<![ء-ي])(?:بس|فقط|من)(?![ء-ي])")
REALISTIC = re.compile(r"\b(?:realistic|possible|feasible|doable|can i|could i|is it)\b|ممكن|معقول|واقعي|اقدر|ينفع")


def _lines_for(pos, cats):
    """The user's spending lines inside the named categories, split essential / flexible."""
    from app.preferences.user_preferences import is_essential, preference_map
    prefs = preference_map(pos["user_id"])
    spent = facts_spending(pos)
    out = []
    for category, sub in cats:
        for s_name, amount in spent["by_subcategory"].get(category, {}).items():
            if sub and s_name != sub or amount <= 0:
                continue
            out.append({"category": category, "subcategory": s_name, "amount": amount,
                        "essential": is_essential(pos["user_id"], category, s_name, prefs)})
    return out


def facts_spending(pos):
    return facts.spending(pos["user_id"], [pos["month"]])


def _previous_level(pos, category, sub):
    prev = facts.spending(pos["user_id"], [facts.shift_month(pos["month"], -1)])
    return prev["by_subcategory"].get(category, {}).get(sub) if prev["count"] else None


def cut_choice(user_id, cur, lang, cats):
    """"Restaurants or transport: what should I cut first?" Each option from the
    records: how much it is, whether it is essential, how it moved, and how much a
    realistic cut (at most 30% of a flexible line, the plan's rule) would free."""
    pos = engine.position(user_id, cur)
    rows = []
    for category, sub in cats:
        lines = _lines_for(pos, [(category, sub)])
        amount = sum((x["amount"] for x in lines), ZERO)
        flexible = sum((x["amount"] for x in lines if not x["essential"]), ZERO)
        realistic = (flexible * engine.CUT_SHARE).quantize(Decimal(1))
        prev = sum((p for p in (_previous_level(pos, x["category"], x["subcategory"]) for x in lines) if p), ZERO)
        rows.append({"category": category, "subcategory": sub, "amount": amount, "flexible": flexible,
                     "realistic_cut": realistic, "previous": prev, "rise": amount - prev if prev else None})
    label = month_label(cur, lang)
    lines, table = [], []
    for r in rows:
        what = cat_label(r["category"], r["subcategory"], lang)
        bits = [L(lang, f"{amt(r['amount'], lang)} in {label}", f"{amt(r['amount'], lang)} في {label}")]
        if r["rise"] is not None:
            bits.append(L(lang, f"{'+' if r['rise'] >= 0 else '−'}{num(abs(r['rise']))} on the month before",
                          f"{'+' if r['rise'] >= 0 else '−'}{num(abs(r['rise']))} عن الشهر السابق"))
        if r["flexible"] < r["amount"]:
            bits.append(L(lang, f"{num(r['amount'] - r['flexible'])} of it is essential",
                          f"منها {num(r['amount'] - r['flexible'])} أساسية"))
        bits.append(L(lang, f"a realistic cut is up to {amt(r['realistic_cut'], lang)} a month ({num(r['flexible'])} × 30%)",
                      f"التخفيض الواقعي حتى {amt(r['realistic_cut'], lang)} شهريًا ({num(r['flexible'])} × 30%)"))
        lines.append(f"- {what}: " + L(lang, "; ", "؛ ").join(bits))
        table.append({"option": what, "verdict": amt(r["realistic_cut"], lang), "detail": amt(r["amount"], lang)})
    ranked = sorted(rows, key=lambda r: (-(r["realistic_cut"]), -(r["rise"] or 0)))
    best, other = ranked[0], ranked[1]
    if best["realistic_cut"] == 0:
        head = L(lang, "**Neither is a good place to cut: both are essential spending in your settings.**",
                 "**لا يصلح أي منهما للتخفيض: كلاهما إنفاق أساسي حسب إعداداتك.**")
    else:
        why = []
        if best["realistic_cut"] > other["realistic_cut"]:
            why.append(L(lang, f"it frees more ({num(best['realistic_cut'])} vs {num(other['realistic_cut'])} a month)",
                         f"يوفر أكثر ({num(best['realistic_cut'])} مقابل {num(other['realistic_cut'])} شهريًا)"))
        if (best["rise"] or 0) > 0:
            why.append(L(lang, f"it rose by {num(best['rise'])} on the month before, so going back is realistic",
                         f"زاد بمقدار {num(best['rise'])} عن الشهر السابق، فالعودة لمستواه واقعية"))
        if other["flexible"] < other["amount"]:
            why.append(L(lang, f"part of {cat_label(other['category'], other['subcategory'], lang)} is essential",
                         f"جزء من {cat_label(other['category'], other['subcategory'], lang)} أساسي"))
        head = L(lang, f"**Cut {cat_label(best['category'], best['subcategory'], lang)} first.** Why: ",
                 f"**ابدأ بتقليل {cat_label(best['category'], best['subcategory'], lang)}.** السبب: ") + \
            (L(lang, "; ", "؛ ").join(why) or L(lang, "it frees the most", "يوفر أكثر")) + "."
    both = sum((r["realistic_cut"] for r in rows), ZERO)
    tail = L(lang, f"Cutting both realistically frees {' + '.join(num(r['realistic_cut']) for r in rows)} = "
                   f"{amt(both, lang)} a month; with income {num(pos['income'] or 0)} − spending {num(pos['expenses'])}, "
                   f"what is left would go from {amt(pos['surplus'] or 0, lang)} to {amt((pos['surplus'] or 0) + both, lang)}.",
             f"تقليل الاثنين بشكل واقعي يوفر {' + '.join(num(r['realistic_cut']) for r in rows)} = {amt(both, lang)} "
             f"شهريًا؛ والمتبقي يصبح {amt((pos['surplus'] or 0) + both, lang)} بدلًا من {amt(pos['surplus'] or 0, lang)}.") \
        if pos["surplus"] is not None else ""
    text = _join(head, _section(L(lang, "The two options", "الخياران"), lines), tail)
    data = {"decision": "cut_choice", "options": rows, "recommended": [best["category"], best["subcategory"]],
            "table": table, "hypothetical": True}
    return _out(text, "plan_savings", data, [], {"intent": "cut_choice", "months": [cur]})


def cut_target(user_id, cur, lang, target, cats):
    """"Save 1,000 a month by cutting only food: realistic?" The flexible part of
    those categories, the realistic cut (30% of each flexible line), and what is
    left over — said plainly when the target is not realistic."""
    pos = engine.position(user_id, cur)
    lines = _lines_for(pos, cats)
    names = L(lang, " and ", " و").join(cat_label(c, s, lang) for c, s in cats)
    total = sum((x["amount"] for x in lines), ZERO)
    flex = [x for x in lines if not x["essential"]]
    flex_total = sum((x["amount"] for x in flex), ZERO)
    realistic = sum(((x["amount"] * engine.CUT_SHARE).quantize(Decimal(1)) for x in flex), ZERO)
    essential = total - flex_total
    facts_lines = [L(lang, f"- {names}: {amt(total, lang)} in {month_label(cur, lang)}; essential "
                           f"{amt(essential, lang)}, flexible {amt(flex_total, lang)} ("
                           + ", ".join(f"{name(x['subcategory'] or x['category'], lang)} {num(x['amount'])}" for x in flex) + ")",
                     f"- {names}: {amt(total, lang)} في {month_label(cur, lang)}؛ أساسي {amt(essential, lang)}، مرن "
                     f"{amt(flex_total, lang)} (" + "، ".join(f"{name(x['subcategory'] or x['category'], lang)} "
                                                             f"{num(x['amount'])}" for x in flex) + ")"),
                   L(lang, f"- Realistic cut: 30% of each flexible line = {amt(realistic, lang)} a month",
                     f"- التخفيض الواقعي: 30% من كل بند مرن = {amt(realistic, lang)} شهريًا")]
    if target <= realistic:
        verdict = "realistic"
        head = L(lang, f"**Yes, {amt(target, lang)} a month from {names} is realistic.**",
                 f"**نعم، {amt(target, lang)} شهريًا من {names} هدف واقعي.**")
        cuts = verify.allocate_cuts(target, [((x["category"], x["subcategory"]), x["amount"]) for x in flex])
        plan = [L(lang, f"- {name(s or c, lang)}: {num(a)} → {num(a - cut)} (−{num(cut)})",
                  f"- {name(s or c, lang)}: {num(a)} ← {num(a - cut)} (−{num(cut)})") for (c, s), a, cut in cuts]
        rest = [_section(L(lang, "How", "الطريقة"), plan)]
    elif target <= flex_total:
        verdict = "stretch"
        share = (target / flex_total * 100).quantize(Decimal("0.1"))
        head = L(lang, f"**Possible on paper, but hard: {amt(target, lang)} is {share}% of your flexible "
                       f"{names} spending ({num(target)} ÷ {num(flex_total)}).**",
                 f"**ممكن نظريًا لكنه صعب: {amt(target, lang)} تساوي {share}% من إنفاقك المرن على {names} "
                 f"({num(target)} ÷ {num(flex_total)}).**")
        rest = [_section(L(lang, "Why it is a stretch", "لماذا هو صعب"), [
            L(lang, f"- A realistic cut is {amt(realistic, lang)} (30% of each line); {amt(target, lang)} means cutting "
                    f"{share}% of it, {num(target)} − {num(realistic)} = {amt(target - realistic, lang)} beyond what "
                    "usually lasts",
              f"- التخفيض الواقعي {amt(realistic, lang)} (30% من كل بند)؛ و{amt(target, lang)} تعني تخفيض {share}% منه، "
              f"أي {num(target)} − {num(realistic)} = {amt(target - realistic, lang)} فوق ما يستمر عادةً")])]
    else:
        verdict = "unrealistic"
        head = L(lang, f"**Not realistic: {amt(target, lang)} is more than all of your flexible {names} spending "
                       f"({amt(flex_total, lang)}).**",
                 f"**غير واقعي: {amt(target, lang)} أكثر من كل إنفاقك المرن على {names} ({amt(flex_total, lang)}).**")
        rest = [_section(L(lang, "Why", "السبب"), [
            L(lang, f"- Reaching it would mean cutting essential spending ({amt(essential, lang)}) too, which I "
                    "don't recommend: that is basic food and bills",
              f"- الوصول إليه يعني تخفيض الإنفاق الأساسي ({amt(essential, lang)}) أيضًا، ولا أنصح بذلك: هذا طعام "
              "وفواتير أساسية")])]
    if verdict != "realistic":
        others = pos["cuttable"] - realistic
        alt = L(lang, f"- A realistic alternative: {amt(realistic, lang)} from {names} + up to {amt(others, lang)} from "
                      f"your other flexible spending = {amt(pos['cuttable'], lang)} a month",
                f"- بديل واقعي: {amt(realistic, lang)} من {names} + حتى {amt(others, lang)} من إنفاقك المرن الآخر = "
                f"{amt(pos['cuttable'], lang)} شهريًا")
        if pos["cuttable"] < target:
            alt += L(lang, f"; the remaining {amt(target - pos['cuttable'], lang)} needs extra income or a smaller target",
                     f"؛ والباقي {amt(target - pos['cuttable'], lang)} يحتاج دخلًا إضافيًا أو هدفًا أصغر")
        rest.append(_section(L(lang, "What would work", "ما الذي ينجح"), [alt]))
    text = _join(head, _section(L(lang, "Your records", "سجلاتك"), facts_lines), *rest)
    data = {"decision": "cut_target", "target": target, "verdict": verdict, "category_total": total,
            "flexible": flex_total, "essential": essential, "realistic_cut": realistic,
            "all_flexible_realistic": pos["cuttable"], "hypothetical": True}
    return _out(text, "plan_savings", data, [], {"intent": "cut_target", "months": [cur]})


# ---------- real changes, after confirmation ----------

def _parse_date(text):
    m = re.search(r"\b(20\d\d)-(\d{2})(?:-(\d{2}))?\b", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = parse.NAMED_MONTH.search(text)
        if not m or not m.group(2):
            return None
        y, mo, d = int(m.group(2)), parse.MONTH_NAMES[m.group(1)], None
    try:
        day = int(d) if d else calendar.monthrange(y, mo)[1]
        return date(y, mo, day).isoformat()
    except ValueError:
        return None


def parse_action(text, question, goals, previous_goal=None):
    """{"kind", ...} for a clear request to change saved data, or None."""
    def goal_in(fragment):
        found = match_goals(goals, fragment)
        if len(found) == 1:
            return found[0], None
        if len(found) > 1:
            return None, found
        if previous_goal and SAME_GOAL.search(fragment):
            same = [g for g in goals if g["name"] == previous_goal]
            if same:
                return same[0], None
        return None, None

    m = PRIORITY_SET.search(text)
    if m:
        g, several = goal_in(m.group(1) or m.group(2))
        if g or several:
            return {"kind": "goal_priority", "goal": g, "several": several}
    recurring = re.search(r"\b(?:each|every|per|a) month\b|\bmonthly\b|شهريا|كل شهر|في الشهر", text)
    asking = re.search(r"\?|؟|\b(?:when|how|what|should|will)\b|(?<![ء-ي])(?:متي|كيف|كم|هل)(?![ء-ي])", text)
    m = None if recurring or asking else CONTRIBUTE.search(text)
    if m:
        value, rest = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        g, several = goal_in(question)
        if g or several or GOAL_WORD.search(rest):
            return {"kind": "contribute", "amount": dec(value.replace(",", "")), "goal": g, "several": several}
    m = SET_TARGET.search(text)
    if m:
        g, several = goal_in(question)
        return {"kind": "goal_target", "amount": dec((m.group(1) or m.group(2)).replace(",", "")), "goal": g,
                "several": several}
    moves = SET_DATE.search(text) or (re.match(_POLITE + r"(?:move|push|postpone|extend|shift)\b", text)
                                      and _parse_date(text))
    if moves and (GOAL_WORD.search(text) or match_goals(goals, question) or SAME_GOAL.search(text)):
        g, several = goal_in(question)
        return {"kind": "goal_date", "date": _parse_date(text), "goal": g, "several": several}
    if RECORD.search(text) and EXPENSE_NOUN.search(text) and not parse.CONDITION.search(text[:10]) \
            and not re.search(r"\badd up\b|\bsave (?:more|less|on)\b", text) and not asking \
            and parse.amounts(text):
        found = [(v, p) for v, is_pct, p in parse.amounts(text) if not is_pct]
        cats = parse.find_categories(text)
        return {"kind": "add_expense", "amount": dec(found[0][0]) if found else None,
                "category": cats[0][1] if cats else None, "subcategory": cats[0][2] if cats else None}
    return None


def _describe(a, lang):
    if a["kind"] == "contribute":
        return L(lang, f'Add {amt(a["amount"], lang)} to "{a["goal"]}": saved goes from {num(a["before"])} to '
                       f'{num(a["after"])} of {num(a["target"])}.',
                 f'إضافة {amt(a["amount"], lang)} إلى "{a["goal"]}": المدخر يصبح {num(a["after"])} بدلًا من '
                 f'{num(a["before"])} من {num(a["target"])}.')
    if a["kind"] == "goal_target":
        return L(lang, f'Change the target of "{a["goal"]}" from {amt(a["before"], lang)} to {amt(a["after"], lang)}.',
                 f'تغيير المبلغ المستهدف لهدف "{a["goal"]}" من {amt(a["before"], lang)} إلى {amt(a["after"], lang)}.')
    if a["kind"] == "goal_date":
        return L(lang, f'Move the date of "{a["goal"]}" from {a["before"] or "none"} to {a["after"]}.',
                 f'تغيير موعد "{a["goal"]}" من {a["before"] or "بدون"} إلى {a["after"]}.')
    if a["kind"] == "goal_priority":
        return L(lang, f'Make "{a["goal"]}" your top priority (goal order is saved on the Goals page).',
                 f'جعل "{a["goal"]}" أولويتك الأولى (يُحفظ الترتيب في صفحة الأهداف).')
    return L(lang, f"Record an expense of {amt(a['amount'], lang)} under {cat_label(a['category'], a['subcategory'], lang)} "
                   f"dated {a['date']}.",
             f"تسجيل مصروف {amt(a['amount'], lang)} في {cat_label(a['category'], a['subcategory'], lang)} بتاريخ {a['date']}.")


ERRORS = {
    "goal_not_found": ("I couldn't find that goal among your goals.", "لم أجد هذا الهدف بين أهدافك."),
    "invalid_amount": ("The amount must be a positive number.", "يجب أن يكون المبلغ رقمًا موجبًا."),
    "invalid_date": ("I couldn't read that date. Please write it like 2027-06-30 or \"June 2027\".",
                     "لم أفهم التاريخ. اكتبه مثل 2027-06-30 أو \"يونيو 2027\"."),
    "date_not_in_future": ("The new date must be in the future.", "يجب أن يكون الموعد الجديد في المستقبل."),
    "invalid_category": ("That category doesn't exist in the app.", "هذه الفئة غير موجودة في التطبيق."),
    "unknown_action": ("I can't make that change from the chat.", "لا أستطيع إجراء هذا التغيير من المحادثة."),
    "not_confirmed": ("I can't match that confirmation to a change I asked you about, so nothing was changed. "
                      "Please ask for the change again.",
                      "لا أستطيع ربط هذا التأكيد بتغيير سألتك عنه، لذلك لم يتغير شيء. اطلب التغيير مرة أخرى."),
    "not_saved": ("The change could not be verified in your records, so I'm not reporting it as done. Please check "
                  "the page and try again.", "لم أتمكن من التحقق من التغيير في سجلاتك، لذلك لا أعتبره تم. "
                                             "راجع الصفحة وحاول مرة أخرى."),
}


def action_request(user_id, lang, a):
    """Ask for what is missing, or ask the user to confirm the exact change."""
    context = {"intent": "action"}
    if a.get("several"):
        names = ", ".join(f'"{g["name"]}"' for g in a["several"])
        return _out(L(lang, f"Which goal do you mean: {names}?", f"أي هدف تقصد: {names}؟"), "clarify", {}, [],
                    context)
    if a["kind"] != "add_expense" and a.get("goal") is None:
        return _out(L(lang, "Which goal should I change? Please name it.", "أي هدف أغير؟ اذكر اسمه من فضلك."),
                    "clarify", {}, [], context)
    if a["kind"] in ("contribute", "goal_target", "add_expense") and a.get("amount") is None:
        return _out(L(lang, "How much? Please give the amount.", "كم المبلغ؟ اذكره من فضلك."), "clarify", {}, [],
                    context)
    if a["kind"] == "goal_date" and not a.get("date"):
        return _out(L(lang, "To which date? For example 2027-06-30 or \"June 2027\".",
                      "إلى أي تاريخ؟ مثلًا 2027-06-30 أو \"يونيو 2027\"."), "clarify", {}, [], context)
    if a["kind"] == "add_expense" and not a.get("category"):
        return _out(L(lang, "Which category is it (for example food, transport, bills)?",
                      "في أي فئة (مثلًا طعام، مواصلات، فواتير)؟"), "clarify", {}, [], context)
    stored = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in a.items() if k not in ("goal", "several")}
    if a.get("goal"):
        stored["goal_id"] = a["goal"]["id"]
    try:
        checked = actions.check(user_id, stored)
    except actions.ActionError as exc:
        return _out(L(lang, *ERRORS[str(exc)]), "clarify", {}, [], context)
    text = L(lang, f"Please confirm this change to your saved data:\n- {_describe(checked, lang)}\n"
                   "Reply **yes** to save it or **no** to cancel. Nothing has been changed yet.",
             f"أكد هذا التغيير على بياناتك المحفوظة:\n- {_describe(checked, lang)}\n"
             "اكتب **نعم** للحفظ أو **لا** للإلغاء. لم يتغير شيء بعد.")
    return _out(text, "clarify", {"pending_action": checked}, [],
                {**context, "awaiting": "confirm_action", "action": stored,
                 "signature": actions.sign(user_id, stored),
                 "goal": checked.get("goal")}, checked.get("goal"))


def confirm(user_id, lang, previous, yes):
    context = {"intent": "action"}
    if not yes:
        return _out(L(lang, "Cancelled: nothing was changed.", "تم الإلغاء: لم يتغير شيء."), "clarify", {}, [], context)
    try:
        # Only the exact change this app showed this user can run: the action
        # comes back from the client, so its signature is checked first.
        if not actions.verify_signature(user_id, previous.get("action"), previous.get("signature")):
            raise actions.ActionError("not_confirmed")
        done = actions.execute(user_id, previous["action"])
    except actions.ActionError as exc:
        return _out(L(lang, *ERRORS[str(exc)]), "clarify", {"saved": False}, [], context)
    if done["kind"] == "add_expense":
        text = L(lang, f"Saved and checked: {amt(done['amount'], lang)} under "
                       f"{cat_label(done['category'], done['subcategory'], lang)} on {done['date']} is now in your "
                       "transactions.",
                 f"تم الحفظ والتحقق: {amt(done['amount'], lang)} في {cat_label(done['category'], done['subcategory'], lang)} "
                 f"بتاريخ {done['date']} موجود الآن في معاملاتك.")
        return _out(text, "update_record", {"saved": True, **done}, [], context)
    f = done["stored"]
    status = L(lang, f'"{f["name"]}" now has {amt(f["saved_so_far"], lang)} of {amt(f["target_amount"], lang)} saved '
                     f'({f["percent_complete"]:.0f}%), {amt(f["remaining_amount"], lang)} to go',
               f'"{f["name"]}" الآن: ادخرت {amt(f["saved_so_far"], lang)} من {amt(f["target_amount"], lang)} '
               f'({f["percent_complete"]:.0f}%)، ويتبقى {amt(f["remaining_amount"], lang)}')
    if f.get("required_monthly_saving"):
        status += L(lang, f"; to finish by {f['target_date']} it needs {amt(f['required_monthly_saving'], lang)} a month",
                    f"؛ ولإنهائه بحلول {f['target_date']} يحتاج {amt(f['required_monthly_saving'], lang)} شهريًا")
    elif f["deadline_status"] == "reached":
        status += L(lang, " — goal reached!", " — تحقق الهدف!")
    if done["kind"] == "goal_priority":
        status = L(lang, f'"{f["name"]}" is now your top priority. ', f'"{f["name"]}" الآن أولويتك الأولى. ') + status
    text = L(lang, f"Saved and checked in your records. {status}.", f"تم الحفظ والتحقق في سجلاتك. {status}.")
    return _out(text, "update_record", {"saved": True, "goal": f}, [], {**context, "goal": f["name"]}, f["name"])


# ---------- entry point ----------

def last_goal(history):
    """The goal the latest assistant answer was about, if any."""
    for m in reversed(list(history or [])):
        if m.get("role") == "assistant":
            return m.get("goal") or (m.get("context") or {}).get("goal")
    return None


def detect(question, user_id, previous=None, age=None, last_goal=None, plan=None):
    """What kind of decision question this is ("purchase", "emergency", "priority",
    "saving_plan", "action", "confirm", "goal_preview", "follow_up") or None.
    last_goal: the goal the latest answer was about ("add 200 to the same goal")."""
    text = parse.normalize(question)
    recent = previous if previous and (age or 1) <= 2 else None
    if recent and age == 1 and recent.get("awaiting") == "confirm_action" and (YES.search(text) or NO.search(text)):
        return "confirm"
    goals = crud.get_goals(user_id)
    if parse_action(text, question, goals, (recent or {}).get("goal") or last_goal):
        return "action"
    from app.services.plan_followups import continues
    if recent and recent.get("intent") in ("purchase", "emergency") and not SPENT_QUESTION.search(text) \
            and (not parse.ASKS_SPENT.search(text) or continues(text, recent)) and _follows(text, recent, age):
        return "follow_up"
    if plan and plan is not recent and not SPENT_QUESTION.search(text) and continues(text, plan):
        return "plan_follow_up"          # the plan from a few answers ago ("and if I earn 1,000 more?")
    if SPENT_QUESTION.search(text) and not RECORD.search(text):
        return None                     # "how much did I spend on emergency expenses?"
    if parse.CONDITION.search(text) and _preview(text, question, goals):
        return "goal_preview"          # "what if I add 500 to my emergency fund?": shown, not saved
    if parse.CONDITION.search(text) and not PURCHASE.search(text) and not EMERGENCY.search(text) \
            and not PLANNED.search(text):
        return None                     # "what if my income drops 20%?" stays a what-if
    if EMERGENCY.search(text) and not re.search(r"\bemergency fund\b|صندوق (?:ال)?طوارئ", text):
        return "emergency"
    cats = parse.find_categories(parse.without_rest_unchanged(text))
    if CUT_WORD.search(text) and len(cats) >= 2 and CHOICE.search(text) and not parse.amounts(text):
        return "cut_choice"
    money_asked = [v for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 10]
    if CUT_WORD.search(text) and cats and money_asked and ONLY.search(text) and REALISTIC.search(text):
        return "cut_target"
    if PRIORITY.search(text):
        return "priority"
    if SAVING_PLAN.search(text):
        return "saving_plan"
    if PLANNED.search(text) and not BOUGHT.search(text):
        return "purchase"
    if PURCHASE.search(text) and not BOUGHT.search(text) and _own_purchase(text):
        return "purchase"
    return None


# "I / my / a price" or an Arabic first-person verb: the user's own purchase, not a
# general question ("is buying in bulk cheaper?").
OWN_BUY = re.compile(rf"(?<!{_A})(?:اشتري|نشتري|بشتري|حاشتري|ساشتري|هشتري|اجيب|اقتني|استلف|اتسلف)(?!{_A})|"
                     r"\bafford\b")


def _own_purchase(text):
    if parse.find_categories(text) and (parse.UP.search(text) or parse.DOWN.search(text)) \
            and not parse.amounts(text) and not OWN_BUY.search(text):
        return False                    # "what if I buy less food?" is a spending what-if
    if re.search(r"\b(?:buy|purchase)\s+(?:less|more|fewer)\b", text):
        return False
    return bool(parse.PERSONAL.search(text) or parse.amounts(text) or OWN_BUY.search(text) or INSTALL.search(text))


HYPOTHETICAL_LEAD = re.compile(r"^(?:and |so |ok(?:ay)?,? )?(?:what (?:if|happens if)|if|suppose|imagine)\s+(?:i\s+)?|"
                               r"^(?:و|طيب )?(?:ماذا لو|لو|اذا|افترض)\s+(?:انا\s+)?")


def _preview(text, question, goals):
    """A hypothetical contribution to a named goal, as {"goal", "amount"}, or None."""
    lead = HYPOTHETICAL_LEAD.search(text)
    if not lead:
        return None
    rest = text[lead.end():].rstrip("?؟ ")
    rest = re.sub(r"^(?:اضفت|ضفت|حطيت|حولت|خصصت|ادخرت|وفرت)", "اضف", rest)
    rest = re.sub(r"^(?:added|put|moved|saved)\b", "add", rest)
    m = CONTRIBUTE.search(rest)
    if not m:
        return None
    found = match_goals(goals, question)
    if len(found) != 1:
        return None
    value = m.group(1) or m.group(3)
    return {"goal": found[0], "amount": dec(value.replace(",", ""))}


def goal_preview(user_id, cur, lang, p):
    g, amount = p["goal"], p["amount"]
    before = goal_facts(g)
    after = goal_facts({**g, "current_amount": float(dec(g["current_amount"]) + amount)})
    lines = [L(lang, f"- Saved: {num(before['saved_so_far'])} + {num(amount)} = {amt(after['saved_so_far'], lang)} of "
                     f"{amt(before['target_amount'], lang)} ({after['percent_complete']:.0f}%)",
               f"- المدخر: {num(before['saved_so_far'])} + {num(amount)} = {amt(after['saved_so_far'], lang)} من "
               f"{amt(before['target_amount'], lang)} ({after['percent_complete']:.0f}%)"),
             L(lang, f"- Still needed: {num(before['target_amount'])} − {num(after['saved_so_far'])} = "
                     f"{amt(after['remaining_amount'], lang)}",
               f"- المتبقي: {num(before['target_amount'])} − {num(after['saved_so_far'])} = "
               f"{amt(after['remaining_amount'], lang)}")]
    if before.get("required_monthly_saving") is not None and after.get("required_monthly_saving") is not None:
        lines.append(L(lang, f"- Needed each month until {before['target_date']}: {num(before['required_monthly_saving'])} → "
                             f"{amt(after['required_monthly_saving'], lang)}",
                       f"- المطلوب شهريًا حتى {before['target_date']}: {num(before['required_monthly_saving'])} ← "
                       f"{amt(after['required_monthly_saving'], lang)}"))
    pos = engine.position(user_id, cur)
    if pos["savings_balance"] < amount:
        lines.append(L(lang, f"- Where it would come from: your savings outside goals are {amt(pos['savings_balance'], lang)}, "
                             f"so the rest would have to come from this month's {amt(pos['surplus'] or 0, lang)} left",
                       f"- من أين يأتي: مدخراتك خارج الأهداف {amt(pos['savings_balance'], lang)}، لذلك الباقي من "
                       f"المتبقي هذا الشهر {amt(pos['surplus'] or 0, lang)}"))
    text = _join(L(lang, f'**Hypothetical: adding {amt(amount, lang)} to "{g["name"]}"** (nothing was saved)',
                   f'**افتراضيًا: إضافة {amt(amount, lang)} إلى "{g["name"]}"** (لم يُحفظ شيء)'), "\n".join(lines),
                 L(lang, f'To really add it, say "add {num(amount)} to {g["name"]}" and confirm.',
                   f'لإضافته فعلًا قل "ضيف {num(amount)} لـ{g["name"]}" ثم أكّد.'))
    data = {"decision": "goal_preview", "goal": g["name"], "amount": amount, "before": before, "after": after,
            "hypothetical": True}
    return _out(text, "simulate_scenario", data, [lines[0].lstrip("- "), lines[1].lstrip("- ")],
                {"intent": "goal_preview", "goal": g["name"], "months": [cur]}, g["name"])


def _follows(text, recent, age):
    awaiting = recent.get("awaiting")
    if age == 1 and awaiting in ("purchase_price", "emergency_amount") and parse.amounts(text) \
            and len(text.split()) <= 8:
        return True
    if age == 1 and awaiting == "purchase_urgency" and len(text.split()) <= 6 and (
            URGENT.search(text) or OPTIONAL.search(text) or YES.search(text) or NO.search(text)):
        return True
    if recent.get("intent") != "purchase":
        return bool(parse.CONDITION.search(text) and parse.amounts(text) and len(text.split()) <= 10)
    if (recent.get("decision") or {}).get("planned") and parse.amounts(text) and len(text.split()) <= 10:
        return True                     # "about 3,000 in 4 months"
    from app.services.plan_followups import continues, horizon
    if age == 1 and awaiting == "purchase_months" and horizon(text) or \
            age == 1 and awaiting == "purchase_months" and parse.amounts(text) and len(text.split()) <= 4:
        return True
    if continues(text, recent):
        return True
    return bool(DELAY.search(text) or INSTALL.search(text) or CHEAPER.search(text) or HALF.search(text)
                or PER_MONTH.search(text)
                or COMPARE_PREV.search(text) or (parse.CONDITION.search(text) and PRICE_IS.search(text))
                or GOAL_FIRST.search(text) and len(text.split()) <= 10)


def _purchase_from(question, text, goals, d=None):
    d = dict(d or {})
    price, months, count, monthly, candidates = _numbers(text)
    if len(candidates) > 1 and not CHEAPER.search(text):
        d["price_conflict"] = [str(c) for c in candidates]     # "for 5,000 ... it costs 6,000": ask
    else:
        d.pop("price_conflict", None)
    stated = STATED_INCOME.search(text)
    if stated:
        d["stated_income"] = str(dec((stated.group(1) or stated.group(2)).replace(",", "")))
    planned = PLANNED.search(text)
    if planned:
        d["planned"] = True
        d.setdefault("item", (planned.group(1) or planned.group(2)).strip())
    item = _item(text)
    if item:
        # the user's own spelling ("Ramadan", "iPhone"), not the lower-cased text
        own = re.search(re.escape(item), question, re.IGNORECASE)
        item = own.group(0) if own else item
    if d.get("item"):
        own = re.search(re.escape(d["item"]), question, re.IGNORECASE)
        d["item"] = own.group(0) if own else d["item"]
    if item and not d.get("item"):
        d["item"] = item
    if price is not None:
        d["price"] = price
    if months and (DELAY.search(text) or d.get("planned")) and not INSTALL.search(text) \
            and not PER_MONTH.search(text):
        d["delay"] = months             # a wait, or the months until a planned expense
    from app.services.plan_followups import horizon
    in_months = horizon(text)
    if in_months and not d.get("planned") and not INSTALL.search(text) and not PER_MONTH.search(text):
        d["horizon"] = in_months        # "... in four months": the user's own time frame
        d.pop("delay", None)
    if monthly is not None and (d.get("loan") or LOAN.search(text) or INSTALL.search(text) or months):
        # "2,000 a month for 24 months": a repayment plan (a loan or installments)
        d["monthly"], d["installments"] = monthly, months or count or d.get("installments")
        d.pop("delay", None)
    if INSTALL.search(text):
        d["installments"] = count or d.get("installments") or (months if months else None)
        if monthly is not None:
            d["monthly"] = monthly
        if d.get("installments") is None and d.get("monthly") is None:
            d["installments"] = None
    u = _urgency(text)
    if u is not None:
        d["urgent"] = u
    if LOAN.search(text):
        d["loan"] = True
    competing, own = _competing_goal(goals, question, d.get("item"))
    if competing:
        d["goal"] = competing["name"]
    if own:
        d["own_goal"] = own["name"]
    if d.get("goal") and GOAL_FIRST.search(text):
        # "the emergency fund is more important": whichever is named first is meant
        goal_at = min((m.start() for m in re.finditer(r"\bgoal|fund|هدف|صندوق|" + re.escape(
            parse.normalize(d["goal"]).split()[0]), text)), default=None)
        item_at = text.find(parse.normalize(d.get("item") or "")) if d.get("item") else -1
        pref_at = GOAL_FIRST.search(text).start()
        if goal_at is not None and (item_at < 0 or abs(goal_at - pref_at) < abs(item_at - pref_at)):
            d["goal_first"] = True
        elif item_at >= 0:
            d["urgent"] = True
    if SAFE_PRICE.search(text):
        d["safe_price"] = True
    return d


def answer(question, user_id, cur, lang, previous=None, age=None, kind=None, last_goal=None, plan=None):
    kind = kind or detect(question, user_id, previous, age, last_goal, plan)
    if kind is None:
        return None
    text = parse.normalize(question)
    goals = crud.get_goals(user_id)
    cur = cur or date.today().strftime("%Y-%m")
    if kind == "confirm":
        return confirm(user_id, lang, previous, bool(YES.search(text)))
    if kind == "plan_follow_up":
        from app.services.plan_followups import follow_up
        return follow_up(question, text, user_id, cur or date.today().strftime("%Y-%m"), lang, plan)
    if kind == "goal_preview":
        return goal_preview(user_id, cur, lang, _preview(text, question, goals))
    if kind == "action":
        a = parse_action(text, question, goals, (previous or {}).get("goal") or last_goal)
        return action_request(user_id, lang, a)
    if kind == "emergency":
        found = [v for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 10]
        return emergency(user_id, cur, lang, dec(max(found)) if found else None)
    if kind == "priority":
        named = match_goals(goals, question)
        return priority(user_id, cur, lang, named[0]["name"] if len(named) == 1 else None)
    if kind == "saving_plan":
        return saving_plan(user_id, cur, lang)
    if kind in ("cut_choice", "cut_target"):
        cats = [(c, sub) for _, c, sub in parse.find_categories(parse.without_rest_unchanged(text))]
        cats = list(dict.fromkeys(cats))
        if kind == "cut_choice":
            return cut_choice(user_id, cur, lang, cats)
        target = dec(max(v for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 10))
        return cut_target(user_id, cur, lang, target, cats)
    if kind == "follow_up":
        return _follow_up(question, text, user_id, cur, lang, previous, goals)
    return purchase(user_id, cur, lang, _purchase_from(question, text, goals), previous)


def _follow_up(question, text, user_id, cur, lang, previous, goals):
    if previous.get("intent") == "emergency":
        found = [v for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 10]
        return emergency(user_id, cur, lang, dec(max(found)) if found else None)
    from app.services import plan_followups
    if previous.get("awaiting") == "purchase_months" and not plan_followups.horizon(text):
        found = [v for v, is_pct, _ in parse.amounts(text) if not is_pct and 0 < v <= 120]
        if found:
            text = f"in {int(found[0])} months"     # "4" answering "in how many months?"
    if plan_followups.continues(text, previous) or previous.get("awaiting") == "purchase_months":
        found = plan_followups.follow_up(question, text, user_id, cur, lang, previous)
        if found:
            return found
    d = {k: (dec(v) if k in ("price", "cheaper", "monthly") and v is not None else v)
         for k, v in (previous.get("decision") or {}).items()}
    d.pop("safe_price", None)
    awaiting = previous.get("awaiting")
    if awaiting == "purchase_urgency" and not (URGENT.search(text) or OPTIONAL.search(text)):
        d["urgent"] = bool(YES.search(text))
    if COMPARE_PREV.search(text) and previous.get("before"):
        return _compare_previous(lang, previous)
    if HALF.search(text):
        monthly = dec((previous.get("summary") or {}).get("monthly") or 0)
        if monthly and d.get("price"):
            d["delay"] = engine.months_needed(d["price"], monthly / 2)
    if CHEAPER.search(text):
        found = [v for v, is_pct, _ in parse.amounts(text) if not is_pct and v >= 10]
        if found:
            d["cheaper"] = dec(found[0])
        return purchase(user_id, cur, lang, d, previous)
    new = _purchase_from(question, text, goals, d)
    if awaiting != "purchase_price" and new.get("price") != d.get("price") and not (
            parse.CONDITION.search(text) and PRICE_IS.search(text)):
        new["price"] = d.get("price")       # "wait 3 months" names months, not a new price
    if DELAY.search(text) and not new.get("delay"):
        new.pop("delay", None)
    return purchase(user_id, cur, lang, new, previous)


def _compare_previous(lang, previous):
    a, b = previous.get("before") or {}, previous.get("summary") or {}

    def desc(s):
        bits = [amt(dec(s["price"]), lang)]
        if s.get("delay"):
            bits.append(L(lang, f"wait {s['delay']} month(s)", f"انتظار {s['delay']} شهر"))
        if s.get("installments"):
            bits.append(L(lang, f"{s['installments']} installments", f"{s['installments']} أقساط"))
        if s.get("cheaper"):
            bits.append(L(lang, f"cheaper at {amt(dec(s['cheaper']), lang)}", f"أرخص بـ {amt(dec(s['cheaper']), lang)}"))
        return ", ".join(bits) + f" → {V(s['verdict'], lang)}; " + L(lang, "recommended: ", "الموصى به: ") + \
            L(lang, *OPTION.get(s["recommended"], (s["recommended"], s["recommended"])))

    text = L(lang, f"**Previous option:** {desc(a)}\n**Latest option:** {desc(b)}",
             f"**الخيار السابق:** {desc(a)}\n**الخيار الأخير:** {desc(b)}")
    return _out(text, "assess_purchase", {"decision": "compare", "before": a, "after": b}, [],
                {"intent": "purchase", "decision": previous.get("decision"), "summary": b, "before": a,
                 "months": previous.get("months")})
