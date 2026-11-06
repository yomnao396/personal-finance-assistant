# app/services/intent.py
# What a chat question asks for, read without the language model: the intent,
# the period, the category and, for a what-if, the exact changes. English and
# Arabic. The parsing is deliberately conservative: a question it does not
# clearly understand returns intent None and goes to the model as before.

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.finance.facts import months_back, shift_month

ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬٪", "0123456789.,%")
DIACRITICS = re.compile(r"[ً-ْـ]")


def language_of(question, default="en"):
    """The language the user wrote in (the answer uses the same one)."""
    if ARABIC.search(question or ""):
        return "ar"
    return "en" if LATIN.search(question or "") else default


# English money words people often misspell ("resturants", "expences"): a word
# of 5+ letters close to one of these is read as it.
VOCABULARY = (
    "restaurant", "restaurants", "transport", "transportation", "entertainment", "electricity", "groceries",
    "utilities", "education", "healthcare", "clothes", "clothing", "shopping", "internet", "breakfast",
    "dinner", "lunch", "movies", "cinema", "furniture", "electronics", "medical", "pharmacy", "medicine",
    "reduce", "increase", "decrease", "expenses", "expense", "spending", "income", "salary", "savings",
    "budget", "remaining", "balance", "together", "categories", "category", "transactions", "emergency",
    "installments", "afford", "purchase", "reduction", "percent",
)
# Real words that are close to the vocabulary but mean something else.
NOT_TYPOS = {"interest", "interests", "internal", "intern", "entertain", "restart", "rental", "rentals",
             "dinners", "lunches", "reduced", "increased", "decreased", "incomes", "budgets", "expensive",
             "balanced", "category", "percentage", "remained", "spending", "savings", "saving", "invest",
             "investment", "medicines", "movie", "cinemas", "shoppers", "clothe", "transfer", "transform"}
_KNOWN = set(VOCABULARY) | NOT_TYPOS


@lru_cache(maxsize=4096)
def _spelling(word):
    if len(word) < 5 or word in _KNOWN:
        return word
    close = difflib.get_close_matches(word, VOCABULARY, n=1, cutoff=0.84)
    return close[0] if close else word


def normalize(text):
    """Lower case, Western digits, one spelling of alef / yaa / taa marbuta, so a
    pattern written once matches the common Arabic spellings; Arabic and Latin
    letters written together are separated ("الـrestaurants"), and common English
    misspellings are corrected ("resturants", "fastfood")."""
    text = DIACRITICS.sub("", (text or "").translate(ARABIC_DIGITS).lower())
    text = re.sub("[أإآ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"([ء-ي])([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])([ء-ي])", r"\1 \2", text)
    text = re.sub(r"[a-z]{5,}", lambda m: _spelling(m.group(0)), text)
    return re.sub(r"\s+", " ", text).strip()


# Arabic letters only: the Arabic comma and question mark (، ؟) sit in the same
# Unicode block but end a word.
AR_LETTER = r"[ء-يٱ-ۓ]"


def _ar(word):
    """An Arabic word with the prefixes و ب ل ف ال in front of it."""
    return rf"(?<!{AR_LETTER})(?:و|ف)?(?:ب|ل|لل)?(?:ال)?{word}(?!{AR_LETTER})"


# ---------- categories (category, subcategory or None) ----------
# Longest phrases first, so "fast food" wins over "food".
CATEGORY_WORDS = [
    (r"fast[ -]?food", "Food", "Fast Food"), (_ar("وجبات (?:ال)?سريعه"), "Food", "Fast Food"),
    (_ar("فاست ?فود"), "Food", "Fast Food"), (_ar("ريستورانت(?:ات)?") + "|" + _ar("كافيه(?:ات)?"), "Food", "Restaurant"),
    (r"restaurants?|eating out|dining out", "Food", "Restaurant"),
    (_ar("مطاعم") + "|" + _ar("مطعم"), "Food", "Restaurant"),
    (r"breakfasts?", "Food", "Breakfast"), (_ar("فطور") + "|" + _ar("افطار"), "Food", "Breakfast"),
    (r"lunch(?:es)?", "Food", "Lunch"), (_ar("غداء"), "Food", "Lunch"),
    (r"dinners?", "Food", "Dinner"), (_ar("عشاء"), "Food", "Dinner"),
    (r"food|groceries|eating", "Food", None), (_ar("طعام") + "|" + _ar("اكل"), "Food", None),
    (r"transport(?:ation)?|bus(?:es)?|taxis?|commut\w*|transit", "Transport", None),
    (_ar("مواصلات") + "|" + _ar("نقل") + "|" + _ar("تنقل") + "|" + _ar("تاكسي") + "|" + _ar("ركشه") + "|"
     + _ar("ترحيل"), "Transport", None),
    (r"utilit(?:y|ies)", "Utilities", None), (_ar("مرافق"), "Utilities", None),
    (r"movies?|cinema", "Entertainment", "Movie"), (_ar("سينما") + "|" + _ar("افلام"), "Entertainment", "Movie"),
    (r"entertainment|fun|leisure", "Entertainment", None), (_ar("ترفيه"), "Entertainment", None),
    (r"cloth(?:es|ing)", "Shopping", "Clothes"),
    (_ar("ملابس") + "|" + _ar("لبس") + "|" + _ar("هدوم") + "|" + _ar("كسوه"), "Shopping", "Clothes"),
    (r"electronics?|gadgets?", "Shopping", "Electronics"), (_ar("الكترونيات"), "Shopping", "Electronics"),
    (r"furniture", "Shopping", "Furniture"), (_ar("اثاث"), "Shopping", "Furniture"),
    (r"shopping", "Shopping", None), (_ar("تسوق") + "|" + _ar("مشتريات"), "Shopping", None),
    (r"rent", "Bills", "Rent"), (_ar("ايجار"), "Bills", "Rent"),
    (r"electricity|power bill", "Bills", "Electricity"), (_ar("كهرباء"), "Bills", "Electricity"),
    (r"water(?: bill)?", "Bills", "Water"), (_ar("مياه") + "|" + _ar("ماء"), "Bills", "Water"),
    (r"internet|wi-?fi", "Bills", "Internet"), (_ar("انترنت") + "|" + _ar("نت"), "Bills", "Internet"),
    (r"bills?", "Bills", None), (_ar("فواتير") + "|" + _ar("فاتوره"), "Bills", None),
    (r"education|tuition|school(?:ing)?|courses?", "Education", None),
    (_ar("تعليم") + "|" + _ar("دراسه") + "|" + _ar("دورات"), "Education", None),
    (r"health(?:care)?|medical|medicines?|pharmacy|doctors?", "Healthcare", None),
    (_ar("صحه") + "|" + _ar("علاج") + "|" + _ar("ادويه") + "|" + _ar("صيدليه"), "Healthcare", None),
    (r"others?|miscellaneous", "Others", None), (_ar("اخري") + "|" + _ar("متفرقات"), "Others", None),
]
_CATEGORY_RES = [(re.compile(p if ARABIC.search(p) else rf"\b(?:{p})\b"), c, s)
                 for p, c, s in CATEGORY_WORDS]


def find_categories(text):
    """[(position, category, subcategory)] in the order they appear; a span is
    used once (so "fast food" is not also "food")."""
    text = normalize(text)
    taken, out = [], []
    for regex, category, sub in _CATEGORY_RES:
        for m in regex.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append((m.start(), category, sub))
    return sorted(out)


# ---------- periods ----------
MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
    "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
                "شهرين": 2, "ثلاثه": 3, "ثلاث": 3, "اربعه": 4, "اربع": 4, "خمسه": 5, "سته": 6,
                "سبعه": 7, "ثمانيه": 8, "تسعه": 9, "عشره": 10}
_N = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
RANGE = re.compile(rf"\b(?:last|past|previous)\s+{_N}\s+months\b|(?:اخر|الاخيره|الماضيه)\s+{_N}\s+(?:اشهر|شهور)"
                   rf"|{_N}\s+(?:اشهر|شهور)\s+(?:الاخيره|الماضيه)|(?:اخر|اخير)\s+شهرين|الشهرين الماضيين")
AGO = re.compile(rf"\b{_N}\s+months?\s+ago\b|قبل\s+{_N}\s*(?:اشهر|شهور)?|قبل شهرين")
LAST_MONTH = re.compile(r"\b(?:last|previous|prior) month\b|"
                        r"الشهر (?:الماضي|السابق|اللي فات|الفات|الفايت|الفائت|القبل|اللي قبله)")
THIS_MONTH = re.compile(r"\b(?:this|current|the) month\b|(?:هذا|هذ) الشهر|الشهر (?:الحالي|ده|هذا)")
ISO_MONTH = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])\b")
NAMED_MONTH = re.compile(rf"(?<![\w{AR_LETTER[1:-1]}])(?:ب|ل|في )?(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
                         + rf")(?![\w{AR_LETTER[1:-1]}])(?:\s+(20\d\d))?")
ALL_TIME = re.compile(r"\b(?:all time|ever|all months|so far in total|since i started)\b|منذ البدايه|كل الاشهر")


def _count(token):
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token, 0)


def find_period(text, current):
    """{"kind": "month", "months": [m]} or {"kind": "range", "months": [...]} or None.
    `current` is the user's current month (facts.current_month)."""
    text = normalize(text)
    if current is None:
        return None
    m = RANGE.search(text)
    if m:
        token = next((g for g in m.groups() if g), None)
        n = _count(token) if token else 2
        if n >= 1:
            return {"kind": "range", "months": months_back(current, n)}
    m = AGO.search(text)
    if m:
        token = next((g for g in m.groups() if g), None)
        n = _count(token) if token else 2
        return {"kind": "month", "months": [shift_month(current, -n)]}
    if LAST_MONTH.search(text):
        return {"kind": "month", "months": [shift_month(current, -1)]}
    m = ISO_MONTH.search(text)
    if m:
        return {"kind": "month", "months": [f"{m.group(1)}-{m.group(2)}"]}
    m = NAMED_MONTH.search(text)
    if m and not (m.group(1) == "may" and not m.group(2)):     # "may" is usually the verb
        mon = MONTH_NAMES[m.group(1)]
        year = int(m.group(2)) if m.group(2) else int(current[:4])
        if not m.group(2) and mon > int(current[5:7]):
            year -= 1                                        # "in December" = the last one
        return {"kind": "month", "months": [f"{year:04d}-{mon:02d}"]}
    if ALL_TIME.search(text):
        return {"kind": "all", "months": []}
    if THIS_MONTH.search(text):
        return {"kind": "month", "months": [current]}
    return None


def periods_in(text, current):
    """Every month the text names, in order ("this month than two months ago")."""
    text = normalize(text)
    found = []
    for regex, month in ((THIS_MONTH, lambda m: current),
                         (LAST_MONTH, lambda m: shift_month(current, -1)),
                         (AGO, lambda m: shift_month(current, -_count(next((g for g in m.groups() if g), "2")))),
                         (ISO_MONTH, lambda m: f"{m.group(1)}-{m.group(2)}")):
        for m in regex.finditer(text):
            found.append((m.start(), month(m)))
    for m in NAMED_MONTH.finditer(text):
        if m.group(1) == "may" and not m.group(2):
            continue
        mon = MONTH_NAMES[m.group(1)]
        year = int(m.group(2)) if m.group(2) else int(current[:4]) - (mon > int(current[5:7]))
        found.append((m.start(), f"{year:04d}-{mon:02d}"))
    out = []
    for _, month in sorted(found):
        if month not in out:
            out.append(month)
    return out


# ---------- amounts ----------
AMOUNT = re.compile(r"(?<![\w.,-])(\d[\d,]*(?:\.\d+)?)(?!\s*-\d)(\s*%|\s*(?:percent|في المئه|بالمئه))?")


# "15 الف", "10 آلاف", "15k", "15 thousand": the number is in thousands
THOUSANDS = re.compile(r"\s*(?:k(?![a-z])|thousand\b|(?:ال)?(?:الف|الاف)(?![ء-ي]))")


def amounts(text):
    """[(value, is_percent, position)] — years and ISO months are skipped."""
    text = normalize(text)
    out = []
    for m in AMOUNT.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if not m.group(2) and THOUSANDS.match(text, m.end()):
            value *= 1000
        elif re.fullmatch(r"20\d\d", m.group(1)) and not m.group(2):     # a year; "2,000" is an amount
            continue
        out.append((value, bool(m.group(2)), m.start()))
    return out


# ---------- what-if changes ----------
# Arabic verbs may carry an object pronoun ("زودها" = raise it, "قللتها" = I cut it).
_PRONOUN = r"(?:ها|ه|هم|تها|ته|تهم)?"
UP = re.compile(r"\b(?:increas\w*|rais\w*|rise\w*|rises|rose|goes up|go up|went up|grow\w*|more|higher|"
                r"double\w*|add\w*|extra|additional|up)\b|"
                + _ar("(?:زاد|زادت|يزيد|تزيد|ارتفع|ارتفعت|يرتفع|ترتفع|زياده|زود|زودت|ازود|يزود|تزود|"
                      r"اكثر|اضفت|اضاف|ضاعفت|ضاعف|اضافي|اضافيه)" + _PRONOUN))
DOWN = re.compile(r"\b(?:decreas\w*|reduc\w*|cut\w*|lower\w*|drop\w*|fall\w*|fell|falls|goes down|go down|"
                  r"went down|less|shrink\w*|trim\w*|halve\w*|down)\b|"
                  + _ar("(?:قل|قلت|قللت|اقلل|قلل|نقص|نقصت|انقص|نقصنا|انخفض|انخفضت|خفض|خفضت|خفضنا|اخفض|تقليل|"
                        r"تخفيض|اقل)" + _PRONOUN))
TO = re.compile(r"(?:\bto\b|\bat\b|\bbecomes?\b|\bbecame\b|\bis\b|\bare\b|=|الي|ليصبح|لتصبح|تصبح|يصبح|ليكون|"
                r"لتكون)\s*$")
# "keep my expenses at 300 only": a new total, not a change of 300.
SET_WORD = re.compile(r"\b(?:only|in total|total of|altogether)\b|(?<![ء-ي])(?:بس|فقط|اجمالي|خليت|خلي|اخلي|نخلي|"
                      r"تكون|يكون|تبقي|يبقي)(?![ء-ي])")
# "spend 300 of what is left": 300 on top of the current spending.
FROM_LEFT = re.compile(r"\bfrom (?:what(?:'s| is) left|the remaining|the rest|my savings|the leftover)\b|"
                       r"من (?:ال)?(?:متبقي|باقي|فايض|مدخر|اللي (?:فاضل|باقي|متبقي))")
# "cut restaurants by half", "double my savings"
FRACTIONS = {"نص": 0.5, "نصف": 0.5, "half": 0.5, "ربع": 0.25, "quarter": 0.25, "ثلث": 1 / 3, "third": 1 / 3}
FRACTION = re.compile(r"(\bto\b\s*(?:a\s*|the\s*)?|(?<![ء-ي])(?:الي\s*ال|ل|لل))?(?:ال)?"
                      r"(نصف|نص|ربع|ثلث|half|quarter|third)(?![ء-ي\w])")
STOP = re.compile(r"\b(?:completely|entirely|altogether|to zero|stop\w*|quit|cut out|give up)\b|"
                  r"(?<![ء-ي])(?:تماما|نهائيا|خالص|كليا|اوقف|وقفت|بطلت|ابطل|الغيت|الغي)(?![ء-ي])")
DOUBLE = re.compile(r"\bdouble\w*\b|(?<![ء-ي])(?:ضاعف|ضاعفت|اضاعف|الضعف|ضعف)")
BY = re.compile(r"(?:\bby\b|بمقدار|بقيمه|ب)\s*$")
INCOME_WORD = re.compile(r"\b(?:income|salary|pay|earn|earns|earning|earnings|wages?|bonus)\b|دخل|راتب|مرتب|علاوه|حافز")
BONUS = re.compile(r"\b(?:bonus|extra income|side income|raise)\b|علاوه|حافز|دخل اضافي")
# Spending as a whole: "my expenses go up 15%", "I have to spend 1,000 more".
EXPENSE_WORD = re.compile(r"\b(?:expenses?|expenditure|spending|spend|spent|costs?|outgoings)\b|"
                          r"مصروف|مصاريف|انفاق|اصرف|نصرف|يصرف|صرف|صرفت|انفق|انفقت")
EXTRA_SAVING = re.compile(r"\b(?:save|savings?|put aside|set aside|put away)\b|ادخر|ادخار|اوفر|توفير|وفرت|احوش|حوشت")
EXTRA_WORD = re.compile(r"\b(?:extra|more|additional|another|on top)\b|اضافي|اضافيه|زياده|اكثر|اخري|كمان")
# The part of a what-if that asks for the result ("..., how much will I save?"):
# its words name what to report, not what changes.
CONDITION = re.compile(r"\b(?:what if|if|suppose|assum\w*|imagine)\b|(?<![ء-ي])(?:لو|اذا|ان|افترض|بافتراض|نفترض)(?![ء-ي])")
ASK_TAIL = re.compile(r"(?:^|[\s,،؛;]+)(?:so |then |ف|و)?(?:how much|how many|what (?:will|would|is|are)|when|"
                      r"(?<![ء-ي])(?:كم|قديش|متي|هل|ماذا سيحدث|ايش|شنو)(?![ء-ي]))")
# A comma between digits ("6,600") is a thousands separator, never a clause break.
CLAUSE_SPLIT = re.compile(r"(?<!\d),|,(?!\d{3}(?!\d))|;|\band\b|\bwhile\b|\bbut\b|،| و | ثم |\bthen\b")
KEEP_SAME = re.compile(r"\b(?:unchanged|same|stays?|remains?|constant|as (?:it|they) (?:is|are))\b|"
                       r"كما هي|كما هو|ثابت|ثابته|بدون تغيير|نفس")


@dataclass
class Change:
    target: str | None           # "category", "expenses" (all spending), "income", "saving",
                                 # or None: "raise it 15%" names no variable (taken from the
                                 # conversation, or the user is asked)
    mode: str                    # "set", "add" (signed amount) or "pct" (signed percent);
                                 # for a saving also "share" (save 10% of the income);
                                 # "ask": no amount given ("if I spent less?"), value = ±1;
                                 # "unclear": "if I spend 300": 300 more, or 300 in total?
    value: float
    category: str | None = None
    subcategory: str | None = None


def _target(clause, anchor):
    """(target, category, subcategory) a clause changes: a category when one is
    named ("transport expenses"), else the income / spending / saving word nearest
    the change verb or amount, so "if my spending rises 300, how much will I save?"
    changes spending, not saving."""
    cats = find_categories(clause)
    if cats:
        _, category, sub = cats[0]
        return "category", category, sub
    found = [(abs(m.start() - anchor), name) for name, regex in
             (("saving", EXTRA_SAVING), ("income", INCOME_WORD), ("expenses", EXPENSE_WORD))
             for m in regex.finditer(clause)]
    if not found:
        return None
    return min(found)[1], None, None


def _fraction(clause, direction):
    """"to half" / "by half" / "double" as a signed percentage, or None."""
    if DOUBLE.search(clause):
        return 100.0
    if STOP.search(clause):
        return -100.0                                # "cut fast food completely": to 0
    m = FRACTION.search(clause)
    if not m:
        return None
    f = FRACTIONS[m.group(2)]
    if m.group(1):                                   # "to half": the new amount is half
        return round((f - 1) * 100, 6)
    return round(direction * f * 100, 6)             # "by half": half of it, up or down


def _clause_change(clause):
    """One change from one clause, or None."""
    found = amounts(clause)
    up, down = UP.search(clause), DOWN.search(clause)
    if KEEP_SAME.search(clause) and not found:
        return None
    if up and down:                 # both words: the one that comes first decides
        direction = 1 if up.start() < down.start() else -1
    else:
        direction = -1 if down else 1
    verb = (up or down)
    fraction = None
    if found:
        value, is_pct, pos = found[0]
    else:
        fraction = _fraction(clause, direction)
        value, is_pct, pos = (fraction, True, (verb.start() if verb else 0)) if fraction is not None else \
            (None, False, verb.start() if verb else 0)
    before = clause[:pos]
    anchor = verb.start() if verb else pos
    picked = _target(clause, anchor)
    if picked is None:
        if (up or down) and not KEEP_SAME.search(clause) and value is not None:
            if fraction is not None:
                return Change(None, "pct", fraction)
            return Change(None, "pct" if is_pct else "add", direction * value)   # "raise it by 15%"
        return None
    target, category, sub = picked
    if value is None:
        # "if I spent less, how much would I save?": the variable is clear, the amount is not
        return Change(target, "ask", float(direction), category, sub) if (up or down) else None
    if fraction is not None:
        if target == "saving":
            return Change("saving", "unclear", 0.0)          # "double my savings": of what, exactly?
        return Change(target, "pct", fraction, category, sub)   # already signed: "to half" is -50%
    if target == "saving":
        if not (up or down or EXTRA_WORD.search(clause)):
            # "save 2,000 a month": the monthly saving IS 2,000 (out of what is left),
            # not 2,000 on top of everything left over.
            return Change("saving", "share" if is_pct else "set", value)
        sign = 1 if direction > 0 or EXTRA_WORD.search(clause) else -1
        return Change("saving", "pct" if is_pct else "add", value * sign)
    if target == "income" and BONUS.search(clause) and not is_pct and not TO.search(before):
        return Change("income", "add", direction * value)      # a bonus comes on top of the income
    if is_pct:
        return Change(target, "pct", direction * value if (up or down) or value < 0 else value, category, sub)
    if TO.search(before) and not BY.search(before):
        return Change(target, "set", value, category, sub)
    if BY.search(before) or up or down:
        return Change(target, "add", direction * value, category, sub)
    if FROM_LEFT.search(clause):
        return Change(target, "add", value, category, sub)    # "spend 300 of what is left"
    if target == "expenses" and not SET_WORD.search(clause):
        return Change(target, "unclear", value)               # "if I spend 300": more, or in total?
    # "spend 500 on transport" / "transport costs 400" / "expenses 300 only": the new amount
    return Change(target, "set", value, category, sub)


def condition_part(text):
    """The conditions of a what-if without the question about the result:
    "if my spending rises 300 and income stays, how much will I save?" ->
    "if my spending rises 300 and income stays"."""
    cond = CONDITION.search(text)
    start = cond.start() if cond else 0
    tail = ASK_TAIL.search(text, start + (len(cond.group(0)) if cond else 0))
    if cond and tail is None:
        return text[start:]
    if tail is not None and tail.start() > start:
        return text[start:tail.start()]
    if cond:                        # "how much will I save if my spending rises 300?"
        return text[start:]
    return text


def _starts_new_clause(word):
    """"والدخل زاد 500": a word after "و" that names a variable or a change."""
    return bool(word) and (INCOME_WORD.search(word) or EXPENSE_WORD.search(word) or EXTRA_SAVING.search(word)
                           or UP.search(word) or DOWN.search(word) or find_categories(word)
                           or word in ("كان", "كانت", "كانو", "مع", "خلي", "خليت"))


def clauses(text):
    out = []
    for piece in CLAUSE_SPLIT.split(text):
        current = []
        for word in piece.split(" "):
            if current and word.startswith("و") and len(word) > 2 and _starts_new_clause(word[1:]):
                out.append(" ".join(current))
                current = [word[1:]]
            else:
                current.append(word)
        out.append(" ".join(current))
    return [c for c in out if c.strip()]


# "keep (all) other expenses unchanged": a constraint on the rest, not the Others category
REST_UNCHANGED = re.compile(r"\b(?:keep|leave)\s+(?:all\s+|the\s+)?(?:other|rest|remaining|everything else)\b[^,.;،]*|"
                            r"\b(?:everything|all) else\b[^,.;،]*|\bothers? (?:unchanged|the same)\b|"
                            r"\ball (?:the )?other (?:expenses|spending|costs|categories|things)\b[^,.;،]*|"
                            r"(?:باقي|بقيه|الباقي)\s+(?:ال)?(?:مصروفات|مصاريف)?[^،,.]*(?:زي ما هي|كما هي|ثابت\w*|بدون تغيير)|"
                            r"خلي (?:ال)?باقي[^،,.]*")


def without_rest_unchanged(text):
    return REST_UNCHANGED.sub(" ", text)


def _has_value(clause):
    return bool(amounts(clause)) or _fraction(clause, 1) is not None


def _direction(clause):
    up, down = UP.search(clause), DOWN.search(clause)
    if not (up or down):
        return 0
    if up and down:
        return 1 if up.start() < down.start() else -1
    return -1 if down else 1


def scenario_changes(text):
    """Every change a what-if names:
    "transport increases to 400 and food drops by 200" -> two changes;
    "reduce restaurants and fast food by 15%" -> the same 15% cut on BOTH: a
    clause that only names categories waits for the amount of the next clause
    (unless that clause goes the other way), and every category in the group
    gets the change."""
    text = without_rest_unchanged(condition_part(normalize(text)))
    out, pending = [], []

    def emit(group):
        joined = " ".join(group)
        change = _clause_change(joined)
        if change is None:
            return
        named = find_categories(joined)
        if change.target == "category" and len(named) > 1:
            for _, category, sub in named:
                out.append(Change("category", change.mode, change.value, category, sub))
        else:
            out.append(change)

    for clause in clauses(text):
        if not _has_value(clause) and not KEEP_SAME.search(clause) and (
                find_categories(clause) or (_direction(clause) and _target(clause, 0) is None)):
            pending.append(clause)          # "reduce restaurants" / "reduce": the amount comes next
            continue
        if pending and _direction(clause) and _direction(pending[0]) and \
                _direction(clause) != _direction(pending[0]):
            for p in pending:                    # "less on food and 200 more on transport"
                emit([p])
            pending = []
        emit(pending + [clause])
        pending = []
    if pending:
        emit(pending)                            # categories that never got an amount: "by how much?"
    return out


def conflicting(changes):
    """Two different values for the same thing ("transport to 400 ... transport to 500"):
    the user has to say which one they mean."""
    seen = {}
    for c in changes:
        key = (c.target, c.category, c.subcategory)
        if c.target in ("category", "income", "expenses") and c.mode == "set":
            if key in seen and seen[key] != c.value:
                return key
            seen[key] = c.value
    return None


# ---------- "I save X of my income Y": the split, as percentages ----------
RATE_WORD = re.compile(r"\b(?:percent\w*|rate|ratio|share|proportion)\b|%|نسبه|نسب|معدل")
SPEND_REST = re.compile(r"\b(?:spend|spent|spending)\b.*\b(?:rest|remainder|remaining|left)\b|"
                        r"(?:صرفت|اصرف|انفقت|انفق) (?:ال)?(?:باقي|متبقي)|الباقي|نسبه الصرف|نسبه الانفاق")


def split_amounts(text):
    """(income, saving) a "save X out of income Y" question states; either may be None."""
    text = normalize(text)
    income = saving = None
    for value, is_pct, pos in amounts(text):
        if is_pct:
            continue
        before = text[max(0, pos - 30):pos]
        if INCOME_WORD.search(before) and income is None and not EXTRA_SAVING.search(before[-12:]):
            income = value
        elif EXTRA_SAVING.search(before) and saving is None:
            saving = value
    return income, saving


# ---------- intents ----------
PERSONAL = re.compile(r"\b(?:my|me|i|i'm|i've|am i|did i|have i|do i|mine)\b|"
                      r"صرفت|انفقت|دخلي|راتبي|هدفي|مصروفاتي|مصاريفي|انفاقي|مدخراتي|ادخرت|وفرت|ميزانيتي|"
                      r"عندي|لدي|اصرف|انفق|احقق|اصل|ادخر|استطيع|اقدر|صرفي|مصروفي|ادخاري|اهدافي|عملياتي|"
                      r"معاملاتي|مرتبي|حاوفر|حادخر|ساوفر|سادخر|اوفر|(?<![ء-ي])انا(?![ء-ي])|بصرف|بتاعتي|بتاعي|"
                      r"حقي|حقتي|خاصتي|بيجيني|يجيني|قروشي|فلوسي")
# ---------- what a "how much" question asks for, part by part ----------
# "How much did I spend, and how much of my income is left?" asks for two things;
# the word "income" in it must not turn it into an income question.
ASKS_INCOME = re.compile(r"\b(?:income|earn\w*|salary)\b|دخلي|دخل|راتبي|مرتبي")
ASKS_SPENT = re.compile(r"\b(?:spen[dt]\w*|expenses?|expenditure)\b|صرفت|انفقت|مصروفاتي|مصاريفي|مصروفات|مصاريف|"
                        r"انفاقي|الانفاق|مصروفي|صرفي")
ASKS_LEFT = re.compile(r"\b(?:left|remain\w*|balance|leftover)\b|المتبقي|متبقي|الباقي|باقي|يتبقي|تبقي|حيتبقي|"
                       r"سيتبقي|فاضل|يفضل|حيفضل|بقي لي|بقي معي")
ASKS = re.compile(r"\bhow much\b|\bwhat(?:'s| is| was| are)\b|\btotal\b|\bsummary\b|"
                  r"(?<![ء-ي])(?:و|ف)?كم(?![ء-ي])|اجمالي|مجموع|ما هو|ملخص|قديش|قد شنو|قدر شنو|قدر ايه|بكم|"
                  r"الفرق بين|عايز اعرف|عاوز اعرف|ابغي اعرف|اريد ان اعرف|اريد معرفه|قولي|قول لي|\btell me\b|"
                  r"وصلت كم|بقت كم|بقي كم")
# "Show me the October transactions", "وريني تفاصيلها"
LIST_WORDS = (r"(?:transactions?|records?|entries|purchases|details|line items|in detail|itemi[sz]ed|"
              r"عمليات|العمليات|معاملات|المعاملات|سجلات|السجلات|تفاصيل|التفاصيل|تفاصيلها|تفاصيله|بالتفصيل)")
TRANSACTIONS = re.compile(
    rf"\b(?:show|list|display|see|give)\b.*\b{LIST_WORDS}|\b(?:itemi[sz]e|breakdown of (?:each|every))\b|"
    rf"(?:اعرض|اعرضي|عرض|وريني|ورني|ارني|ارينى|اظهر|اظهرلي|طلع|جيب|اعطني|عايز اشوف|ابغي اشوف|اريد ان اري|اريد)"
    rf"(?: لي| ليا)?.*(?:{LIST_WORDS}|مصاريف|مصروفات|المصاريف|المصروفات)|^(?:و|طيب )?{LIST_WORDS}")
# A request to explain where an earlier number came from.
EXPLAIN_SOURCE = re.compile(
    r"\bwhere (?:did|does|do) (?:you get|that|this|the|these)\b|\bhow did you get\b|\bsource of (?:the|that|this)\b|"
    r"من وين جبت|من فين جبت|من اين جبت|من اين اتيت|منين جبت|جبت (?:الرقم|رقم|هذا الرقم|ده)|مصدر (?:هذا |ده )?الرقم|"
    r"كيف طلع|من وين طلع|طلع منين")


def asked_parts(text):
    """{"income", "spent", "left"}: the values a question asks for."""
    text = normalize(text)
    parts = set()
    if ASKS_LEFT.search(text):
        parts.add("left")
    if ASKS_SPENT.search(text):
        parts.add("spent")
    if ASKS_INCOME.search(text):
        parts.add("income")
    return parts


# "What are my goals?", "how much have I saved for each goal?", "which goal is closest?"
GOALS_ALL = re.compile(
    r"\b(?:my|all|each|every|which|the) (?:\w+ )?goals\b|\bwhich goal\b|\beach goal\b|\bgoals? (?:list|overview)\b|"
    r"(?<![ء-ي])(?:اهدافي|الاهداف|اهداف)(?![ء-ي])|كل هدف|لكل هدف|(?<![ء-ي])اي هدف")
# "What is 15% of my income?"
PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:من|of)\s+(?:my |the )?(.{2,30})")
# "How much will I save next month?" — a forecast, not this month's records.
FORECAST_SAVE = re.compile(r"\b(?:save|saving|savings|left|remain\w*|spend|spending)\b|"
                           r"اوفر|ادخر|يفضل|حيفضل|يتبقي|حيتبقي|اصرف|حاصرف|ساصرف|ادخار|توفير|المتبقي")
ANOMALY = re.compile(r"\b(?:unusual|abnormal|anomal\w*|strange|odd|spikes?|out of the ordinary|weird)\b|"
                     r"غير طبيعي|غير عادي|غريب|شاذ|مفاجئ|ملفت|مش طبيعي")
TREND = re.compile(r"\btrends?\b|\bover time\b|\bmonth by month\b|\bhow (?:has|have) my (?:spending|expenses) "
                   r"(?:changed|moved)|(?<![ء-ي])(?:اتجاه|تطور|تطورات)(?![ء-ي])|شهر بشهر|كيف تغير")
LARGEST = re.compile(r"\b(?:largest|biggest|most expensive|highest|top)\b.{0,20}\b(?:transaction|purchase|payment|"
                     r"single expense)s?\b|(?:اكبر|اغلي|اعلي) (?:عمليه|معامله|مشتريات|عمليات|دفعه|شي|حاجه)")
# "How much did I spend on X?": the X after the spending word (a period is not a thing)
SPENT_ON = re.compile(r"(?:\bspen[dt]\w*|صرفت|انفقت|بصرف|اصرف|دفعت)\s+(?:\w+\s+)?(?:on|for|علي|في|عشان)\s+"
                      r"(?!(?:this|last|the|that|it|them|average|each|every|الشهر|شهر|هذا|ده|هذه|كل|متوسط)\b)"
                      r"(?!" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")([^\s؟?,،.]{2,})")
# "How am I doing?", "ايه رأيك في وضعي المالي؟"
OVERVIEW = re.compile(r"\bhow am i doing\b|\bmy (?:financial|money) (?:situation|health|status|position|picture)\b|"
                      r"\b(?:financial )?(?:overview|health check)\b|\bhow (?:are|is) my finances\b|"
                      r"وضعي المالي|حالتي الماليه|وضعي (?:كيف|كويس)|كيف وضعي|رايك في وضعي|تقييم وضعي|ملخص وضعي")
# "Is my saving rate good?": a judgement, against the guideline in the knowledge base
JUDGE = re.compile(r"\b(?:good|ok(?:ay)?|enough|healthy|reasonable|fine|normal|bad|low|high|too much|a lot|"
                   r"too high|overspend\w*)\b|كويس|كويسه|جيد|جيده|مناسب|مناسبه|كافي|كافيه|معقول|معقوله|ضعيف|ضعيفه|"
                   r"منخفض|عالي|تمام|زين|كتير|كثير|زياده عن اللزوم|مبالغ")
# "analyze my food expenses", "حلل مصاريف الأكل": an instruction about the user's own records
ANALYZE_WORD = re.compile(r"\banaly[sz]\w*|\bbreak ?down\b|\breview\b|(?<![ء-ي])(?:حلل|حللي|تحليل|راجع|فصل|تفصيل)(?![ء-ي])")
DAILY = re.compile(r"\b(?:daily|per day|a day|each day)\b|يومي|اليومي|في اليوم|باليوم|كل يوم")
WEEKLY = re.compile(r"\b(?:weekly|per week|a week|each week)\b|اسبوعي|الاسبوعي|في الاسبوع|بالاسبوع|كل اسبوع")


INTENTS = [
    ("transactions", None),   # a list of the records themselves (below)
    ("explain", re.compile(
        r"\bhow (?:did|do|have) you (?:calculate|get|work|compute|figure|arrive|come)|"
        r"\bshow (?:me )?(?:how|the (?:calculation|math|maths|formula|working)|your (?:work|math|maths|calculation))|"
        r"\bexplain (?:the|your|this|that) (?:calculation|math|maths|number|figure|result)|"
        r"\bwhere (?:did|does|do) (?:that|this|these|the) (?:number|figure|amount|numbers)s? come from|"
        r"\bwhat (?:formula|calculation) did you use|"
        r"كيف حسبت|كيف وصلت|اشرح (?:لي )?(?:الحساب|الحسبه|طريقه الحساب)|وضح (?:لي )?(?:الحساب|الحسبه)|"
        r"من اين (?:جاء|جاءت|اتي)|ما المعادله|" + EXPLAIN_SOURCE.pattern)),
    ("interest", re.compile(r"\binterest\b|فائده|الفائده|فوائد")),
    ("split", None),          # "of my 6,600 income, if I save 1,500: what are the percentages?"
    ("what_if", re.compile(
        r"\bwhat if\b|\bsuppose\b|\bassum\w*|\bimagine\b|\bhypothetical\w*|\bwhat happens if\b|"
        r"\bif (?:i|my|the|we)\b.*\b(?:next month|instead|each month|every month|per month|a month)\b|"
        r"\bif i (?:have to|had to|must|need to) (?:spend|pay)\b|"
        r"\bif (?:i|my|the|we|our)\b|\bwhat happens\b.*\bif\b|"
        # An instruction to change a number ("increase my expenses by 15%") is a what-if
        # too: the chat never edits saved records.
        r"^(?:please )?(?:increase|raise|reduce|decrease|lower|cut|add|double|halve|save|put aside)\b|"
        r"^(?:طيب )?(?:زود|زودها|زوده|قلل|قللها|قلله|خفض|خفضها|اخفض|ارفع|ضاعف)|"
        r"ماذا لو|لو |افترض|بافتراض|نفترض|فرضا|اذا (?:زاد|زادت|قل|قلت|خفضت|ادخرت|وفرت|ارتفع|ارتفعت|انخفض|"
        r"اضطريت|اضطررت|صرفت)")),
    ("percent_of", PERCENT_OF),   # after what-if: "what if I save 10% of my income?" is a what-if
    ("forecast", None),       # "how much will I save next month?"
    ("goal_timing", re.compile(
        r"\bhow (?:much|many months?) (?:sooner|faster|earlier|quicker)|\bhow (?:much )?(?:sooner|faster|earlier)\b|"
        r"\bwhen (?:will|would|could|can) i (?:reach|hit|finish|get|achieve|complete)|"
        r"\bhow long (?:will|would) it take|\bhow many months (?:will|would|until|to)|"
        r"كم شهر|متي (?:سا|ا|س)?(?:صل|حقق)|متي ساحقق|كم (?:اسرع|ابكر)|كم من الوقت")),
    ("why", re.compile(
        r"\bwhy\b.*\b(?:spend\w*|spent|expenses?|costs?|more|increas\w*|higher|up|rise|rose)\b|"
        r"\bwhat (?:made|caused|is causing|drove|drives) (?:my )?(?:spending|expenses|costs)|"
        r"لماذا|ليش|(?<![ء-ي])ليه(?![ء-ي])|ما سبب|سبب (?:زياده|ارتفاع)")),
    ("compare", re.compile(
        r"\bcompar\w*|\bvs\.?\b|\bversus\b|\bdifference between\b|"
        r"\b(?:more|less) (?:did i spend |have i spent |i spent |was spent )?(?:this month )?than\b|"
        r"\b(?:increas\w*|ris\w*|rose|went up|go up|chang\w*|drop\w*|fell|decreas\w*) (?:\w+ )?(?:from|since|over|"
        r"compared (?:to|with)) (?:last|the previous|previous|\w+ber|\w+ary|march|april|may|june|july|august)\b|"
        r"قارن|مقارنه|الفرق بين|اكثر من الشهر|اقل من الشهر|"
        # "كم زادت مصاريفي عن الشهر اللي فات؟": how much more than another month
        r"(?<![ء-ي])(?:زاد|زادت|نقص|نقصت|قل|قلت|ارتفع|ارتفعت|انخفض|انخفضت)(?![ء-ي]).{0,25}(?<![ء-ي])عن(?![ء-ي])")),
    ("trend", TREND),
    ("anomalies", ANOMALY),
    ("summary", None),        # income, spending and what is left, together (below)
    ("goals", GOALS_ALL),
    ("overview", OVERVIEW),
    ("budget", re.compile(
        r"\bbudget\b.*\b(?:left|remain\w*|over|under|status|used|spent|stand|how much|on track|exceed\w*)\b|"
        r"\b(?:left|remain\w*|over|exceed\w*)\b.*\bbudget\b|ميزاني|الميزانيه")),
    ("top", re.compile(
        r"\b(?:top|biggest|largest|highest|main|greatest|most expensive) (?:expense|spending|cost|category|categories)|"
        r"\bspend (?:the )?most\b|\bmost of my money\b|\bwhere (?:does|did) (?:most of )?my money go|"
        r"اكبر مصروف|اكبر مصاريف|اعلي مصروف|اكثر شيء اصرف|اين اصرف|اين يذهب|اكبر فئه|اكثر فئه|"
        r"(?:اكتر|اكثر) (?:حاجه|شي|شيء|بند) (?:ب|ا|ن)?(?:صرف|بصرف)|وين بتمشي قروشي|وين بيروح (?:مرتبي|دخلي|قروشي)")),
    ("average", re.compile(r"\baverage\b|\bon average\b|\bper month on average\b|متوسط|معدل (?:انفاقي|مصروفاتي|صرفي)")),
    ("goal", re.compile(
        r"\b(?:goal|target)s?\b.*\b(?:left|remain\w*|need|short|reach|on time|on track|progress|how much|"
        r"how far|achiev\w*|feasible|realistic|possible)\b|"
        r"\b(?:left|remain\w*|needed|missing|short)\b.*\b(?:goal|target)s?\b|"
        r"\b(?:reach|achieve|hit|make) (?:my|the|it|that)\b|\bon time\b|\bon track\b|\bshort of\b|"
        r"\bhow far (?:am i|are we|is it) from\b|\bhow much (?:more )?do i (?:still )?need\b|"
        r"\bhow much (?:more )?do i need to save\b|\bneed to save (?:each|every|per|a) month\b|"
        r"\bhow much (?:should|must|do) i (?:save|put aside|set aside) (?:each|every|per|a) month\b|"
        r"\b(?:required|needed) monthly (?:saving|contribution)s?\b|"
        r"كم (?:يجب ان|لازم|المفروض|محتاج) (?:ادخر|اوفر|احوش)|المطلوب شهريا|"
        r"هدف|احقق|اصل الي|في الموعد|في الوقت")),
    ("savings", re.compile(
        r"\bsaving(?:s)? rate\b|\brate of saving\b|\bhow much (?:did|have|do|can) i (?:save|saved|put aside)|"
        r"\bhow much (?:money )?(?:is|do i have) left\b|\bleft ?over\b|\bremaining balance\b|\bmy balance\b|"
        r"\bmy savings\b|\bhave i saved\b|\bdid i save\b|\bi saved\b|"
        r"\bhow much (?:will|would|is going to) (?:be )?(?:remain|be left|left)\b|"
        r"\bwhat percent(?:age)? (?:of my income )?(?:do|did) i save\b|"
        r"ادخرت|وفرت|معدل الادخار|نسبه الادخار|مدخراتي|الرصيد المتبقي|نسبه ادخاري|معدل ادخاري|نسبه التوفير|"
        r"كم (?:تبقي|بقي|يتبقي|حيتبقي|سيتبقي|هيتبقي|حيفضل|سيفضل|يفضل|فضل) (?:لي|معي|من دخلي)")),
    ("income", re.compile(
        r"\b(?:my|monthly|total) (?:\w+ )?(?:income|salary|earnings|pay)\b|\bhow much (?:do|did) i (?:earn|make|get paid)\b|"
        r"\bincome\b.*\bmy\b|دخلي|راتبي|مرتبي|كم (?:اكسب|ادخل|دخلي)|بيجيني|يجيني|بجيني|بتجيني|ماهيتي")),
    ("category", None),       # a category named next to a spending word (below)
    ("total", re.compile(
        r"\bhow much (?:did|have|do) i (?:spend|spent)|\btotal (?:spending|expenses|expenditure)|"
        r"\bmy (?:total )?(?:spending|expenses|expenditure)\b|\bhow much (?:money )?(?:went out|have i used)|"
        r"\bwhat did i spend\b|كم صرفت|كم انفقت|مجموع (?:مصروفاتي|مصاريفي|انفاقي)|اجمالي (?:مصروفاتي|مصاريفي|انفاقي|الانفاق|المصروفات)|"
        r"مصروفاتي|مصاريفي|انفاقي")),
]
SPEND_WORD = re.compile(r"\b(?:spen[dt]\w*|spending|expenses?|cost\w*|paid|pay|went|go(?:es)? to|share|percent\w*|"
                        r"portion|part)\b|%|صرفت|انفقت|اصرف|بصرف|بنصرف|مصروف|مصاريف|انفاق|دفعت|نسبه|حصه")
# "How much will remain?" is about the user's own money even without "my".
LEFT_OVER = re.compile(r"\bhow much (?:will|would|is going to) (?:be )?(?:remain|be left|left)\b|"
                       r"كم (?:حيتبقي|سيتبقي|هيتبقي|حيفضل|سيفضل)")
# A question about the future of the actual data ("how much will I spend next
# month?") is a forecast: it is not answered with this month's actual numbers.
FUTURE = re.compile(r"\bnext (?:month|year|quarter)\b|\bin the coming months\b|\bforecast\w*|\bpredict\w*|"
                    r"الشهر (?:الجاي|القادم|المقبل|الجديد)|السنه (?:الجايه|القادمه)|توقع|تنبؤ")
FOLLOW_UP_ONLY = re.compile(r"^(?:and|what about|how about|ok(?:ay)?,? (?:and|what about)|same for|for)\b|"
                            r"^(?:و|وماذا عن|ماذا عن|وكم|طيب و|وبالنسبه ل)")


@dataclass
class Query:
    question: str
    text: str                    # normalized
    intent: str | None
    language: str
    period: dict | None = None
    months: list = field(default_factory=list)       # every month named, in order
    categories: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    follow_up: bool = False
    needs_amount: bool = False   # "and if I cut it?": a what-if without the amount
    from_context: bool = False   # "raise it": the variable was taken from the conversation
    followup_note: str | None = None   # how a scenario follow-up was understood, shown first
    together: bool = False       # "cut restaurants and fast food by 300 together": one amount shared


def detect(question, current, language="en"):
    """Read one question. `current` is the user's current month (or None)."""
    text = normalize(question)
    q = Query(question, text, None, language_of(question, language),
              period=find_period(text, current), months=periods_in(text, current) if current else [],
              categories=[(c, s) for _, c, s in find_categories(without_rest_unchanged(text))])
    q.follow_up = bool(FOLLOW_UP_ONLY.search(text)) and len(text.split()) <= 8
    for name, regex in INTENTS:
        if name == "split":
            income, saving = split_amounts(text)
            if saving is not None and RATE_WORD.search(text) and (income is not None or SPEND_REST.search(text)):
                q.intent = "split"
                break
            continue
        if name == "forecast":
            if FUTURE.search(text) and FORECAST_SAVE.search(text) and not amounts(text):
                q.intent = "forecast"
                break
            continue
        if name == "transactions":
            if TRANSACTIONS.search(text) or LARGEST.search(text):
                q.intent = "transactions"
                break
            continue
        if name == "summary":
            parts = asked_parts(text)
            goalish = re.search(r"\b(?:goal|target|budget)s?\b|هدف|ميزاني", text)
            if parts == {"spent"} and ASKS.search(text) and PERSONAL.search(text) and not goalish \
                    and not q.categories and not re.search(r"\b(?:average|most|biggest|top)\b|متوسط|اكبر|اكثر", text):
                q.intent = "total"          # "الشهر ده صرفت كم؟": the total, whatever the word order
                break
            if not goalish and (ASKS.search(text) or len(text.split()) <= 4) and (
                    ("left" in parts and (len(parts) > 1 or len(text.split()) <= 5 or PERSONAL.search(text)))
                    or {"income", "spent"} <= parts):
                q.intent = "summary"
                break
            continue
        if name == "compare" and regex.search(text) and {"income", "spent"} <= asked_parts(text) \
                and len(q.months) < 2:
            continue                # "the difference between my income and my expenses" is what is left
        if name == "category":
            if q.categories and SPEND_WORD.search(text) and (PERSONAL.search(text) or ANALYZE_WORD.search(text)):
                q.intent = "category"
                break
            continue
        if regex.search(text):
            if name == "what_if":
                q.changes = scenario_changes(text)
                if not q.changes:
                    if (UP.search(text) or DOWN.search(text)) and not amounts(text) and len(text.split()) <= 6:
                        q.needs_amount = True       # "and if I reduce it?": ask how much
                    else:
                        continue        # "what if I do that?" — the model handles it
            if name == "interest" and not (amounts(text) and "%" in text or "percent" in text):
                continue
            if name in ("budget", "average", "savings", "income", "total", "why", "trend") \
                    and not PERSONAL.search(text) and not (name == "savings" and LEFT_OVER.search(text)):
                continue                # a general question about the topic
            if name in ("total", "category", "top", "average", "income", "savings", "budget") \
                    and FUTURE.search(text):
                continue                # a forecast, not this month's actual figures
            q.intent = name
            break
    if q.intent == "total" and not q.categories and SPENT_ON.search(text):
        q.intent = "unknown_category"   # "how much did I spend on X?" with an X the app does not know
    return q
