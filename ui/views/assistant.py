# ui/views/assistant.py
# Chat with the local Qwen model. It answers in the selected language and only
# uses numbers returned by the finance tools.

import streamlit as st

from ui import api
from ui.i18n import current_language, t
from ui.theme import esc, note, page_header

SUGGESTIONS = ["chat.q1", "chat.q2", "chat.q3", "chat.q4", "chat.q5", "chat.q6"]


def _evidence(message):
    """Which tools produced the numbers and which sources back the facts."""
    tools = message.get("tools_used") or []
    sources = message.get("sources") or []
    if not tools and not sources:
        return
    parts = []
    if tools:
        names = ", ".join(dict.fromkeys(t(f"tool.{n}") for n in tools))
        parts.append(f"{esc(t('chat.based_on'))}: {esc(names)}")
    for source in sources:
        parts.append(f'{esc(t("tips.source"))}: <a href="{esc(source["url"])}" target="_blank" '
                     f'rel="noopener">{esc(source["publisher"])} — {esc(source["title"])}</a>')
    st.markdown('<div class="pfa-sources">' + "<br>".join(parts) + "</div>",
                unsafe_allow_html=True)


def _decision_table(message):
    """The options a decision answer compared, as a small table (the text above
    explains each one)."""
    table = message.get("table") or []
    if not table:
        return
    rows = "".join(f"<tr><td>{esc(r['option'])}</td><td>{esc(r['verdict'])}</td><td>{esc(r['detail'])}</td></tr>"
                   for r in table)
    st.markdown(f'<details class="pfa-sources"><summary>{esc(t("chat.options"))}</summary><table>'
                f"<tr><th>{esc(t('chat.col_option'))}</th><th>{esc(t('chat.col_verdict'))}</th>"
                f"<th>{esc(t('chat.col_detail'))}</th></tr>{rows}</table></details>", unsafe_allow_html=True)


def _table_of(reply):
    results = reply.get("tool_results") or []
    data = results[0].get("result") if results else None
    return data.get("table") if isinstance(data, dict) else None


def _ask(question):
    history = st.session_state.chat_history
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner(t("chat.thinking")):
            # "goal" and "context" tell the API what each answer was about (the
            # goal, period, category, what-if and calculation), so a follow-up such
            # as "reach it faster?" or "what about last month?" continues from it.
            reply, error = api.chat(question, [{"role": m["role"], "content": m["content"],
                                                "goal": m.get("goal"), "context": m.get("context")}
                                               for m in history], current_language())
        if error:
            st.error(t("chat.error", detail=error))
            return
        message = {"role": "assistant", "content": reply.get("answer", ""),
                   "tools_used": reply.get("tools_used", []), "sources": reply.get("sources", []),
                   "goal": reply.get("goal"), "context": reply.get("context"), "table": _table_of(reply)}
        st.write(message["content"])
        _decision_table(message)
        _evidence(message)
    history.append({"role": "user", "content": question})
    history.append(message)
    if (message.get("context") or {}).get("awaiting") == "confirm_action":
        st.rerun()          # draw the confirm / cancel buttons under this reply


def render():
    page_header("chat.title", "chat.lede")
    st.session_state.setdefault("chat_history", [])

    status = api.get("/status") or {}
    llm = status.get("llm", {})
    if not llm.get("available", False):
        note(esc(t("chat.offline", reason=llm.get("reason", ""))), "bad")

    if st.session_state.chat_history and st.button(t("chat.clear")):
        st.session_state.chat_history = []
        st.rerun()

    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.write(message["content"])
            if message["role"] == "assistant":
                _decision_table(message)
                _evidence(message)

    pending = None
    last = st.session_state.chat_history[-1] if st.session_state.chat_history else {}
    if (last.get("context") or {}).get("awaiting") == "confirm_action":
        # A change to saved data waits for the user's answer; the buttons send it.
        c1, c2 = st.columns(2)
        if c1.button(t("chat.confirm"), key=f"confirm_{len(st.session_state.chat_history)}", type="primary"):
            pending = t("chat.confirm")
        if c2.button(t("chat.cancel"), key=f"cancel_{len(st.session_state.chat_history)}"):
            pending = t("chat.cancel")
    if not st.session_state.chat_history:
        st.caption(t("chat.try"))
        cols = st.columns(2)
        for i, key in enumerate(SUGGESTIONS):
            if cols[i % 2].button(t(key), key=f"suggest_{key}", width="stretch"):
                pending = t(key)

    question = st.chat_input(t("chat.placeholder")) or pending
    if question:
        _ask(question)
