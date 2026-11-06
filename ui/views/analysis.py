# ui/views/analysis.py
# Where the money goes, how it changes month to month, where it comes from,
# and what next month will probably look like.

import streamlit as st

from ui import api, charts
from ui.i18n import cat, money, t
from ui.theme import color_for, esc, facts, note, page_header, panel, trend_chip


def _categories(a):
    ca = a["category_analysis"]
    st.caption(t("analysis.window", month=ca.get("window", "")))
    with panel():
        st.subheader(t("analysis.by_category"))
        charts.category_bars(ca["by_category"])

    by_sub = {c: subs for c, subs in ca["by_subcategory"].items()
              if len(subs) > 1 or "(unspecified)" not in subs}
    if by_sub:
        with panel():
            st.subheader(t("analysis.drilldown"))
            choice = st.selectbox(t("field.category"), list(by_sub), format_func=cat,
                                  key="drill_cat")
            subs = by_sub[choice]
            charts.category_bars(subs, color=color_for(choice))


def _trends(a):
    p = a["spending_patterns"]
    with panel():
        st.subheader(t("analysis.income_vs_spending"))
        charts.income_vs_spending(a["income_analysis"].get("monthly_income", {}),
                                  p.get("monthly_spending", {}))
    with panel():
        st.subheader(t("analysis.category_trends"))
        charts.category_trend_lines(p.get("category_monthly_spending", {}))
        rows = "".join(
            f"<tr><td>{esc(cat(c))}</td><td>{trend_chip(tr['direction'], tr.get('change_percent'))}</td>"
            f"<td style='text-align:end'>{esc(money(tr.get('recent_average') or 0))}</td></tr>"
            for c, tr in sorted(p.get("category_trends", {}).items(),
                                key=lambda kv: -(kv[1].get("change_percent") or 0))
        )
        st.markdown(
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<thead><tr><th style='text-align:start'>{esc(t('field.category'))}</th>"
            f"<th style='text-align:start'>{esc(t('analysis.direction'))}</th>"
            f"<th style='text-align:end'>{esc(t('analysis.recent_avg'))}</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
            unsafe_allow_html=True,
        )


def _income(a):
    ia = a["income_analysis"]
    if not ia.get("num_sources"):
        note(esc(t("analysis.no_income")))
        return
    primary = ia.get("primary_source") or {}
    facts([
        (t("analysis.avg_income"), money(ia["average_monthly_income"]),
         t("dash.over_months", n=ia["months_observed"])),
        (t("analysis.sources"), str(ia["num_sources"]), None),
        (t("analysis.main_source"), cat(primary.get("source", "")),
         t("analysis.share", pct=f'{primary.get("percent", 0):.0f}')),
        (t("analysis.trend"), t(f"trend.{ia['trend']['direction']}"), None),
    ])
    with panel():
        st.subheader(t("analysis.by_source"))
        charts.income_sources(ia["by_source"])
    stability = ia.get("stability", {})
    if stability.get("is_stable_claim_supported"):
        note(esc(t("analysis.stable_yes", n=ia["months_observed"])), "good")
    else:
        note(esc(t("analysis.stable_no")))


def _forecast():
    f = api.get("/prediction")
    if not f:
        return
    if "error" in f:
        note(esc(t("analysis.forecast_empty")))
        return
    facts([
        (t("analysis.forecast_total", month=f["month"]), money(f["total_predicted"]),
         t("dash.vs_now", change=money(f["total_change"], signed=True))),
        (t("analysis.expected_saving"),
         money(f["expected_saving"]) if f["expected_saving"] is not None else "—",
         t("analysis.if_income", income=money(f["expected_income"]))),
        (t("analysis.months_used"), str(f["months_used"]), None),
    ])
    with panel():
        st.subheader(t("analysis.forecast_by_category"))
        charts.forecast_bars(f["by_category"])
    st.caption(t("analysis.forecast_method", n=f["months_used"]))


def render():
    page_header("analysis.title", "analysis.lede")
    a = api.get("/analysis")
    if a is None:
        return
    if not a["expenses"] and not a["income"]:
        note(esc(t("dash.no_data")))
        return
    tabs = st.tabs([t("analysis.tab_categories"), t("analysis.tab_trends"),
                    t("analysis.tab_income"), t("analysis.tab_forecast")])
    with tabs[0]:
        _categories(a)
    with tabs[1]:
        _trends(a)
    with tabs[2]:
        _income(a)
    with tabs[3]:
        _forecast()
