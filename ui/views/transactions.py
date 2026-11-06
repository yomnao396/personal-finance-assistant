# ui/views/transactions.py
# Add, filter, edit and delete spending. Category -> subcategory pickers are
# driven by the category tree served from the database.

import io
from datetime import date

import pandas as pd
import streamlit as st

from ui import api
from ui.i18n import cat, money, t
from ui.theme import empty, page_header, panel

NONE = ""


def expense_tree(tree):
    return {c: subs for c, subs in tree.items() if c != "Income"}


def category_pickers(tree, key, default_cat=None, default_sub=None):
    """Two linked selectboxes; returns (category, subcategory or None)."""
    cats = list(tree)
    c1, c2 = st.columns(2)
    category = c1.selectbox(
        t("field.category"), cats, format_func=cat, key=f"{key}_cat",
        index=cats.index(default_cat) if default_cat in cats else 0,
    )
    subs = [NONE] + tree.get(category, [])
    subcategory = c2.selectbox(
        t("field.subcategory"), subs, key=f"{key}_sub_{category}",
        format_func=lambda s: t("field.not_specified") if s == NONE else cat(s),
        index=subs.index(default_sub) if default_sub in subs else 0,
        disabled=len(subs) == 1,
    )
    return category, (subcategory or None)


def _add_form(tree):
    with panel():
        st.subheader(t("tx.add_title"))
        c1, c2, c3 = st.columns([3, 2, 2])
        description = c1.text_input(t("field.description"), key="tx_desc",
                                    placeholder=t("tx.desc_placeholder"))
        amount = c2.number_input(t("field.amount"), min_value=0.0, step=10.0, key="tx_amount")
        when = c3.date_input(t("field.date"), value=date.today(), key="tx_date")
        category, subcategory = category_pickers(tree, "tx_new", "Food")

        st.button(t("tx.add_button"), type="primary", on_click=_submit_expense,
                  args=(description, amount, when, category, subcategory))
        message = st.session_state.pop("tx_message", None)
        if message == "added":
            st.toast(t("tx.added"))
        elif message:
            st.error(t(message))


def _submit_expense(description, amount, when, category, subcategory):
    """Runs before the page is redrawn, so the form can be cleared after a save."""
    if amount <= 0:
        st.session_state["tx_message"] = "error.amount_positive"
    elif not description.strip():
        st.session_state["tx_message"] = "error.description_required"
    elif api.post("/transactions", {
        "user_id": api.user_id(), "description": description.strip(), "amount": amount,
        "category": category, "subcategory": subcategory, "date": str(when),
    }):
        st.session_state["tx_message"] = "added"
        st.session_state["tx_desc"] = ""
        st.session_state["tx_amount"] = 0.0


def _edit_form(tree, row):
    with panel():
        st.subheader(t("tx.edit_title", description=row["description"] or "—"))
        c1, c2, c3 = st.columns([3, 2, 2])
        description = c1.text_input(t("field.description"), value=row["description"] or "",
                                    key=f"edit_desc_{row['id']}")
        amount = c2.number_input(t("field.amount"), min_value=0.0, step=10.0,
                                 value=float(row["amount"]), key=f"edit_amount_{row['id']}")
        current = date.fromisoformat(row["date"][:10]) if row.get("date") else date.today()
        when = c3.date_input(t("field.date"), value=current, key=f"edit_date_{row['id']}")
        category, subcategory = category_pickers(tree, f"edit_{row['id']}",
                                                 row["category"], row.get("subcategory"))
        if st.button(t("tx.save_button"), type="primary", key=f"save_{row['id']}"):
            if amount <= 0:
                st.error(t("error.amount_positive"))
            elif api.put(f"/transactions/{row['id']}", {
                "description": description.strip(), "amount": amount, "category": category,
                "subcategory": subcategory or "", "date": str(when),
            }):
                st.toast(t("tx.saved"))
                st.rerun()


def _import_form():
    with panel():
        st.subheader(t("import.title"))
        st.caption(t("import.hint"))
        template = (api.get("/transactions/import/template") or {}).get("csv", "")
        st.download_button(t("import.template"), template, file_name="transactions_template.csv",
                           mime="text/csv")
        upload = st.file_uploader(t("import.file"), type=["csv"], key="import_file")
        if upload is None:
            return
        text = upload.getvalue().decode("utf-8-sig", errors="replace")
        try:
            preview = pd.read_csv(io.StringIO(text), nrows=5)
            st.caption(t("import.preview"))
            st.dataframe(preview, hide_index=True, width="stretch")
        except Exception:  # noqa: BLE001 — the API reports the exact problem
            pass
        if st.button(t("import.button"), type="primary"):
            result = api.post("/transactions/import", {"user_id": api.user_id(), "csv": text},
                              timeout=120)
            if result:
                st.success(t("import.done", expenses=result["added_expenses"],
                             income=result["added_income"], mapped=result["categories_mapped"]))
                if result["errors"]:
                    st.warning(t("import.errors", n=len(result["errors"])))
                    st.dataframe(pd.DataFrame(result["errors"]).rename(
                        columns={"row": t("import.row"), "error": t("import.problem")}),
                        hide_index=True, width="stretch")


def render(tree):
    page_header("tx.title", "tx.lede")
    tree = expense_tree(tree)
    tab_add, tab_import = st.tabs([t("tx.tab_add"), t("tx.tab_import")])
    with tab_add:
        _add_form(tree)
    with tab_import:
        _import_form()

    rows = api.get("/transactions") or []
    rows = [r for r in rows if r.get("category") != "Income"]
    if not rows:
        empty("tx.empty_title", "tx.empty_body")
        return

    df = pd.DataFrame(rows)
    df["month"] = df["date"].fillna("").str[:7]

    f1, f2 = st.columns(2)
    months = sorted({m for m in df["month"] if m}, reverse=True)
    month = f1.selectbox(t("field.month"), ["*"] + months,
                         format_func=lambda m: t("filter.all_months") if m == "*" else m)
    cats = sorted(df["category"].unique(), key=cat)
    category = f2.selectbox(t("field.category"), ["*"] + cats,
                            format_func=lambda c: t("filter.all_categories") if c == "*" else cat(c),
                            key="tx_filter_cat")
    if month != "*":
        df = df[df["month"] == month]
    if category != "*":
        df = df[df["category"] == category]
    df = df.sort_values(["date", "id"], ascending=False).reset_index(drop=True)

    st.caption(t("tx.total_shown", n=len(df), total=money(df["amount"].sum())))

    view = pd.DataFrame({
        t("field.date"): df["date"],
        t("field.description"): df["description"],
        t("field.category"): df["category"].map(cat),
        t("field.subcategory"): df["subcategory"].fillna("").map(lambda s: cat(s) if s else ""),
        t("field.amount"): df["amount"],
    })
    event = st.dataframe(
        view, hide_index=True, width="stretch", on_select="rerun",
        selection_mode="multi-row", key="tx_table",
        column_config={t("field.amount"): st.column_config.NumberColumn(format="%.2f")},
    )
    picked = [df.iloc[i].to_dict() for i in event.selection.rows]

    if not picked:
        st.caption(t("tx.select_hint"))
        return

    if len(picked) == 1:
        _edit_form(tree, picked[0])

    if st.button(t("tx.delete_button", n=len(picked)), key="tx_delete"):
        for row in picked:
            api.delete(f"/transactions/{int(row['id'])}")
        st.toast(t("tx.deleted", n=len(picked)))
        st.rerun()
