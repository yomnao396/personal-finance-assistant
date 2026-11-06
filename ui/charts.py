# ui/charts.py
# Altair charts. Rules: one axis per chart, colour follows the category (fixed
# map in ui/theme.py), thin marks with rounded data-ends, recessive grid, a
# tooltip on every mark, and value labels so colour never carries meaning alone.

import altair as alt
import pandas as pd
import streamlit as st

from ui.i18n import cat, t
from ui.theme import MUTED, color_for

SERIES = ["#2a78d6", "#eb6834"]       # two-series charts use categorical slots 1 and 2
FONT = "IBM Plex Sans Arabic"
BAR_STEP = {"step": 30}               # height per bar on horizontal bar charts
PAIR_STEP = {"step": 16}              # height per bar when two bars sit side by side


def _style(chart, height):
    return (
        chart.properties(height=height)
        .configure(font=FONT, background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=MUTED, titleColor=MUTED, gridColor="#E6EAE5", domain=False,
            tickColor="#E6EAE5", labelFontSize=12, titleFontSize=12, titleFontWeight=500,
        )
        .configure_legend(
            labelColor="#14213D", titleColor=MUTED, orient="top", labelFontSize=12,
            titleFontSize=12, symbolType="square",
        )
    )


def show(chart, height=300):
    st.altair_chart(_style(chart, height), width="stretch")


def category_bars(values, color=None):
    """values: {'Food': 1800, ...} -> horizontal bars, largest first, each labelled
    with its amount and its share of the total.
    color: one colour for every bar (used for a category's subcategories)."""
    df = pd.DataFrame(
        [{"key": k, "label": cat(k), "amount": v} for k, v in values.items() if v]
    )
    if df.empty:
        return
    df = df.sort_values("amount", ascending=False)
    df["share"] = df["amount"] / df["amount"].sum() * 100
    df["text"] = [f"{a:,.0f} · {s:.0f}%" for a, s in zip(df["amount"], df["share"])]
    domain = list(df["label"])
    colors = [color or color_for(k) for k in df["key"]]

    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=domain, title=None, axis=alt.Axis(labelLimit=220)),
        x=alt.X("amount:Q", title=t("chart.amount")),
        tooltip=[alt.Tooltip("label:N", title=t("chart.category")),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f"),
                 alt.Tooltip("share:Q", title=t("chart.share"), format=".1f")],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, height=16).encode(
        color=alt.Color("label:N", scale=alt.Scale(domain=domain, range=colors), legend=None)
    )
    labels = base.mark_text(align="left", dx=6, color="#14213D", fontSize=12).encode(
        text="text:N"
    )
    show(bars + labels, BAR_STEP)


def income_vs_spending(income_by_month, spending_by_month):
    months = sorted(set(income_by_month) | set(spending_by_month))
    if not months:
        return
    income_label, spending_label = t("chart.income"), t("chart.spending")
    rows = []
    for m in months:
        rows.append({"month": m, "series": income_label, "amount": income_by_month.get(m, 0)})
        rows.append({"month": m, "series": spending_label, "amount": spending_by_month.get(m, 0)})
    df = pd.DataFrame(rows)

    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4, width=alt.RelativeBandSize(0.8)).encode(
        x=alt.X("month:N", title=None, axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("series:N", sort=[income_label, spending_label]),
        y=alt.Y("amount:Q", title=t("chart.amount")),
        color=alt.Color("series:N", sort=[income_label, spending_label],
                        scale=alt.Scale(domain=[income_label, spending_label], range=SERIES),
                        title=None),
        tooltip=[alt.Tooltip("month:N", title=t("chart.month")),
                 alt.Tooltip("series:N", title=" "),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f")],
    )
    show(chart, 280)


def category_trend_lines(category_monthly):
    rows = [
        {"key": c, "label": cat(c), "month": m, "amount": v}
        for c, series in category_monthly.items()
        for m, v in series.items()
    ]
    if not rows:
        return
    df = pd.DataFrame(rows)
    keys = list(dict.fromkeys(df.sort_values("amount", ascending=False)["key"]))
    domain = [cat(k) for k in keys]
    colors = [color_for(k) for k in keys]
    hover = alt.selection_point(fields=["month"], nearest=True, on="pointerover", empty=False)

    base = alt.Chart(df).encode(
        x=alt.X("month:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("amount:Q", title=t("chart.amount")),
        color=alt.Color("label:N", scale=alt.Scale(domain=domain, range=colors),
                        title=t("chart.category")),
    )
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(size=70, filled=True, stroke="white", strokeWidth=2).encode(
        tooltip=[alt.Tooltip("label:N", title=t("chart.category")),
                 alt.Tooltip("month:N", title=t("chart.month")),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f")],
    ).add_params(hover)
    rule = alt.Chart(df).mark_rule(color="#B9C0C9").encode(x="month:N").transform_filter(hover)
    show(lines + points + rule, 320)


def budget_vs_spent(rows):
    """rows: [{'key': 'Food', 'budget': 2000, 'spent': 1800}, ...]"""
    budget_label, spent_label = t("chart.budget"), t("chart.spent")
    data = []
    for r in rows:
        data.append({"label": cat(r["key"]), "series": budget_label, "amount": r["budget"]})
        data.append({"label": cat(r["key"]), "series": spent_label, "amount": r["spent"]})
    df = pd.DataFrame(data)
    if df.empty:
        return
    order = [cat(r["key"]) for r in rows]

    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=220)),
        yOffset=alt.YOffset("series:N", sort=[budget_label, spent_label]),
        x=alt.X("amount:Q", title=t("chart.amount")),
        color=alt.Color("series:N", sort=[budget_label, spent_label],
                        scale=alt.Scale(domain=[budget_label, spent_label], range=SERIES),
                        title=None),
        tooltip=[alt.Tooltip("label:N", title=t("chart.category")),
                 alt.Tooltip("series:N", title=" "),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f")],
    )
    show(chart, PAIR_STEP)


def forecast_bars(by_category):
    last_label, next_label = t("chart.last_month"), t("chart.forecast")
    data = []
    for c, v in by_category.items():
        data.append({"label": cat(c), "series": last_label, "amount": v["last_month"]})
        data.append({"label": cat(c), "series": next_label, "amount": v["predicted"]})
    df = pd.DataFrame(data)
    if df.empty:
        return
    order = [cat(c) for c in by_category]

    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=220)),
        yOffset=alt.YOffset("series:N", sort=[last_label, next_label]),
        x=alt.X("amount:Q", title=t("chart.amount")),
        color=alt.Color("series:N", sort=[last_label, next_label],
                        scale=alt.Scale(domain=[last_label, next_label], range=SERIES),
                        title=None),
        tooltip=[alt.Tooltip("label:N", title=t("chart.category")),
                 alt.Tooltip("series:N", title=" "),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f")],
    )
    show(chart, PAIR_STEP)


def probability_bars(probabilities):
    """Model confidence per action, one hue (magnitude, not identity)."""
    df = pd.DataFrame(
        [{"label": t(f"action.{a}"), "p": p} for a, p in probabilities.items()]
    ).sort_values("p", ascending=False)
    if df.empty:
        return
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=list(df["label"]), title=None, axis=alt.Axis(labelLimit=220)),
        x=alt.X("p:Q", title=t("chart.confidence"), scale=alt.Scale(domain=[0, 1]),
                axis=alt.Axis(format="%")),
        tooltip=[alt.Tooltip("label:N", title=" "), alt.Tooltip("p:Q", format=".0%",
                                                                  title=t("chart.confidence"))],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, height=14, color=SERIES[0])
    rule = alt.Chart(pd.DataFrame({"x": [0.5]})).mark_rule(
        strokeDash=[4, 3], color=MUTED).encode(x="x:Q")
    labels = base.mark_text(align="left", dx=6, color="#14213D", fontSize=12).encode(
        text=alt.Text("p:Q", format=".0%"))
    show(bars + labels + rule, BAR_STEP)


def income_sources(by_source):
    df = pd.DataFrame(
        [{"label": cat(s), "amount": v} for s, v in by_source.items()]
    ).sort_values("amount", ascending=False)
    if df.empty:
        return
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=list(df["label"]), title=None, axis=alt.Axis(labelLimit=220)),
        x=alt.X("amount:Q", title=t("chart.amount")),
        tooltip=[alt.Tooltip("label:N", title=t("chart.source")),
                 alt.Tooltip("amount:Q", title=t("chart.amount"), format=",.0f")],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, height=16, color=SERIES[0])
    labels = base.mark_text(align="left", dx=6, color="#14213D", fontSize=12).encode(
        text=alt.Text("amount:Q", format=",.0f"))
    show(bars + labels, BAR_STEP)

