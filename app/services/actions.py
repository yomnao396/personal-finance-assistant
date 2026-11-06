# app/services/actions.py
# Real changes the user asks for in the chat ("add 500 to my emergency fund",
# "move the laptop deadline to June 2027", "record 200 on lunch"). The chat
# never changes data on its own:
#
#   1. describe()  checks the request (the goal belongs to this user, the amount
#                  is a positive number, the date is in the future, the category
#                  exists) and says exactly what will change;
#   2. the user confirms ("yes");
#   3. execute()   re-checks everything, writes, reads the record back and only
#                  reports success when the stored value is the expected one.
#
# A what-if or a piece of advice never comes here.

import hashlib
import hmac
import json
import os
import secrets
from datetime import date
from decimal import Decimal

from app.config import validate_category
from app.database import crud
from app.finance.facts import dec
from app.services.goal_context import goal_facts

MAX_AMOUNT = Decimal("100000000")
# Signs the change shown for confirmation, so the "yes" can only run that change for
# that user (the pending change travels in the chat history the client sends back).
# Set PFA_ACTION_SECRET to keep pending confirmations valid across restarts.
_SECRET = (os.getenv("PFA_ACTION_SECRET") or secrets.token_hex(32)).encode()
KINDS = ("contribute", "goal_date", "goal_target", "add_expense", "goal_priority")


class ActionError(ValueError):
    """The request cannot be carried out as given (the message says why)."""


def sign(user_id, action):
    payload = json.dumps({"user": user_id, "action": action}, sort_keys=True, default=str)
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def verify_signature(user_id, action, signature):
    return bool(action and signature) and hmac.compare_digest(sign(user_id, action), str(signature))


def _goal(user_id, goal_id):
    """The goal, only when it belongs to this user."""
    try:
        goal_id = int(goal_id)
    except (TypeError, ValueError):
        raise ActionError("goal_not_found")
    found = [g for g in crud.get_goals(user_id) if g["id"] == goal_id]
    if not found:
        raise ActionError("goal_not_found")
    return found[0]


def _amount(value):
    amount = dec(value)
    if amount is None or not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
        raise ActionError("invalid_amount")
    return amount


def _future_date(value):
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        raise ActionError("invalid_date")
    if day <= date.today():
        raise ActionError("date_not_in_future")
    return day


def check(user_id, action):
    """The validated action with its before/after values (nothing is written)."""
    kind = action.get("kind")
    if kind not in KINDS:
        raise ActionError("unknown_action")
    if kind == "add_expense":
        amount = _amount(action.get("amount"))
        try:
            category, sub = validate_category(action.get("category"), action.get("subcategory"))
        except ValueError:
            raise ActionError("invalid_category")
        day = action.get("date") or date.today().isoformat()
        try:
            day = date.fromisoformat(str(day)).isoformat()
        except ValueError:
            raise ActionError("invalid_date")
        return {**action, "amount": float(amount), "category": category, "subcategory": sub, "date": day}
    goal = _goal(user_id, action.get("goal_id"))
    out = {**action, "goal_id": goal["id"], "goal": goal["name"]}
    if kind == "contribute":
        amount = _amount(action.get("amount"))
        before = dec(goal["current_amount"]) or Decimal(0)
        return {**out, "amount": float(amount), "before": float(before), "after": float(before + amount),
                "target": goal["target_amount"]}
    if kind == "goal_target":
        amount = _amount(action.get("amount"))
        return {**out, "amount": float(amount), "before": goal["target_amount"], "after": float(amount),
                "saved": goal["current_amount"]}
    if kind == "goal_date":
        day = _future_date(action.get("date"))
        return {**out, "date": day.isoformat(), "before": goal.get("target_date"), "after": day.isoformat()}
    order = sorted(crud.get_goals(user_id), key=lambda g: (g.get("priority") or 99, g["id"]))
    return {**out, "order": [goal["id"]] + [g["id"] for g in order if g["id"] != goal["id"]]}


def execute(user_id, action):
    """Carry out a confirmed action and return what is stored now. Raises
    ActionError when it is invalid or the stored value is not the expected one."""
    a = check(user_id, action)
    kind = a["kind"]
    if kind == "add_expense":
        new_id = crud.add_transaction(user_id, a.get("description") or a["category"], a["amount"],
                                      a["category"], a["date"], a["subcategory"])
        stored = [t for t in crud.get_transactions(user_id) if t["id"] == new_id]
        if not stored or round(float(stored[0]["amount"]), 2) != round(a["amount"], 2):
            raise ActionError("not_saved")
        return {**a, "transaction_id": new_id, "stored": dict(stored[0])}
    if kind == "contribute":
        crud.contribute_to_goal(a["goal_id"], a["amount"])
    elif kind == "goal_target":
        crud.update_goal(a["goal_id"], target_amount=a["amount"])
    elif kind == "goal_date":
        crud.update_goal(a["goal_id"], target_date=a["date"])
    else:
        crud.set_goal_priorities(user_id, a["order"])
    goal = _goal(user_id, a["goal_id"])
    ok = {"contribute": lambda: round(goal["current_amount"], 2) == round(a["after"], 2),
          "goal_target": lambda: round(goal["target_amount"], 2) == round(a["after"], 2),
          "goal_date": lambda: goal.get("target_date") == a["after"],
          "goal_priority": lambda: goal.get("priority") == 1}[kind]()
    if not ok:
        raise ActionError("not_saved")
    return {**a, "stored": goal_facts(goal)}
