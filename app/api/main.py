import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import CATEGORY_TREE, validate_category, validate_income_source
from app.database.schema import create_tables
from app.database import crud
from app.engines.financial_engine import analyze_finances
from app.engines.budget_engine import calculate_budget
from app.engines import insights_engine
from app.engines.recommendation_engine import (
    generate_recommendation_plan,
    generate_recommendations,
)
from app.preferences import user_preferences as prefs
from app.tools.financial_tools import (
    get_category_spending,
    calculate_goal,
)
from app.llm.agent import run_agent_detailed
from app.ml.goal_assessment import assess_goal
from app.ml.prediction import data_state
from app.services.importer import TEMPLATE_CSV, import_csv_text
from app.services.onboarding import create_user, save_profile
from app.services.scenario import rank_goals, simulate_scenario
from app.finance import decisions
from app.services.money_tips import money_tips
from app.finance.calculations import compound_interest, simple_interest
from app.rag import knowledge
from app.llm.qwen import LLMUnavailable, status as llm_status

logger = logging.getLogger(__name__)
# One line per chat answer: which stages ran and how long they took (counts and
# timings only; see app/services/trace.py). Shown in the uvicorn console.
_processing_log = logging.getLogger("pfa.processing")
if not _processing_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    _processing_log.addHandler(_handler)
    _processing_log.setLevel(logging.INFO)
    _processing_log.propagate = False
from app.ml.prediction import predict_spending as forecast_spending
from app.ml.recommendation_model import model_is_available


@asynccontextmanager
async def lifespan(_app):
    # Create the tables when the server starts (not on import, so importing the
    # app in tests never touches the real database file).
    create_tables()
    yield


app = FastAPI(title="Personal Finance AI Assistant", lifespan=lifespan)


class TransactionIn(BaseModel):
    user_id: int = 1
    description: str
    amount: float
    category: str
    subcategory: str | None = None
    date: str | None = None


class PreferenceIn(BaseModel):
    user_id: int = 1
    main_category: str
    subcategory: str | None = None
    is_essential: bool = True


class TransactionUpdate(BaseModel):
    description: str | None = None
    amount: float | None = None
    category: str | None = None
    subcategory: str | None = None
    date: str | None = None


class IncomeIn(BaseModel):
    user_id: int = 1
    amount: float
    source: str | None = None
    date: str | None = None


class RecurringIn(BaseModel):
    user_id: int = 1
    description: str
    amount: float
    category: str
    subcategory: str | None = None
    frequency: str = "monthly"


class GoalIn(BaseModel):
    user_id: int = 1
    name: str
    target_amount: float
    current_amount: float = 0
    target_date: str | None = None


class GoalUpdate(BaseModel):
    name: str | None = None
    target_amount: float | None = None
    current_amount: float | None = None
    target_date: str | None = None


class GoalOrderIn(BaseModel):
    user_id: int = 1
    goal_ids: list[int]


class ContributionIn(BaseModel):
    amount: float


class UserIn(BaseModel):
    name: str
    email: str | None = None


class GoalSpec(BaseModel):
    name: str
    target_amount: float
    months: int = 6


class ProfileIn(BaseModel):
    monthly_income: float
    category_estimates: dict[str, float] = {}
    current_savings: float = 0
    income_source: str = "Salary"
    goal: GoalSpec | None = None


class ScenarioIn(BaseModel):
    user_id: int = 1
    target_amount: float | None = None
    months: int | None = None
    goal_name: str | None = None
    cut_category: str | None = None
    cut_amount: float = 0
    extra_saving: float = 0
    income_change_pct: float = 0
    extra_months: int = 0


class ImportIn(BaseModel):
    user_id: int = 1
    csv: str


class ChatIn(BaseModel):
    user_id: int = 1
    message: str
    history: list | None = None
    language: str = "en"
    mode: str = "full"


def _require_positive(value, field="amount"):
    if value is None or value <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be greater than 0",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def system_status():
    """What is ready for the demo: API, recommendation model, local LLM."""
    return {
        "api": "ok",
        "recommendation_model": model_is_available(),
        "llm": llm_status(),
    }


@app.post("/demo/reset")
def reset_demo():
    """Wipe all user data, load the three-month sample user (user 1) and the
    demo profiles A–E plus a new onboarding-only user."""
    from scripts.seed_profiles import seed_profiles
    from scripts.seed_sample import seed

    crud.clear_user_data()
    uid = seed()
    return {"status": "reset", "user_id": uid, "profiles": seed_profiles()}


# ---------- users & onboarding ----------
@app.get("/users")
def users():
    out = []
    for u in crud.list_users():
        state, months = data_state(u["id"])
        out.append({**u, "data_state": state, "months_of_history": months})
    return out


@app.post("/users")
def new_user(u: UserIn):
    if not u.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    return {"id": create_user(u.name.strip(), u.email), "status": "created"}


@app.post("/users/{user_id}/profile")
def onboarding(user_id: int, p: ProfileIn):
    try:
        return save_profile(user_id, p.monthly_income, p.category_estimates,
                            p.current_savings, p.income_source,
                            p.goal.model_dump() if p.goal else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/data-state")
def user_data_state(user_id: int = 1):
    state, months = data_state(user_id)
    return {"user_id": user_id, "data_state": state, "months_of_history": months}


@app.get("/categories")
def categories():
    tree = crud.get_category_tree() or CATEGORY_TREE
    return {"categories": tree}


@app.post("/transactions")
def add_transaction(t: TransactionIn):
    if t.amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="amount must be greater than 0",
        )

    try:
        validate_category(t.category, t.subcategory)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    tid = crud.add_transaction(
        t.user_id,
        t.description,
        t.amount,
        t.category,
        t.date,
        t.subcategory,
    )

    return {"id": tid, "status": "added"}


@app.get("/transactions")
def list_transactions(user_id: int = 1):
    return crud.get_transactions(user_id)


@app.put("/transactions/{transaction_id}")
def change_transaction(transaction_id: int, t: TransactionUpdate):
    if t.amount is not None:
        _require_positive(t.amount)

    try:
        changed = crud.update_transaction(
            transaction_id,
            description=t.description,
            amount=t.amount,
            category=t.category,
            subcategory=t.subcategory,
            date=t.date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not changed:
        raise HTTPException(status_code=404, detail="transaction not found")

    return {"status": "updated", "id": transaction_id}


@app.delete("/transactions/{transaction_id}")
def remove_transaction(transaction_id: int):
    crud.delete_transaction(transaction_id)
    return {"status": "deleted", "id": transaction_id}


@app.get("/transactions/import/template")
def import_template():
    return {"csv": TEMPLATE_CSV}


@app.post("/transactions/import")
def import_transactions(body: ImportIn):
    try:
        return import_csv_text(body.user_id, body.csv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/income")
def list_income(user_id: int = 1):
    return crud.get_income_records(user_id)


@app.post("/income")
def add_income(i: IncomeIn):
    _require_positive(i.amount)

    try:
        validate_income_source(i.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    crud.add_income(i.user_id, i.amount, i.source, i.date)
    return {"status": "added"}


@app.delete("/income/{income_id}")
def remove_income(income_id: int):
    if not crud.delete_income(income_id):
        raise HTTPException(status_code=404, detail="income record not found")
    return {"status": "deleted", "id": income_id}


@app.get("/recurring")
def list_recurring(user_id: int = 1):
    return crud.get_recurring_expenses(user_id)


@app.post("/recurring")
def add_recurring(r: RecurringIn):
    _require_positive(r.amount)

    try:
        crud.add_recurring_expense(
            r.user_id,
            r.description,
            r.amount,
            r.category,
            r.frequency,
            r.subcategory,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "added"}


@app.delete("/recurring/{expense_id}")
def remove_recurring(expense_id: int):
    if not crud.delete_recurring_expense(expense_id):
        raise HTTPException(status_code=404, detail="recurring expense not found")
    return {"status": "deleted", "id": expense_id}


@app.get("/analysis")
def analysis(user_id: int = 1):
    return analyze_finances(user_id)


@app.get("/analysis/categories")
def analysis_categories(user_id: int = 1):
    return analyze_finances(user_id)["category_analysis"]


@app.get("/analysis/income")
def analysis_income(user_id: int = 1):
    return analyze_finances(user_id)["income_analysis"]


@app.get("/analysis/patterns")
def analysis_patterns(user_id: int = 1):
    return analyze_finances(user_id)["spending_patterns"]


@app.get("/analysis/savings-goal")
def analysis_savings_goal(user_id: int = 1):
    data = analyze_finances(user_id)

    return {
        "savings_analysis": data["savings_analysis"],
        "goal_analysis": data["goal_analysis"],
        "goals": data["goals"],
    }


@app.get("/budget")
def budget(user_id: int = 1):
    return calculate_budget(user_id)


@app.get("/spending")
def spending(
    user_id: int = 1,
    category: str | None = None,
):
    return get_category_spending(user_id, category)


@app.get("/prediction")
def prediction(user_id: int = 1):
    return forecast_spending(user_id)


@app.get("/goals")
def goals(user_id: int = 1):
    return crud.get_goals(user_id)


@app.post("/goals")
def create_goal(g: GoalIn):
    _require_positive(g.target_amount, "target_amount")

    if g.current_amount < 0:
        raise HTTPException(status_code=400, detail="current_amount cannot be negative")

    gid = crud.add_goal(
        g.user_id,
        g.name,
        g.target_amount,
        g.current_amount,
        g.target_date,
    )
    return {"id": gid, "status": "added"}


@app.put("/goals/order")
def order_goals(o: GoalOrderIn):
    """Save the drag-and-drop priority order; the first goal drives the plan."""
    crud.set_goal_priorities(o.user_id, o.goal_ids)
    return {"status": "ordered", "goal_ids": o.goal_ids}


@app.put("/goals/{goal_id}")
def change_goal(goal_id: int, g: GoalUpdate):
    if g.target_amount is not None:
        _require_positive(g.target_amount, "target_amount")

    if not crud.update_goal(
        goal_id,
        name=g.name,
        target_amount=g.target_amount,
        current_amount=g.current_amount,
        target_date=g.target_date,
    ):
        raise HTTPException(status_code=404, detail="goal not found")

    return {"status": "updated", "id": goal_id}


@app.post("/goals/{goal_id}/contribute")
def contribute(goal_id: int, c: ContributionIn):
    _require_positive(c.amount)

    goal = crud.contribute_to_goal(goal_id, c.amount)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")

    return goal


@app.delete("/goals/{goal_id}")
def remove_goal(goal_id: int):
    if not crud.delete_goal(goal_id):
        raise HTTPException(status_code=404, detail="goal not found")
    return {"status": "deleted", "id": goal_id}


@app.get("/goals/rank")
def goals_rank(user_id: int = 1):
    return rank_goals(user_id)


@app.get("/assess_goal")
def assess(user_id: int = 1, target_amount: float | None = None, months: int | None = None,
           goal_name: str | None = None):
    """Probability of reaching a target in time, with a label and an action."""
    return assess_goal(user_id, target_amount, months, goal_name)


@app.post("/simulate")
def simulate(sc: ScenarioIn):
    return simulate_scenario(sc.user_id, sc.target_amount, sc.months, sc.goal_name,
                             sc.cut_category, sc.cut_amount, sc.extra_saving,
                             sc.income_change_pct, sc.extra_months)


# ---------- decision support (read-only: nothing here writes) ----------

@app.get("/decisions/position")
def decision_position(user_id: int = 1):
    """The verified money picture decisions use: surplus, goal needs, free money."""
    return decisions.as_json(decisions.position(user_id))


@app.get("/decisions/purchase")
def decision_purchase(price: float, user_id: int = 1, delay_months: int | None = None,
                      installments: int | None = None, monthly_payment: float | None = None,
                      cheaper_price: float | None = None, urgent: bool | None = None):
    for value, name in ((price, "price"), (monthly_payment, "monthly_payment"), (cheaper_price, "cheaper_price")):
        if value is not None:
            _require_positive(value, name)
    for value, name in ((delay_months, "delay_months"), (installments, "installments")):
        if value is not None and not 1 <= value <= 120:
            raise HTTPException(status_code=400, detail=f"{name} must be between 1 and 120")
    result = decisions.assess_purchase(decisions.position(user_id), price, delay_months, installments,
                                       monthly_payment, cheaper_price, urgent)
    return decisions.as_json({**result, "hypothetical": True})


@app.get("/decisions/emergency")
def decision_emergency(amount: float, user_id: int = 1):
    _require_positive(amount)
    return decisions.as_json({**decisions.plan_emergency(decisions.position(user_id), amount), "hypothetical": True})


@app.get("/decisions/goal-priorities")
def decision_goal_priorities(user_id: int = 1, preferred: str | None = None):
    return decisions.as_json(decisions.prioritize_goals(decisions.position(user_id), preferred))


@app.get("/decisions/saving-plan")
def decision_saving_plan(user_id: int = 1):
    pos = decisions.position(user_id)
    if not pos["month"]:
        return {"error": "no spending recorded"}
    return decisions.as_json(decisions.saving_plan(user_id, pos["month"]))


@app.get("/interest")
def interest(principal: float, annual_rate_pct: float, years: float,
             compound: bool = False, times_per_year: int = 12):
    try:
        if compound:
            return compound_interest(principal, annual_rate_pct, years, times_per_year)
        return simple_interest(principal, annual_rate_pct, years)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/tips")
def tips(user_id: int = 1):
    """Practical money tips; a tip carries this user's numbers when their data supports it."""
    return money_tips(user_id)


# ---------- knowledge base (RAG) ----------
@app.get("/knowledge")
def knowledge_entries():
    return {"entries": knowledge.list_entries()}


@app.get("/knowledge/search")
def knowledge_search(q: str, lang: str | None = None, k: int = 3):
    return knowledge.search(q, k=k, lang=lang)


@app.get("/goal-check")
def goal_check(
    user_id: int = 1,
    goal_name: str | None = None,
    monthly_saving: float | None = None,
):
    return calculate_goal(
        user_id,
        goal_name,
        monthly_saving,
    )


@app.get("/recommendations")
def recommendations(user_id: int = 1):
    return {
        "recommendations": generate_recommendations(user_id)
    }


@app.get("/recommendations/plan")
def recommendation_plan(user_id: int = 1):
    return generate_recommendation_plan(user_id)


class InsightStatusIn(BaseModel):
    user_id: int = 1
    key: str
    status: str


@app.get("/recommendations/insights")
def recommendation_insights(user_id: int = 1):
    """Evidence-based advice cards; the plan is passed in so the two never disagree."""
    analysis = analyze_finances(user_id)
    plan = generate_recommendation_plan(user_id, analysis=analysis)
    return insights_engine.build_insights(user_id, analysis=analysis, plan=plan)


@app.post("/recommendations/status")
def recommendation_status(body: InsightStatusIn):
    try:
        card = insights_engine.set_status(body.user_id, body.key, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if card is None:
        raise HTTPException(status_code=404, detail="no such advice card for this user")
    return {"key": card["key"], "status": card["status"]}


@app.get("/recommendations/what-if")
def recommendation_what_if(user_id: int = 1, spend_less: float = 0.0,
                           to_goal: float | None = None):
    """Read-only preview: nothing is saved."""
    return insights_engine.preview_change(user_id, spend_less, to_goal)


@app.get("/preferences")
def list_preferences(user_id: int = 1):
    return {
        "preferences": prefs.list_preferences(user_id)
    }


@app.post("/preferences")
def create_preference(p: PreferenceIn):
    try:
        pid = prefs.set_preference(
            p.user_id,
            p.main_category,
            p.subcategory,
            p.is_essential,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return {
        "id": pid,
        "status": "saved",
        "note": "takes effect immediately — no model retraining required",
    }


@app.put("/preferences")
def change_preference(p: PreferenceIn):
    try:
        changed = prefs.update_preference(
            p.user_id,
            p.main_category,
            p.subcategory,
            p.is_essential,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    if not changed:
        raise HTTPException(
            status_code=404,
            detail="preference not found",
        )

    return {"status": "updated"}


@app.delete("/preferences")
def remove_preference(
    user_id: int = 1,
    main_category: str = "",
    subcategory: str = "",
):
    if not prefs.delete_preference(
        user_id,
        main_category,
        subcategory or None,
    ):
        raise HTTPException(
            status_code=404,
            detail="preference not found",
        )

    return {"status": "deleted"}


@app.post("/preferences/defaults")
def seed_preferences(user_id: int = 1):
    return {
        "written": prefs.seed_default_preferences(user_id)
    }


@app.post("/chat")
def chat(c: ChatIn):
    if c.mode not in ("full", "no_rag", "no_tools"):
        raise HTTPException(status_code=400, detail="mode must be full, no_rag or no_tools")

    try:
        result = run_agent_detailed(
            c.message,
            history=c.history,
            user_id=c.user_id,
            language=c.language,
            mode=c.mode,
        )
    except LLMUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:  # noqa: BLE001
        # The full traceback goes to the server log under a short reference; the
        # user sees the reference, not internals.
        ref = uuid.uuid4().hex[:8]
        logger.exception("chat failed [ref %s]", ref)
        raise HTTPException(status_code=500, detail=(
            f"something went wrong while answering (reference {ref}; details are in "
            "the API server log). Please try again."))

    return {
        "answer": result["answer"],
        "tools_used": [t["name"] for t in result["tools_used"]],
        "sources": result["sources"],
        "goal": result.get("goal"),
        # Sent back with the next question so follow-ups continue from this answer.
        "context": result.get("context"),
        "calculation": result.get("calculation"),
        "fast_path": bool(result.get("fast_path")),
        # The structured figures behind the answer (used by the evaluation).
        "tool_results": result.get("tool_results", []),
        # Which processing stages ran and how long they took (no financial values).
        "processing": result.get("processing"),
    }