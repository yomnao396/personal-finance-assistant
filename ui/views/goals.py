# ui/views/goals.py
# Goals: drag to set priority (the top goal drives the plan), track progress,
# add savings, create new goals and test "what if I save X a month".

from datetime import date, timedelta

import streamlit as st
from streamlit_sortables import sort_items

from ui import api
from ui.board import board_style
from ui.forms import form_key, reset_form
from ui.i18n import money, t
from ui.theme import empty, esc, goal_progress, note, page_header, panel
from ui.views.afford import afford_panel


def _labels(goals):
    """Unique, readable card text for every goal -> {label: goal_id}."""
    labels = {}
    for g in goals:
        # Unicode isolates keep an English name and a number readable inside Arabic text
        label = f"\u2068{g['name']}\u2069 — \u2068{money(g['target_amount'])}\u2069"
        if label in labels:
            label = f"{label} #{g['id']}"
        labels[label] = g["id"]
    return labels


def _priority_board(goals):
    with panel():
        st.subheader(t("goals.priority_title"))
        st.caption(t("goals.priority_hint"))
        labels = _labels(goals)
        current = list(labels)
        # One-column board in multi-container mode: the component's single-list mode
        # does not report a reorder back to the app (found in UAT).
        board = sort_items([{"header": t("goals.priority_header"), "items": current}],
                           multi_containers=True, direction="vertical",
                           custom_style=board_style(numbered=True),
                           key=f"goal_board_{'|'.join(map(str, labels.values()))}_{st.session_state.get('lang')}")
        ordered = board[0]["items"]
        if ordered != current:
            api.put("/goals/order", {"user_id": api.user_id(),
                                     "goal_ids": [labels[label] for label in ordered]})
            st.toast(t("goals.order_saved"))
            st.rerun()

        ranking = api.get("/goals/rank") or {}
        ranked = ranking.get("ranked_goals") or []
        if ranked:
            suggested = [r["goal_id"] for r in ranked]
            names = " → ".join(r["goal_name"] for r in ranked)
            st.caption(t("goals.suggested", order=names))
            if ranking.get("goals_compete_for_money"):
                st.caption(t("goals.compete", need=money(ranking["total_required_monthly_saving"]),
                             have=money(ranking["monthly_saving_capacity"])))
            if suggested != list(labels.values()) and st.button(t("goals.use_suggested"),
                                                                  key="use_suggested"):
                api.put("/goals/order", {"user_id": api.user_id(), "goal_ids": suggested})
                st.toast(t("goals.order_saved"))
                st.rerun()


def _goal_card(goal, is_primary):
    with panel():
        if is_primary:
            st.markdown(f'<span class="pfa-chip goal">{esc(t("goals.drives_plan"))}</span>',
                        unsafe_allow_html=True)
        goal_progress(goal)
        c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
        amount = c1.number_input(t("goals.add_savings"), min_value=0.0, step=100.0,
                                 key=form_key(f"contrib{goal['id']}", "amount"))
        if c2.button(t("goals.add_button"), key=f"contrib_btn_{goal['id']}", type="primary",
                     width="stretch"):
            if amount <= 0:
                st.error(t("error.amount_positive"))
            elif api.post(f"/goals/{goal['id']}/contribute", {"amount": amount}):
                st.toast(t("goals.contributed", amount=money(amount), name=goal["name"]))
                reset_form(f"contrib{goal['id']}")
                st.rerun()
        if c3.button(t("goals.delete"), key=f"del_goal_{goal['id']}", width="stretch"):
            api.delete(f"/goals/{goal['id']}")
            st.rerun()


def _new_goal():
    with panel():
        st.subheader(t("goals.new_title"))
        c1, c2, c3 = st.columns([3, 2, 2])
        name = c1.text_input(t("field.goal_name"), placeholder=t("goals.name_placeholder"),
                             key=form_key("goal", "name"))
        target = c2.number_input(t("field.target"), min_value=0.0, step=500.0, key=form_key("goal", "target"))
        saved = c3.number_input(t("field.saved_so_far"), min_value=0.0, step=100.0, key=form_key("goal", "saved"))
        has_date = st.checkbox(t("goals.has_date"), value=True, key=form_key("goal", "has_date"))
        when = st.date_input(t("field.target_date"), value=date.today() + timedelta(days=180),
                             min_value=date.today(), key=form_key("goal", "date"), disabled=not has_date)
        if st.button(t("goals.create"), type="primary"):
            if not name.strip():
                st.error(t("error.name_required"))
            elif target <= 0:
                st.error(t("error.target_positive"))
            elif api.post("/goals", {"user_id": api.user_id(), "name": name.strip(),
                                     "target_amount": target, "current_amount": saved,
                                     "target_date": str(when) if has_date else None}):
                st.toast(t("goals.created", name=name.strip()))
                reset_form("goal")
                st.rerun()


def _calculator(goals):
    with panel():
        st.subheader(t("goals.calc_title"))
        names = [g["name"] for g in goals]
        c1, c2 = st.columns(2)
        name = c1.selectbox(t("field.goal_name"), names, key="calc_goal")
        monthly = c2.number_input(t("goals.calc_monthly"), min_value=0.0, step=100.0,
                                  value=1000.0, key="calc_monthly")
        if monthly <= 0:
            return
        result = api.get("/goal-check", goal_name=name, monthly_saving=monthly)
        if not result or "error" in result:
            return
        if result.get("months_needed") == 0:
            note(esc(t("goals.calc_done")), "good")
        else:
            note(t("goals.calc_result", monthly=f"<b>{esc(money(monthly))}</b>",
                   months=f"<b>{result['months_needed']}</b>",
                   remaining=f"<b>{esc(money(result['remaining']))}</b>"), "good")


def render():
    page_header("goals.title", "goals.lede")
    goals = api.get("/goals")
    if goals is None:
        return
    analysis = api.get("/analysis") or {}
    primary_name = (analysis.get("goal_analysis") or {}).get("goal_name")

    afford_panel()

    if not goals:
        empty("goals.empty_title", "goals.empty_body")
        _new_goal()
        return

    left, right = st.columns([3, 2], gap="medium")
    with left:
        for g in goals:
            _goal_card(g, g["name"] == primary_name)
    with right:
        if len(goals) > 1:
            _priority_board(goals)
        _calculator(goals)
    _new_goal()
