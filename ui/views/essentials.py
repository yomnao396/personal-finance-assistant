# ui/views/essentials.py
# Essential vs flexible spending as a two-column board. Dragging a card across
# saves the preference at once; the plan never cuts anything in "Essential".

import streamlit as st
from streamlit_sortables import sort_items

from ui import api
from ui.board import board_style, column_tint
from ui.i18n import cat, t
from ui.theme import esc, note, page_header


def _resolve(prefs, category, sub):
    """Same rule as the engine: exact row, then category row, else flexible."""
    if (category, sub) in prefs:
        return prefs[(category, sub)]
    return prefs.get((category, ""), False)


def _cards(tree, prefs):
    """[(label, category, subcategory, is_essential), ...] in tree order."""
    cards = []
    for category, subs in tree.items():
        if category == "Income":
            continue
        for sub in subs or [""]:
            label = f"{cat(category)} › {cat(sub)}" if sub else cat(category)
            cards.append((label, category, sub, _resolve(prefs, category, sub)))
    return cards


def render(tree):
    page_header("ess.title", "ess.lede")
    data = api.get("/preferences")
    if data is None:
        return
    prefs = {(p["main_category"], p["subcategory"]): p["is_essential"]
             for p in data["preferences"]}

    cards = _cards(tree, prefs)
    by_label = {label: (c, s) for label, c, s, _ in cards}
    essential = [label for label, *_, ess in cards if ess]
    flexible = [label for label, *_, ess in cards if not ess]

    board = [
        {"header": t("ess.essential_col", n=len(essential)), "items": essential},
        {"header": t("ess.flexible_col", n=len(flexible)), "items": flexible},
    ]
    style = (board_style()
             + column_tint(1, "#157F62", "#E3F1EC")
             + column_tint(2, "#D99A1E", "#FBF1DC"))
    result = sort_items(board, multi_containers=True, direction="vertical", custom_style=style,
                        key=f"ess_board_{hash(tuple(essential))}_{st.session_state.get('lang')}")

    new_essential = set(result[0]["items"])
    moved = [(label, label in new_essential) for label in by_label
             if (label in new_essential) != (label in essential)]
    if moved:
        for label, is_essential in moved:
            category, sub = by_label[label]
            api.post("/preferences", {"user_id": api.user_id(), "main_category": category,
                                      "subcategory": sub or None, "is_essential": is_essential})
        st.toast(t("ess.saved", n=len(moved)))
        st.rerun()

    note(esc(t("ess.explain")), "good")
    if not data["preferences"]:
        if st.button(t("ess.use_defaults"), type="primary"):
            api.post(f"/preferences/defaults?user_id={api.user_id()}")
            st.rerun()
