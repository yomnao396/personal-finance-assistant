# ui/views/afford.py
# "Can I afford it?" — the forward-looking goal assessment with its what-if
# scenarios. Every sentence is rebuilt from the numbers so it reads naturally in
# both languages.

import streamlit as st

from ui import api
from ui.i18n import cat, money, t
from ui.theme import CORAL, MINT, SAFFRON, esc, facts, note, panel

LABEL_STYLE = {"Likely": ("down", MINT), "Borderline": ("goal", SAFFRON), "Unlikely": ("up", CORAL)}
CUTTABLE = ["Shopping", "Entertainment", "Food", "Transport", "Others"]


def _meter(probability, label):
    kind, color = LABEL_STYLE[label]
    pct = probability * 100
    return f"""
<div class="pfa-meter" role="img" aria-label="{esc(t('afford.meter_aria', pct=f'{pct:.0f}'))}">
  <div class="pfa-meter-top">
    <span class="pfa-meter-value">{pct:.0f}%</span>
    <span class="pfa-chip {kind}">{esc(t(f'afford.label.{label}'))}</span>
  </div>
  <div class="pfa-meter-track">
    <span style="width:{pct:.1f}%;background:{color}"></span>
    <i style="inset-inline-start:40%"></i><i style="inset-inline-start:70%"></i>
  </div>
  <div class="pfa-meter-scale"><span>0%</span><span>40%</span><span>70%</span><span>100%</span></div>
</div>"""


def _action(r):
    if r["extra_monthly_saving_needed"] == 0:
        return esc(t("afford.on_track"))
    extra = f"<b>{esc(money(r['extra_monthly_saving_needed']))}</b>"
    if r["extra_months_needed"] is None:
        return t("afford.action_save_only", extra=extra)
    return t("afford.action_both", extra=extra, months=f"<b>{r['extra_months_needed']}</b>")


def _honesty(r):
    return t(f"state.{r['data_state']}", n=r.get("history_months", 0))


def _what_if(target, months, baseline):
    with st.expander(t("afford.whatif_title"), expanded=True):
        st.caption(t("afford.whatif_hint"))
        c1, c2 = st.columns(2)
        category = c1.selectbox(t("afford.cut_category"), [""] + CUTTABLE, key="wi_cat",
                                format_func=lambda c: t("afford.no_cut") if not c else cat(c))
        cut = c2.number_input(t("afford.cut_amount"), min_value=0.0, step=50.0, key="wi_cut",
                              disabled=not category)
        c3, c4, c5 = st.columns(3)
        extra = c3.number_input(t("afford.extra_saving"), min_value=0.0, step=100.0, key="wi_extra")
        income = c4.slider(t("afford.income_change"), -50, 50, 0, step=5, key="wi_income",
                           format="%d%%")
        later = c5.number_input(t("afford.extra_months"), min_value=0, max_value=60, step=1,
                                key="wi_months")
        if not (category and cut) and not extra and not income and not later:
            return
        result = api.post("/simulate", {
            "user_id": api.user_id(), "target_amount": target, "months": months,
            "cut_category": category or None, "cut_amount": cut, "extra_saving": extra,
            "income_change_pct": income, "extra_months": later,
        })
        if not result or "error" in result:
            return
        before, after = result["baseline"], result["scenario"]
        change = result["probability_change"] * 100
        kind = "good" if change > 0 else ("bad" if change < 0 else "")
        note(t("afford.whatif_result",
               before=f"<b>{before['probability'] * 100:.0f}%</b>",
               after=f"<b>{after['probability'] * 100:.0f}%</b>",
               change=f"<b>{change:+.0f}</b>",
               label=esc(t(f"afford.label.{after['label']}"))), kind)
        if result["warnings"]:
            st.caption(t("afford.essential_warning", category=cat(category)))


def _decision(price, item):
    """Buy now vs wait vs installments, from the decision engine (read-only)."""
    with st.expander(t("afford.decision_title"), expanded=True):
        st.caption(t("afford.decision_hint"))
        c1, c2 = st.columns(2)
        count = c1.number_input(t("afford.installments"), min_value=0, max_value=60, value=0, step=1,
                                key="af_installments")
        urgent = c2.checkbox(t("afford.urgent"), key="af_urgent")
        params = {"price": price, "urgent": urgent}
        if count:
            params["installments"] = int(count)
        r = api.get("/decisions/purchase", **params)
        if not r or r.get("verdict") == "insufficient_information":
            note(esc(t("afford.need_data")), "bad")
            return
        rec = r.get("recommended")
        note(esc(t("afford.recommended", option=t(f"afford.option.{rec}"))), "good")
        rows = []
        for o in r["options"]:
            verdict = t(f"afford.verdict.{o['verdict']}")
            if o["kind"] == "delay" and o.get("months"):
                detail = t("afford.months_wait", months=o["months"], amount=money(o["per_month"]))
            elif o["kind"] == "installments":
                detail = f"{o['months']} × {money(o['per_month'])}"
            elif o["kind"] == "cheaper" and o.get("max_price") is not None:
                detail = "≤ " + money(o["max_price"])
            else:
                detail = money(o.get("cost") or price)
            rows.append((t(f"afford.option.{o['kind']}"), verdict, detail))
        facts(rows)
        if r.get("max_safe_price") is not None:
            st.caption(t("afford.safe_price", amount=money(r["max_safe_price"])))


def afford_panel():
    with panel():
        st.subheader(t("afford.title"))
        st.caption(t("afford.hint"))
        c1, c2, c3 = st.columns([3, 2, 2])
        item = c1.text_input(t("afford.item"), placeholder=t("afford.item_placeholder"),
                             key="af_item")
        price = c2.number_input(t("afford.price"), min_value=0.0, step=500.0, value=5000.0,
                                key="af_price")
        months = c3.number_input(t("afford.months"), min_value=1, max_value=120, value=4,
                                 step=1, key="af_months")
        if price <= 0:
            return

        r = api.get("/assess_goal", target_amount=price, months=int(months))
        if r is None:
            return
        if "error" in r:
            note(esc(t("afford.need_data")), "bad")
            return

        question = t("afford.question", item=item.strip() or t("afford.this"),
                     price=money(price), months=int(months))
        st.markdown(f"**{esc(question)}**")
        st.markdown(_meter(r["probability"], r["label"]), unsafe_allow_html=True)
        facts([
            (t("afford.expected_saving"), money(r["expected_monthly_saving"]),
             t("afford.per_month")),
            (t("afford.projected"), money(r["projected_savings"]),
             t("afford.plus_minus", spread=money(r["projected_spread"]))),
            (t("afford.spending_forecast"), money(r["predicted_monthly_spending"]),
             t("afford.plus_minus", spread=money(r["spending_uncertainty"]))),
        ])
        note(_action(r), "good" if r["extra_monthly_saving_needed"] == 0 else "")
        st.caption(_honesty(r) + " " + t("afford.method"))
        _what_if(price, int(months), r)
        _decision(price, item)
