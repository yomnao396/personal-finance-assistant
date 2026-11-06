# tests/test_routing_regressions.py
# Regression tests for bugs seen in the running app (October 2026 data):
#   A. "how much did I spend and how much of my income is left?" was answered with
#      the income analysis only, because the word "income" (دخلي) won the routing;
#   B. the model, given no tool result, wrote an expense total of 4,400 that is in
#      no record, and the answer was not checked;
#   C. "where did 4,400 come from? show me the 16 transactions" went to the model,
#      which repeated its earlier answer;
#   D. that model answer carried an unrelated citation and a Chinese sentence.
# The October figures below are a test fixture copied from the user's export;
# production code never contains them.

import json

import pytest

from app.database import crud
from app.database.connection import get_connection
from app.finance import facts
from app.finance.verify import unsupported_amounts
from app.llm.agent import run_agent_detailed
from app.services import intent

OCT, SEP = "2026-10", "2026-09"
OCTOBER = [  # 16 expense transactions, 3,320 SDG
    ("Breakfast at home", 100, "Food", "Breakfast"), ("Work lunches", 300, "Food", "Lunch"),
    ("Dinner groceries", 200, "Food", "Dinner"), ("Restaurant evenings", 800, "Food", "Restaurant"),
    ("Fast food", 400, "Food", "Fast Food"), ("Bus and taxi", 300, "Transport", None),
    ("Electricity bill", 150, "Bills", "Electricity"), ("Water bill", 50, "Bills", "Water"),
    ("Internet bill", 100, "Bills", "Internet"), ("Gas and cleaning", 100, "Utilities", None),
    ("Cinema", 150, "Entertainment", "Movie"), ("Clothes", 300, "Shopping", "Clothes"),
    ("Headphones", 100, "Shopping", "Electronics"), ("Online course", 100, "Education", None),
    ("Pharmacy", 50, "Healthcare", None), ("Phone credit", 120, "Others", None),
]
SEPTEMBER = [  # 14 expense transactions, 2,870 SDG
    ("Breakfast at home", 100, "Food", "Breakfast"), ("Work lunches", 300, "Food", "Lunch"),
    ("Dinner groceries", 200, "Food", "Dinner"), ("Restaurant evenings", 650, "Food", "Restaurant"),
    ("Fast food", 350, "Food", "Fast Food"), ("Bus and taxi", 300, "Transport", None),
    ("Electricity bill", 150, "Bills", "Electricity"), ("Water bill", 50, "Bills", "Water"),
    ("Internet bill", 100, "Bills", "Internet"), ("Gas and cleaning", 100, "Utilities", None),
    ("Cinema", 120, "Entertainment", "Movie"), ("Clothes", 300, "Shopping", "Clothes"),
    ("Online course", 100, "Education", None), ("Pharmacy", 50, "Healthcare", None),
]


def _profile(name="October profile"):
    uid = crud.add_user(name)
    for source, amount in (("Salary", 5000), ("Freelancer", 800), ("From Dad", 800)):
        crud.add_income(uid, amount, source, f"{OCT}-01")
    for source, amount in (("Salary", 5000), ("Freelancer", 800), ("From Dad", 200)):
        crud.add_income(uid, amount, source, f"{SEP}-01")
    for month, rows in ((OCT, OCTOBER), (SEP, SEPTEMBER)):
        for i, (desc, amount, cat, sub) in enumerate(rows):
            crud.add_transaction(uid, desc, amount, cat, f"{month}-{10 + i % 15:02d}", sub)
    return uid


@pytest.fixture
def uid():
    return _profile()


class Model:
    """A stand-in for Qwen: fails the test when called, unless given replies."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, messages, tools=None):
        self.calls.append(messages)
        assert self.replies, "the model was called for a question the app answers from the records"
        return {"role": "assistant", "content": self.replies.pop(0)}


def ask(question, user, history=None, model=None, fast=True, language=None):
    return run_agent_detailed(question, history=history, user_id=user,
                              language=language or intent.language_of(question),
                              chat_fn=model or Model(), fast=fast)


def turn(history, question, r):
    history += [{"role": "user", "content": question},
                {"role": "assistant", "content": r["answer"], "goal": r.get("goal"), "context": r.get("context")}]


def tool(r):
    return r["tools_used"][0]["name"] if r["tools_used"] else None


def data(r):
    return r["tool_results"][0]["result"]


# ---------- the seven acceptance questions ----------

def test_acceptance_conversation(uid):
    history = []

    r = ask("كم كان إجمالي دخلي في أكتوبر 2026؟", uid, history)
    assert tool(r) == "analyze_income"
    assert data(r)["income"] == 6600
    assert data(r)["by_source"] == {"Salary": 5000, "Freelancer": 800, "From Dad": 800}
    turn(history, "كم كان إجمالي دخلي في أكتوبر 2026؟", r)

    r = ask("وكم صرفت؟", uid, history)
    assert tool(r) == "get_monthly_spending"
    assert data(r)["total"] == 3320 and data(r)["count"] == 16 and data(r)["months"] == [OCT]
    turn(history, "وكم صرفت؟", r)

    r = ask("وكم المتبقي من دخلي بعد المصروفات؟", uid, history)
    assert tool(r) == "calculate_balance"
    assert (data(r)["income"], data(r)["expenses"], data(r)["remaining"]) == (6600, 3320, 3280)
    assert "6,600 − 3,320 = 3,280" in r["answer"]
    turn(history, "وكم المتبقي من دخلي بعد المصروفات؟", r)

    r = ask("كم دخلي وكم صرفت وكم المتبقي؟", uid, history)
    assert tool(r) == "calculate_balance"
    for figure in ("6,600", "3,320", "3,280"):
        assert figure in r["answer"]
    turn(history, "كم دخلي وكم صرفت وكم المتبقي؟", r)

    r = ask("اعرض لي العمليات الـ16 المسجلة في أكتوبر 2026 ومجموعها.", uid, history)
    assert tool(r) == "list_transactions"
    assert data(r)["count"] == 16 and data(r)["total"] == 3320
    assert len(data(r)["transactions"]) == 16
    assert sum(t["amount"] for t in data(r)["transactions"]) == 3320
    assert "16 عملية = 3,320" in r["answer"] and "Phone credit" in r["answer"]
    turn(history, "اعرض لي العمليات الـ16 المسجلة في أكتوبر 2026 ومجموعها.", r)

    r = ask("ليه مصروفاتي زادت في أكتوبر عن سبتمبر؟", uid, history)
    assert tool(r) == "analyze_spending_patterns"
    assert data(r)["totals"] == [3320, 2870] and data(r)["difference"] == 450
    assert data(r)["percent_change"] == pytest.approx(15.68, abs=0.01)
    assert "المطاعم" in r["answer"]                         # the category-level reason
    turn(history, "ليه مصروفاتي زادت في أكتوبر عن سبتمبر؟", r)

    r = ask("من وين جبت رقم المصروفات؟", uid, history)
    assert tool(r) == "explain_calculation"
    assert data(r)["expenses"] == 3320 and data(r)["expense_count"] == 16
    assert "16 عملية" in r["answer"] and "3,320" in r["answer"]


# ---------- A. income ----------

def test_income_with_sources(uid):
    r = ask("How much was my income in October 2026?", uid)
    assert tool(r) == "analyze_income" and data(r)["income"] == 6600
    assert "Salary 5,000" in r["answer"] and "Freelancer 800" in r["answer"]


def test_income_follow_up_keeps_the_month(uid):
    history = []
    turn(history, "How much did I spend in September 2026?", ask("How much did I spend in September 2026?", uid))
    r = ask("And my income?", uid, history)
    assert tool(r) == "analyze_income" and data(r)["month"] == SEP and data(r)["income"] == 6000


def test_month_without_income_says_so():
    uid = crud.add_user("no income")
    crud.add_transaction(uid, "Bus", 300, "Transport", f"{OCT}-05")
    r = ask("How much did I earn and spend, and how much is left?", uid)
    assert tool(r) == "calculate_balance" and data(r)["income"] is None and data(r)["remaining"] is None
    assert "none recorded" in r["answer"]


# ---------- B. expenses ----------

def test_expense_total_count_and_month_filter(uid):
    r = ask("كم صرفت في أكتوبر؟", uid)
    assert tool(r) == "get_monthly_spending" and data(r)["total"] == 3320 and data(r)["count"] == 16
    r = ask("كم صرفت في سبتمبر؟", uid)
    assert data(r)["total"] == 2870 and data(r)["count"] == 14


def test_expense_breakdown_by_category(uid):
    r = ask("How much did I spend on food in October 2026?", uid)
    assert data(r)["amount"] == 1800 and data(r)["total_spending"] == 3320


def test_listing_for_an_empty_month(uid):
    r = ask("Show me my transactions in August 2026", uid)
    assert tool(r) == "list_transactions" and data(r)["count"] == 0
    assert "No expense transactions" in r["answer"]


def test_listing_a_category_as_a_follow_up(uid):
    history = []
    turn(history, "كم صرفت على الطعام في أكتوبر 2026؟", ask("كم صرفت على الطعام في أكتوبر 2026؟", uid))
    r = ask("وريني تفاصيلها", uid, history)
    assert tool(r) == "list_transactions"
    assert data(r)["count"] == 5 and data(r)["total"] == 1800
    assert {t["category"] for t in data(r)["transactions"]} == {"Food"}


# ---------- C. balance ----------

@pytest.mark.parametrize("question", [
    "كم المتبقي من دخلي بعد المصروفات؟",
    "بعد خصم كل مصروفاتي في أكتوبر 2026 من إجمالي دخلي، كم المبلغ المتبقي معاي؟",
    "وكم صرفت؟ وكم المتبقي من دخلي بعد المصروفات؟",
    "How much is left after my expenses?",
    "How much did I earn and spend in October 2026?",
])
def test_balance_questions_compute_income_minus_expenses(uid, question):
    r = ask(question, uid)
    assert tool(r) == "calculate_balance"
    assert (data(r)["income"], data(r)["expenses"], data(r)["remaining"]) == (6600, 3320, 3280)
    assert data(r)["saving_rate"] == pytest.approx(49.70, abs=0.01)


def test_balance_as_a_follow_up(uid):
    history = []
    turn(history, "كم صرفت في سبتمبر؟", ask("كم صرفت في سبتمبر؟", uid))
    r = ask("وكم المتبقي؟", uid, history)
    assert data(r)["month"] == SEP and data(r)["remaining"] == 6000 - 2870


def test_negative_balance():
    uid = crud.add_user("overspending")
    crud.add_income(uid, 1000, "Salary", f"{OCT}-01")
    crud.add_transaction(uid, "Rent", 1500, "Bills", f"{OCT}-02", "Rent")
    r = ask("How much is left after my expenses?", uid)
    assert data(r)["remaining"] == -500 and "500 SDG more than your income" in r["answer"]


def test_zero_income():
    uid = crud.add_user("zero income")
    crud.add_income(uid, 0, "Salary", f"{OCT}-01")
    crud.add_transaction(uid, "Bus", 200, "Transport", f"{OCT}-02")
    r = ask("كم دخلي وكم صرفت وكم المتبقي؟", uid)
    assert data(r)["income"] == 0 and data(r)["remaining"] == -200 and data(r)["saving_rate"] is None
    assert "غير محددة" in r["answer"]


# ---------- D. intents and routing ----------

@pytest.mark.parametrize("question,expected", [
    ("كم دخلي في أكتوبر؟", "income"),
    ("كم صرفت في أكتوبر؟", "total"),
    ("كم المتبقي من دخلي بعد المصروفات؟", "summary"),
    ("كم دخلي وكم صرفت وكم المتبقي؟", "summary"),
    ("وكم صرفت؟ وكم المتبقي من دخلي بعد المصروفات؟", "summary"),
    ("اعرض لي عمليات أكتوبر بالتفصيل.", "transactions"),
    ("Show me my October transactions", "transactions"),
    ("ليه مصروفاتي زادت عن سبتمبر؟", "why"),
    ("من وين جبت الرقم؟", "explain"),
    ("Where did you get that number?", "explain"),
    ("What is my saving rate?", "savings"),
    ("How much is left in my food budget?", "budget"),
    ("How much is left of my goal?", "goal"),
    ("How much did I earn in October?", "income"),
])
def test_intents(question, expected):
    assert intent.detect(question, OCT).intent == expected


def test_expense_questions_never_route_to_income_analysis(uid):
    for question in ("وكم صرفت؟ وكم المتبقي من دخلي بعد المصروفات؟", "كم صرفت من دخلي هذا الشهر؟",
                     "How much of my income did I spend?"):
        assert tool(ask(question, uid)) != "analyze_income", question


def test_a_new_question_overrides_the_previous_intent(uid):
    history = []
    turn(history, "كم دخلي في أكتوبر 2026؟", ask("كم دخلي في أكتوبر 2026؟", uid))
    r = ask("اعرض لي العمليات", uid, history)
    assert tool(r) == "list_transactions" and data(r)["count"] == 16


# ---------- E. conversation ----------

def test_the_quoted_wrong_number_is_named_and_the_records_listed(uid):
    q = ("من وين جبت رقم المصروفات 4,400؟ اعرض لي العمليات الـ16 المسجلة في أكتوبر 2026 ومجموعها، "
         "بدون افتراض أو استخدام بيانات من خارج البرنامج.")
    r = ask(q, uid)
    assert tool(r) == "list_transactions" and data(r)["total"] == 3320 and data(r)["count"] == 16
    assert "4,400 غير موجود في سجلاتك" in r["answer"]


def test_where_did_it_come_from_after_a_model_answer(uid):
    wrong = ("في أكتوبر 2026، كان دخلك الإجمالي 6,600 ج.س وصرفت 4,400 ج.س. لذلك، المتبقي 2,200 ج.س. "
             "因此，你的剩余金额是2,200")
    history = [{"role": "user", "content": "وكم صرفت؟"},
               {"role": "assistant", "content": wrong, "context": {"intent": "model"}}]
    r = ask("من وين جبت رقم المصروفات؟", uid, history)
    assert tool(r) == "explain_calculation" and data(r)["expenses"] == 3320
    assert data(r)["unsupported_numbers"] == [4400, 2200]
    assert "3,320" in r["answer"] and "4,400 لا يأتي من سجلاتك" in r["answer"]


def test_a_count_that_does_not_match_the_records_is_pointed_out(uid):
    r = ask("Show me the 20 transactions recorded in October 2026", uid)
    assert data(r)["count"] == 16 and "You mentioned 20 transactions; your records have 16" in r["answer"]


def test_follow_ups_keep_the_profile():
    a, b = _profile("profile A"), crud.add_user("profile B")
    crud.add_income(b, 9000, "Salary", f"{OCT}-01")
    crud.add_transaction(b, "Rent", 4000, "Bills", f"{OCT}-02", "Rent")
    history = []
    turn(history, "كم صرفت في أكتوبر 2026؟", ask("كم صرفت في أكتوبر 2026؟", a))
    r = ask("وكم المتبقي؟", a, history)
    assert data(r)["remaining"] == 3280
    r = ask("وكم المتبقي؟", b, history)
    assert data(r)["remaining"] == 5000


# ---------- F. data integrity ----------

def test_no_duplicate_aggregation_and_type_mixing(uid):
    crud.add_transaction(uid, "Salary paid in", 5000, "Income", f"{OCT}-01")    # an Income-category row
    r = ask("كم دخلي وكم صرفت وكم المتبقي؟", uid)
    assert data(r)["expenses"] == 3320 and data(r)["income"] == 6600
    conn = get_connection()
    try:
        sql_total = conn.execute(
            "SELECT SUM(amount), COUNT(*) FROM transactions WHERE user_id = ? AND category != 'Income' "
            "AND substr(date, 1, 7) = ?", (uid, OCT)).fetchone()
    finally:
        conn.close()
    assert tuple(sql_total) == (3320, 16)


def test_month_boundaries():
    uid = crud.add_user("boundaries")
    crud.add_income(uid, 1000, "Salary", f"{OCT}-01")
    crud.add_transaction(uid, "Last day of September", 70, "Food", f"{SEP}-30")
    crud.add_transaction(uid, "First of October", 30, "Food", f"{OCT}-01")
    crud.add_transaction(uid, "Last of October", 20, "Food", f"{OCT}-31")
    crud.add_transaction(uid, "First of November", 90, "Food", "2026-11-01")
    r = ask("Show me my transactions in October 2026", uid)
    assert data(r)["total"] == 50 and data(r)["count"] == 2


def test_no_cross_profile_leakage(uid):
    other = _profile("other")
    crud.add_transaction(other, "Car", 10000, "Transport", f"{OCT}-03")
    assert data(ask("اعرض لي عمليات أكتوبر 2026", uid))["total"] == 3320


def test_current_records_win_over_an_earlier_answer(uid):
    history = []
    turn(history, "كم صرفت في أكتوبر 2026؟", ask("كم صرفت في أكتوبر 2026؟", uid))
    crud.add_transaction(uid, "New purchase", 80, "Shopping", f"{OCT}-20", "Clothes")
    r = ask("كم دخلي وكم صرفت وكم المتبقي؟", uid, history)
    assert data(r)["expenses"] == 3400 and data(r)["remaining"] == 3200


# ---------- G. response quality ----------

def test_a_model_figure_not_in_the_records_is_replaced(uid):
    model = Model("في أكتوبر 2026، كان دخلك الإجمالي 6,600 ج.س وصرفت 4,400 ج.س. لذلك، المتبقي 2,200 ج.س.")
    r = ask("كم صرفت وكم المتبقي من دخلي؟", uid, model=model, fast=False)
    assert "4,400" not in r["answer"].split("\n", 1)[1] and r["unsupported_numbers"] == [4400, 2200]
    assert "3,320" in r["answer"] and "3,280" in r["answer"]
    assert r["sources"] == []


def test_a_model_answer_with_record_figures_is_kept(uid):
    model = Model("صرفت 3,320 ج.س في أكتوبر 2026.")
    r = ask("كم صرفت في أكتوبر 2026؟", uid, model=model, fast=False)
    assert r["answer"] == "صرفت 3,320 ج.س في أكتوبر 2026." and r["unsupported_numbers"] == []


def test_no_chinese_in_an_arabic_answer(uid):
    model = Model("المتبقي 3,280 ج.س. 因此，你的剩余金额是3,280", "المتبقي 3,280 ج.س. 因此，你的剩余金额是3,280")
    r = ask("كم المتبقي من دخلي بعد المصروفات في أكتوبر 2026؟", uid, model=model, fast=False)
    assert not any("一" <= ch <= "鿿" for ch in r["answer"])
    assert "3,280" in r["answer"]


def test_no_knowledge_notes_or_citations_for_own_number_questions(uid):
    model = Model("صرفت 3,320 ج.س.")
    r = ask("بعد خصم كل مصروفاتي في أكتوبر 2026 من إجمالي دخلي، كم المبلغ المتبقي معاي؟", uid,
            model=model, fast=False)
    sent = json.dumps(model.calls[0], ensure_ascii=False)
    assert "Verified notes" not in sent and r["sources"] == []


def test_earlier_model_figures_are_not_sent_back_to_the_model(uid):
    model = Model("سأحسبها من سجلاتك.")
    history = [{"role": "user", "content": "وكم صرفت؟"},
               {"role": "assistant", "content": "صرفت 4,400 ج.س.", "context": {"intent": "model"}}]
    ask("ما رأيك في وضعي المالي؟", uid, history, model=model, fast=False)
    assert "4,400" not in json.dumps(model.calls[0], ensure_ascii=False)


def test_every_figure_in_direct_answers_is_backed_by_the_records(uid):
    snap = facts.month_snapshot(uid, OCT)
    allowed = [snap["expenses"], snap["income"]["total"], snap["remaining"], 5000, 800, 49.70,
               100]                                     # the "× 100" of the rate formula
    for question in ("كم دخلي وكم صرفت وكم المتبقي؟", "How much is left after my expenses?"):
        r = ask(question, uid)
        assert unsupported_amounts(r["answer"], allowed) == [], question
        assert ("أكتوبر 2026" in r["answer"]) or ("October 2026" in r["answer"])
        assert ("ج.س" in r["answer"]) if intent.language_of(question) == "ar" else ("SDG" in r["answer"])


def test_a_clarifying_question_shows_no_calculation_label(uid):
    r = ask("What if I raise it by 15%?", uid)
    assert r["tools_used"] == []


def test_a_real_figure_in_the_wrong_place_is_replaced(uid):
    # The income (6,600) given as the spending: every number exists, but the answer is wrong.
    model = Model("صرفت في أكتوبر 2026 حوالي 6600 ج.س. المتبقي من دخلك هو 6600 ج.س.")
    r = ask("كم صرفت وكم المتبقي من دخلي في أكتوبر 2026؟", uid, model=model, fast=False)
    assert "3,320" in r["answer"] and "3,280" in r["answer"] and "لم أتمكن من التحقق" in r["answer"]


def test_a_replaced_answer_is_labelled_with_the_records_not_the_model_tool(uid):
    model = Model("صرفت 4,400 ج.س.")
    r = ask("كم صرفت في أكتوبر 2026؟", uid, model=model, fast=False)
    assert [t["name"] for t in r["tools_used"]] == ["calculate_balance"] and r["sources"] == []
