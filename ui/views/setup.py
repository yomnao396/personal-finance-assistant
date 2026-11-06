# ui/views/setup.py
# Getting started: create a profile and tell the app the basics. The answers are
# stored as estimates, and every forecast says "based on what you entered" until
# real spending is recorded.

import streamlit as st

from ui import api
from ui.i18n import cat, money, t
from ui.theme import esc, note, page_header, panel

ESTIMATE_CATEGORIES = ["Food", "Bills", "Transport", "Shopping", "Entertainment",
                       "Utilities", "Education", "Healthcare", "Others"]


def _create_profile():
    with panel():
        st.subheader(t("setup.new_title"))
        name = st.text_input(t("setup.name"), placeholder=t("setup.name_placeholder"),
                             key="setup_name")
        if st.button(t("setup.create"), type="primary"):
            if not name.strip():
                st.error(t("error.name_required"))
                return
            created = api.post("/users", {"name": name.strip()})
            if created:
                st.session_state["user_id"] = created["id"]
                st.session_state["chat_history"] = []
                st.toast(t("setup.created", name=name.strip()))
                st.rerun()


def _profile_form(tree):
    with panel():
        st.subheader(t("setup.form_title"))
        st.caption(t("setup.form_hint"))
        c1, c2, c3 = st.columns(3)
        income = c1.number_input(t("setup.income"), min_value=0.0, step=500.0, key="setup_income")
        sources = tree.get("Income", ["Salary"])
        source = c2.selectbox(t("field.source"), sources, format_func=cat, key="setup_source")
        savings = c3.number_input(t("setup.savings"), min_value=0.0, step=500.0, key="setup_savings")

        st.markdown(f"**{esc(t('setup.spending_title'))}**")
        st.caption(t("setup.spending_hint"))
        cols = st.columns(3)
        estimates = {}
        for i, category in enumerate(ESTIMATE_CATEGORIES):
            value = cols[i % 3].number_input(cat(category), min_value=0.0, step=100.0,
                                             key=f"setup_est_{category}")
            if value:
                estimates[category] = value

        st.markdown(f"**{esc(t('setup.goal_title'))}**")
        g1, g2, g3 = st.columns([3, 2, 2])
        goal_name = g1.text_input(t("field.goal_name"), placeholder=t("goals.name_placeholder"),
                                  key="setup_goal_name")
        goal_amount = g2.number_input(t("field.target"), min_value=0.0, step=500.0,
                                      key="setup_goal_amount")
        goal_months = g3.number_input(t("afford.months"), min_value=1, max_value=120, value=6,
                                      key="setup_goal_months")

        total = sum(estimates.values())
        if income:
            st.caption(t("setup.summary", income=money(income), spending=money(total),
                         left=money(income - total)))

        if st.button(t("setup.save"), type="primary"):
            if income <= 0:
                st.error(t("error.income_required"))
                return
            goal = ({"name": goal_name.strip(), "target_amount": goal_amount,
                     "months": int(goal_months)} if goal_name.strip() and goal_amount > 0 else None)
            saved = api.post(f"/users/{api.user_id()}/profile", {
                "monthly_income": income, "category_estimates": estimates,
                "current_savings": savings, "income_source": source, "goal": goal,
            })
            if saved:
                st.toast(t("setup.saved"))
                st.session_state["page"] = "dashboard"
                st.rerun()


def render(tree):
    page_header("setup.title", "setup.lede")
    state = api.get("/data-state") or {}
    if state.get("data_state") in ("recent", "pattern"):
        note(esc(t("setup.has_history", n=state.get("months_of_history", 0))), "good")
    elif state.get("data_state") == "entered":
        note(esc(t("setup.has_estimates")))
    _profile_form(tree)
    _create_profile()
