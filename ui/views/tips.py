# ui/views/tips.py
# Money tips: practical actions grouped by theme. Each tip has a why, three
# concrete steps and either the user's own numbers (from /tips) or a general
# example when their data can't support one. Below them: a search over the
# verified notes the assistant answers from, and an exact interest calculator.

import streamlit as st

from ui import api
from ui.i18n import cat, current_language, join, money, t
from ui.theme import chip, esc, facts, note, page_header, panel


def _source_line(source):
    return (f'<div class="pfa-sources">{esc(t("tips.source"))}: '
            f'<a href="{esc(source["url"])}" target="_blank" rel="noopener">'
            f'{esc(source["publisher"])} — {esc(source["title"])}</a></div>')


# ---------- the user's numbers for each tip ----------

def _personal(tip):
    """The tip's sentence built from the user's numbers, or None."""
    p, k = tip["personal"], tip["id"]
    if not p:
        return None
    if k == "leak_check":
        if p["kind"] == "leak":
            return t("tip.leak_check.mine_leak", name=cat(p["name"]), count=p["count"],
                     first=p["first"], last=p["last"], avg=money(p["average"]),
                     per_month=p["per_month"])
        return t("tip.leak_check.mine_top", month=p["month"], items=join(
            [f'{i["description"]} ({money(i["amount"])})' for i in p["items"]]))
    if k == "wait_rule":
        return t("tip.wait_rule.mine", threshold=money(p["threshold"]), count=p["count"],
                 total=money(p["total"]), month=p["month"])
    if k == "adaptive_limits":
        return t("tip.adaptive_limits.mine", items=join(
            [t("tip.adaptive_limits.item", name=cat(i["name"]), now=money(i["now"]),
               limit=money(i["limit"]), month=i["limit_month"]) for i in p["items"]]))
    if k == "needs_wants":
        return t("tip.needs_wants.mine", month=p["month"], flexible=money(p["flexible"]),
                 pct=p["flexible_pct"], essential=money(p["essential"]))
    if k == "goal_boost":
        text = t("tip.goal_boost.mine", extra=money(p["extra"]), avg=money(p["flexible_average"]),
                 goal=p["goal"], saving=money(p["saving"]))
        if p["months_after"] < p["months_before"]:
            return text + " " + t("tip.goal_boost.sooner", before=p["date_before"],
                                  after=p["date_after"])
        return text + " " + t("tip.goal_boost.added", added=money(p["added"]),
                              date=p["date_before"])
    if k == "pay_first":
        if p["irregular"] or not p["payday"]:
            text = t("tip.pay_first.mine_irregular", rate=p["rate"])
        else:
            text = t("tip.pay_first.mine", payday=p["payday"], day=p["transfer_day"],
                     amount=money(p["amount"]))
        if p["goal"]:
            text += " " + t("tip.pay_first.goal", goal=p["goal"])
        return text
    if k == "opportunity_cost":
        return t("tip.opportunity_cost.mine", name=cat(p["name"]), monthly=money(p["monthly"]),
                 yearly=money(p["yearly"]), trim=p["trim_pct"], trimmed=money(p["trimmed_yearly"]),
                 goal=p["goal"], pct=p["goal_pct"])
    if k == "subscription_audit":
        return t("tip.subscription_audit.mine", items=join(
            [f'{i["name"]} ({money(i["amount"])} {t("freq." + i["frequency"])})' for i in p["items"]]),
            monthly=money(p["monthly"]), yearly=money(p["yearly"]))
    if k == "bill_calendar":
        text = t("tip.bill_calendar.mine", items=join(
            [f'{i["name"]} ({money(i["monthly"])}' +
             (f', {t("tip.bill_calendar.day", day=i["day"])})' if i["day"] else ")")
             for i in p["items"]]), total=money(p["total"]))
        if p["upcoming"]:
            text += " " + t("tip.bill_calendar.upcoming", items=join(p["upcoming"]))
        return text
    if k == "weekly_review":
        return t("tip.weekly_review.mine", start=p["start"], end=p["end"], count=p["count"],
                 total=money(p["total"]), top=cat(p["top"]), top_amount=money(p["top_amount"]))
    if k == "emergency_ladder":
        text = t("tip.emergency_ladder.mine", available=money(p["available"]), months=p["months"],
                 essential=money(p["essential"]))
        if tip["doing_well"]:
            return text
        text += " " + t("tip.emergency_ladder.step", next=p["next_months"], step=money(p["step"]))
        if p["months_to_step"]:
            text += " " + t("tip.emergency_ladder.pace", share=p["share_pct"],
                            monthly=money(p["monthly"]), months=p["months_to_step"])
        return text
    return None


def _tip_card(tip):
    k = tip["id"]
    mine = _personal(tip)
    chips = chip(t(f"tips.group.{tip['group']}"))
    if tip["doing_well"]:
        chips += " " + chip(t("tips.doing_well"), "down")
    elif mine:
        chips += " " + chip(t("tips.personal"), "goal")
    steps = "".join(f"<li>{esc(t(f'tip.{k}.s{i}'))}</li>" for i in (1, 2, 3))
    label, line = (t("tips.your_numbers"), mine) if mine else (t("tips.example"), t(f"tip.{k}.example"))
    st.markdown(
        f'<div class="pfa-step" style="grid-template-columns:1fr"><div>'
        f'<div class="title">{esc(t(f"tip.{k}.title"))} {chips}</div>'
        f'<div>{esc(t(f"tip.{k}.why"))}</div>'
        f'<div class="body"><b>{esc(t("tips.try_this"))}</b><ol style="margin:.2rem 0 .3rem">{steps}</ol></div>'
        f'<div class="body"><b>{esc(label)}:</b> {esc(line)}</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def _tips():
    data = api.get("/tips")
    if not data:
        return
    options = ["all"] + data["groups"]
    group = st.segmented_control(t("tips.show"), options, default="all", key="tips_group",
                                 format_func=lambda g: t(f"tips.group.{g}"))
    shown = [tp for tp in data["tips"] if group in (None, "all") or tp["group"] == group]
    with panel():
        for tip in shown:
            _tip_card(tip)
    st.caption(t("tips.honesty"))


# ---------- reference tools ----------

def _search():
    st.subheader(t("tips.search_title"))
    st.caption(t("tips.search_hint"))
    query = st.text_input(t("tips.search_label"), placeholder=t("tips.search_placeholder"),
                          key="tips_query")
    if not query.strip():
        return
    result = api.get("/knowledge/search", q=query, lang=current_language())
    if not result:
        return
    if not result["found"]:
        note(esc(t("tips.nothing")))
        return
    for r in result["results"]:
        st.markdown(f"**{esc(r['title'])}**")
        st.markdown(f"{esc(r['text'])}{_source_line(r['source'])}", unsafe_allow_html=True)


def _interest():
    st.subheader(t("tips.interest_title"))
    c1, c2, c3 = st.columns(3)
    principal = c1.number_input(t("tips.principal"), min_value=0.0, value=1000.0, step=100.0,
                                key="int_p")
    rate = c2.number_input(t("tips.rate"), min_value=0.0, value=5.0, step=0.5, key="int_r")
    years = c3.number_input(t("tips.years"), min_value=0.0, value=2.0, step=1.0, key="int_y")
    compound = st.toggle(t("tips.compound"), value=True, key="int_c")
    times = st.selectbox(t("tips.times"), [1, 4, 12, 365], index=2, key="int_n",
                         format_func=lambda n: t(f"tips.times_{n}"), disabled=not compound)
    params = {"principal": principal, "annual_rate_pct": rate, "years": years,
              "compound": compound, "times_per_year": times}
    r = api.get("/interest", **params)
    if r:
        facts([(t("tips.interest"), money(r["interest"]), None),
               (t("tips.total"), money(r["total"]), r["formula"])])
        st.caption(t("tips.islamic_note"))


def render():
    page_header("tips.title", "tips.lede")
    _tips()
    st.subheader(t("tips.tools_title"))
    left, right = st.columns([3, 2], gap="medium")
    with left:
        with panel():
            _search()
    with right:
        with panel():
            _interest()
