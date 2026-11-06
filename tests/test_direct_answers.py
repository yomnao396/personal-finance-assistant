# tests/test_direct_answers.py
# Questions about the user's own numbers are answered from the database and the
# finance engines without the model (services/direct_answers.py): exact numbers,
# one user and one period at a time, what-ifs that change only what the user
# said, follow-ups that keep their context, and the calculation on request.

import json
from datetime import date

import pytest

from app.database import crud
from app.database.connection import get_connection
from app.finance import facts
from app.llm.agent import run_agent_detailed
from app.services import intent

THIS = date.today().strftime("%Y-%m")
LAST = facts.shift_month(THIS, -1)


def _in_months(n):
    total = date.today().year * 12 + date.today().month - 1 + n
    return f"{total // 12}-{total % 12 + 1:02d}-28"


class NoModel:
    """Fails the test if the model is called; records calls otherwise."""

    def __init__(self, allow=False):
        self.calls, self.allow = [], allow

    def __call__(self, messages, tools=None):
        self.calls.append(messages)
        assert self.allow, "the model was called for a question the app can answer itself"
        return {"role": "assistant", "content": "model answer"}


def ask(question, uid, history=None, language="en", model=None):
    model = model or NoModel()
    r = run_agent_detailed(question, history=history, user_id=uid, language=language, chat_fn=model)
    return r


def turn(history, question, r):
    history += [{"role": "user", "content": question},
                {"role": "assistant", "content": r["answer"], "goal": r.get("goal"),
                 "context": r.get("context")}]


def _spec_user():
    """The example in the requirements: income 6,600, spending 3,320 of which
    Transport is 300, and a goal that needs 4,000 a month."""
    uid = crud.add_user("spec example")
    crud.add_income(uid, 6600, "Salary", f"{THIS}-01")
    for desc, amount, cat, sub in (("Rent", 1500, "Bills", "Rent"), ("Lunches", 900, "Food", "Lunch"),
                                   ("Restaurants", 620, "Food", "Restaurant"),
                                   ("Bus and taxi", 300, "Transport", None)):
        crud.add_transaction(uid, desc, amount, cat, f"{THIS}-05", sub)
    crud.add_goal(uid, "Laptop and course", 19000, 3000, _in_months(4))
    return uid


# ---------- the requirement's worked what-if ----------

def test_explicit_what_if_changes_only_what_the_user_said():
    uid = _spec_user()
    r = ask("Assume transportation increases to 400 SDG next month, while all other expenses and "
            "income remain unchanged.", uid)
    data = r["tool_results"][0]["result"]
    assert data["baseline"] == {"income": 6600, "expenses": 3320, "remaining": 3280}
    assert data["scenario"]["expenses"] == 3420               # 3,320 - 300 + 400
    assert data["scenario"]["remaining"] == 3180              # 6,600 - 3,420
    assert data["scenario"]["income"] == 6600
    assert "3,320 − 300 + 400 = 3,420 SDG" in r["answer"]
    assert "6,600 − 3,420 = 3,180 SDG" in r["answer"]
    # No goal was asked about, so none is added (an unrelated goal calculation was a
    # complaint in UAT); "... and how does it affect my goal?" adds it.
    assert "goal" not in r["answer"].lower()
    assert "Hypothetical" in r["answer"] and r["fast_path"]


def test_a_what_if_never_writes_to_the_database():
    uid = _spec_user()
    conn = get_connection()
    before = conn.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE user_id = ?",
                          (uid,)).fetchone()
    conn.close()
    ask("What if transport goes up to 1000 and my income drops by 10%?", uid)
    conn = get_connection()
    after = conn.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE user_id = ?",
                         (uid,)).fetchone()
    conn.close()
    assert tuple(before) == tuple(after)
    assert crud.get_income(uid) == 6600


@pytest.mark.parametrize("question,expected", [
    ("What if I cut restaurants by 200?", 3120),                 # subcategory, by
    ("What if I spend 200 less on restaurants?", 3120),
    ("What if transport costs 500?", 3520),                      # set
    ("What if my food spending increases by 10%?", 3320 + 152),  # 1,520 x 10%
    ("What if transport increases to 400 and food drops by 100?", 3320),
])
def test_what_if_variants(question, expected):
    uid = _spec_user()
    r = ask(question, uid)
    assert r["tool_results"][0]["result"]["scenario"]["expenses"] == expected


def test_income_what_if_keeps_spending():
    r = ask("What if my salary drops to 6000?", _spec_user())
    s = r["tool_results"][0]["result"]["scenario"]
    assert s["income"] == 6000 and s["expenses"] == 3320 and s["remaining"] == 2680


def test_arabic_what_if():
    r = ask("ماذا لو زادت المواصلات إلى 400؟", _spec_user(), language="ar")
    assert r["tool_results"][0]["result"]["scenario"]["expenses"] == 3420
    assert "3,320 − 300 + 400 = 3,420 ج.س" in r["answer"] and "SDG" not in r["answer"]


# ---------- the benchmark's numeric questions, exact and without the model ----------

QUESTIONS = json.loads(open("data/evaluation/questions.json", encoding="utf-8").read())["questions"]


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q["type"] == "numeric"], ids=lambda q: q["id"])
def test_benchmark_numbers_are_exact(sample_user, q):
    from app.evaluation.scoring import numeric_correct
    r = ask(q["question"], sample_user)
    assert r["fast_path"], q["question"]
    assert numeric_correct(r["answer"], q["expected"]), r["answer"]


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q["type"] != "numeric"], ids=lambda q: q["id"])
def test_general_and_unanswerable_questions_still_go_to_the_model(sample_user, q):
    r = ask(q["question"], sample_user, model=NoModel(allow=True))
    assert not r.get("fast_path"), r["answer"]


# ---------- the follow-up conversation from the requirements ----------

def test_goal_conversation_keeps_its_context(sample_user):
    h = []
    steps = [
        ("Can I reach my goal on time?", ["Laptop and course", "4,000 SDG a month", "1,200 SDG a month short"]),
        ("What should I do to reach it faster?", ["Restaurant: cut 240 SDG", "720 SDG a month is still missing"]),
        ("What if I save an extra 500 every month?", ["2,800 + 500 = 3,300", "700 SDG short", "1 month(s) sooner"]),
        ("How much sooner will I reach it?", ["5 month(s)", "instead of 6 month(s)", "1 month(s) sooner"]),
        ("Show me how you calculated that", ["16,000 ÷ 2,800, rounded up = 6", "16,000 ÷ 3,300, rounded up = 5"]),
    ]
    for question, expected in steps:
        r = ask(question, sample_user, history=h)
        for text in expected:
            assert text in r["answer"], (question, r["answer"])
        assert r["goal"] == "Laptop and course"
        turn(h, question, r)


def test_spending_follow_ups(sample_user):
    h = []
    r = ask("How much did I spend on transport?", sample_user, h)
    assert "300 SDG on Transport" in r["answer"]
    turn(h, "How much did I spend on transport?", r)
    r = ask("and food?", sample_user, h)
    assert "1,800 SDG on Food" in r["answer"] and "56.25%" in r["answer"]
    turn(h, "and food?", r)
    r = ask("What about last month?", sample_user, h)
    assert "1,600 SDG on Food" in r["answer"]                # Sept: 100+300+200+650+350


def test_an_old_what_if_is_not_reused_by_a_new_one(sample_user):
    h = []
    r = ask("What if I save an extra 500 every month?", sample_user, h)
    turn(h, "What if I save an extra 500 every month?", r)
    r = ask("What if I save an extra 1000 every month?", sample_user, h)
    assert "2,800 + 1,000 = 3,800" in r["answer"]            # replaces, not 2,800 + 1,500
    turn(h, "x", r)
    r = ask("What if I also cut restaurants by 200?", sample_user, h)
    assert "2,800 + 1,000" not in r["answer"] or "−" in r["answer"]
    data = r["tool_results"][0]["result"]
    assert data["scenario"]["expenses"] == 3000 and data["scenario"]["monthly_saving"] == 4000


# ---------- data rules ----------

def test_users_never_see_each_others_numbers(sample_user):
    other = crud.add_user("other")
    crud.add_income(other, 100, "Salary", f"{THIS}-01")
    crud.add_transaction(other, "x", 40, "Food", f"{THIS}-02", "Lunch")
    assert "40 SDG" in ask("How much did I spend this month?", other)["answer"]
    assert "3,200 SDG" in ask("How much did I spend this month?", sample_user)["answer"]


def test_periods_and_month_boundaries():
    uid = crud.add_user("periods")
    crud.add_income(uid, 1000, "Salary", f"{THIS}-01")
    crud.add_transaction(uid, "end of last month", 70, "Food", f"{LAST}-31" if LAST[5:] in
                         ("01", "03", "05", "07", "08", "10", "12") else f"{LAST}-28", "Lunch")
    crud.add_transaction(uid, "start of this month", 30, "Food", f"{THIS}-01", "Lunch")
    assert "30 SDG" in ask("How much did I spend this month?", uid)["answer"]
    assert "70 SDG" in ask("How much did I spend last month?", uid)["answer"]
    r = ask("How much did I spend over the last 2 months?", uid)
    assert "100 SDG in total" in r["answer"] and "50 SDG a month on average" in r["answer"]


def test_refunds_are_netted_and_duplicates_flagged():
    uid = crud.add_user("refunds")
    crud.add_income(uid, 1000, "Salary", f"{THIS}-01")
    crud.add_transaction(uid, "shoes", 300, "Shopping", f"{THIS}-03", "Clothes")
    crud.add_transaction(uid, "shoes returned", -100, "Shopping", f"{THIS}-04", "Clothes")
    crud.add_transaction(uid, "coffee", 10, "Food", f"{THIS}-05", "Breakfast")
    crud.add_transaction(uid, "coffee", 10, "Food", f"{THIS}-05", "Breakfast")
    r = ask("How much did I spend this month?", uid)
    assert "220 SDG" in r["answer"]
    steps = " ".join(r["calculation"])
    assert "refund" in steps and "duplicates" in steps


def test_decimal_sums_have_no_float_noise():
    uid = crud.add_user("decimals")
    crud.add_income(uid, 1, "Salary", f"{THIS}-01")
    crud.add_transaction(uid, "a", 0.1, "Food", f"{THIS}-02", "Lunch")
    crud.add_transaction(uid, "b", 0.2, "Food", f"{THIS}-02", "Lunch")
    assert facts.spending(uid, [THIS])["total"] == facts.dec("0.3")
    r = ask("How much did I save this month?", uid)
    assert "0.70 SDG left" in r["answer"] and "70.00%" in r["answer"]


def test_spending_above_income_and_no_income():
    uid = crud.add_user("deficit")
    crud.add_income(uid, 500, "Salary", f"{THIS}-01")
    crud.add_transaction(uid, "rent", 800, "Bills", f"{THIS}-02", "Rent")
    assert "300 SDG more than your income" in ask("How much did I save this month?", uid)["answer"]
    nobody = crud.add_user("no income")
    crud.add_transaction(nobody, "rent", 800, "Bills", f"{THIS}-02", "Rent")
    assert "no income is recorded" in ask("How much did I save this month?", nobody)["answer"]


def test_empty_user_gets_a_clear_message():
    uid = crud.add_user("empty")
    assert "no spending recorded yet" in ask("How much did I spend this month?", uid)["answer"]


def test_goal_without_deadline_asks_only_for_the_date():
    uid = _spec_user()
    conn = get_connection()
    conn.execute("UPDATE financial_goals SET target_date = NULL WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()
    r = ask("Can I reach my goal on time?", uid)
    assert "By what date" in r["answer"] and "16,000 SDG still needed" in r["answer"]
    assert "target amount" not in r["answer"]


def test_budget_remaining(sample_user):
    r = ask("How much is left in my budget?", sample_user)
    data = r["tool_results"][0]["result"]
    assert data["remaining"] == round(data["allocated"] - data["spent"], 2)
    assert data["spent"] == 3200


def test_compare_and_why(sample_user):
    r = ask("Compare this month with last month", sample_user)
    assert "330 SDG more" in r["answer"] and "+11.50%" in r["answer"]
    r = ask("Why am I spending more?", sample_user)
    assert "Restaurant (Food) +150 (650 → 800)" in r["answer"] and "Headphones" in r["answer"]


# ---------- parsing ----------

@pytest.mark.parametrize("question,expected", [
    ("How much did I spend this month?", "total"),
    ("What is my biggest spending category?", "top"),
    ("How much is left in my budget?", "budget"),
    ("Compare this month with last month", "compare"),
    ("Why am I spending more?", "why"),
    ("What if I save 500 more monthly?", "what_if"),
    ("Show me how you calculated that", "explain"),
    ("كم صرفت على الطعام هذا الشهر؟", "category"),
    ("ما هو أكبر مصروف عندي؟", "top"),
    ("What is an emergency fund?", None),
    ("What will the dollar exchange rate be next month?", None),
])
def test_intents(question, expected):
    assert intent.detect(question, THIS).intent == expected


def test_language_follows_the_question():
    assert intent.language_of("كم صرفت؟", "en") == "ar"
    assert intent.language_of("How much did I spend?", "ar") == "en"
