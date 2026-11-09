# app/preferences/user_preferences.py
# Manages user-defined essential (protected) and flexible expense preferences.
#
# These preferences are RUNTIME CONSTRAINTS on the Recommendation Engine.
# They are NOT model targets: changing them changes the plan immediately and
# NEVER requires retraining the Random Forest (the model does not see them).
#
# Resolution order when the engine asks "is Food/Lunch essential?":
# 1. an exact (category, subcategory) preference row
# 2. a category-level row (subcategory = '')
# 3. the default: NOT essential (flexible), so nothing is silently protected

from app.config import (
    DEFAULT_ESSENTIAL_PREFERENCES,
    validate_category,
)

from app.database import crud


DEFAULT_IS_ESSENTIAL = False


def list_preferences(user_id):
    """Every stored preference for this user."""

    return crud.get_preferences(user_id)


def get_preference(
    user_id,
    category,
    subcategory=None
):
    return crud.get_preference(
        user_id,
        category,
        subcategory
    )


def set_preference(
    user_id,
    category,
    subcategory=None,
    is_essential=False
):
    """Create the preference, or overwrite it if it already exists."""

    validate_category(
        category,
        subcategory
    )

    return crud.add_preference(
        user_id,
        category,
        subcategory,
        is_essential
    )


def update_preference(
    user_id,
    category,
    subcategory=None,
    is_essential=False
):
    """Change an existing preference. False if there was nothing to change."""

    validate_category(
        category,
        subcategory
    )

    return crud.update_preference(
        user_id,
        category,
        subcategory,
        is_essential
    )


def delete_preference(
    user_id,
    category,
    subcategory=None
):
    return crud.delete_preference(
        user_id,
        category,
        subcategory
    )


def clear_preferences(user_id):
    return crud.delete_all_preferences(
        user_id
    )


def seed_default_preferences(
    user_id,
    overwrite=False
):
    """Give a new user a sensible starting point that they can change at any time."""

    existing = {
        (
            p["main_category"],
            p["subcategory"]
        )
        for p in list_preferences(user_id)
    }

    written = 0

    for category, subcategory, essential in DEFAULT_ESSENTIAL_PREFERENCES:

        if (
            not overwrite
            and (category, subcategory) in existing
        ):
            continue

        crud.add_preference(
            user_id,
            category,
            subcategory or None,
            bool(essential)
        )

        written += 1

    return written


def preference_map(user_id):
    """{('Food', 'Lunch'): True, ('Utilities', ''): True, ...}"""

    return {
        (
            p["main_category"],
            p["subcategory"]
        ): p["is_essential"]
        for p in list_preferences(user_id)
    }


def is_essential(
    user_id,
    category,
    subcategory=None,
    prefs=None
):
    """Apply the resolution order described at the top of this file."""

    prefs = (
        preference_map(user_id)
        if prefs is None
        else prefs
    )

    sub = subcategory or ""

    if (category, sub) in prefs:
        return prefs[(category, sub)]

    if (category, "") in prefs:
        return prefs[(category, "")]

    return DEFAULT_IS_ESSENTIAL


def split_category_spending(
    user_id,
    category,
    subcategory_totals,
    prefs=None
):
    """
    Split one category's spending into PROTECTED and ELIGIBLE (reducible) money.

    subcategory_totals:
    {"Restaurant": 800.0, "Lunch": 300.0, ...}
    """

    prefs = (
        preference_map(user_id)
        if prefs is None
        else prefs
    )

    protected = 0.0
    eligible = 0.0

    protected_subs = []
    eligible_subs = []

    for sub, amount in subcategory_totals.items():

        real_sub = (
            None
            if sub == "(unspecified)"
            else sub
        )

        if is_essential(
            user_id,
            category,
            real_sub,
            prefs
        ):
            protected += amount

            protected_subs.append({
                "subcategory": sub,
                "amount": round(amount, 2)
            })

        else:
            eligible += amount

            eligible_subs.append({
                "subcategory": sub,
                "amount": round(amount, 2)
            })

    eligible_subs.sort(
        key=lambda item: -item["amount"]
    )

    protected_subs.sort(
        key=lambda item: -item["amount"]
    )

    return {
        "category": category,
        "total": round(
            protected + eligible,
            2
        ),
        "protected_amount": round(
            protected,
            2
        ),
        "eligible_amount": round(
            eligible,
            2
        ),
        "protected_subcategories": protected_subs,
        "eligible_subcategories": eligible_subs,
        "fully_protected": (
            eligible <= 0
            and protected > 0
        ),
    }