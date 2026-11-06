# app/evaluation/intelligence_cases.py
# The financial-intelligence evaluation: real questions (English, Arabic and
# Sudanese Arabic) with what a correct assistant must understand, retrieve,
# calculate and say. Used by tests/test_financial_intelligence.py (no model) and
# scripts/evaluate_intelligence.py (through the API, with the local Qwen model).
#
# Each case says
#   question / history   what the user asks, after which earlier questions
#   intent               the task the assistant must recognise ("clarify" when it
#                        should ask one short question instead of guessing)
#   entities             what must be extracted from the question itself
#   retrieval            figures that must come from the records
#   calc                 figures that must be calculated from them
#   says / never_says    text the final answer must / must not contain
#
# The profile below copies the October 2026 data of the app's owner (16
# expenses = 3,320 SDG, income 6,600 SDG) plus September and two goals. It is
# built in a throw-away database; production code never contains these numbers.

OCT, SEP = "2026-10", "2026-09"
OCTOBER = [
    ("Breakfast at home", 100, "Food", "Breakfast"), ("Work lunches", 300, "Food", "Lunch"),
    ("Dinner groceries", 200, "Food", "Dinner"), ("Restaurant evenings", 800, "Food", "Restaurant"),
    ("Fast food", 400, "Food", "Fast Food"), ("Bus and taxi", 300, "Transport", None),
    ("Electricity bill", 150, "Bills", "Electricity"), ("Water bill", 50, "Bills", "Water"),
    ("Internet bill", 100, "Bills", "Internet"), ("Gas and cleaning", 100, "Utilities", None),
    ("Cinema", 150, "Entertainment", "Movie"), ("Clothes", 300, "Shopping", "Clothes"),
    ("Headphones", 100, "Shopping", "Electronics"), ("Online course", 100, "Education", None),
    ("Pharmacy", 50, "Healthcare", None), ("Phone credit", 120, "Others", None),
]
SEPTEMBER = [
    ("Breakfast at home", 100, "Food", "Breakfast"), ("Work lunches", 300, "Food", "Lunch"),
    ("Dinner groceries", 200, "Food", "Dinner"), ("Restaurant evenings", 650, "Food", "Restaurant"),
    ("Fast food", 350, "Food", "Fast Food"), ("Bus and taxi", 300, "Transport", None),
    ("Electricity bill", 150, "Bills", "Electricity"), ("Water bill", 50, "Bills", "Water"),
    ("Internet bill", 100, "Bills", "Internet"), ("Gas and cleaning", 100, "Utilities", None),
    ("Cinema", 120, "Entertainment", "Movie"), ("Clothes", 300, "Shopping", "Clothes"),
    ("Online course", 100, "Education", None), ("Pharmacy", 50, "Healthcare", None),
]


def _month_end(months_ahead):
    import calendar
    from datetime import date
    today = date.today()
    total = today.year * 12 + today.month - 1 + months_ahead
    year, month = total // 12, total % 12 + 1
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def build_profile(name="Evaluation profile"):
    """The evaluation user, in whatever database app.config.DB_PATH points at."""
    from app.database import crud
    from app.preferences.user_preferences import seed_default_preferences
    uid = crud.add_user(name)
    for source, amount in (("Salary", 5000), ("Freelancer", 800), ("From Dad", 800)):
        crud.add_income(uid, amount, source, f"{OCT}-01")
    for source, amount in (("Salary", 5000), ("Freelancer", 800), ("From Dad", 200)):
        crud.add_income(uid, amount, source, f"{SEP}-01")
    for month, rows in ((OCT, OCTOBER), (SEP, SEPTEMBER)):
        for i, (desc, amount, cat, sub) in enumerate(rows):
            crud.add_transaction(uid, desc, amount, cat, f"{month}-{10 + i % 15:02d}", sub)
    # Deadlines relative to today (end of the month 4 and 8 months ahead), so the
    # goals always have 4 and 8 months left (4,000 and 750 a month) whenever the
    # evaluation runs; fixed dates made the expected values drift with the calendar.
    crud.add_goal(uid, "Laptop and course", 19000, 3000, _month_end(4))
    crud.add_goal(uid, "Emergency fund", 10000, 4000, _month_end(8))
    seed_default_preferences(uid)
    return uid


def C(id, group, question, intent, history=(), entities=None, retrieval=None, calc=None, says=(),
      never_says=()):
    return {"id": id, "group": group, "question": question, "history": list(history), "intent": intent,
            "entities": entities or {}, "retrieval": retrieval or {}, "calc": calc or {},
            "says": list(says), "never_says": list(never_says)}


CASES = [
    # ---- A. the same question, asked many ways ----
    C("remaining-ar-msa", "understanding", "كم المتبقي من دخلي بعد المصروفات؟", "summary",
      retrieval={"income": 6600, "expenses": 3320}, calc={"remaining": 3280}, says=["3,280"]),
    C("remaining-sd-1", "understanding", "حيفضل لي كم من المرتب؟", "summary",
      retrieval={"income": 6600, "expenses": 3320}, calc={"remaining": 3280}, says=["3,280"]),
    C("remaining-sd-2", "understanding", "بعد خصم كل مصروفاتي في أكتوبر 2026 من إجمالي دخلي، كم المبلغ المتبقي معاي؟",
      "summary", entities={"months": [OCT]}, calc={"remaining": 3280}, says=["3,280"]),
    C("remaining-en", "understanding", "How much money will remain after expenses?", "summary",
      calc={"remaining": 3280}, says=["3,280"]),
    C("combined-ar", "understanding", "كم دخلي وكم صرفت وكم المتبقي؟", "summary",
      retrieval={"income": 6600, "expenses": 3320, "expense_count": 16}, calc={"remaining": 3280},
      says=["6,600", "3,320", "3,280"]),
    C("save-next-month-sd", "understanding", "كم حأوفر الشهر الجاي؟", "forecast",
      says=["توقع", "ليس بيانات مسجلة"], never_says=["في أكتوبر 2026 صرفت"]),
    C("save-next-month-en", "understanding", "How much will I save next month?", "forecast",
      says=["forecast", "not recorded data"]),
    C("spent-less-no-amount", "understanding", "لو صرفت أقل، حأدخر كم؟", "clarify",
      says=["بكم تريد تخفيض"]),
    C("income-ar", "understanding", "كم كان إجمالي دخلي في أكتوبر 2026؟", "income",
      entities={"months": [OCT]}, retrieval={"income": 6600,
                                            "by_source": {"Salary": 5000, "Freelancer": 800, "From Dad": 800}},
      says=["6,600", "5,000"]),
    C("income-source-share", "understanding", "كم نسبة الراتب من دخلي؟", "income",
      retrieval={"income": 6600}, says=["75.76%"]),
    C("spent-oct", "understanding", "كم صرفت في أكتوبر؟", "total", entities={"months": [OCT]},
      retrieval={"total": 3320, "count": 16}, says=["3,320"]),
    C("top-category", "understanding", "ما هي أكبر فئة صرف؟", "top", says=["الطعام", "1,800"]),
    C("category-share", "understanding", "كم نسبة الأكل من مصروفاتي؟", "category",
      entities={"categories": [["Food", None]]}, retrieval={"amount": 1800}, calc={"percent_of_spending": 54.22},
      says=["54.22%"]),
    C("percent-of-income", "understanding", "كم 15% من دخلي؟", "percent_of",
      retrieval={"base": 6600}, calc={"value": 990}, says=["990"]),

    # ---- B. data retrieval: periods, records, goals ----
    C("list-oct", "retrieval", "اعرض لي العمليات الـ16 المسجلة في أكتوبر 2026 ومجموعها.", "transactions",
      entities={"months": [OCT]}, retrieval={"count": 16, "total": 3320}, says=["16 عملية = 3,320"]),
    C("largest-oct", "retrieval", "كم أكبر عملية صرفتها في أكتوبر؟", "transactions",
      says=["Restaurant evenings", "800"]),
    C("spent-sep", "retrieval", "كم صرفت في سبتمبر؟", "total", entities={"months": [SEP]},
      retrieval={"total": 2870, "count": 14}, says=["2,870"]),
    C("goals-all", "retrieval", "ما هي أهدافي الحالية؟", "goals", calc={"total_saved": 7000},
      says=["Laptop and course", "Emergency fund", "16%", "40%"]),
    C("goals-each", "retrieval", "كم ادخرت لكل هدف؟", "goals", says=["3,000", "4,000"]),
    C("goals-closest", "retrieval", "أي هدف أقرب للاكتمال؟", "goals", says=['الأقرب للاكتمال: "Emergency fund"']),
    C("goal-monthly", "retrieval", "كم لازم أوفر في الشهر عشان أوصل صندوق الطوارئ في موعده؟", "goal",
      calc={"required_monthly_saving": 750}, says=["750"]),
    C("goal-laptop-named", "retrieval", "متى حأوصل هدف اللابتوب بمعدل ادخاري الحالي؟", "goal",
      calc={"required_monthly_saving": 4000}, says=["Laptop and course", "4,000"]),

    # ---- C. calculations ----
    C("daily-avg", "calculation", "كم متوسط صرفي اليومي في أكتوبر؟", "average",
      calc={"daily_average": 107.1}, says=["3,320 ÷ 31"]),
    C("weekly-avg", "calculation", "What is my average spending per week in October 2026?", "average",
      calc={"weekly_average": 749.68}, says=["749.68"]),
    C("why-up", "calculation", "ليه مصروفاتي زادت في أكتوبر عن سبتمبر؟", "why",
      entities={"months": [OCT, SEP]}, retrieval={"totals": [3320, 2870]},
      calc={"difference": 450, "percent_change": 15.68}, says=["450", "15.68%", "المطاعم"]),
    C("compare-food", "calculation", "كم صرفت على الأكل في أكتوبر مقارنة بسبتمبر؟", "compare",
      retrieval={"totals": [1800, 1600]}, calc={"difference": 200}, says=["200"]),
    C("trend", "calculation", "اتجاه مصروفاتي في آخر 3 شهور", "trend",
      retrieval={"totals": [2870, 3320]}, calc={"change": 450}, says=["+450"]),
    C("split", "calculation", "من دخلي البالغ 6,600 جنيه، لو ادخرت 1,500 جنيه وصرفت الباقي، كم نسبة الادخار ونسبة الصرف؟",
      "split", calc={"saving_rate": 22.73, "spending_rate": 77.27}, says=["22.73%", "77.27%"]),

    # ---- D. what-if scenarios: the direction and meaning of each change ----
    C("whatif-mandatory", "scenario", "لو زاد صرفي في الشهر القادم 300 وكانت المصاريف والدخل ثابت كم سادخر؟",
      "what_if", entities={"changes": [["expenses", "add", 300]]},
      calc={"scenario.expenses": 3620, "scenario.income": 6600, "scenario.remaining": 2980},
      says=["3,320 + 300 = 3,620", "2,980"]),
    C("whatif-increase-en", "scenario", "What if I increase my expenses by 300?", "what_if",
      entities={"changes": [["expenses", "add", 300]]}, calc={"scenario.remaining": 2980}, says=["2,980"]),
    C("whatif-set-total", "scenario", "لو خليت المصاريف 300 بس كم حادخر؟", "what_if",
      entities={"changes": [["expenses", "set", 300]]}, calc={"scenario.expenses": 300, "scenario.remaining": 6300}),
    C("whatif-decrease", "scenario", "What if I reduce my expenses by 300?", "what_if",
      entities={"changes": [["expenses", "add", -300]]}, calc={"scenario.remaining": 3580}),
    C("whatif-income-up", "scenario", "لو زاد دخلي 300 كم حادخر؟", "what_if",
      entities={"changes": [["income", "add", 300]]}, calc={"scenario.income": 6900, "scenario.remaining": 3580}),
    C("whatif-save-extra", "scenario", "لو وفرت 300 زيادة كم حيكون ادخاري؟", "what_if",
      entities={"changes": [["saving", "add", 300]]}, calc={"scenario.monthly_saving": 3580}),
    C("whatif-spend-from-left", "scenario", "لو صرفت 300 من المتبقي كم حيفضل لي؟", "what_if",
      entities={"changes": [["expenses", "add", 300]]}, calc={"scenario.remaining": 2980}),
    C("whatif-two-changes", "scenario", "لو قللت المصاريف 300 والدخل زاد 500؟", "what_if",
      entities={"changes": [["expenses", "add", -300], ["income", "add", 500]]},
      calc={"scenario.expenses": 3020, "scenario.income": 7100, "scenario.remaining": 4080}),
    C("whatif-half", "scenario", "لو قللت المطاعم للنص كم حأوفر؟", "what_if",
      entities={"changes": [["category", "pct", -50]]}, calc={"scenario.expenses": 2920}),
    C("whatif-15pct", "scenario", "لو زادت مصروفاتي الشهرية بنسبة 15% عن أكتوبر، مع ثبات دخلي عند 6,600 جنيه، كم حتكون "
      "مصروفاتي الجديدة؟ وكم حيتبقى لي؟", "what_if",
      calc={"scenario.expenses": 3818, "scenario.remaining": 2782}, says=["498", "3,818", "2,782"]),
    C("whatif-ambiguous-total", "scenario", "لو صرفت 300 كم حيفضل؟", "clarify", says=["زيادة", "إجمالي"]),

    # ---- E. conversation ----
    C("followup-month", "conversation", "What about September?", "total",
      history=["How much did I spend in October?"], retrieval={"total": 2870}, says=["2,870"]),
    C("followup-another", "conversation", "What if I increase them by another 200?", "what_if",
      history=["What if I increase my expenses by 300?"],
      calc={"scenario.expenses": 3820, "scenario.remaining": 2780}, says=["previous scenario"]),
    C("followup-remaining", "conversation", "وكم المتبقي؟", "summary", history=["كم صرفت في سبتمبر؟"],
      calc={"remaining": 3130}, says=["سبتمبر"]),
    C("followup-details", "conversation", "وريني تفاصيلها", "transactions", history=["كم صرفت على الطعام في أكتوبر 2026؟"],
      retrieval={"count": 5, "total": 1800}),
    C("followup-mode", "conversation", "زيادة", "what_if", history=["لو صرفت 300 كم حيفضل؟"],
      calc={"scenario.remaining": 2980}),
    C("followup-source", "conversation", "من وين جبت رقم المصروفات؟", "explain", history=["كم صرفت في أكتوبر؟"],
      retrieval={"expenses": 3320, "expense_count": 16}, says=["16 عملية"]),

    # ---- F. error handling ----
    C("raise-it-no-context", "errors", "What if I raise it by 15%?", "clarify", says=["Which amount"]),
    C("contradiction", "errors", "What if transport costs 400 and transport costs 500?", "clarify",
      says=["400", "500"]),
    C("quoted-wrong-number", "errors",
      "من وين جبت رقم المصروفات 4,400؟ اعرض لي العمليات الـ16 المسجلة في أكتوبر 2026 ومجموعها", "transactions",
      retrieval={"total": 3320}, says=["4,400 غير موجود في سجلاتك"]),
    C("unusual", "errors", "هل في صرف غير طبيعي الشهر ده؟", "anomalies",
      says=["أخرى", "الإلكترونيات"], never_says=["الكهرباء"]),

    # ---- G. held-out round 1: written after the fixes above, run once untuned
    # (19/24 at first; the 5 failures were fixed in the parser, not here) ----
 C("heldout1-1", "held-out", "قديش فاضل لي من الماهية بعد المصاريف؟", "summary", calc={"remaining": 3280}, says=["3,280"]),
 C("heldout1-2", "held-out", "الشهر ده صرفت كم؟", "total", retrieval={"total": 3320}, says=["3,320"]),
 C("heldout1-3", "held-out", "دخلي الشهر الفات كان كم؟", "income", retrieval={"income": 6000}, says=["6,000"]),
 C("heldout1-4", "held-out", "How much did I make in September?", "income", retrieval={"income": 6000}),
 C("heldout1-5", "held-out", "لو المصاريف زادت 500 جنيه الدخل ثابت حيفضل كم؟", "what_if", calc={"scenario.remaining": 2780}, says=["2,780"]),
 C("heldout1-6", "held-out", "افترض دخلي نقص 1000 كم حادخر؟", "what_if", calc={"scenario.income": 5600, "scenario.remaining": 2280}),
 C("heldout1-7", "held-out", "If my rent goes up by 200, how much will I have left?", "what_if", calc={"scenario.remaining": 3080}, says=["no Rent (Bills) spending is recorded"]),
 C("heldout1-8", "held-out", "What if I spend 200 more on food and my salary rises by 10%?", "what_if", calc={"scenario.expenses": 3520, "scenario.income": 7260, "scenario.remaining": 3740}),
 C("heldout1-9", "held-out", "لو قللت الأكل 20% كم حيفضل؟", "what_if", calc={"scenario.expenses": 2960, "scenario.remaining": 3640}),
 C("heldout1-10", "held-out", "لو المواصلات بقت 500 كم المصاريف الجديدة؟", "what_if", calc={"scenario.expenses": 3520}),
 C("heldout1-11", "held-out", "كم صرفت على المواصلات في سبتمبر؟", "category", retrieval={"amount": 300}),
 C("heldout1-12", "held-out", "قارن مصاريفي الشهر ده بالشهر الفات", "compare", calc={"difference": 450}),
 C("heldout1-13", "held-out", "What share of my spending goes to shopping?", "category", calc={"percent_of_spending": 12.05}),
 C("heldout1-14", "held-out", "How far am I from my emergency fund?", "goal", says=["6,000"]),
 C("heldout1-15", "held-out", "كم فاضل لهدف اللابتوب؟", "goal", says=["16,000"]),
 C("heldout1-16", "held-out", "Show me all my goals", "goals", says=["Laptop and course", "Emergency fund"]),
 C("heldout1-17", "held-out", "متوسط صرفي في اليوم كم؟", "average", calc={"daily_average": 107.1}),
 C("heldout1-18", "held-out", "What did I spend the most on?", "top", says=["1,800"]),
 C("heldout1-19", "held-out", "Show me my expenses in detail for October 2026", "transactions", retrieval={"count": 16, "total": 3320}),
 C("heldout1-20", "held-out", "كم نسبة الادخار عندي؟", "savings", says=["49.70%"]),
 C("heldout1-21", "held-out", "كم ادخرت الشهر ده؟", "savings", says=["3,280"]),
 C("heldout1-22", "held-out", "And in September?", "total", history=["How much did I spend in October 2026?"], retrieval={"total": 2870}),
 C("heldout1-23", "held-out", "وكم دخلي؟", "income", history=["كم صرفت في سبتمبر؟"], retrieval={"income": 6000}),
 C("heldout1-24", "held-out", "What happens to my savings if I cut fast food completely?", "what_if", calc={"scenario.expenses": 2920}),

    # ---- H. held-out round 2: new phrasings, run once untuned (14/20 at first;
    # fixed in the parser: Sudanese "قد شنو", "how much more than last month",
    # "the difference between income and expenses", "show me the food expenses",
    # "الوجبات السريعة"; "when will I finish" is accepted as goal timing) ----
 C("heldout2-1", "held-out", "صرفت قد شنو في أكتوبر؟", "total", retrieval={"total": 3320}),
 C("heldout2-2", "held-out", "كم الفرق بين دخلي ومصاريفي؟", "summary", calc={"remaining": 3280}),
 C("heldout2-3", "held-out", "How much do I have left this month?", "summary", calc={"remaining": 3280}),
 C("heldout2-4", "held-out", "What is my total income?", "income", retrieval={"income": 6600}),
 C("heldout2-5", "held-out", "دخلي من العمل الحر كم؟", "income", says=["800"]),
 C("heldout2-6", "held-out", "لو دخلي بقى 8000 كم حيفضل؟", "what_if", calc={"scenario.income": 8000, "scenario.remaining": 4680}),
 C("heldout2-7", "held-out", "If I spend 400 less on restaurants, what will I save?", "what_if", calc={"scenario.expenses": 2920}),
 C("heldout2-8", "held-out", "لو ضاعفت صرفي على التعليم؟", "what_if", calc={"scenario.expenses": 3420}),
 C("heldout2-9", "held-out", "لو زادت الفواتير 10% والأكل نقص 100", "what_if", calc={"scenario.expenses": 3250}),
 C("heldout2-10", "held-out", "Suppose my income drops to 5,000 — how much is left?", "what_if", calc={"scenario.remaining": 1680}),
 C("heldout2-11", "held-out", "كم صرفت على الترفيه؟", "category", retrieval={"amount": 150}),
 C("heldout2-12", "held-out", "كم زادت مصاريفي عن الشهر اللي فات؟", "compare", calc={"difference": 450}),
 C("heldout2-13", "held-out", "Compare my food spending with last month", "compare", calc={"difference": 200}),
 C("heldout2-14", "held-out", "كم نسبة اكتمال هدف الطوارئ؟", "goal", says=["40%"]),
 C("heldout2-15", "held-out", "When will I finish my laptop goal?", "goal_timing", says=["Laptop and course"]),
 C("heldout2-16", "held-out", "ورني مصاريف الأكل", "transactions", retrieval={"count": 5, "total": 1800}),
 C("heldout2-17", "held-out", "What's my savings rate this month?", "savings", says=["49.70%"]),
 C("heldout2-18", "held-out", "وفي سبتمبر؟", "summary", history=["كم دخلي وكم صرفت وكم المتبقي؟"], calc={"remaining": 3130}),
 C("heldout2-19", "held-out", "and food?", "category", history=["How much did I spend on transport in October 2026?"], retrieval={"amount": 1800}),
 C("heldout2-20", "held-out", "لو وقفت الوجبات السريعة خالص كم حأوفر؟", "what_if", calc={"scenario.expenses": 2920}),

    # ---- I. found in real use with the local Qwen model (each went to the model
    # and came back wrong or empty before the fix) ----
    C("real-advice", "real-world", "كيف أقدر أقلل مصاريفي؟", "advice", says=["خفّض"], never_says=["16 مصروفًا"]),
    C("real-overview", "real-world", "ايه رأيك في وضعي المالي؟", "overview",
      retrieval={"income": 6600, "expenses": 3320}, calc={"remaining": 3280, "saving_rate": 49.70},
      says=["49.70%", "20%", "الطعام"]),
    C("real-rate-judged", "real-world", "هل نسبة ادخاري كويسة؟", "savings", calc={"saving_rate": 49.70},
      says=["49.70%", "20%"], never_says=["32%"]),
    C("real-too-much-food", "real-world", "هل أنا بصرف كتير على الأكل؟", "category", retrieval={"amount": 1800},
      says=["1,800", "سبتمبر 2026 كان 1,600"]),
    C("real-define-and-mine", "real-world", "ما هو صندوق الطوارئ وكم المفروض يكون عندي؟", "goal",
      says=["لا يوجد مبلغ واحد صحيح", "ادخرت 4,000 من 10,000"]),

    # ---- J. held-out round 3: new phrasings, run once untuned (13/20 at first;
    # fixed: Sudanese words for clothes, freelance income, "بتاعتي", "عايز أعرف",
    # "أكتر حاجة", "how much do I still need"; an unknown category is now asked
    # about instead of answered with the total) ----
 C("heldout3-1", "held-out", "المصاريف بتاعتي الشهر ده وصلت كم؟", "total", retrieval={"total": 3320}),
 C("heldout3-2", "held-out", "عايز أعرف دخلي ومصروفي", "summary", calc={"remaining": 3280}),
 C("heldout3-3", "held-out", "How much is left from my salary?", "summary", calc={"remaining": 3280}),
 C("heldout3-4", "held-out", "كم بيجيني من الشغل الحر؟", "income", says=["800"]),
 C("heldout3-5", "held-out", "لو المرتب زاد 1000 والمصاريف زادت 500 حيفضل كم؟", "what_if", calc={"scenario.remaining": 3780}),
 C("heldout3-6", "held-out", "What if my food spending drops by 300?", "what_if", calc={"scenario.expenses": 3020}),
 C("heldout3-7", "held-out", "لو بطلت السينما كم حأوفر؟", "what_if", calc={"scenario.expenses": 3170}),
 C("heldout3-8", "held-out", "If I add 250 to my transport budget how much remains?", "what_if", calc={"scenario.remaining": 3030}),
 C("heldout3-9", "held-out", "لو دخلي قل 20%؟", "what_if", calc={"scenario.income": 5280, "scenario.remaining": 1960}),
 C("heldout3-10", "held-out", "كم صرفت على اللبس؟", "category", retrieval={"amount": 300}),
 C("heldout3-11", "held-out", "أكتر حاجة بصرف عليها شنو؟", "top", says=["الطعام"]),
 C("heldout3-12", "held-out", "How much more did I spend this month than last month?", "compare", calc={"difference": 450}),
 C("heldout3-13", "held-out", "متوسط صرفي الأسبوعي كم؟", "average", calc={"weekly_average": 749.68}),
 C("heldout3-14", "held-out", "هدف الطوارئ وصل لوين؟", "goal", says=["40%"]),
 C("heldout3-15", "held-out", "How much do I still need for the laptop?", "goal", says=["16,000"]),
 C("heldout3-16", "held-out", "List all my transactions this month", "transactions", retrieval={"count": 16}),
 C("heldout3-17", "held-out", "Is my spending on restaurants too high?", "category", says=["800"]),
 C("heldout3-18", "held-out", "طيب والشهر الفات؟", "summary", history=["كم دخلي وكم صرفت وكم المتبقي؟"], calc={"remaining": 3130}),
 C("heldout3-19", "held-out", "زودها 100 كمان", "what_if", history=["لو المواصلات زادت 100؟"], calc={"scenario.expenses": 3520}),
 C("heldout3-20", "held-out", "لو صرفت كل المتبقي على هدف اللابتوب حأوصله متين؟", "goal", says=["Laptop and course"]),
    C("unknown-category", "errors", "كم صرفت على الكماليات؟", "clarify", says=["لن أخمّن"], never_says=["3,320 ج.س في"]),

    # ---- K. decision support: purchases, emergencies, goal priority, saving
    # plans, trade-offs and change requests (app/finance/decisions.py). A change
    # is only requested here, never confirmed, so the profile stays the same for
    # every case. ----
    C("decide-price-missing", "decisions", "Can I afford to buy a new phone?", "clarify",
      says=["How much does the phone cost?"]),
    C("decide-price-given", "decisions", "It costs 5,000 SDG", "purchase", history=["Can I afford to buy a new phone?"],
      retrieval={"price": 5000}, calc={"verdict": "risky", "recommended": "delay"},
      says=["1,039 from emergency savings", "nothing was bought or saved"]),
    C("decide-wait", "decisions", "what if I wait 3 months?", "purchase", history=["Should I buy a phone for 5,000?"],
      says=["3 month(s), putting 1,666.67 SDG a month aside"]),
    C("decide-installments", "decisions", "Would paying for a 6,000 laptop in 6 installments be reasonable?",
      "purchase", calc={"price": 6000}, says=["6 × 1,000 SDG = 6,000 SDG"]),
    C("decide-urgent", "decisions", "My laptop died and I need a new one for work. Can I afford one for 3,000?",
      "purchase", calc={"recommended": "buy_now", "reason": "urgent_least_harm"}),
    C("decide-trade-off", "decisions", "I want to buy a phone for 5,000 SDG, but I also want to complete my emergency fund",
      "purchase", calc={"recommended": "both", "both.item_months": 2, "both.goal_per_month": 750}),
    C("decide-safe-price", "decisions", "How much can I safely spend on this purchase?", "purchase",
      calc={"max_safe_price": 0, "with_goal_contributions": 3280, "with_flexible_cuts": 3961}),
    C("decide-contradiction", "decisions", "My income is 10,000, can I afford a 5,000 phone?", "purchase",
      calc={"price": 5000}, says=["your records show 6,600 SDG"]),
    C("decide-ar-purchase", "decisions", "هل يمكنني شراء هاتف جديد بسعر 4000 جنيه؟", "purchase",
      calc={"price": 4000}, says=["التوصية"]),
    C("decide-sd-installments", "decisions", "ولو بالتقسيط على 6 اشهر؟", "purchase",
      history=["ممكن اشتري تلفون جديد بـ 4000 جنيه؟"], says=["6 × 666.67"]),
    C("decide-emergency", "decisions", "I have an unexpected medical bill of 2,000 SDG", "emergency",
      calc={"refill_months": 1, "shortfall": 0}, says=["2,000 SDG from emergency savings", "Recovery plan"]),
    C("decide-emergency-sd", "decisions", "عندي مصروف طارئ 2500 جنيه للعلاج", "emergency", calc={"amount": 2500},
      says=["خطة التعافي"]),
    C("decide-priority", "decisions", "Which goal should I focus on first?", "priority",
      calc={"recommended": "focus", "total_need": 4750, "capacity": 3280}),
    C("decide-priority-sd", "decisions", "باي هدف ابدا؟", "priority", calc={"recommended": "focus"}),
    C("decide-saving-plan", "decisions", "Give me a saving plan based on my spending", "saving_plan",
      calc={"total_saving": 450}, says=["150 + 120 + 100 + 50 + 30 = 450"]),
    C("decide-saving-plan-sd", "decisions", "عايز خطة ادخار من صرفي", "saving_plan", calc={"total_saving": 450}),
    C("decide-goal-preview", "decisions", "what if I add 500 to my emergency fund?", "goal_preview",
      says=["4,000 + 500 = 4,500", "nothing was saved"]),
    C("decide-action-asks", "decisions", "add 500 to my emergency fund", "clarify",
      says=["Nothing has been changed yet"]),
    C("decide-goal-status", "decisions", "Tell me about my emergency fund", "goal", says=["6,000 SDG still needed"]),
    C("decide-planned", "decisions", "about 3,000 in 4 months", "purchase",
      history=["How should I prepare for Ramadan expenses?"], says=["set aside 750 SDG a month"]),
    C("decide-planned-sd", "decisions", "حوالي 3000 بعد 4 شهور", "purchase", history=["كيف استعد لمصاريف رمضان؟"],
      says=["خصص 750 ج.س شهريًا"]),
    C("decide-loan", "decisions", "2,000 a month for 24 months", "purchase",
      history=["Should I take a loan for a car?", "40,000"], says=["24 × 2,000 SDG = 48,000 SDG", "8,000 SDG more"]),
]
