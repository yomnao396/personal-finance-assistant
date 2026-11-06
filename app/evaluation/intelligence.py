# app/evaluation/intelligence.py
# Grades the assistant on the cases in intelligence_cases.py, one dimension at a
# time, so a wrong answer shows WHERE it went wrong:
#   intent      the task it recognised
#   entities    what it extracted from the question (changes, months, categories)
#   retrieval   figures it read from the records
#   calculation figures it worked out from them
#   answer      the text: required content present, nothing contradicting it,
#               every sum correct
#   fabricated  money amounts in the answer that no record, question or
#               calculation contains
# `ask(question, history)` is any function returning the /chat response shape
# (answer, tools_used, context, tool_results), so the same grading runs on the
# Python call and on the HTTP API.

import json
import re
from decimal import Decimal

from app.finance import facts
from app.finance.verify import EXPRESSION, NUMBER, amounts_in, check_arithmetic, unsupported_amounts
from app.services import intent as parse
from app.services.goal_context import list_goals

DIMENSIONS = ("intent", "entities", "retrieval", "calculation", "answer")


def _get(data, path):
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _same(a, b):
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.01")
    if isinstance(b, list) and isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(b, dict) and isinstance(a, dict):
        return all(_same(a.get(k), v) for k, v in b.items())
    return a == b


def record_figures(user_id):
    """Every figure the user's records support directly: each transaction, the
    monthly, category, subcategory and income-source totals, the budget lines,
    and each goal's stored and required amounts."""
    out = {Decimal(100)}                                # the "× 100" of a percentage
    for month in facts.expense_months(user_id):
        snap = facts.month_snapshot(user_id, month)
        spent = snap["spending"]
        out.update(t["amount"] for t in spent["transactions"])
        out.update(spent["by_category"].values())
        out.update(v for subs in spent["by_subcategory"].values() for v in subs.values())
        out.update(v for v in (spent["total"], snap["income"]["total"], snap["remaining"]) if v is not None)
        out.update(snap["income"]["by_source"].values())
    from app.engines.budget_engine import calculate_budget
    plan = calculate_budget(user_id)                    # the budget engine's lines are figures too
    if "error" not in plan:
        out.update(Decimal(str(v)) for k, v in plan.items() if not k.startswith("_") and isinstance(v, (int, float)))
    for g in list_goals(user_id)["goals"]:
        out.update(Decimal(str(g[k])) for k in ("target_amount", "saved_so_far", "remaining_amount",
                                                "required_monthly_saving") if g.get(k) is not None)
    return out


CENT = Decimal("0.01")
# "1,020 short (4,000 − 2,980)": a result written before its working
RESULT_FIRST = re.compile(rf"({NUMBER})[^\d(]{{0,25}}\(((?:{NUMBER})(?:\s*[+\-−×÷*/]\s*(?:{NUMBER}))+)\)")
PERCENT_TIMES = re.compile(rf"({NUMBER})\s*×\s*({NUMBER})\s*%\s*=\s*({NUMBER})")


def _q(text):
    return Decimal(text.replace(",", "")).quantize(CENT)


def _derived(answer, backed):
    """What the answer may state: backed figures, one addition or subtraction of
    two of them, and results whose working is shown with backed inputs (repeated,
    so a result can feed the next step)."""
    base = {Decimal(str(b)).quantize(CENT) for b in backed}
    backed = set(base) | {abs(a - b) for a in base for b in base} | {a + b for a in base for b in base}
    plain = (answer or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))
    for _ in range(4):
        grew = False
        found = [(m.group(1), m.group(2)) for m in EXPRESSION.finditer(plain)] + \
                [(m.group(2), m.group(1)) for m in RESULT_FIRST.finditer(plain)]
        for expression, result in found:
            inputs = [_q(n) for n in re.findall(NUMBER, expression)]
            value = _q(result.lstrip("-"))
            if all(i in backed or i <= 100 for i in inputs) and value not in backed:
                backed.add(value)
                grew = True
        for m in PERCENT_TIMES.finditer(plain):
            if _q(m.group(1)) in backed and _q(m.group(3)) not in backed:
                backed.add(_q(m.group(3)))
                grew = True
        if not grew:
            break
    return backed


def understood(response):
    """The task the assistant acted on, as one word."""
    context = response.get("context") or {}
    tools = [t["name"] if isinstance(t, dict) else t for t in response.get("tools_used") or []]
    if context.get("awaiting") and not tools:
        return "clarify"
    if "explain_calculation" in tools:
        return "explain"
    return context.get("intent")


def _entities(case):
    """Compare what the parser extracts from the question with the case."""
    expected = case["entities"]
    if not expected:
        return None
    q = parse.detect(case["question"], "2026-10")
    problems = []
    if "months" in expected and q.months != expected["months"]:
        problems.append(f"months {q.months} != {expected['months']}")
    if "categories" in expected and [list(c) for c in q.categories] != expected["categories"]:
        problems.append(f"categories {q.categories} != {expected['categories']}")
    if "changes" in expected:
        got = [[c.target, c.mode, c.value] for c in q.changes]
        if not _same(got, expected["changes"]):
            problems.append(f"changes {got} != {expected['changes']}")
    return problems


def grade(case, response, figures=frozenset()):
    """{dimension: True / False / None (not checked)} plus the problems found."""
    data = (response.get("tool_results") or [{}])[0].get("result") or {}
    answer = response.get("answer") or ""
    problems, result = [], {}

    got = understood(response)
    result["intent"] = got == case["intent"]
    if not result["intent"]:
        problems.append(f"intent {got!r} != {case['intent']!r}")

    entity_problems = _entities(case)
    result["entities"] = None if entity_problems is None else not entity_problems
    problems += entity_problems or []

    for dim, key in (("retrieval", "retrieval"), ("calculation", "calc")):
        if not case[key]:
            result[dim] = None
            continue
        wrong = [f"{path}={_get(data, path)!r} != {want!r}" for path, want in case[key].items()
                 if not _same(_get(data, path), want)]
        result[dim] = not wrong
        problems += wrong

    missing = [s for s in case["says"] if s not in answer]
    present = [s for s in case["never_says"] if s in answer]
    _, sums = check_arithmetic(answer)
    result["answer"] = not (missing or present or sums) and bool(answer.strip())
    problems += [f"answer lacks {m!r}" for m in missing] + [f"answer has {p!r}" for p in present] + \
        [f"wrong sum: {s}" for s in sums]

    # Wrong sums are already counted above: here only amounts that come from nowhere.
    # The engines' own outputs (tool results) are calculations, not model text.
    backed = set(figures) | set(amounts_in(case["question"])) | {a for h in case["history"] for a in amounts_in(h)} \
        | set(amounts_in(json.dumps(data, default=str)))
    fabricated = unsupported_amounts(answer, _derived(answer, backed))
    result["fabricated"] = [float(v) for v in fabricated]
    if fabricated:
        problems.append(f"amounts not backed by the data: {fabricated}")
    return result, problems


def run(cases, ask, figures=frozenset()):
    """Run every case; returns (rows, summary). `figures` is record_figures(user)."""
    rows = []
    for case in cases:
        history = []
        for earlier in case["history"]:
            r = ask(earlier, history)
            history += [{"role": "user", "content": earlier},
                        {"role": "assistant", "content": r["answer"], "goal": r.get("goal"),
                         "context": r.get("context")}]
        response = ask(case["question"], history)
        result, problems = grade(case, response, figures)
        rows.append({"id": case["id"], "group": case["group"], "question": case["question"],
                     "reply": response.get("answer", ""), "fast_path": response.get("fast_path"),
                     **result, "problems": problems})
    return rows, summarise(rows)


def summarise(rows):
    out = {}
    for dim in DIMENSIONS:
        checked = [r[dim] for r in rows if r[dim] is not None]
        out[dim] = {"passed": sum(checked), "checked": len(checked),
                    "rate": round(sum(checked) / len(checked), 3) if checked else None}
    fabricated = sum(1 for r in rows if r["fabricated"])
    out["fabricated_answers"] = {"count": fabricated, "of": len(rows), "rate": round(fabricated / len(rows), 3)}
    out["fully_correct"] = sum(1 for r in rows if not r["problems"])
    out["cases"] = len(rows)
    return out
