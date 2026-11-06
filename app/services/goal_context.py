# app/services/goal_context.py
# The user's SAVED goals, made available to the assistant so it never asks for
# numbers the Goals page already has.
#
#   goal_facts()    — target, saved, remaining, deadline, months left and the
#                     monthly saving the deadline requires (goals_engine maths)
#   find_goal()     — one goal from a loosely written name ("laptop & course",
#                     "the laptop"), or a clear "not found" / "which one?"
#   resolve_goal()  — which goal a chat question is about: named in the question,
#                     carried over from earlier turns ("it"), or the only open
#                     goal; with several open goals and none named, ask which
#
# Everything reads goals through crud.get_goals(user_id), so one user never sees
# another user's goals.

import difflib
import math
import re
from datetime import date

from app.config import CATEGORY_TREE
from app.database.crud import get_goals
from app.engines.goals_engine import _parse_date, calculate_goal_progress, months_between
from app.services.intent import GOALS_ALL, normalize

STOP_WORDS = {
    "a", "an", "the", "my", "our", "your", "for", "to", "of", "on", "in", "and", "or",
    "goal", "goals", "target", "saving", "savings", "fund", "buy", "get",
    "هدف", "هدفي", "الهدف", "شراء", "و", "في", "على", "من", "إلى", "الى",
}
# Words that make a question about one specific goal even when none is named.
def _ar_words(words):
    """Whole Arabic words (with و ف ب ل س ح ال in front): "اصل" (reach) must not
    match inside "المواصلات" (transport)."""
    return rf"(?<![ء-ي])(?:و|ف)?(?:ب|ل|س|ح)?(?:ال)?(?:{words})(?:ه|ها|ي)?(?![ء-ي])"


GOAL_TALK = re.compile(
    r"\b(goals?|targets?|reach|on time|deadline|afford|how far|how much more (?:do i need|to save)|"
    r"(?:how much )?do i still need|still need)\b"
    r"|هدف|" + _ar_words("أحقق|احقق|أصل|اصل|أوصل|اوصل|نوصل|الموعد|موعد"),
    re.IGNORECASE)
# Follow-up wording: continues an earlier goal ("reach it faster", "what if I save
# 500 more?"), but on its own does not need one ("what should I cut?").
FOLLOW_UP = re.compile(
    r"\b(it|faster|sooner|quicker|what if|extra|more each month|more a month|"
    r"save more|monthly|each month)\b"
    r"|ماذا لو|" + _ar_words("أسرع|اسرع|إضافي|اضافي|شهري|شهريا|شهريًا"),
    re.IGNORECASE)
# "tell me about my emergency fund", "how is my laptop goal doing?": the status of
# a goal the question names.
STATUS = re.compile(r"\b(?:tell me about|how(?:'s| is| are) (?:my |the )?\w+(?: \w+)? (?:doing|going)|status of|"
                    r"progress (?:on|of)|update on|where (?:am i|do i stand) (?:on|with))\b|"
                    r"(?:كيف|وين) (?:وصل|ماشي|حال|وضع)|وضع (?:ال)?هدف|اخبار", re.IGNORECASE)
# "can I afford a 5,000 phone?" — a hypothetical purchase, not a saved goal.
PURCHASE = re.compile(r"\b(afford|buy|cost|costs|price)\b|يكلف|شراء|سعر", re.IGNORECASE)
NUMBER = re.compile(r"\d")
# "my house goal", "my laptop": the user names something; if no saved goal has
# that name, say so instead of answering about a different goal.
NAMED_THING = re.compile(r"\b(?:my|the)\s+([a-z][a-z-]*(?:\s+[a-z][a-z-]*)?)", re.IGNORECASE)
GENERIC = {
    "goal", "goals", "target", "targets", "money", "savings", "saving", "budget", "income",
    "salary", "spending", "expenses", "deadline", "plan", "current", "monthly", "own",
    "finances", "account", "numbers", "progress", "main", "first", "next", "time", "month",
    "on", "in", "by", "for", "to", "faster", "sooner", "date", "amount", "rate",
}
TYPO = 0.85
FOLLOW_UP_WINDOW = 6        # messages looked back at for "it" / "that goal"
# Questions that only make sense for a goal with a deadline.
DEADLINE_TALK = re.compile(
    r"\b(on time|in time|deadline|short|on track|due|by the date|"
    r"need(?:s)? (?:to save )?(?:each|every|per|a) month|needs)\b|الموعد|في الوقت|موعد",
    re.IGNORECASE)


# Arabic words for things goals are often named after in English, so "هدف
# اللابتوب" finds the goal "Laptop and course".
ALIASES = {
    "لابتوب": "laptop", "لاب": "laptop", "كمبيوتر": "computer", "حاسوب": "computer",
    "كورس": "course", "دوره": "course", "دورة": "course", "طوارئ": "emergency", "طوارىء": "emergency",
    "سياره": "car", "سيارة": "car", "عربيه": "car", "عربية": "car", "بيت": "house", "منزل": "house",
    "شقه": "apartment", "شقة": "apartment", "زواج": "wedding", "عرس": "wedding", "سفر": "travel",
    "رحله": "trip", "رحلة": "trip", "موبايل": "phone", "جوال": "phone", "هاتف": "phone", "تلفون": "phone",
    "جامعه": "university", "جامعة": "university", "دراسه": "study", "دراسة": "study",
}


def _tokens(text):
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[أإآ]", "ا", text)
    words = re.findall(r"[\wء-يٱ-ۓ]+", text)      # Arabic letters, not ، or ؟
    out = set()
    for w in words:
        if w.startswith("ال") and len(w) > 4:
            w = w[2:]
        if w not in STOP_WORDS and not w.isdigit():
            out.add(ALIASES.get(w, w))
    return out


def _overlap(goal_words, text_words):
    """How many of the goal's words appear in the text (allowing small typos)."""
    hits = 0
    for g in goal_words:
        if g in text_words or difflib.get_close_matches(g, text_words, n=1, cutoff=TYPO):
            hits += 1
    return hits


def match_goals(goals, text):
    """Goals whose name the text mentions, best first. Several equally good
    matches mean the text is ambiguous."""
    text_words = _tokens(text)
    if not text_words:
        return []
    scored = []
    for g in goals:
        goal_words = _tokens(g["name"]) or {g["name"].lower()}
        hits = _overlap(goal_words, text_words)
        if hits == 0 or hits / len(goal_words) < 0.5:
            continue
        # Share of the goal's name found, plus share of the text that is the name:
        # "laptop and course" prefers the goal "Laptop and course" over "Laptop".
        scored.append((hits / len(goal_words) + hits / len(text_words), g))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [g for s, g in sorted(scored, key=lambda x: -x[0]) if best - s < 1e-9]


def find_goal(user_id, goal_name):
    """(goal, None) or (None, error dict). Never falls back to a different goal."""
    goals = get_goals(user_id)
    if not goals:
        return None, {"error": "the user has no saved goals",
                      "hint": "add a goal on the Goals page, or ask about a target amount and date"}
    exact = [g for g in goals if g["name"].strip().lower() == str(goal_name).strip().lower()]
    found = exact or match_goals(goals, goal_name)
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, {"error": f"no saved goal called '{goal_name}'",
                      "saved_goals": [g["name"] for g in goals]}
    return None, {"error": f"several saved goals match '{goal_name}' — ask the user which one",
                  "matching_goals": [g["name"] for g in found]}


def goal_facts(goal, today=None):
    """The stored numbers of one goal plus what its deadline requires."""
    today = today or date.today()
    progress = calculate_goal_progress(goal)
    target_date = _parse_date(goal.get("target_date"))
    remaining = progress.get("remaining", max(goal["target_amount"] - goal["current_amount"], 0))
    facts = {
        "goal_id": goal["id"],
        "name": goal["name"],
        "target_amount": round(float(goal["target_amount"]), 2),
        "saved_so_far": round(float(goal["current_amount"] or 0), 2),
        "remaining_amount": round(float(remaining), 2),
        "percent_complete": progress.get("percent_complete"),
        "target_date": goal.get("target_date"),
        "priority": goal.get("priority"),
    }
    if progress.get("reached"):
        facts.update(status="reached", deadline_status="reached")
        return facts
    if target_date is None:
        facts.update(status="in progress", deadline_status="no deadline saved",
                     months_left=None, required_monthly_saving=None)
        return facts
    months = months_between(today, target_date)
    if target_date < today:
        facts.update(status="in progress", deadline_status="deadline passed",
                     months_left=0, required_monthly_saving=None)
        return facts
    months = max(months, 1)
    facts.update(status="in progress", deadline_status="upcoming", months_left=months,
                 required_monthly_saving=round(remaining / months, 2))
    return facts


def list_goals(user_id):
    goals = get_goals(user_id)
    return {"count": len(goals), "goals": [goal_facts(g) for g in goals]}


def months_to_finish(remaining, monthly_saving):
    if remaining <= 0:
        return 0
    if monthly_saving <= 0:
        return None
    return math.ceil(remaining / monthly_saving)


def _unknown_name(question):
    """The thing the question names ("house" in "my house goal"), if any."""
    for match in NAMED_THING.finditer(question or ""):
        kept = []
        for w in match.group(1).lower().split():
            if w in GENERIC:        # "my laptop on time" -> "laptop"
                break
            kept.append(w)
        if kept:
            return " ".join(kept)
    return None


def _mostly_name(goal, text):
    text_words = _tokens(text)
    goal_words = _tokens(goal["name"]) or {goal["name"].lower()}
    return bool(text_words) and _overlap(goal_words, text_words) / len(text_words) >= 0.5


def resolve_goal(user_id, question, history=None):
    """Which saved goal the question is about.

    Returns {"goals": [...], "active": goal or None, "how": ..., "candidates": [...]}.
    "how" is "named", "earlier" (a follow-up such as "reach it faster"), "only goal",
    "only dated goal" (a deadline question, one open goal has a deadline),
    "choose" (several open goals, none named: ask which), "ask" (a name that fits several goals),
    "unknown" (a name no saved goal has), "hypothetical", "no goals" or None
    (no specific goal: another topic, or a general "what should I cut?").
    """
    goals = get_goals(user_id)
    out = {"goals": goals, "active": None, "how": None, "candidates": []}
    if GOALS_ALL.search(normalize(question or "")) and not match_goals(goals, question):
        return out          # "what are my goals?" is about all of them: no single goal to pick
    about_goal = bool(GOAL_TALK.search(question or "") or (STATUS.search(question or "")
                                                        and match_goals(goals, question)))
    follow_up = bool(FOLLOW_UP.search(question or ""))
    named = match_goals(goals, question)
    # A name counts when the question is about goals, or when the message is
    # mostly that name (the reply "Laptop and course."); "What is an emergency
    # fund?" is a general question, not the goal "Emergency fund".
    if named and (about_goal or follow_up or _mostly_name(named[0], question)):
        if len(named) == 1:
            return {**out, "active": named[0], "how": "named"}
        return {**out, "how": "ask", "candidates": named}
    if not about_goal and not follow_up:
        return out          # a different topic: do not carry an earlier goal over
    if not goals:
        return {**out, "how": "no goals"} if about_goal else out
    if PURCHASE.search(question) and NUMBER.search(question):
        return {**out, "how": "hypothetical"}
    unknown = _unknown_name(question) if about_goal else None
    if unknown:
        return {**out, "how": "unknown", "asked_for": unknown}

    # A follow-up: the goal a recent answer was about (recorded by the app, so it
    # works whatever language the answer was in), else a recent question about a
    # goal that names exactly one. Only the last few messages count, and a
    # general question ("What is an emergency fund?") never sets the goal.
    for message in reversed(list(history or [])[-FOLLOW_UP_WINDOW:]):
        if message.get("goal"):
            found = [g for g in goals if g["name"] == message["goal"]]
            if found:
                return {**out, "active": found[0], "how": "earlier"}
        if message.get("context") is not None:
            continue        # an answer from this app version: its goal (if any) is recorded
        content = message.get("content", "")
        if message.get("role") == "user" and not (
                GOAL_TALK.search(content) or FOLLOW_UP.search(content)
                or any(_mostly_name(g, content) for g in goals)):
            continue
        found = match_goals(goals, content)
        if len(found) == 1:
            return {**out, "active": found[0], "how": "earlier"}
        if found:
            break           # that message listed several goals: still undecided
    if len(goals) == 1:
        return {**out, "active": goals[0], "how": "only goal"}
    open_goals = [g for g in goals if (g["current_amount"] or 0) < g["target_amount"]]
    if len(open_goals) == 1:
        return {**out, "active": open_goals[0], "how": "only goal"}
    if about_goal:
        dated = [g for g in open_goals if goal_facts(g)["deadline_status"] == "upcoming"]
        if DEADLINE_TALK.search(question or "") and len(dated) == 1:
            # "on time" / "short of what it needs" only make sense for a goal with a date.
            return {**out, "active": dated[0], "how": "only dated goal"}
        # "my goal" with several goals still open: ask which one rather than guess
        # (the reply, e.g. "Laptop and course", then answers the question).
        return {**out, "how": "choose", "candidates": open_goals or goals}
    return out


# ---------- which engine answers a goal question ----------
# A small model often answers "reach it faster?" or "what if I save 500 more?"
# from memory instead of calling a tool (found with Qwen 2.5 3B). For these clear
# intents the app runs the engine itself, before the model writes the answer.
WHAT_IF = re.compile(r"\bwhat if\b|\bsuppose\b|ماذا لو|لو ", re.IGNORECASE)
SAVE = re.compile(r"\b(save|saving)\b|ادخر|أدخر|أوفر|اوفر", re.IGNORECASE)
SAVE_MORE = re.compile(r"\b(save|saving|extra|more)\b|ادخر|أدخر|إضافي|اضافي", re.IGNORECASE)
SPEND_LESS = re.compile(r"\b(cut|spend less|reduce)\b|أقلل|اقلل|أخفض|اخفض", re.IGNORECASE)
WAIT = re.compile(r"\b(months?|wait|later|extend)\b|أشهر|شهور|أؤجل|اؤجل", re.IGNORECASE)
PLAN = re.compile(
    r"\b(faster|sooner|quicker|what should i (?:do|cut)|what can i cut|how can i (?:save|reach)|"
    r"cut to save|save more|(?:give|any) (?:me )?(?:practical )?(?:advice|tips)|"
    r"advice for (?:me|this month)|what do you recommend|"
    r"(?:improve|increase|boost|grow) (?:my )?savings?|save more money)\b|"
    r"أسرع|اسرع|ماذا أقلل|ماذا اقلل|كيف أوفر|كيف اوفر|كيف أصل|كيف اصل|انصحني|نصيحة لي|نصائح لي",
    re.IGNORECASE)
FEASIBLE = re.compile(
    r"\b(reach|on time|on track|afford|how far|how much more (?:do i need|to save)|"
    r"how much (?:do i need|should i) save)\b"
    r"|" + _ar_words("أحقق|احقق|أصل|اصل|الموعد"),
    re.IGNORECASE)
TOP_SPENDING = re.compile(
    r"\b(?:top|biggest|largest|highest|main) (?:expense|spending|cost|category)|"
    r"\bspend (?:the )?most\b|\bmost of my money\b|"
    r"أكبر مصروف|أكبر مصاريف|أعلى مصروف|أكثر شيء أصرف|أين أصرف أكثر",
    re.IGNORECASE)
AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def planned_tool(question, context):
    """(tool name, arguments) the app runs itself for a clear question, or None."""
    question = question or ""
    goal = context.get("active")
    named = {"goal_name": goal["name"]} if goal else {}
    amount = AMOUNT.search(question)
    if WHAT_IF.search(question) and goal and amount:
        value = float(amount.group(1).replace(",", ""))
        if WAIT.search(question) and not SAVE.search(question):   # "wait 3 more months"
            return "simulate_scenario", {**named, "extra_months": int(value)}
        if SPEND_LESS.search(question):
            category = next((c for c in CATEGORY_TREE if c.lower() in question.lower()), None)
            if category:
                return "simulate_scenario", {**named, "cut_category": category, "cut_amount": value}
            return None
        if SAVE_MORE.search(question):
            return "simulate_scenario", {**named, "extra_saving": value}
        return None
    if TOP_SPENDING.search(question):
        return "analyze_categories", {}
    if PLAN.search(question):
        return "get_recommendation_plan", named
    if goal and FEASIBLE.search(question):
        return "assess_goal", named
    return None
