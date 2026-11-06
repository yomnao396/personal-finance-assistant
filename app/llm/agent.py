# app/llm/agent.py
# The tool-calling loop. Qwen reads the question, calls tools for every number
# and every fact, and explains what the tools returned.
#
# run_agent_detailed() also reports WHICH tools were used and WHICH sources were
# retrieved, so the interface can show the reason and the source behind each
# answer (proposal Â§1.6.1). Its `mode` switches parts off for the evaluation:
#   "full"     â€” all tools, including the knowledge base (RAG)
#   "no_rag"   â€” all tools except search_knowledge; general questions are
#                answered from the model's own memory
#   "no_tools" â€” no tools at all; the user's raw records are pasted into the
#                prompt and the model must do the arithmetic itself

import inspect
import json
import re
import time
from decimal import Decimal

from app.database.crud import get_goals
from app.finance import facts
from app.finance.verify import amounts_in, check_arithmetic, unsupported_amounts, unsupported_percents
from app.llm.qwen import chat
from app.llm.prompts import SYSTEM_PROMPT
from app.rag.knowledge import list_entries, search
from app.services import trace
from app.services.direct_answers import answer as direct_answer
from app.services.goal_context import goal_facts, planned_tool, resolve_goal
from app.services.intent import ASKS, asked_parts, find_period, language_of
from app.tools.financial_tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

MODES = ("full", "no_rag", "no_tools")
CURRENCY = "SDG (Sudanese pounds)"
ARABIC = re.compile(r"[\u0600-\u06FF]")
CJK = re.compile(r"[\u3040-\u30FF\u3400-\u9FFF]")
DOLLAR_AMOUNT = re.compile(r"(?:US)?\$\s?(\d[\d,]*(?:\.\d+)?)")
CURRENCY_WORD = {"en": "SDG", "ar": "Ø¬.Ø³"}
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"(?<![(\[])https?://[^\s)\]]+")
UNVERIFIED = {"en": "(unverified source removed)", "ar": "(Ø£ÙØ²ÙŠÙ„ Ù…ØµØ¯Ø± ØºÙŠØ± Ù…ÙˆØ«Ù‘Ù‚)"}
ANSWER_NOW = "Answer my question now, in one or two sentences, using only the tool results above."
ONE_LANGUAGE = {"en": "Write that answer again in English only.",
                "ar": "Write that answer again in Arabic only."}
NO_ANSWER = {
    "en": "I could not put together an answer to that. Please try asking it another way.",
    "ar": "Ù„Ù… Ø£ØªÙ…ÙƒÙ† Ù…Ù† ØªÙƒÙˆÙŠÙ† Ø¥Ø¬Ø§Ø¨Ø© Ø¹Ù„Ù‰ Ø°Ù„Ùƒ. ÙŠÙØ±Ø¬Ù‰ Ø¥Ø¹Ø§Ø¯Ø© ØµÙŠØ§ØºØ© Ø§Ù„Ø³Ø¤Ø§Ù„.",
}

# When to put knowledge-base notes in front of the model. Notes about budgeting
# distract a small model from questions about the user's OWN numbers, so a
# personal question gets them only on a strong match. Thresholds measured on the
# 30-question benchmark: personal-number questions scored 0.135-0.275, general
# questions 0.242-0.568.
STRONG_MATCH = 0.30
PERSONAL = re.compile(
    r"\b(my|me|i'm|am i|did i|have i|can i afford|do i need|i spent|i saved)\b"
    r"|صرفت|صرفتي|دخلي|راتبي|هدفي|مصروفاتي|إنفاقي|انفاقي|مدخراتي|ادخرت|أدخرت|ميزانيتي|أستطيع|وضعي|عندي|ادخاري|مصاريفي|مصروفي|صرفي|اهدافي|أهدافي|حالتي|مرتبي",
    re.IGNORECASE)

DEFINITION = re.compile(r"\bwhat (?:is|are) (?:an? |the )?(?!my\b)\w+|\bdefine\b|\bmeaning of\b|"
                        r"(?:Ù…Ø§|Ù…Ø§Ù‡Ùˆ|Ù…Ø§ Ù‡Ùˆ|Ù…Ø§ Ù‡ÙŠ|Ø§ÙŠØ´|Ø´Ù†Ùˆ) (?:Ù‡Ùˆ |Ù‡ÙŠ )?(?!Ø¯Ø®Ù„ÙŠ|Ù…ØµØ±ÙˆÙØ§ØªÙŠ)[^\sØŸ?]+|ÙŠØ¹Ù†ÙŠ Ø§ÙŠÙ‡|Ù…Ø¹Ù†ÙŠ")
RETRIEVED_NOTES = """Verified notes that match the user's question. If the question is a
general money question, answer ONLY from these notes and name their source
(publisher and title). If it is about the user's own numbers, ignore them and use
the tools. Never cite any other source.
{notes}"""
MAX_TOOL_ROUNDS = 5

# The user's saved goals, put in front of the model for goal questions so it uses
# the Goals page instead of asking the user to type the numbers again.
GOAL_TOOLS = {"assess_goal", "simulate_scenario", "calculate_goal", "get_recommendation_plan"}
SAVED_GOALS = """The user's saved goals (from the Goals page; amounts in SDG). These are
already known: never ask the user for a saved goal's amount, savings or deadline.
{lines}
{focus}"""
GOAL_FOCUS = {
    "named": 'This question is about the saved goal "{name}".',
    "earlier": 'This question continues the conversation about the saved goal "{name}".',
    "only goal": 'This question is about the saved goal "{name}", the user\'s only open goal.',
    "only dated goal": ('This question is about the saved goal "{name}", the only open goal with '
                        "a deadline; say which goal you mean."),
}
GOAL_TOOL_HINT = ('For numbers about it, call assess_goal (can I reach it / on time), '
                  'simulate_scenario (what if I save more / cut / wait) or '
                  'get_recommendation_plan (how to reach it faster, what to cut) with '
                  'goal_name="{name}". Treat a what-if as hypothetical: nothing is changed.')
NO_GOALS = ("The user has no saved goals. Say so, and ask only for the target amount and "
            "the date, or suggest adding the goal on the Goals page.")
HYPOTHETICAL = ("This is a hypothetical purchase, not a saved goal: call assess_goal with "
                "target_amount and months.")
NOT_FOUND = {
    "en": ('You have no saved goal called "{asked}". Your saved goals are: {names}. Do you mean '
           "one of them? To add a new goal, use the Goals page: only its target amount and date "
           "are needed."),
    "ar": ('Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ù„Ø¯ÙŠÙƒ Ù‡Ø¯Ù Ù…Ø­ÙÙˆØ¸ Ø¨Ø§Ø³Ù… "{asked}". Ø£Ù‡Ø¯Ø§ÙÙƒ Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø©: {names}. Ù‡Ù„ ØªÙ‚ØµØ¯ Ø£Ø­Ø¯Ù‡Ø§ØŸ '
           "Ù„Ø¥Ø¶Ø§ÙØ© Ù‡Ø¯Ù Ø¬Ø¯ÙŠØ¯ Ø§Ø³ØªØ®Ø¯Ù… ØµÙØ­Ø© Ø§Ù„Ø£Ù‡Ø¯Ø§Ù: ÙŠÙƒÙÙŠ Ø§Ù„Ù…Ø¨Ù„Øº Ø§Ù„Ù…Ø³ØªÙ‡Ø¯Ù ÙˆØ§Ù„ØªØ§Ø±ÙŠØ®."),
}
WHICH_GOAL = {
    "en": "More than one of your saved goals matches that: {names}. Which one do you mean?",
    "ar": "Ø£ÙƒØ«Ø± Ù…Ù† Ù‡Ø¯Ù Ù…Ø­ÙÙˆØ¸ ÙŠØ·Ø§Ø¨Ù‚ Ø°Ù„Ùƒ: {names}. Ø£ÙŠÙ‘Ù‡Ø§ ØªÙ‚ØµØ¯ØŸ",
}
CHOOSE_GOAL = {
    "en": "You have {count} saved goals: {names}. Which one do you mean?",
    "ar": "Ù„Ø¯ÙŠÙƒ {count} Ø£Ù‡Ø¯Ø§Ù Ù…Ø­ÙÙˆØ¸Ø©: {names}. Ø£ÙŠÙ‘Ù‡Ø§ ØªÙ‚ØµØ¯ØŸ",
}
GOAL_SHORT = {
    "en": '"{name}" ({remaining:,.0f} SDG left, {deadline})',
    "ar": '"{name}" (ÙŠØªØ¨Ù‚Ù‰ {remaining:,.0f} Ø¬.Ø³ØŒ {deadline})',
}
NO_DEADLINE = {"en": "no deadline", "ar": "Ø¨Ø¯ÙˆÙ† Ù…ÙˆØ¹Ø¯"}
DUE = {"en": "due {date}", "ar": "Ù…ÙˆØ¹Ø¯Ù‡ {date}"}

LANGUAGE_INSTRUCTIONS = {
    "en": "Always answer in English.",
    "ar": (
        "Always answer in Modern Standard Arabic. Keep numbers as digits and "
        "keep category names understandable (you may add the English name in brackets)."
    ),
}

NO_RAG_PROMPT = """You are a Personal Finance AI Assistant for a single user.
All amounts are in Sudanese pounds (SDG).
Use the tools for the user's own numbers. Answer general money questions from
your own knowledge. You are an educational assistant, not a licensed financial advisor.
"""

NO_TOOLS_PROMPT = """You are a Personal Finance AI Assistant for a single user.
All amounts are in Sudanese pounds (SDG).
You have no tools. The user's records are given below as JSON; work out any
numbers yourself from them. Answer general money questions from your own
knowledge. You are an educational assistant, not a licensed financial advisor.

USER RECORDS:
{records}
"""


def call_tool(name, args, user_id=1):
    """Run one tool safely and return a JSON-friendly result."""
    func = TOOL_FUNCTIONS.get(name)

    if func is None:
        return {"error": f"unknown tool {name}"}

    params = inspect.signature(func).parameters
    # A small model sometimes adds an argument a tool does not take (e.g. "limit");
    # drop it instead of failing the whole call (found by the benchmark).
    args = {k: v for k, v in dict(args or {}).items() if k in params}

    if "user_id" in params:
        args["user_id"] = user_id

    try:
        result = func(**args)

    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}

    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{name} failed: {exc.__class__.__name__}: {exc}"
        }

    # Bare numbers invite the model to add "$"; name the currency next to them.
    if isinstance(result, dict) and "error" not in result:
        result = {"currency": CURRENCY, **result}
    return result


def _user_records(user_id):
    from app.database import crud

    return json.dumps({
        "income": crud.get_income_records(user_id),
        "expenses": crud.get_expense_transactions(user_id),
        "recurring_expenses": crud.get_recurring_expenses(user_id),
        "goals": crud.get_goals(user_id),
    }, default=str)


def _setup(mode, user_id, language):
    language_rule = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["en"])
    if mode == "no_tools":
        return NO_TOOLS_PROMPT.format(records=_user_records(user_id)) + "\n" + language_rule, None
    if mode == "no_rag":
        tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] != "search_knowledge"]
        return NO_RAG_PROMPT + "\n" + language_rule, tools
    return SYSTEM_PROMPT + "\n" + language_rule, TOOL_SCHEMAS


def _sources(result):
    """The distinct sources inside a search_knowledge result."""
    out = []
    for r in result.get("results", []) if isinstance(result, dict) else []:
        source = dict(r["source"], entry_id=r["entry_id"], passage_id=r["passage_id"])
        if all(s["entry_id"] != source["entry_id"] for s in out):
            out.append(source)
    return out


def _for_model(question):
    """Qwen 2.5 3B often ends its reply at once (an empty answer, no tool call)
    when an Arabic question comes with the tool list. Framing the question with
    a short English instruction makes it call the tools reliably (found in UAT).
    Only what the model sees changes; the user's words stay as they were."""
    if not ARABIC.search(question):
        return question
    return (f'The user asked, in Arabic: "{question}"\n'
            f"Use the tools you need, then answer in Arabic.")


def fix_currency(answer, language="en"):
    """Every amount in this app is in Sudanese pounds; a small model sometimes
    still writes "$2,476". Rewrite such amounts as "2,476 SDG" (or Ø¬.Ø³)."""
    unit = CURRENCY_WORD.get(language, "SDG")
    answer = DOLLAR_AMOUNT.sub(lambda m: f"{m.group(1)} {unit}", answer or "")
    if language == "ar":    # tool sentences say "SDG"; Arabic answers use Ø¬.Ø³
        answer = re.sub(r"\bSDG\b", unit, answer)
    return answer


MAX_HISTORY = 6


def _money_amounts(text):
    return [a for a in amounts_in(text) if a >= 100]


def _wrong_language(text, language):
    """An English answer written mostly in Arabic, or the other way round (a small
    model copies the language of earlier turns or of a note)."""
    arabic = len(re.findall(r"[Ø¡-ÙŠ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if language == "en":
        return arabic > latin
    if language == "ar":
        return latin > 3 * arabic + 20
    return False


def _strip_cjk(text):
    """Drop sentences written in Chinese or Japanese from an Arabic or English answer."""
    parts = re.split(r"(?<=[.!?ã€‚ï¼ï¼ŸØŸ\n])", text or "")
    return "".join(p for p in parts if not CJK.search(p)).strip()


def _own_numbers_question(question):
    """A question about the user's own figures ("how much did I spend?")."""
    return bool(PERSONAL.search(question or "") and ASKS.search(question.lower()))


UNVERIFIED_ANSWER = {
    "en": "I could not verify that answer against your records, so I will not show it. Try asking for one "
          "figure at a time (for example: how much did I spend in October?).",
    "ar": "Ù„Ù… Ø£ØªÙ…ÙƒÙ† Ù…Ù† Ø§Ù„ØªØ­Ù‚Ù‚ Ù…Ù† Ù‡Ø°Ù‡ Ø§Ù„Ø¥Ø¬Ø§Ø¨Ø© Ù…Ù‚Ø§Ø¨Ù„ Ø³Ø¬Ù„Ø§ØªÙƒØŒ Ù„Ø°Ù„Ùƒ Ù„Ù† Ø£Ø¹Ø±Ø¶Ù‡Ø§. Ø¬Ø±Ù‘Ø¨ Ø£Ù† ØªØ³Ø£Ù„ Ø¹Ù† Ø±Ù‚Ù… ÙˆØ§Ø­Ø¯ ÙÙŠ ÙƒÙ„ Ù…Ø±Ø© "
          "(Ù…Ø«Ù„Ù‹Ø§: ÙƒÙ… ØµØ±ÙØª ÙÙŠ Ø£ÙƒØªÙˆØ¨Ø±ØŸ)."}
# "your income is 10,000", "Ø§Ù„Ø¯Ø®Ù„ Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ù‡ÙŠ 10,000": a statement of the user's income.
INCOME_CLAIM = re.compile(r"\b(?:your|the user's|monthly|total)\s+(?:monthly\s+|total\s+)?(?:income|salary|earnings)"
                          r"\b[^\d.\n]{0,25}?(\d[\d,]*(?:\.\d+)?)|"
                          r"(?:Ø¯Ø®Ù„Ùƒ|Ø±Ø§ØªØ¨Ùƒ|Ù…Ø±ØªØ¨Ùƒ|Ø§Ù„Ø¯Ø®Ù„ Ø§Ù„Ø´Ù‡Ø±ÙŠ|Ø§Ù„Ø¯Ø®Ù„ Ø§Ù„Ø§Ø¬Ù…Ø§Ù„ÙŠ|Ø§Ù„Ø¯Ø®Ù„ Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠ|Ù„Ù„Ø¯Ø®Ù„ Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠ|Ù„Ù„Ø¯Ø®Ù„)"
                          r"[^\d.\n]{0,25}?(\d[\d,]*(?:\.\d+)?)")


# "increase your monthly income by 2,053.33", "Ø¯Ø®Ù„ Ø¥Ø¶Ø§ÙÙŠ 500": an amount of CHANGE,
# not a statement of what the income is (a correct answer was withheld for this).
INCOME_CHANGE = re.compile(r"\b(?:increase|increasing|raise|raising|boost|grow|add|adding|extra|additional|more)\b"
                           r"[^.\n]{0,30}$|\bby\s*$|(?:Ø²ÙŠØ§Ø¯Ù‡|Ø²ÙŠØ§Ø¯Ø©|Ø²ÙˆØ¯|Ø²ÙˆÙ‘Ø¯|ØªØ±ÙØ¹|Ø±ÙØ¹|Ø§Ø¶Ø§ÙÙŠ|Ø¥Ø¶Ø§ÙÙŠ|Ø§Ø¶Ø§ÙÙŠØ©|Ø¥Ø¶Ø§ÙÙŠØ©)[^.\n]{0,30}$")


def _wrong_income_claims(answer, user_id):
    """Income figures the answer attributes to the user that no recorded month has."""
    plain = (answer or "").translate(str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u066b\u066c", "0123456789.,"))
    claims = []
    for m in INCOME_CLAIM.finditer(plain):
        number_at = m.start(1) if m.group(1) else m.start(2)
        if INCOME_CHANGE.search(plain[max(0, m.start() - 40):number_at]):
            continue                    # an increase to the income, not the income itself
        claims.append(Decimal((m.group(1) or m.group(2)).replace(",", "")))
    if not claims:
        return []
    months = facts.expense_months(user_id)
    recorded = {facts.income(user_id, m)["total"] for m in months} | {facts.income(user_id, "0000-00")["total"]}
    recorded = {Decimal(r).quantize(Decimal("0.01")) for r in recorded if r is not None}
    if not recorded:
        return []
    return [c for c in claims if c.quantize(Decimal("0.01")) not in recorded]


def _plan_figures(history):
    """The figures of the latest purchase-plan answer (verified, current values)."""
    from app.services.plan_followups import last_plan
    plan = last_plan(history)
    if not plan:
        return None
    latest = next((m for m in reversed(list(history or [])) if m.get("role") == "assistant"
                   and m.get("context") is plan), None)
    figures = list(amounts_in((latest or {}).get("content", "")))
    s = plan.get("summary") or {}
    figures += [Decimal(str(v)) for k, v in s.items() if k != "verdict" and v not in (None, "")
                and re.fullmatch(r"-?\d+(?:\.\d+)?", str(v))]
    figures.append(Decimal(str((plan.get("decision") or {}).get("price"))))
    return figures


def _without_unsupported_sentences(answer, allowed):
    """The answer minus sentences whose amounts are not supported."""
    sentences = re.split(r"(?<=[.!?ØŸ])\s+|\n+", answer or "")
    kept = [x for x in sentences if not unsupported_amounts(x, allowed)]
    return "\n".join(k for k in kept if k.strip())


def _verified_or_records(answer, question, tool_results, user_id, language, history=None):
    """For a question about the user's own numbers, every money amount in the
    model's answer must come from a tool result, the question or the user's
    records, and each value asked for (spent, left, income) must be there as the
    records give it. Otherwise the answer is replaced by the figures from the
    records. Returns (answer, unsupported amounts, the records answer or None)."""
    wrong_income = _wrong_income_claims(answer, user_id)
    if wrong_income:
        # "your income is 10,000" when the records say 6,600: never shown, whatever was asked.
        return UNVERIFIED_ANSWER.get(language, UNVERIFIED_ANSWER["en"]), wrong_income, None
    plan_figures = _plan_figures(history) if PERSONAL.search(question or "") else None
    if not _own_numbers_question(question) and not plan_figures:
        return answer, [], None
    month = facts.current_month(user_id)
    allowed = list(amounts_in(question)) + [100]        # "Ã— 100" in a percentage formula
    for r in tool_results:
        allowed += amounts_in(json.dumps(r["result"], default=str))
    if month:
        for m in (month, facts.shift_month(month, -1)):
            snap = facts.month_snapshot(user_id, m)
            allowed += [snap["expenses"], snap["income"]["total"], snap["remaining"]]
            allowed += list(snap["spending"]["by_category"].values()) + list(snap["income"]["by_source"].values())
    for g in get_goals(user_id):
        allowed += [g["target_amount"], g["current_amount"], (g["target_amount"] or 0) - (g["current_amount"] or 0)]
    allowed += plan_figures or []
    bad = unsupported_amounts(answer, allowed)
    if plan_figures and not _own_numbers_question(question):
        # A judgement question in a purchase conversation ("is it worth it?"): the
        # sentences with figures neither the records nor the current plan support
        # (an outdated price, an invented total) are removed, the rest is kept.
        if not bad:
            return answer, [], None
        trimmed = _without_unsupported_sentences(answer, allowed)
        if len(trimmed.split()) < 12:
            return UNVERIFIED_ANSWER.get(language, UNVERIFIED_ANSWER["en"]), bad, None
        return trimmed, bad, None
    # Percentages too: a model wrote "your saving rate is 32%" when it was 49.70%.
    percents = [100, 50, 30, 20, 10] + [a for a in amounts_in(question)]   # 100 and the 50/30/20 guideline
    for r in tool_results:
        percents += amounts_in(json.dumps(r["result"], default=str))
    if month:
        snap = facts.month_snapshot(user_id, month)
        percents.append(snap["saving_rate"])
        if snap["spending"]["total"]:
            percents += [facts.percent(v, snap["spending"]["total"]) for v in snap["spending"]["by_category"].values()]
        if snap["income"]["total"]:
            percents += [facts.percent(v, snap["income"]["total"]) for v in snap["income"]["by_source"].values()]
    for g in get_goals(user_id):
        if g["target_amount"]:
            percents.append(round(100 * (g["current_amount"] or 0) / g["target_amount"], 2))
    bad += unsupported_percents(answer, percents)
    # A real figure in the wrong place ("you spent 6,600", the income) is caught too:
    # each value the question asks for must appear, as the records give it.
    period = find_period(question, month) if month else None
    asked_month = period["months"][0] if period and period["kind"] == "month" else month
    if asked_month:
        snap = facts.month_snapshot(user_id, asked_month)
        needed = {"spent": snap["expenses"] if snap["spending"]["count"] else None,
                  "left": snap["remaining"], "income": snap["income"]["total"]}
        written = {a.quantize(Decimal("0.01")) for a in amounts_in(answer)}
        missing = [p for p in asked_parts(question)
                   if needed.get(p) is not None and Decimal(needed[p]).quantize(Decimal("0.01")) not in written]
    else:
        missing = []
    if not bad and not missing:
        return answer, [], None
    unverified = UNVERIFIED_ANSWER
    if not asked_parts(question):
        # The records answer income / spending / what is left; for anything else a
        # replacement would answer a different question than the one asked.
        return unverified.get(language, unverified["en"]), bad, None
    from app.services.direct_answers import answer as direct_answer_for
    month_words = f" {asked_month}" if asked_month else ""
    found = direct_answer_for(
        ("?? ???? ??? ???? ??? ??????? ??" if language == "ar"
         else "How much did I earn, spend and have left in") + month_words + "?",
        user_id, None, language)
    note = {
        "en": "I could not verify the figures in my first answer against your records, so here they are "
              "straight from your saved data:",
        "ar": "?? ????? ?? ?????? ?? ??????? ?? ?????? ?????? ????? ??????? ???? ??? ?? ??????? ?? ??????? ???????? ??????:",
    }.get(language, "")
    if found:
        return note + "\n" + found["answer"], bad, found
    return NO_ANSWER.get(language, NO_ANSWER["en"]), bad, None


def _records_note(user_id):
    """The user's verified figures, put in front of the model for a question about
    their own money: it explains them and must not ask for them or change them."""
    month = facts.current_month(user_id)
    if not month:
        return None
    snap = facts.month_snapshot(user_id, month)
    spent, got = snap["spending"], snap["income"]
    lines = [f"Verified figures from the user's saved records for {month} (SDG). Use only these numbers, never ask "
             "the user for them, and never change or estimate them:",
             f"- expenses: {spent['total']:,.2f} ({spent['count']} transactions)"]
    if got["total"] is not None:
        lines.append(f"- income: {got['total']:,.2f} (" + ", ".join(f"{k} {v:,.2f}" for k, v in got["by_source"].items())
                     + ")")
        lines.append(f"- left after expenses: {snap['remaining']:,.2f}; saving rate {snap['saving_rate']}%")
    for row in facts.ranking(spent["by_category"], spent["total"])[:3]:
        lines.append(f"- {row['category']}: {row['amount']:,.2f} ({row['percent']}% of expenses)")
    prev = facts.spending(user_id, [facts.shift_month(month, -1)])
    if prev["count"]:
        lines.append(f"- expenses the month before: {prev['total']:,.2f}")
    for g in get_goals(user_id):
        lines.append(f"- goal \"{g['name']}\": saved {g['current_amount'] or 0:,.2f} of {g['target_amount']:,.2f}"
                     + (f", deadline {g['target_date']}" if g.get("target_date") else ""))
    return "\n".join(lines)


def _established_facts(history, language):
    """What the conversation has already settled (the current purchase plan, the last
    scenario), as verified values, so the model neither asks for them again nor uses
    an outdated one after a correction."""
    from app.services.direct_answers import last_context
    from app.services.plan_followups import last_plan
    lines = []
    plan = last_plan(history)
    if plan:
        d, s = plan.get("decision") or {}, plan.get("summary") or {}
        bits = [f"item: {d.get('item')}", f"price: {Decimal(str(d['price'])):,.2f} SDG (latest value given by the user)"]
        if d.get("horizon"):
            bits.append(f"time frame: {d['horizon']} months")
        if s.get("per_month"):
            bits.append(f"monthly amount needed: {Decimal(s['per_month']):,.2f} SDG")
        if s.get("money_left"):
            bits.append(f"left after spending each month: {Decimal(s['money_left']):,.2f} SDG; goals need "
                        f"{Decimal(s['goals_need']):,.2f} SDG a month; with the plan the goals receive "
                        f"{Decimal(s['goals_lose_monthly']):,.2f} SDG a month less "
                        f"({Decimal(s['goals_lose_total']):,.2f} SDG in total)")
        if s.get("verdict"):
            bits.append({"fits": "fits without affecting goals", "goals_slow": "possible only by giving goals less",
                         "too_tight": "more than is left each month"}.get(s["verdict"], s["verdict"]))
        lines.append("Current purchase plan in this conversation: " + "; ".join(bits) + ".")
    ctx, _ = last_context(history)
    scenario = (ctx or {}).get("scenario") or {}
    if scenario.get("changes"):
        lines.append(f"Last what-if scenario: {len(scenario['changes'])} change(s) to the {scenario.get('month')} figures; "
                     f"money left in that scenario: {scenario.get('saving')} SDG (hypothetical, not saved).")
    if not lines:
        return None
    return ("Facts already established in this conversation. Use them; do not ask the user for them again, and use "
            "the latest value when the user has corrected one:\n- " + "\n- ".join(lines))


def _usable_history(history):
    """The recent conversation, minus exchanges that failed: a small model tends to
    copy an earlier "I could not answer" (found in UAT). Earlier Arabic questions
    are framed like the current one: a raw Arabic turn anywhere in the history
    also makes Qwen 3B stop at once."""
    turns, failed = list(history or []), set(NO_ANSWER.values())
    kept = []
    for i, m in enumerate(turns):
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if m["content"] in failed or (m["role"] == "user" and nxt and nxt["content"] in failed):
            continue
        if m["role"] == "assistant" and (m.get("context") or {}).get("intent") == "model" \
                and _money_amounts(m["content"]):
            # A figure the model wrote earlier is not a record: left in, a small model
            # repeated a wrong total instead of looking the number up again.
            continue
        if m["role"] == "assistant" and (m.get("context") or {}).get("intent") == "purchase" \
                and any((later.get("context") or {}).get("intent") == "purchase" for later in turns[i + 1:]
                        if later.get("role") == "assistant"):
            continue    # an earlier version of the plan: its figures are out of date
        content = _for_model(m["content"]) if m["role"] == "user" else m["content"]
        kept.append({"role": m["role"], "content": content})
    return kept[-MAX_HISTORY:]


def _retrieve(question, language):
    """Retrieve-then-generate: the app searches the knowledge base itself, so a
    general question is grounded even when the model forgets to call
    search_knowledge (found in UAT: it answered from memory and invented a source)."""
    found = search(question, lang=language if language in ("en", "ar") else None)
    if not found["found"]:
        return None, []
    if PERSONAL.search(question) and found["results"][0]["score"] < STRONG_MATCH:
        return None, []
    notes = "\n".join(
        f"[{i}] {r['source']['publisher']} \u2014 {r['source']['title']}: {r['text']}"
        for i, r in enumerate(found["results"], start=1))
    return RETRIEVED_NOTES.format(notes=notes), _sources(found)


def guard_sources(answer, language="en"):
    """Remove any link the knowledge base does not contain: the assistant may only
    cite verified sources. The link text stays, marked as unverified."""
    allowed = {e["source"]["url"] for e in list_entries()}
    note = UNVERIFIED.get(language, UNVERIFIED["en"])

    def link(match):
        text, url = match.group(1), match.group(2)
        return match.group(0) if url in allowed else f"{text} {note}"

    answer = MARKDOWN_LINK.sub(link, answer or "")
    return BARE_URL.sub(lambda m: m.group(0) if m.group(0) in allowed else note, answer)


def _goal_line(f):
    line = (f'- "{f["name"]}": target {f["target_amount"]:,.0f}, saved {f["saved_so_far"]:,.0f}, '
            f"remaining {f['remaining_amount']:,.0f}")
    status = f["deadline_status"]
    if status == "reached":
        return line + ", already reached"
    if status == "no deadline saved":
        return line + ", no deadline saved"
    if status == "deadline passed":
        return line + f", deadline {f['target_date']} has passed"
    return line + (f", deadline {f['target_date']} ({f['months_left']} month(s) left, needs "
                   f"{f['required_monthly_saving']:,.0f} a month)")


def _goal_notes(context):
    """The saved goals and which one the question is about (None if not a goal question)."""
    how, goal = context["how"], context["active"]
    if how is None:
        return None
    if how == "no goals":
        return NO_GOALS
    if how == "hypothetical":
        focus = HYPOTHETICAL
    else:
        name = goal["name"]
        focus = GOAL_FOCUS[how].format(name=name) + " " + GOAL_TOOL_HINT.format(name=name)
    lines = "\n".join(_goal_line(goal_facts(g)) for g in context["goals"])
    return SAVED_GOALS.format(lines=lines, focus=focus)


def _with_goal(name, args, goal):
    """Goal tools work on the goal the conversation is about unless the model named
    another one. A saved goal's own amount is the truth: an amount the model copied
    in would turn it into a hypothetical purchase, so it is dropped."""
    if name not in GOAL_TOOLS or goal is None:
        return args
    args = dict(args)
    if not args.get("goal_name") and args.get("target_amount") is None:
        args["goal_name"] = goal["name"]
    elif args.get("goal_name") and args.get("target_amount") is not None:
        args.pop("target_amount")
    return args


def _which_goal(context, language, mode):
    """A name that fits several saved goals, none, or no name with several open
    goals: answered without the model, so it never asks for numbers the app has."""
    how = context["how"]
    shown = context["candidates"] if how in ("ask", "choose") else context["goals"]
    sep = " / " if language != "ar" else "ØŒ "
    if how == "choose":
        lang = language if language in CHOOSE_GOAL else "en"
        items = []
        for g in shown:
            f = goal_facts(g)
            deadline = (DUE[lang].format(date=f["target_date"]) if f.get("target_date")
                        else NO_DEADLINE[lang])
            items.append(GOAL_SHORT[lang].format(name=g["name"], remaining=f["remaining_amount"],
                                                 deadline=deadline))
        text = CHOOSE_GOAL[lang].format(count=len(shown), names=sep.join(items))
    else:
        names = sep.join(f'"{g["name"]}"' for g in shown)
        template = WHICH_GOAL if how == "ask" else NOT_FOUND
        text = template.get(language, template["en"]).format(
            names=names, asked=context.get("asked_for", ""))
    found = {"count": len(shown), "goals": [goal_facts(g) for g in shown]}
    return {"answer": text, "mode": mode, "tools_used": [{"name": "list_goals", "arguments": {}}],
            "sources": [], "tool_results": [{"name": "list_goals", "result": found}],
            "goal": None, "context": {"intent": "choose_goal"}}


# "Could you provide your monthly income?" when the records already have it.
ASKS_FOR_RECORDS = re.compile(
    r"\b(?:provide|share|tell me|let me know|give me|what is|what's|what are|need to know|send)\b[^.?!\n]{0,40}"
    r"\b(?:your|the)\s+(?:current\s+|monthly\s+|typical\s+|total\s+)*(?:income|salary|spending|expenses|savings|budget)"
    r"|(?:ÙƒÙ…|Ù…Ø§ Ù‡Ùˆ|Ù…Ø§ Ù‡ÙŠ|Ø§Ø®Ø¨Ø±Ù†ÙŠ|Ø£Ø®Ø¨Ø±Ù†ÙŠ|Ø²ÙˆØ¯Ù†ÙŠ|Ø§Ø±Ø³Ù„|Ø£Ø±Ø³Ù„)[^.ØŸ\n]{0,30}(?:Ø¯Ø®Ù„Ùƒ|Ø±Ø§ØªØ¨Ùƒ|Ù…Ø±ØªØ¨Ùƒ|Ù…ØµØ±ÙˆÙØ§ØªÙƒ|Ù…ØµØ§Ø±ÙŠÙÙƒ|Ù…Ø¯Ø®Ø±Ø§ØªÙƒ)",
    re.IGNORECASE)
USE_RECORDS = {
    "en": "The user's income, expenses, savings and goals are already in the verified figures above. Do not ask "
          "for them: answer the question using those figures.",
    "ar": "Ø¯Ø®Ù„ Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù… ÙˆÙ…ØµØ±ÙˆÙØ§ØªÙ‡ ÙˆÙ…Ø¯Ø®Ø±Ø§ØªÙ‡ ÙˆØ£Ù‡Ø¯Ø§ÙÙ‡ Ù…ÙˆØ¬ÙˆØ¯Ø© ÙÙŠ Ø§Ù„Ø£Ø±Ù‚Ø§Ù… Ø§Ù„Ù…ÙˆØ«Ù‚Ø© Ø£Ø¹Ù„Ø§Ù‡. Ù„Ø§ ØªØ³Ø£Ù„ Ø¹Ù†Ù‡Ø§: Ø£Ø¬Ø¨ Ø¹Ù† Ø§Ù„Ø³Ø¤Ø§Ù„ "
          "Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù… Ù‡Ø°Ù‡ Ø§Ù„Ø£Ø±Ù‚Ø§Ù….",
}


def _asks_for_records(text):
    return bool(ASKS_FOR_RECORDS.search(text or ""))


def _drop_record_requests(text):
    """The answer without sentences that ask for figures the records already hold."""
    sentences = re.split(r"(?<=[.?!ØŸ])\s+", text or "")
    kept = [x for x in sentences if not ASKS_FOR_RECORDS.search(x)]
    return " ".join(kept).strip()


PLAN_REFERENCE = re.compile(r"\b(?:it|this|that|the purchase|buying|buy|worth|afford)\b|"
                            r"(?<=[Ø¡-ÙŠ])(?:Ù‡Ø§|Ù‡)(?![Ø¡-ÙŠ])|(?<![Ø¡-ÙŠ])(?:Ø¯Ù‡|Ø¯Ø§|Ø¯ÙŠ|Ø§Ù„Ø´Ø±Ø§Ø¡|ÙŠØ³ØªØ§Ù‡Ù„|ØªØ³ØªØ§Ù‡Ù„|ÙŠØ³ØªØ­Ù‚)(?![Ø¡-ÙŠ])")


def _plan_in_question(question, history, user_id, language):
    """The verified assessment of the purchase plan the question refers to, or None."""
    from app.services.plan_followups import last_plan, plan_answer
    plan = last_plan(history)
    d = (plan or {}).get("decision") or {}
    if not (plan and d.get("horizon") and PLAN_REFERENCE.search(question or "")):
        return None
    month = facts.current_month(user_id)
    found = plan_answer(user_id, month, language, {**d, "price": Decimal(str(d["price"]))}, ask="verdict")
    return found["data"] if found and found.get("tool") == "assess_purchase" else None


def _decision_question(question, user_id, history):
    """A purchase, emergency, goal-priority or saving-plan question, or a change
    request: the decision engine handles it (and asks about the goal itself)."""
    from app.services.decision_answers import detect, last_goal
    from app.services.direct_answers import last_context
    from app.services.plan_followups import last_plan
    previous, age = last_context(history)
    return detect(question, user_id, previous, age, last_goal(history), last_plan(history)) is not None


def _direct(found, mode):
    """A fast-path answer in the shape run_agent_detailed returns. A clarifying
    question used no data, so it shows no "calculated with" label."""
    return {
        "answer": fix_currency(found["answer"], found["language"]),
        "mode": mode,
        "tools_used": [] if found["tool"] == "clarify" else [{"name": found["tool"], "arguments": {}}],
        "sources": found.get("sources") or [],
        "tool_results": [{"name": found["tool"], "result": found["data"]}],
        "goal": found.get("goal"),
        "context": found["context"],
        "calculation": found["calculation"],
        "fast_path": True,
    }


def _compact_plan(plan):
    """The plan's answer-relevant part. The full plan is ~5 KB of JSON in which a
    small model loses its way (found with Qwen 3B: it misread an income total)."""
    if not isinstance(plan, dict) or "error" in plan:
        return plan
    gap = plan.get("financial_gap", {})
    goal = gap.get("goal") or {}
    cuts = []
    for r in plan.get("reductions", []):
        parts = r.get("subcategory_plan") or [{"subcategory": None, "current_spending":
                                               r["current_spending"], "reduce_by": r["reduce_by"]}]
        for part in parts:
            cuts.append({"category": r["category"], "subcategory": part["subcategory"],
                         "spent_per_month": part["current_spending"],
                         "cut_per_month": part["reduce_by"]})
    protected = [f"{r['category']}/{p['subcategory']}" for r in plan.get("reductions", [])
                 for p in r.get("protected_subcategories", [])]
    other = [a.get("explanation") for a in plan.get("income_actions", []) +
             plan.get("timeline_actions", []) + plan.get("saving_actions", [])
             if a.get("explanation")]
    goal_view = None
    if goal:
        goal_view = {k: goal.get(k) for k in ("goal_name", "remaining", "months_remaining",
                                               "required_monthly_saving", "current_monthly_saving",
                                               "saving_gap")}
        # Spelled out so a small model repeats it instead of redoing the subtraction.
        short = goal.get("saving_gap") or 0
        goal_view["in_words"] = (
            f"The {goal.get('current_monthly_saving', 0):,.0f} left each month after spending is "
            f"{short:,.0f} short of the {goal.get('required_monthly_saving', 0):,.0f} needed each month"
            if short > 0 else
            f"The {goal.get('current_monthly_saving', 0):,.0f} left each month after spending already covers the "
            f"{goal.get('required_monthly_saving', 0):,.0f} needed each month")
    cuts_in_words = [
        f"{c['subcategory'] or c['category']}: cut {c['cut_per_month']:,.0f} a month "
        f"(from {c['spent_per_month']:,.0f} to {c['spent_per_month'] - c['cut_per_month']:,.0f})"
        for c in cuts]
    freed, missing = gap.get("total_planned_reduction") or 0, gap.get("remaining_gap") or 0
    totals_in_words = (f"These cuts free {freed:,.0f} a month in total"
                       + (f"; {missing:,.0f} a month is still missing after them" if missing > 0
                          else "; nothing is missing after them"))
    return {
        "currency": CURRENCY,
        "cuts_in_words": cuts_in_words,
        "totals_in_words": totals_in_words,
        "summary": plan.get("summary"),
        "goal": goal_view,
        "cuts_from_flexible_spending": cuts,
        "protected_essential_spending": protected,
        "blocked_by_preferences": plan.get("blocked_by_preferences", []),
        "still_missing_per_month": gap.get("remaining_gap"),
        "other_options": other,
        "evidence_based_tips": plan.get("evidence_based_tips", []),
    }


def _categories_in_words(result):
    """The category ranking as a sentence, first, so a small model reads the answer
    instead of picking a number out of the tables (it once named the 4th category)."""
    ranking = result.get("ranking") or []
    if not ranking:
        return result
    top, rest = ranking[0], ranking[1:3]
    words = (f"Biggest expense in {result.get('window')}: {top['category']}, "
             f"{top['amount']:,.0f} SDG ({top['percent_of_expenses']:.2f}% of the "
             f"{result.get('total_expenses', 0):,.0f} SDG spent)")
    if rest:
        words += ". Next: " + ", ".join(
            f"{r['category']} {r['amount']:,.0f} SDG ({r['percent_of_expenses']:.2f}%)" for r in rest)
    # The ranking already holds every share; the "dominant subcategory" table made
    # the model name Transport (100% of its own category) as the top expense.
    return {"in_words": words, "window": result.get("window"),
            "total_expenses": result.get("total_expenses"), "ranking": ranking,
            "by_subcategory": result.get("by_subcategory")}


def _model_view(name, result):
    if not isinstance(result, dict) or "error" in result:
        return result
    if name == "get_recommendation_plan":
        return _compact_plan(result)
    if name == "analyze_categories":
        return _categories_in_words(result)
    return result


def run_agent_detailed(question, history=None, user_id=1, language="en", mode="full",
                       chat_fn=None, fast=True):
    """_answer() with processing diagnostics: which stages ran, how long each took,
    how many database queries / records and model calls it needed (no amounts or
    text are logged). The result gains a "processing" summary."""
    tr = trace.start()
    model = chat_fn or chat

    def counted(messages, tools=None):
        trace.count_model_call()
        with trace.stage("model_generation"):
            return model(messages, tools=tools)

    result = _answer(question, history, user_id, language, mode, counted, fast)
    path = "fast" if result.get("fast_path") else "model" if tr.model_calls else "clarify"
    result["processing"] = tr.summary(path, (result.get("context") or {}).get("intent"))
    trace.log(result["processing"])
    return result


def _answer(question, history=None, user_id=1, language="en", mode="full",
            chat_fn=None, fast=True):
    """Answer one question. Returns the answer plus the tools and sources it used.

    Questions about the user's own numbers that the app understands (spending,
    income, savings, budget, goals, what-ifs, "how did you calculate that?") are
    answered by the fast path (services/direct_answers.py): exact engine numbers,
    no model call. Everything else goes to the model. fast=False always uses the
    model (for comparisons); the "no_tools" baseline never uses the fast path."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    chat_fn = chat_fn or chat
    language = language_of(question, language)     # answer in the language of the question

    system, tools = _setup(mode, user_id, language)
    messages = [{"role": "system", "content": system}]
    sources, goal, context = [], None, None
    if tools is not None:
        context = resolve_goal(user_id, question, history)
        if context["how"] in ("ask", "unknown", "choose") and not (fast and _decision_question(
                question, user_id, history)):
            return _which_goal(context, language, mode)
        if fast:
            found = direct_answer(question, user_id, history, language, context)
            if found:
                return _direct(found, mode)
        goal = context["active"]
        goal_notes = _goal_notes(context)
        if goal_notes:
            messages.append({"role": "system", "content": goal_notes})
    # A question the engine answers from the user's own data gets no general notes:
    # with them a small model answered "top expense" from a food-budget passage.
    # A question that also asks what something IS ("what is an emergency fund and
    # how much should I have?") still gets the notes for that part.
    defines = bool(DEFINITION.search(question or ""))
    data_question = bool(context and planned_tool(question, context)) or (_own_numbers_question(question)
                                                                          and not defines)
    if tools is not None and PERSONAL.search(question or ""):
        note = _records_note(user_id)
        if note:
            messages.append({"role": "system", "content": note})
    if mode == "full" and not data_question:
        notes, sources = _retrieve(question, language)
        if notes:
            messages.append({"role": "system", "content": notes})
    established = _established_facts(history, language)
    if established:
        messages.append({"role": "system", "content": established})
    messages.extend(_usable_history(history))
    messages.append({"role": "user", "content": _for_model(question)})

    trace.mark("context_and_notes", trace.current().started if trace.current() else time.perf_counter())
    tools_used, tool_results = [], []
    allowed = {t["function"]["name"] for t in tools or []}

    done = {}

    def run(name, args):
        key = (name, json.dumps(args or {}, sort_keys=True, default=str))
        if key in done:
            # The same tool with the same arguments again: the verified result is
            # given again without running the engine a second time.
            messages.append({"role": "tool", "name": name,
                             "content": json.dumps(_model_view(name, done[key]), default=str)})
            return
        with trace.stage(f"tool:{name}"):
            result = (call_tool(name, args, user_id=user_id) if name in allowed
                      else {"error": f"tool {name} is not available"})
        done[key] = result
        tools_used.append({"name": name, "arguments": args})
        tool_results.append({"name": name, "result": result})
        if name == "search_knowledge":
            for s in _sources(result):
                if all(x["entry_id"] != s["entry_id"] for x in sources):
                    sources.append(s)
        messages.append({
            "role": "tool",
            "name": name,
            "content": json.dumps(_model_view(name, result), default=str),
        })

    # Clear goal questions are answered from the engine the app picks itself;
    # the model may still call more tools before it answers.
    planned = planned_tool(question, context) if context else None
    if not planned and context and context["how"] == "named":
        # A reply that only names the goal ("Laptop and course.") answers the
        # previous question ("Can I reach my goal on time?"): use that intent.
        earlier = next((m["content"] for m in reversed(history or []) if m["role"] == "user"), "")
        planned = planned_tool(earlier, context)
    plan_result = _plan_in_question(question, history, user_id, language) if tools is not None else None
    if plan_result is not None:
        # A question about the purchase plan already under discussion ("is it worth
        # it?"): the app re-runs the plan assessment, which counts the existing goals,
        # and the model only explains that result. Left to choose, a small model
        # picked a goal tool that ignores the other goals and contradicted the plan.
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "assess_purchase", "arguments": {}}}]})
        tools_used.append({"name": "assess_purchase", "arguments": {}})
        tool_results.append({"name": "assess_purchase", "result": plan_result})
        messages.append({"role": "tool", "name": "assess_purchase", "content": json.dumps(plan_result, default=str)})
        reply = chat_fn(messages, tools=None)
    elif planned and planned[0] in allowed:
        name, args = planned
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": name, "arguments": args}}]})
        run(name, args)
        # The engine already has the answer: the model only explains it (with more
        # tools a small model wandered off, e.g. to an interest calculation).
        reply = chat_fn(messages, tools=None)
    else:
        reply = chat_fn(messages, tools=tools)

    for _ in range(MAX_TOOL_ROUNDS):
        tool_calls = reply.get("tool_calls")
        if not tool_calls or tools is None:
            break

        messages.append(reply)

        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"].get("arguments", {}) or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            run(name, _with_goal(name, args, goal))

        reply = chat_fn(messages, tools=tools)

    if not (reply.get("content") or "").strip() and tools_used:
        # A small model sometimes stops after its tool calls without answering
        # (found in UAT); ask once more, then fall back to a clear message.
        messages.append({"role": "user", "content": ANSWER_NOW})
        reply = chat_fn(messages, tools=None)
    if CJK.search(reply.get("content") or "") or _wrong_language(reply.get("content") or "", language):
        # Qwen occasionally slips into Chinese mid-answer (found in Arabic UAT).
        messages += [reply, {"role": "user", "content": ONE_LANGUAGE.get(language, ONE_LANGUAGE["en"])}]
        reply = chat_fn(messages, tools=None)
    if tools is not None and _asks_for_records(reply.get("content") or "") and facts.current_month(user_id):
        # Qwen 3B sometimes asks for the income or spending it was just given (UAT):
        # once more, pointing at the verified figures; a question that remains is dropped.
        messages += [reply, {"role": "user", "content": USE_RECORDS.get(language, USE_RECORDS["en"])}]
        retry = chat_fn(messages, tools=None)
        if (retry.get("content") or "").strip():
            reply = retry
        reply = {**reply, "content": _drop_record_requests(reply.get("content") or "")}
    # Still mixed after the retry: the foreign sentences are dropped, never shown.
    answer = _strip_cjk(reply.get("content") or "") or NO_ANSWER.get(language, NO_ANSWER["en"])
    unsupported, replacement = [], None
    checking = time.perf_counter()
    if tools is not None:           # the "no_tools" baseline is measured as the model wrote it
        answer, unsupported, replacement = _verified_or_records(answer, question, tool_results, user_id,
                                                                language, history)
    if replacement:
        # The answer now comes from the records: its label and evidence say so,
        # and no note or citation from the model's attempt is kept.
        sources = []
        tools_used = [{"name": replacement["tool"], "arguments": {}}]
        tool_results = [{"name": replacement["tool"], "result": replacement["data"]}]
    # The model explains numbers but may not invent a sum: a wrong "a + b = c" or a
    # list whose stated total is wrong is corrected before the user sees it.
    answer, arithmetic_fixes = check_arithmetic(answer)
    trace.mark("validate", checking)

    return {
        "answer": guard_sources(fix_currency(answer, language), language),
        "arithmetic_fixes": arithmetic_fixes,
        "unsupported_numbers": [float(v) for v in unsupported],
        "mode": mode,
        "tools_used": tools_used,
        "sources": sources,
        "tool_results": tool_results,
        "goal": goal["name"] if goal else None,
        "context": {"intent": "model", "goal": goal["name"] if goal else None},
    }


def run_agent(question, history=None, user_id=1, language="en"):
    """The answer text only (kept for scripts and the original API)."""
    return run_agent_detailed(question, history, user_id, language)["answer"]

