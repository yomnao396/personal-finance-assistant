# tests/test_recommendation_engine.py
# Verifies the Strategic Recommendation Engine: single and multiple actions, every
# supported action type, contradiction filtering, protected expenses, dynamic
# amounts and the honest remaining gap.
#
# Most tests inject a FIXED set of predicted actions so the engine's own logic is
# what is being tested; the last two run the real Random Forest end to end.

from datetime import date

import pytest

from app.database import crud
from app.engines import recommendation_engine as engine
from app.preferences import user_preferences as prefs


TODAY = date.today()


def _month(offset):
    year = TODAY.year + (TODAY.month - 1 + offset) // 12
    month = (TODAY.month - 1 + offset) % 12 + 1
    return f"{year:04d}-{month:02d}"


def make_user(income=6000, spending=None, goal=None, preferences=None, months=3):
    """Build a user with `months` of identical monthly spending, plus an optional
    goal and preference set. spending: [(category, subcategory, amount), ...]"""
    uid = crud.add_user("Engine Test User")

    for offset in range(-(months - 1), 1):
        month = _month(offset)
        crud.add_income(uid, income, "Salary", f"{month}-01")

        for category, subcategory, amount in (spending or []):
            crud.add_transaction(
                uid,
                f"{category} spend",
                amount,
                category,
                f"{month}-10",
                subcategory,
            )

    if goal:
        target, current, months_ahead = goal
        year = TODAY.year + (TODAY.month - 1 + months_ahead) // 12
        month_no = (TODAY.month - 1 + months_ahead) % 12 + 1

        crud.add_goal(
            uid,
            "Test goal",
            target,
            current,
            f"{year:04d}-{month_no:02d}-01",
        )

    for category, subcategory, essential in (preferences or []):
        prefs.set_preference(uid, category, subcategory, essential)

    return uid


@pytest.fixture
def fixed_actions(monkeypatch):
    """Force the 'model' to return a chosen action set."""

    def _apply(actions):
        monkeypatch.setattr(
            engine,
            "predict_actions",
            lambda features: {
                "actions": list(actions),
                "action_names": list(actions),
                "labels": {},
                "probabilities": {},
                "source": "test_stub",
            },
        )

    return _apply


@pytest.fixture
def sample_user():
    """Create the seeded demonstration user for the real end-to-end tests."""
    from scripts.seed_sample import seed

    return seed()


# Standard user: 3200 of monthly spending, 6000 income, 2800 saved.
STANDARD_SPENDING = [
    ("Food", "Breakfast", 100),
    ("Food", "Lunch", 300),
    ("Food", "Dinner", 200),
    ("Food", "Restaurant", 800),
    ("Food", "Fast Food", 400),
    ("Shopping", "Clothes", 300),
    ("Shopping", "Electronics", 100),
    ("Transport", None, 300),
    ("Bills", "Electricity", 150),
    ("Bills", "Water", 50),
    ("Bills", "Internet", 100),
    ("Utilities", None, 100),
    ("Entertainment", "Movie", 150),
    ("Education", None, 100),
    ("Healthcare", None, 50),
]

STANDARD_PREFS = [
    ("Food", "Breakfast", True),
    ("Food", "Lunch", True),
    ("Food", "Dinner", True),
    ("Food", "Restaurant", False),
    ("Food", "Fast Food", False),
]


# ---------------- one recommendation ----------------


def test_single_recommendation_reduce_food(fixed_actions):
    """One action, one reduction. Food flexible money is 1200 (Restaurant 800 +
    Fast Food 400), so at most 30% = 360 can be cut, and a 300 gap is fully closed."""
    uid = make_user(
        goal=(6000, 0, 2),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )  # needs 3000/month, saves 2800 -> gap 200

    fixed_actions(["Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["financial_gap"]["target_amount"] == 200.0
    assert len(plan["reductions"]) == 1

    r = plan["reductions"][0]

    assert r["category"] == "Food"
    assert r["reduce_by"] == 200.0
    assert plan["financial_gap"]["gap_closed"] is True
    assert plan["financial_gap"]["remaining_gap"] == 0.0


# ---------------- multiple recommendations ----------------


def test_multiple_recommendations_split_the_gap(fixed_actions):
    """Two actions share the gap in proportion to each category's capacity:
    Food capacity 360, Shopping capacity 120 -> 480 total, split 75%/25%."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )  # needs 4800, saves 2800 -> gap 2000

    fixed_actions(["Reduce_Food", "Reduce_Shopping"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["financial_gap"]["target_amount"] == 2000.0

    categories = {r["category"]: r for r in plan["reductions"]}

    assert set(categories) == {"Food", "Shopping"}
    assert categories["Food"]["reduce_by"] == 360.0
    assert categories["Shopping"]["reduce_by"] == 120.0
    assert plan["financial_gap"]["total_planned_reduction"] == 480.0


def test_reduce_entertainment(fixed_actions):
    """Entertainment is 150 and fully flexible -> at most 45 can be cut."""
    uid = make_user(
        goal=(12000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Entertainment"])
    plan = engine.generate_recommendation_plan(uid)

    r = plan["reductions"][0]

    assert r["category"] == "Entertainment"
    assert r["reduce_by"] == 45.0
    assert r["subcategory_plan"][0]["subcategory"] == "Movie"


def test_reduce_transport(fixed_actions):
    """Transport has no subcategories; it is still reducible as a whole."""
    uid = make_user(
        goal=(12000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Transport"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["reductions"][0]["category"] == "Transport"
    assert plan["reductions"][0]["reduce_by"] == 90.0


def test_reduce_shopping(fixed_actions):
    """Reduce Shopping cuts 30% of the 400 fully-flexible Shopping spend and splits it
    across both subcategories. Expected: 120, over Clothes and Electronics."""
    uid = make_user(
        goal=(12000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Shopping"])
    plan = engine.generate_recommendation_plan(uid)

    r = plan["reductions"][0]

    assert r["category"] == "Shopping"
    assert r["reduce_by"] == 120.0
    assert {s["subcategory"] for s in r["subcategory_plan"]} == {
        "Clothes",
        "Electronics",
    }


# ---------------- income / saving / timeline actions ----------------


def test_increase_income_amount_equals_the_remaining_gap(fixed_actions):
    """After cutting what it can, the engine asks for exactly the money still
    missing — never a round invented number."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )  # gap 2000

    fixed_actions(["Reduce_Food", "Increase_Income"])
    plan = engine.generate_recommendation_plan(uid)

    remaining = plan["financial_gap"]["remaining_gap"]

    assert plan["income_actions"][0]["required_extra_monthly_income"] == remaining
    assert remaining == round(
        2000 - plan["financial_gap"]["total_planned_reduction"],
        2,
    )


def test_increase_saving_without_a_goal(fixed_actions):
    """With no goal there is no gap, so Increase Saving targets the 20% rate:
    income 6000 -> target 1200, already saving 800 -> 400 to find."""
    spending = [
        ("Food", "Restaurant", 3000),
        ("Shopping", "Clothes", 2200),
    ]

    uid = make_user(spending=spending)

    fixed_actions(["Increase_Saving"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["financial_gap"]["target_amount"] == 400.0
    assert plan["saving_actions"][0]["target_saving_rate"] == 20.0
    assert plan["saving_actions"][0]["current_monthly_saving"] == 800.0


def test_maintain_current_plan(fixed_actions):
    """A user who is on track keeps the plan and gets no reductions."""
    uid = make_user(
        goal=(4000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Maintain_Current_Plan"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["maintain_current_plan"] is True
    assert plan["reductions"] == []
    assert plan["financial_gap"]["target_amount"] == 0.0
    assert "On track" in plan["summary"]


def test_extend_goal_timeline_computes_the_new_horizon(fixed_actions):
    """The engine works out how long the goal really takes at the achievable
    saving rate: remaining 19200 at 2800+360 per month = 7 months, 3 more than 4."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Food", "Extend_Goal_Timeline"])
    plan = engine.generate_recommendation_plan(uid)

    t = plan["timeline_actions"][0]

    assert t["achievable_monthly_saving"] == 3160.0
    assert t["months_needed_at_that_rate"] == 7
    assert t["extra_months_required"] == 3


# ---------------- contradiction filtering ----------------


def test_maintain_plus_reduce_is_filtered_when_there_is_a_gap(fixed_actions):
    """Maintain Current Plan cannot survive next to a real funding gap."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Maintain_Current_Plan", "Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["maintain_current_plan"] is False
    assert [f["action"] for f in plan["filtered_out"]] == [
        "Maintain_Current_Plan"
    ]
    assert plan["reductions"][0]["category"] == "Food"


def test_maintain_wins_when_the_user_is_on_track(fixed_actions):
    """No gap: the corrective action is dropped instead, never both."""
    uid = make_user(
        goal=(4000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Maintain_Current_Plan", "Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["maintain_current_plan"] is True
    assert plan["reductions"] == []
    assert [f["action"] for f in plan["filtered_out"]] == ["Reduce_Food"]


def test_reduce_a_category_with_no_spending_is_filtered(fixed_actions):
    """The engine will not ask to cut something the user does not spend on."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=[("Food", "Restaurant", 3200)],
    )

    fixed_actions(["Reduce_Shopping"])
    plan = engine.generate_recommendation_plan(uid)

    assert any(
        f["action"] == "Reduce_Shopping"
        for f in plan["filtered_out"]
    )


def test_extend_without_a_goal_is_filtered(fixed_actions):
    """There is nothing to extend when the user has no goal. Expected: the action appears
    in filtered_out."""
    uid = make_user(spending=[("Food", "Restaurant", 3000)])

    fixed_actions(["Extend_Goal_Timeline"])
    plan = engine.generate_recommendation_plan(uid)

    assert any(
        f["action"] == "Extend_Goal_Timeline"
        for f in plan["filtered_out"]
    )


# ---------------- protected expenses ----------------


def test_protected_subcategories_are_never_reduced(fixed_actions):
    """Breakfast, Lunch and Dinner are essential: they appear as protected and
    never receive a reduction line."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    r = plan["reductions"][0]

    assert r["protected_spending"] == 600.0
    assert r["eligible_spending"] == 1200.0

    touched = {line["subcategory"] for line in r["subcategory_plan"]}

    assert touched == {"Restaurant", "Fast Food"}
    assert touched.isdisjoint({"Breakfast", "Lunch", "Dinner"})


def test_fully_protected_category_blocks_the_action_and_substitutes(fixed_actions):
    """If EVERY Food subcategory is essential, Reduce Food is dropped with a
    reason, and the engine looks for another category that still has flexible
    money instead of violating the constraint."""
    all_food_essential = [
        ("Food", sub, True)
        for sub in ("Breakfast", "Lunch", "Dinner", "Restaurant", "Fast Food")
    ]

    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=all_food_essential,
    )

    fixed_actions(["Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    assert plan["blocked_by_preferences"][0]["action"] == "Reduce_Food"
    assert plan["blocked_by_preferences"][0]["protected_amount"] == 1800.0
    assert all(r["category"] != "Food" for r in plan["reductions"])
    assert plan["substituted_actions"], "a substitute category should have been found"


def test_no_reduction_ever_touches_protected_money(fixed_actions):
    """Across every reduction, the cut can never exceed the flexible money."""
    uid = make_user(
        goal=(19200, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions([
        "Reduce_Food",
        "Reduce_Shopping",
        "Reduce_Entertainment",
    ])

    plan = engine.generate_recommendation_plan(uid)

    for r in plan["reductions"]:
        assert r["reduce_by"] <= r["eligible_spending"]
        assert r["reduce_by"] <= r["max_reduction_allowed"] + 0.01


# ---------------- amounts are derived, never hard-coded ----------------


def test_amounts_scale_with_actual_spending(fixed_actions):
    """Double the flexible spending, double the possible reduction."""
    small = make_user(
        goal=(48000, 0, 4),
        spending=[("Food", "Restaurant", 1000)],
    )

    big = make_user(
        goal=(48000, 0, 4),
        spending=[("Food", "Restaurant", 2000)],
    )

    fixed_actions(["Reduce_Food"])

    a = engine.generate_recommendation_plan(small)["reductions"][0]["reduce_by"]
    b = engine.generate_recommendation_plan(big)["reductions"][0]["reduce_by"]

    assert a == 300.0 and b == 600.0


def test_remaining_gap_is_reported_not_fabricated(fixed_actions):
    """When the cuts cannot cover the gap, the shortfall is stated exactly."""
    uid = make_user(
        goal=(48000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )  # needs 12000/month, saves 2800

    fixed_actions(["Reduce_Food", "Reduce_Shopping"])
    plan = engine.generate_recommendation_plan(uid)

    gap = plan["financial_gap"]

    assert gap["target_amount"] == 9200.0
    assert gap["total_planned_reduction"] == 480.0
    assert gap["remaining_gap"] == 8720.0
    assert gap["gap_closed"] is False
    assert "still open" in plan["summary"]


def test_engine_adds_a_funding_action_when_a_gap_is_left(fixed_actions):
    """An open gap is never left without an action; the added action is marked as
    derived so it is clear the model did not predict it."""
    uid = make_user(
        goal=(48000, 0, 4),
        spending=STANDARD_SPENDING,
        preferences=STANDARD_PREFS,
    )

    fixed_actions(["Reduce_Food"])
    plan = engine.generate_recommendation_plan(uid)

    derived = {a["action"] for a in plan["derived_actions"]}

    assert "Increase_Income" in derived
    assert (
        plan["income_actions"][0]["required_extra_monthly_income"]
        == plan["financial_gap"]["remaining_gap"]
    )


# ---------------- end to end with the real Random Forest ----------------


def test_end_to_end_plan_with_the_trained_model(sample_user):
    """The seeded demonstration user, straight through the real pipeline."""
    plan = engine.generate_recommendation_plan(sample_user)

    assert plan["prediction_source"] == "random_forest"
    assert plan["financial_gap"]["target_amount"] == 1200.0
    assert plan["reductions"], "the model should have asked for at least one reduction"

    for r in plan["reductions"]:
        assert r["reduce_by"] > 0

        for line in r["subcategory_plan"]:
            assert line["subcategory"] not in (
                "Breakfast",
                "Lunch",
                "Dinner",
            )


def test_text_recommendations_are_built_from_the_plan(sample_user):
    """The short sentences quote the plan's own numbers."""
    lines = engine.generate_recommendations(sample_user)

    assert lines and isinstance(lines[0], str)
    assert any("Reduce" in line or "On track" in line for line in lines)