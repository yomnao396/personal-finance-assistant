# app/tools/financial_tools.py

# "Tools" are ordinary functions with a simple, JSON-friendly output.
# Qwen is told these exist and calls them to get real data.
# Each tool works on its own WITHOUT the LLM.
#
# UPDATED: classify_transaction was REMOVED with the expense classifier.
# Six new tools expose the expanded analysis, the recommendation plan and the
# user's essential-expense preferences.

from app.database.crud import (
    get_transactions as _get_txns,
    get_recurring_expenses as _get_rec,
    get_expense_transactions,
    get_goals,
    get_category_tree,
)
from app.finance.calculations import calculate_category_spending, calculate_total_expenses
from app.engines.financial_engine import analyze_finances as _analyze
from app.engines.budget_engine import calculate_budget as _budget
from app.engines.goals_engine import calculate_goal_time, calculate_goal_progress
from app.engines.recommendation_engine import (
    generate_recommendation_plan as _plan,
    generate_recommendations as _recs,
)
from app.preferences import user_preferences as _prefs
from app.config import CATEGORY_TREE
from app.finance.calculations import compound_interest, simple_interest
from app.ml.goal_assessment import assess_goal as _assess
from app.services.scenario import rank_goals as _rank, simulate_scenario as _simulate
from app.services.onboarding import save_profile as _save_profile
from app.services.goal_context import find_goal as _find_goal, list_goals as _list_goals
from app.engines.goals_engine import calculate_goal_gap
from app.rag.knowledge import search as _search
from app.engines.insights_engine import build_insights as _insights


# ---------- data ----------
def get_transactions(user_id: int = 1, month: str = None, limit: int = 20):
    """The user's expenses, newest first (optionally one month like 2026-08)."""
    rows = sorted(_get_txns(user_id, month),
                  key=lambda t: (t.get("date") or "", t["id"]), reverse=True)
    return {"month": month or "all", "count": len(rows), "transactions": rows[:limit]}


def _tree_name(name, names):
    """'restaurants' or 'FOOD' -> the tree's own spelling ('Restaurant', 'Food')."""
    key = (name or "").strip().lower()
    for n in names:
        if key in (n.lower(), n.lower() + "s", n.lower() + "es"):
            return n
    return name


def _months_of(transactions):
    return sorted({(t.get("date") or "")[:7] for t in transactions if t.get("date")})


def get_monthly_spending(user_id: int = 1, month: str = None):
    """Total spending in one month (default: this month, the latest with data),
    every month's total, and how this month compares with earlier months."""
    txns = get_expense_transactions(user_id)
    by_month = {m: calculate_total_expenses([t for t in txns if (t.get("date") or "")[:7] == m])
                for m in _months_of(txns)}
    if not by_month:
        return {"error": "no dated spending recorded yet"}
    month = month if month in by_month else max(by_month)
    months = list(by_month)
    i = months.index(month)
    return {
        "month": month,
        "total": by_month[month],
        "by_month": by_month,
        "change_vs_earlier_months": {
            f"{k} month(s) earlier ({months[i - k]})": round(by_month[month] - by_month[months[i - k]], 2)
            for k in range(1, i + 1)
        },
    }


def get_category_spending(user_id: int = 1, category: str = None, subcategory: str = None,
                          month: str = None):
    """Spending in one category (and optionally one subcategory): in one month
    (default: this month, the latest with data), per month, and over all months."""
    txns = get_expense_transactions(user_id)
    months = _months_of(txns)
    month = month if month in months else (months[-1] if months else None)
    if not category:
        this = [t for t in txns if (t.get("date") or "")[:7] == month]
        return {"month": month, "by_category": calculate_category_spending(this)}

    category = _tree_name(category, CATEGORY_TREE)
    subcategory = _tree_name(subcategory, CATEGORY_TREE.get(category, [])) if subcategory else None
    picked = [t for t in txns if t.get("category") == category
              and (not subcategory or (t.get("subcategory") or "") == subcategory)]
    by_month = {m: round(sum(t["amount"] for t in picked if (t.get("date") or "")[:7] == m), 2)
                for m in months}
    return {
        "category": category,
        "subcategory": subcategory,
        "month": month,
        "amount_this_month": by_month.get(month, 0.0),
        "by_month": by_month,
        "total_all_months": round(sum(by_month.values()), 2),
    }


def get_current_savings(user_id: int = 1):
    return {"savings": _analyze(user_id)["savings"]}


def get_recurring_expenses(user_id: int = 1):
    return _get_rec(user_id)


def get_categories(user_id: int = 1):
    """The official category/subcategory tree, read from the database."""
    return {"categories": get_category_tree()}


# ---------- analysis ----------
def analyze_finances(user_id: int = 1):
    return _analyze(user_id)


def analyze_categories(user_id: int = 1):
    """Spending by category and subcategory, percentages, ranking, dominance."""
    a = _analyze(user_id)["category_analysis"]
    return {
        "window": a["window"],
        "total_expenses": a["total_expenses"],
        "by_category": a["by_category"],
        "percent_of_expenses": a["percent_of_expenses"],
        "percent_of_income": a["percent_of_income"],
        "ranking": a["ranking"],
        "highest_category": a["highest_category"],
        "lowest_category": a["lowest_category"],
        "by_subcategory": a["by_subcategory"],
        "dominant_subcategory": a["dominant_subcategory"],
    }


def analyze_income(user_id: int = 1):
    """Total income, income by source, percentages, primary source, trend."""
    return _analyze(user_id)["income_analysis"]


def analyze_spending_patterns(user_id: int = 1):
    """Monthly spending, per-category trends, highest/lowest month."""
    return _analyze(user_id)["spending_patterns"]


def analyze_savings_and_goal(user_id: int = 1):
    """Savings, saving rate, the goal and the monthly saving gap."""
    a = _analyze(user_id)
    return {
        "savings_analysis": a["savings_analysis"],
        "goal_analysis": a["goal_analysis"],
        "goals": a["goals"],
    }


def predict_spending(user_id: int = 1):
    from app.ml.prediction import predict_spending as _pred
    return _pred(user_id)


def calculate_budget(user_id: int = 1):
    return _budget(user_id)


def list_goals(user_id: int = 1):
    """The user's saved goals as stored on the Goals page, with what each needs."""
    return _list_goals(user_id)


def calculate_goal(
    user_id: int = 1,
    goal_name: str = None,
    monthly_saving: float = None,
):
    goals = get_goals(user_id)
    if not goals:
        return {"error": "no goals found"}

    if goal_name:
        goal, error = _find_goal(user_id, goal_name)
        if error:
            return error
    else:
        goal = goals[0]

    if monthly_saving is None:
        monthly_saving = _analyze(user_id)["savings"]

    out = calculate_goal_progress(goal)
    out.update(calculate_goal_time(goal, monthly_saving))
    out["goal_name"] = goal["name"]
    return out


# ---------- recommendations ----------
def get_recommendation_plan(user_id: int = 1, goal_name: str = None):
    """The full strategic plan: model actions + real amounts + protected expenses.
    By default it plans for the primary goal; goal_name plans for another saved goal."""
    if not goal_name:
        analysis = _analyze(user_id)
        plan = _plan(user_id, analysis=analysis)
        plan["evidence_based_tips"] = _tips(user_id, analysis, plan)
        return plan
    goal, error = _find_goal(user_id, goal_name)
    if error:
        return error
    analysis = _analyze(user_id)
    analysis["goal_analysis"] = calculate_goal_gap(goal, analysis["savings"])
    return _plan(user_id, analysis=analysis)


def _tips(user_id, analysis, plan, limit=4):
    """The top advice cards in short form, so the assistant explains the same
    verified numbers the Plan page shows. Never fails the plan itself."""
    try:
        cards = _insights(user_id, analysis=analysis, plan=plan)["insights"]
    except Exception:  # noqa: BLE001
        return []
    return [{"finding": c["summary"], "action": c["action"],
             "saves_per_month": (c.get("impact") or {}).get("monthly_saving"),
             "optional": c["optional"]} for c in cards[:limit]]


def generate_recommendations(user_id: int = 1):
    return {"recommendations": _recs(user_id)}


# ---------- user preferences ----------
def get_user_preferences(user_id: int = 1):
    prefs = _prefs.list_preferences(user_id)

    return {
        "preferences": prefs,
        "essential": [
            f"{p['main_category']}/{p['subcategory']}" if p["subcategory"]
            else p["main_category"]
            for p in prefs
            if p["is_essential"]
        ],
        "flexible": [
            f"{p['main_category']}/{p['subcategory']}" if p["subcategory"]
            else p["main_category"]
            for p in prefs
            if not p["is_essential"]
        ],
    }


def set_user_preference(
    user_id: int = 1,
    category: str = None,
    subcategory: str = None,
    is_essential: bool = True,
):
    """Mark a category or subcategory as essential (protected) or flexible."""
    try:
        _prefs.set_preference(
            user_id,
            category,
            subcategory,
            bool(is_essential),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    return {
        "status": "saved",
        "category": category,
        "subcategory": subcategory,
        "is_essential": bool(is_essential),
        "note": "preferences take effect immediately; no model retraining is needed",
    }


# ---------- future / what-if ----------
def assess_goal(user_id: int = 1, target_amount: float = None, months: int = None,
                goal_name: str = None):
    """Probability of reaching a target (or a stored goal) in time + an action."""
    return _assess(user_id, target_amount, months, goal_name)


def simulate_scenario(user_id: int = 1, target_amount: float = None, months: int = None,
                      goal_name: str = None, cut_category: str = None, cut_amount: float = 0,
                      extra_saving: float = 0, income_change_pct: float = 0,
                      extra_months: int = 0):
    """Baseline vs what-if probability for a goal."""
    return _simulate(user_id, target_amount, months, goal_name, cut_category, cut_amount,
                     extra_saving, income_change_pct, extra_months)


def rank_goals(user_id: int = 1):
    """Order the user's goals by importance x urgency, with a feasibility check."""
    return _rank(user_id)


def save_profile(user_id: int = 1, monthly_income: float = None, category_estimates: dict = None,
                 current_savings: float = 0, goal_name: str = None,
                 goal_amount: float = None, goal_months: int = None):
    """Store a NEW user's onboarding answers (estimates, not history)."""
    goal = ({"name": goal_name, "target_amount": goal_amount, "months": goal_months}
            if goal_name and goal_amount else None)
    try:
        return _save_profile(user_id, monthly_income, category_estimates, current_savings,
                             goal=goal)
    except ValueError as exc:
        return {"error": str(exc)}


def calculate_interest(principal: float, annual_rate_pct: float, years: float,
                       compound: bool = False, times_per_year: int = 12):
    """Simple or compound interest, calculated exactly in Python."""
    try:
        if compound:
            return compound_interest(principal, annual_rate_pct, years, times_per_year)
        return simple_interest(principal, annual_rate_pct, years)
    except ValueError as exc:
        return {"error": str(exc)}


# ---------- knowledge (RAG) ----------
def search_knowledge(query: str, language: str = None):
    """Closest passages from the verified knowledge base, each with its source."""
    result = _search(query, lang=language)
    for r in result["results"]:
        r.pop("lang", None)
    return result


# Registry so the agent can look tools up by name.
TOOL_FUNCTIONS = {
    "get_transactions": get_transactions,
    "get_monthly_spending": get_monthly_spending,
    "get_category_spending": get_category_spending,
    "get_current_savings": get_current_savings,
    "get_recurring_expenses": get_recurring_expenses,
    "get_categories": get_categories,
    "predict_spending": predict_spending,
    "calculate_budget": calculate_budget,
    "calculate_goal": calculate_goal,
    "analyze_finances": analyze_finances,
    "analyze_categories": analyze_categories,
    "analyze_income": analyze_income,
    "analyze_spending_patterns": analyze_spending_patterns,
    "analyze_savings_and_goal": analyze_savings_and_goal,
    "get_recommendation_plan": get_recommendation_plan,
    "generate_recommendations": generate_recommendations,
    "get_user_preferences": get_user_preferences,
    "set_user_preference": set_user_preference,
    "list_goals": list_goals,
    "assess_goal": assess_goal,
    "simulate_scenario": simulate_scenario,
    "rank_goals": rank_goals,
    "save_profile": save_profile,
    "calculate_interest": calculate_interest,
    "search_knowledge": search_knowledge,
}


# Descriptions Qwen reads to decide which tool to call (Ollama tool-calling format).
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_category_spending",
            "description": (
                "How much the user spent in one category (optionally one subcategory such "
                "as Restaurant): this month, every month, and the total over all months. "
                "Use for 'how much did I spend on food this month' or 'on restaurants in "
                "the last three months'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "e.g. Food, Bills, Shopping"},
                    "subcategory": {"type": "string", "description": "e.g. Restaurant, Lunch, Rent"},
                    "month": {"type": "string", "description": "YYYY-MM; leave out for this month"},
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly_spending",
            "description": (
                "The user's total spending this month (or a month like 2026-08), every "
                "month's total, and the difference from each earlier month. Use for 'how "
                "much did I spend this month' or 'how much more than two months ago'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "string"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_savings",
            "description": "Get the user's current monthly savings (income minus expenses).",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": (
                "List the user's individual expenses (description, amount, category, "
                "subcategory, date), newest first. Use for 'what did I buy', "
                "'show my last expenses' or questions about one specific purchase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "optional month like 2026-08"},
                    "limit": {"type": "integer", "description": "how many to return, default 20"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recurring_expenses",
            "description": (
                "List the user's recurring bills and subscriptions (e.g. internet, rent) "
                "with amount, category and how often they are paid."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_categories",
            "description": "List the official categories and subcategories stored in the database.",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_categories",
            "description": (
                "Category analysis: spending per category and subcategory, each "
                "category's percentage of expenses and of income, the ranking, the "
                "highest and lowest category, and the dominant subcategory."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_income",
            "description": (
                "Income analysis: total income, income by source, each source's "
                "percentage, the primary source, how many sources, monthly income "
                "history and the income trend."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_spending_patterns",
            "description": (
                "Spending pattern analysis: monthly spending, spending per category "
                "per month, which categories are increasing, decreasing or stable, "
                "the average monthly spending and the highest and lowest months."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_savings_and_goal",
            "description": (
                "Savings and goal analysis: monthly saving, saving rate, the goal, "
                "the required monthly saving and the monthly saving gap."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "predict_spending",
            "description": "Predict the user's total spending for next month.",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_budget",
            "description": "Build a personalized monthly budget for the user.",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_goal",
            "description": "Check a savings goal: progress and how many months to reach it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_name": {"type": "string"},
                    "monthly_saving": {"type": "number"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_finances",
            "description": "Full financial summary: income, expenses, savings and all analyses.",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recommendation_plan",
            "description": (
                "The strategic recommendation plan: which actions the model "
                "predicted, exactly how much to cut from each category and "
                "subcategory, which expenses are protected, and the remaining gap. "
                "Use this for any 'what should I do', 'what should I cut', 'how can I save' "
                "or 'how do I reach my goal faster' question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_name": {"type": "string",
                                  "description": "a saved goal to plan for; omit for the main goal"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_recommendations",
            "description": "Short recommendation sentences built from the user's real numbers.",
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_preferences",
            "description": (
                "List which categories and subcategories the user marked as "
                "essential (protected) or flexible."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_preference",
            "description": (
                "Mark a category or subcategory as essential (never reduced) or "
                "flexible. Use it when the user says something like 'Lunch is "
                "essential' or 'Restaurant is flexible'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "subcategory": {"type": "string"},
                    "is_essential": {"type": "boolean"},
                },
                "required": ["category", "is_essential"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_goals",
            "description": (
                "The user's SAVED goals from the Goals page: name, target amount, amount saved, "
                "remaining amount, deadline, months left and the monthly saving the deadline "
                "needs. Call this before asking the user anything about a goal."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assess_goal",
            "description": (
                "Can the user afford something / reach a goal in time? Returns the probability, "
                "a Likely/Borderline/Unlikely label and a concrete action, using the user's own "
                "spending forecast. Use for 'can I afford a 5000 laptop in 4 months?' (pass "
                "target_amount and months) or for a stored goal (pass goal_name, or nothing)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_amount": {"type": "number", "description": "price / amount needed"},
                    "months": {"type": "integer", "description": "how many months from now"},
                    "goal_name": {"type": "string", "description": "name of a saved goal"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_scenario",
            "description": (
                "What-if: recompute the goal probability with one change — spend less in a "
                "category, save extra each month, an income change in percent, or more months. "
                "Returns baseline vs scenario."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_amount": {"type": "number"},
                    "months": {"type": "integer"},
                    "goal_name": {"type": "string"},
                    "cut_category": {"type": "string", "description": "e.g. Shopping"},
                    "cut_amount": {"type": "number", "description": "less spent per month"},
                    "extra_saving": {"type": "number", "description": "more saved per month"},
                    "income_change_pct": {"type": "number", "description": "e.g. -10 for a 10% cut"},
                    "extra_months": {"type": "integer", "description": "deadline moved later"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_goals",
            "description": (
                "Which goal should come first? Ranks the user's goals by importance x urgency "
                "and says whether they compete for the same money."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_profile",
            "description": (
                "Onboarding for a NEW user with no data: store their monthly income, typical "
                "monthly spending per category, current savings and one goal. Only call after "
                "the user has given these numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monthly_income": {"type": "number"},
                    "category_estimates": {
                        "type": "object",
                        "description": "typical monthly amount per category, e.g. {\"Food\": 1500, \"Bills\": 800}",
                    },
                    "current_savings": {"type": "number"},
                    "goal_name": {"type": "string"},
                    "goal_amount": {"type": "number"},
                    "goal_months": {"type": "integer"},
                },
                "required": ["monthly_income"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_interest",
            "description": (
                "Exact simple or compound interest on an amount. Never calculate interest "
                "yourself — call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "principal": {"type": "number"},
                    "annual_rate_pct": {"type": "number", "description": "e.g. 5 for 5% a year"},
                    "years": {"type": "number"},
                    "compound": {"type": "boolean", "description": "true for compound interest"},
                    "times_per_year": {"type": "integer", "description": "how often interest is added per year: 1 = once a year, 4 = quarterly, 12 = monthly (default)"},
                },
                "required": ["principal", "annual_rate_pct", "years"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search the verified financial knowledge base (budgeting rules, emergency funds, "
                "interest, debt repayment, inflation, Islamic finance). Use for any general "
                "money question or definition that is not about the user's own numbers. "
                "Answer only from the returned passages and name their source."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the question, in the user's words"},
                    "language": {"type": "string", "description": "en or ar"},
                },
                "required": ["query"],
            },
        },
    },
]
