# ui/views/money.py
# Income records and recurring bills: the two inputs the budget is built from.

from datetime import date

import pandas as pd
import streamlit as st

from ui import api
from ui.forms import form_key, reset_form
from ui.i18n import cat, money, t
from ui.theme import empty, page_header, panel
from ui.views.transactions import category_pickers, expense_tree

FREQUENCIES = ["monthly", "weekly", "yearly"]


def _selectable_table(view, key):
    event = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="multi-row", key=key)
    return event.selection.rows


def _income(tree):
    with panel():
        st.subheader(t("money.add_income"))
        c1, c2, c3 = st.columns(3)
        amount = c1.number_input(t("field.amount"), min_value=0.0, step=100.0, key=form_key("inc", "amount"))
        sources = tree.get("Income", ["Other"])
        source = c2.selectbox(t("field.source"), sources, format_func=cat, key=form_key("inc", "source"))
        when = c3.date_input(t("field.date"), value=date.today().replace(day=1), key=form_key("inc", "date"))
        if st.button(t("money.add_income_button"), type="primary"):
            if amount <= 0:
                st.error(t("error.amount_positive"))
            elif api.post("/income", {"user_id": api.user_id(), "amount": amount,
                                      "source": source, "date": str(when)}):
                st.toast(t("money.income_added"))
                reset_form("inc")
                st.rerun()

    rows = api.get("/income") or []
    if not rows:
        empty("money.income_empty_title", "money.income_empty_body")
        return
    df = pd.DataFrame(rows).sort_values(["date", "id"], ascending=False).reset_index(drop=True)
    st.caption(t("money.income_total", n=len(df), total=money(df["amount"].sum())))
    picked = _selectable_table(pd.DataFrame({
        t("field.date"): df["date"],
        t("field.source"): df["source"].fillna("Other").map(cat),
        t("field.amount"): df["amount"],
    }), "inc_table")
    if picked and st.button(t("money.delete_selected", n=len(picked)), key="inc_delete"):
        for i in picked:
            api.delete(f"/income/{int(df.iloc[i]['id'])}")
        st.rerun()


def _recurring(tree):
    with panel():
        st.subheader(t("money.add_recurring"))
        c1, c2, c3 = st.columns([3, 2, 2])
        description = c1.text_input(t("field.description"), key=form_key("rec", "desc"),
                                    placeholder=t("money.recurring_placeholder"))
        amount = c2.number_input(t("field.amount"), min_value=0.0, step=10.0, key=form_key("rec", "amount"))
        frequency = c3.selectbox(t("field.frequency"), FREQUENCIES,
                                 format_func=lambda f: t(f"freq.{f}"), key=form_key("rec", "freq"))
        category, subcategory = category_pickers(expense_tree(tree), form_key("rec", "cat"), "Bills")
        if st.button(t("money.add_recurring_button"), type="primary"):
            if amount <= 0:
                st.error(t("error.amount_positive"))
            elif not description.strip():
                st.error(t("error.description_required"))
            elif api.post("/recurring", {
                "user_id": api.user_id(), "description": description.strip(), "amount": amount,
                "category": category, "subcategory": subcategory, "frequency": frequency,
            }):
                st.toast(t("money.recurring_added"))
                reset_form("rec")
                st.rerun()

    rows = api.get("/recurring") or []
    if not rows:
        empty("money.recurring_empty_title", "money.recurring_empty_body")
        return
    df = pd.DataFrame(rows)
    picked = _selectable_table(pd.DataFrame({
        t("field.description"): df["description"],
        t("field.category"): df["category"].map(cat),
        t("field.subcategory"): df["subcategory"].fillna("").map(lambda s: cat(s) if s else ""),
        t("field.frequency"): df["frequency"].map(lambda f: t(f"freq.{f}") if f in FREQUENCIES else f),
        t("field.amount"): df["amount"],
    }), "rec_table")
    if picked and st.button(t("money.delete_selected", n=len(picked)), key="rec_delete"):
        for i in picked:
            api.delete(f"/recurring/{int(df.iloc[i]['id'])}")
        st.rerun()


def render(tree):
    page_header("money.title", "money.lede")
    tab_income, tab_recurring = st.tabs([t("money.tab_income"), t("money.tab_recurring")])
    with tab_income:
        _income(tree)
    with tab_recurring:
        _recurring(tree)
