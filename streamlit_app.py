# streamlit_app.py
# The user interface. Run the API first (uvicorn app.api.main:app), then:
#   streamlit run streamlit_app.py
# Pages live in ui/views/, texts in ui/i18n.py (English + Arabic), styling in ui/theme.py.

import streamlit as st

from ui import api, theme
from ui.i18n import LANGUAGES, t
from ui.views import (analysis, assistant, budget, dashboard, essentials, goals, money, plan,
                      setup, tips, transactions)

st.set_page_config(page_title="Personal Finance AI Assistant", page_icon="💰", layout="wide")

PAGES = {
    "dashboard": lambda tree: dashboard.render(),
    "transactions": transactions.render,
    "money": money.render,
    "analysis": lambda tree: analysis.render(),
    "budget": lambda tree: budget.render(),
    "goals": lambda tree: goals.render(),
    "plan": lambda tree: plan.render(),
    "essentials": essentials.render,
    "assistant": lambda tree: assistant.render(),
    "tips": lambda tree: tips.render(),
    "setup": setup.render,
}

st.session_state.setdefault("lang", "en")
st.session_state.setdefault("page", "dashboard")


def _reset_demo():
    result = api.post("/demo/reset", timeout=60)
    if result:
        st.session_state["chat_history"] = []
        st.session_state["user_id"] = result["user_id"]
        st.session_state["page"] = "dashboard"
        st.toast(t("sidebar.demo_loaded"))


def _switch_user():
    st.session_state["user_id"] = st.session_state["profile_picker"]
    st.session_state["chat_history"] = []


def _switch_page():
    st.session_state["page"] = st.session_state["nav"]


def _profile_picker():
    users = api.get("/users") or []
    if not users:
        return
    ids = [u["id"] for u in users]
    labels = {u["id"]: f"{u['username']} ({t('state.short.' + u['data_state'])})" for u in users}
    current = st.session_state.get("user_id", api.DEFAULT_USER_ID)
    if current not in ids:
        current = ids[0]
        st.session_state["user_id"] = current
    # The real choice lives in st.session_state["user_id"] and is pushed into the
    # widget on every run: its translated labels change with the language, which
    # Streamlit may treat as a brand-new widget, so the widget must never own it.
    st.session_state["profile_picker"] = current
    st.markdown(f"**{t('sidebar.profile')}**")
    st.selectbox("profile", ids, key="profile_picker", format_func=labels.get,
                 on_change=_switch_user, label_visibility="collapsed")


def sidebar(status):
    with st.sidebar:
        st.markdown(f"### {t('app.name')}")
        st.caption(t("app.tagline"))

        lang = st.segmented_control(
            t("sidebar.language"), list(LANGUAGES), format_func=LANGUAGES.get,
            default=st.session_state["lang"], key="lang_picker",
        )
        if lang and lang != st.session_state["lang"]:
            st.session_state["lang"] = lang
            st.rerun()

        _profile_picker()

        st.session_state["nav"] = st.session_state["page"]   # same pattern as the profile
        st.radio("pages", list(PAGES), key="nav", on_change=_switch_page,
                 format_func=lambda p: t(f"nav.{p}"), label_visibility="collapsed")

        st.divider()
        if status:
            model_ok = status.get("recommendation_model")
            llm_ok = status.get("llm", {}).get("available")
            st.caption(("● " if model_ok else "○ ") + t("sidebar.model_ready" if model_ok else "sidebar.model_missing"))
            st.caption(("● " if llm_ok else "○ ") + t("sidebar.llm_ready" if llm_ok else "sidebar.llm_missing"))

        with st.popover(t("sidebar.load_demo"), width="stretch"):
            st.write(t("sidebar.demo_warning"))
            st.button(t("sidebar.demo_confirm"), type="primary", on_click=_reset_demo)


def api_down():
    st.title(t("down.title"))
    st.markdown(t("down.body"))
    st.code("uvicorn app.api.main:app --reload", language="bash")
    st.caption(t("down.url", url=api.API))
    if st.button(t("down.retry"), type="primary"):
        st.rerun()


theme.apply()

try:
    status = api.get("/status")
    tree = (api.get("/categories") or {}).get("categories", {})
    sidebar(status)
    PAGES[st.session_state["page"]](tree)
except api.ApiDown:
    api_down()
