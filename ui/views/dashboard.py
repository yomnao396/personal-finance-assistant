# ui/views/dashboard.py
# The month told as one sentence, the income split bar, then the few facts that
# matter: saving rate, the forecast, the goal, and the first step of the plan.

import streamlit as st

from ui import api, charts
from ui.i18n import cat, money, t
from ui.theme import esc, facts, goal_progress, money_split, note, panel


def _go_to(page):
    st.session_state["page"] = page


def render():
    analysis = api.get("/analysis")
    if analysis is None:
        return

    st.title(t("dash.title"))

    income = analysis["income"]
    spent = analysis["expenses"]
    kept = analysis["savings"]

    if not income and not spent:
        note(esc(t("dash.no_data")))
        st.button(t("dash.start_setup"), type="primary", key="dash_setup",
                  on_click=_go_to, args=("setup",))
        return

    state = api.get("/data-state") or {}
    if state.get("data_state") == "entered":
        note(esc(t("dash.estimates_note")))

    month = analysis.get("current_month") or t("dash.all_time")
    if kept >= 0:
        sentence = t("dash.sentence", income=f'<b class="in">{esc(money(income))}</b>',
                     spent=f'<b class="out">{esc(money(spent))}</b>',
                     kept=f'<b class="kept">{esc(money(kept))}</b>')
    else:
        sentence = t("dash.sentence_deficit", income=f'<b class="in">{esc(money(income))}</b>',
                     spent=f'<b class="out">{esc(money(spent))}</b>',
                     over=f'<b class="out">{esc(money(-kept))}</b>')

    st.markdown(
        f'<div class="pfa-hero"><div class="when">{esc(t("dash.month_of", month=month))}</div>'
        f'<div class="sentence">{sentence}</div>'
        f'{money_split(analysis["categories"], max(kept, 0))}</div>',
        unsafe_allow_html=True,
    )

    forecast = analysis.get("prediction") or {}
    highest = analysis["category_analysis"].get("highest_category") or {}
    patterns = analysis["spending_patterns"]
    items = [
        (t("dash.saving_rate"), f'{analysis["savings_rate"]:.1f}%', t("dash.saving_rate_hint")),
        (t("dash.avg_spending"), money(patterns["average_monthly_spending"]),
         t("dash.over_months", n=patterns["months_observed"])),
    ]
    if highest:
        items.append((t("dash.biggest"), cat(highest["category"]),
                      t("dash.of_spending", pct=f'{highest["percent_of_expenses"]:.0f}')))
    if forecast.get("total_predicted") is not None:
        items.append((t("dash.forecast", month=forecast["month"]), money(forecast["total_predicted"]),
                      t("dash.vs_now", change=money(forecast["total_change"], signed=True))))
    facts(items)

    left, right = st.columns([3, 2], gap="medium")

    with left:
        with panel():
            st.subheader(t("dash.months_title"))
            charts.income_vs_spending(
                analysis["income_analysis"].get("monthly_income", {}),
                patterns.get("monthly_spending", {}),
            )

    with right:
        goal = analysis.get("goal_analysis")
        with panel():
            st.subheader(t("dash.goal_title"))
            if goal:
                primary = next((g for g in analysis["goals"] if g["name"] == goal["goal_name"]), None)
                if primary:
                    goal_progress(primary)
                if goal["on_track"]:
                    note(esc(t("dash.goal_on_track")), "good")
                else:
                    note(t("dash.goal_gap", need=f"<b>{esc(money(goal['required_monthly_saving']))}</b>",
                           now=f"<b>{esc(money(goal['current_monthly_saving']))}</b>",
                           gap=f"<b>{esc(money(goal['saving_gap']))}</b>"), "bad")
                st.button(t("dash.open_plan"), type="primary", on_click=_go_to, args=("plan",),
                          width="stretch")
            else:
                st.caption(t("dash.no_goal"))
                st.button(t("dash.add_goal"), on_click=_go_to, args=("goals",))
