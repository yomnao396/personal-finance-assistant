# ui/views/budget.py
# The suggested monthly budget next to what was actually spent this month.

import pandas as pd
import streamlit as st

from ui import api, charts
from ui.i18n import cat, join, money, t
from ui.theme import esc, facts, note, page_header, panel


def render():
    page_header("budget.title", "budget.lede")
    budget = api.get("/budget")
    analysis = api.get("/analysis")
    if budget is None or analysis is None:
        return
    if "error" in budget:
        note(esc(t("budget.no_income")))
        return

    spent = analysis["categories"]
    savings = budget.get("Savings", 0)
    buffer = budget.get("Buffer", 0)
    facts([
        (t("budget.income"), money(budget["_income"]), None),
        (t("budget.savings_first"), money(savings),
         t("budget.share_of_income", pct=f'{savings / budget["_income"] * 100:.0f}')),
        (t("budget.buffer"), money(buffer), t("budget.buffer_hint")),
    ])

    rows = sorted(
        ({"key": k, "budget": v, "spent": spent.get(k, 0.0)}
         for k, v in budget.items() if not k.startswith("_") and k not in ("Savings", "Buffer")),
        key=lambda r: -r["budget"],
    )
    with panel():
        st.subheader(t("budget.vs_spent", month=analysis.get("current_month") or ""))
        charts.budget_vs_spent(rows)

    table = pd.DataFrame({
        t("field.category"): [cat(r["key"]) for r in rows],
        t("chart.budget"): [r["budget"] for r in rows],
        t("chart.spent"): [r["spent"] for r in rows],
        t("budget.left"): [round(r["budget"] - r["spent"], 2) for r in rows],
    })
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="%.2f") for c in table.columns[1:]
    })

    over = [r for r in rows if r["spent"] > r["budget"]]
    if over:
        note(esc(t("budget.over", names=join([cat(r["key"]) for r in over]))), "bad")
    st.caption(t("budget.rules"))
