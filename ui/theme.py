# ui/theme.py
# Visual identity: IBM Plex Sans Arabic (one family for Arabic and Latin),
# cool sage paper, ink-navy structure, mint for money kept, coral for overspend,
# saffron reserved for goals. Also the small HTML building blocks the pages share.

import html
from itertools import count

import streamlit as st

from ui.i18n import cat, is_rtl, money, t

INK = "#14213D"
MUTED = "#5B6475"
PAPER = "#F2F4F1"
LINE = "#DCE1DA"
MINT = "#157F62"
CORAL = "#C8433A"
SAFFRON = "#D99A1E"

# Fixed colour per category (validated categorical palette; colour follows the
# category, never its rank). "Others" is the neutral fold-in slot.
CATEGORY_COLORS = {
    "Food": "#2a78d6",
    "Shopping": "#eb6834",
    "Transport": "#1baf7a",
    "Bills": "#eda100",
    "Entertainment": "#e87ba4",
    "Utilities": "#008300",
    "Education": "#6250d6",
    "Healthcare": "#e34948",
    "Others": "#9aa0a6",
}
SAVED_COLOR = INK


def color_for(category):
    return CATEGORY_COLORS.get(category, CATEGORY_COLORS["Others"])


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap');

:root {
  --ink: #14213D; --muted: #5B6475; --paper: #F2F4F1; --surface: #FFFFFF;
  --line: #DCE1DA; --mint: #157F62; --coral: #C8433A; --saffron: #D99A1E;
  --mint-soft: #E3F1EC; --coral-soft: #F8E6E4; --saffron-soft: #FBF1DC; --ink-soft: #E8ECF3;
}

/* every element except Streamlit's icon glyphs, which need their own icon font */
html, body, .stApp, .stApp *:not([data-testid="stIconMaterial"]) {
  font-family: 'IBM Plex Sans Arabic', 'Segoe UI', Tahoma, sans-serif !important;
}
.stApp { background: var(--paper); color: var(--ink); }
.stApp [data-testid="stHeader"] { background: transparent; }
.block-container { max-width: 1180px; padding-top: 2.2rem; padding-bottom: 4rem; }

h1 { font-size: 1.9rem !important; font-weight: 700 !important; color: var(--ink) !important;
     letter-spacing: -0.01em; margin-bottom: .2rem !important; }
h2, h3 { color: var(--ink) !important; font-weight: 600 !important; }
h3 { font-size: 1.12rem !important; }

/* sidebar: the ink-navy spine of the app */
[data-testid="stSidebar"] { background: var(--ink); }
[data-testid="stSidebar"] :is(p, h3, label, span, small) { color: #E8ECF3; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color: #AEB8CC; }
[data-testid="stSidebar"] [data-testid="stIconMaterial"] { color: #AEB8CC; }
[data-testid="stSidebar"] label[data-testid="stRadioOption"] {
  padding: .45rem .7rem; border-radius: 8px; margin-bottom: 2px; width: 100%; cursor: pointer;
}
[data-testid="stSidebar"] label[data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) { display: none; }
[data-testid="stSidebar"] label[data-testid="stRadioOption"]:hover { background: rgba(255,255,255,.07); }
[data-testid="stSidebar"] label[data-testid="stRadioOption"]:has(input:checked) {
  background: rgba(255,255,255,.14); box-shadow: inset 3px 0 0 var(--saffron);
}
[data-testid="stSidebar"] label[data-testid="stRadioOption"]:has(input:checked) p { font-weight: 600; }
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,.15); }
[data-testid="stSidebar"] button {
  background: transparent !important; border: 1px solid rgba(255,255,255,.3) !important;
}
[data-testid="stSidebar"] button :is(p, span, div) { color: #E8ECF3 !important; }
[data-testid="stSidebar"] button:hover { border-color: #fff !important; }
[data-testid="stSidebar"] button[aria-checked="true"] { background: #E8ECF3 !important; }
[data-testid="stSidebar"] button[aria-checked="true"] :is(p, span, div) { color: var(--ink) !important; }

/* panels (see panel() below) are flat white cards */
.stApp [class*="st-key-pfa_panel_"] {
  background: var(--surface); border-color: var(--line) !important; border-radius: 14px;
  padding: 1.1rem 1.2rem;
}
/* fields inside a white panel get a tinted fill so their edges stay visible */
.stApp [class*="st-key-pfa_panel_"] :is([data-baseweb="input"], [data-baseweb="base-input"],
  [data-baseweb="select"] > div, [data-baseweb="textarea"], input) {
  background-color: #F4F6F3 !important;
}
.stApp [class*="st-key-pfa_panel_"] :is([data-baseweb="input"], [data-baseweb="select"] > div) {
  border: 1px solid var(--line) !important;
}
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {
  background: var(--ink); border-color: var(--ink); color: #fff;
}
.stButton button[kind="primary"]:hover { background: #22345c; border-color: #22345c; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--saffron) !important; outline-offset: 2px; }

[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
.stTabs [data-baseweb="tab-list"] { gap: .25rem; }
.stTabs [data-baseweb="tab"] { font-weight: 500; }

/* ---- page intro ---- */
.pfa-lede { color: var(--muted); font-size: 1rem; margin: 0 0 1.4rem; max-width: 62ch; }

/* ---- dashboard hero: the month as a sentence ---- */
.pfa-hero { background: var(--surface); border: 1px solid var(--line); border-radius: 18px;
            padding: 1.8rem 2rem 1.5rem; margin-bottom: 1.2rem; }
.pfa-hero .when { color: var(--muted); font-size: .95rem; margin-bottom: .5rem; }
.pfa-hero .sentence { font-size: clamp(1.45rem, 2.6vw, 2.15rem); line-height: 1.45;
                      font-weight: 500; color: var(--ink); max-width: 34ch; }
.pfa-hero .sentence b { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pfa-hero .sentence .in { color: var(--ink); }
.pfa-hero .sentence .out { color: var(--coral); }
.pfa-hero .sentence .kept { color: var(--mint); }
.pfa-split { display: flex; height: 22px; border-radius: 6px; overflow: hidden; gap: 2px;
             margin: 1.4rem 0 .7rem; background: var(--paper); }
.pfa-split span { display: block; height: 100%; }
.pfa-legend { display: flex; flex-wrap: wrap; gap: .35rem 1.1rem; font-size: .86rem; color: var(--muted); }
.pfa-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
                margin-inline-end: .4rem; vertical-align: middle; }
.pfa-legend b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }

/* ---- small facts row ---- */
.pfa-facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .8rem;
             margin-bottom: 1.2rem; }
.pfa-fact { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: .85rem 1rem; }
.pfa-fact .k { color: var(--muted); font-size: .84rem; }
.pfa-fact .v { font-size: 1.25rem; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--ink); }
.pfa-fact .s { font-size: .8rem; color: var(--muted); }

/* ---- chips ---- */
.pfa-chip { display: inline-block; padding: .12rem .6rem; border-radius: 999px; font-size: .8rem;
            font-weight: 500; white-space: nowrap; }
.pfa-chip.up { background: var(--coral-soft); color: #9A2F28; }
.pfa-chip.down { background: var(--mint-soft); color: #0F5E49; }
.pfa-chip.flat { background: var(--ink-soft); color: var(--ink); }
.pfa-chip.goal { background: var(--saffron-soft); color: #7A5410; }

/* ---- goal progress ---- */
.pfa-goal { margin: .2rem 0 .4rem; }
.pfa-goal .top { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .2rem 1rem;
                 align-items: baseline; }
.pfa-goal .name { font-weight: 600; font-size: 1.05rem; }
.pfa-goal .nums { color: var(--muted); font-size: .9rem; font-variant-numeric: tabular-nums; }
.pfa-track { height: 10px; background: var(--saffron-soft); border-radius: 999px; overflow: hidden; margin: .55rem 0 .35rem; }
.pfa-track span { display: block; height: 100%; background: var(--saffron); border-radius: 999px; }

/* ---- probability meter (goal assessment) ---- */
.pfa-meter { margin: .6rem 0 1rem; }
.pfa-meter-top { display: flex; align-items: baseline; gap: .8rem; }
.pfa-meter-value { font-size: 2.4rem; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
.pfa-meter-track { position: relative; height: 12px; background: var(--paper); border-radius: 999px;
                   margin: .7rem 0 .3rem; overflow: hidden; }
.pfa-meter-track span { display: block; height: 100%; border-radius: 999px; }
.pfa-meter-track i { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--surface); }
.pfa-meter-scale { position: relative; height: 1rem; font-size: .75rem; color: var(--muted); }
.pfa-meter-scale span { position: absolute; }
.pfa-meter-scale span:nth-child(1) { inset-inline-start: 0; }
.pfa-meter-scale span:nth-child(2) { inset-inline-start: calc(40% - .8em); }
.pfa-meter-scale span:nth-child(3) { inset-inline-start: calc(70% - .8em); }
.pfa-meter-scale span:nth-child(4) { inset-inline-end: 0; }

/* ---- sources under an assistant answer ---- */
.pfa-sources { font-size: .85rem; color: var(--muted); margin-top: .4rem; }
.pfa-sources a { color: var(--ink); }

/* ---- plan steps (a real sequence, so they are numbered) ---- */
.pfa-step { display: grid; grid-template-columns: 2.1rem 1fr; gap: .9rem; padding: .9rem 0;
            border-top: 1px solid var(--line); }
.pfa-step:first-child { border-top: 0; }
.pfa-step .n { width: 2.1rem; height: 2.1rem; border-radius: 50%; background: var(--ink); color: #fff;
               display: grid; place-items: center; font-weight: 600; }
.pfa-step .title { font-weight: 600; margin-bottom: .15rem; }
.pfa-step .body { color: var(--muted); font-size: .93rem; }
.pfa-step .body b { color: var(--ink); font-variant-numeric: tabular-nums; }

.pfa-note { border-inline-start: 3px solid var(--saffron); background: var(--saffron-soft);
            padding: .7rem 1rem; border-radius: 6px; font-size: .93rem; margin: .4rem 0 1rem; }
.pfa-note.bad { border-color: var(--coral); background: var(--coral-soft); }
.pfa-note.good { border-color: var(--mint); background: var(--mint-soft); }

.pfa-empty { text-align: center; padding: 2.4rem 1rem; color: var(--muted); }
.pfa-empty b { color: var(--ink); display: block; font-size: 1.1rem; margin-bottom: .3rem; }

@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
@media (max-width: 640px) {
  .pfa-hero { padding: 1.2rem 1.1rem; border-radius: 14px; }
  .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.2rem; }
  h1 { font-size: 1.6rem !important; }
  .pfa-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .6rem; }
  .pfa-fact { padding: .7rem .8rem; }
  .pfa-fact .v { font-size: 1.1rem; }
  .pfa-step { grid-template-columns: 1.8rem 1fr; gap: .7rem; }
  .pfa-step .n { width: 1.8rem; height: 1.8rem; font-size: .9rem; }
  .stApp [class*="st-key-pfa_panel_"] { padding: .9rem .8rem; }
}
</style>
"""

RTL_CSS = """
<style>
.stApp .block-container, [data-testid="stSidebar"] [data-testid="stSidebarContent"] { direction: rtl; text-align: right; }
.stApp .block-container [data-testid="stVegaLiteChart"], .stApp .block-container .vega-embed { direction: ltr; }
.stApp [data-testid="stChatMessage"] { direction: rtl; text-align: right; }
[data-testid="stSidebar"] label[data-testid="stRadioOption"]:has(input:checked) {
  box-shadow: inset -3px 0 0 var(--saffron);
}
.pfa-hero .sentence b { unicode-bidi: isolate; }
.stApp input, .stApp textarea { text-align: right; }
.stApp .block-container :is(.pfa-step, .pfa-goal, .pfa-note, .pfa-legend, .pfa-fact) { text-align: right; }
.stApp [data-testid="stNumberInput"] input { direction: ltr; text-align: right; }
</style>
"""


_panel_ids = count()


def panel():
    """A bordered white panel. The key gives it a CSS class we can style."""
    return st.container(border=True, key=f"pfa_panel_{next(_panel_ids)}")


def apply():
    global _panel_ids
    _panel_ids = count()   # same keys on every rerun
    st.markdown(CSS, unsafe_allow_html=True)
    if is_rtl():
        st.markdown(RTL_CSS, unsafe_allow_html=True)


def esc(text):
    return html.escape(str(text))


def page_header(title_key, lede_key=None, **fmt):
    st.title(t(title_key))
    if lede_key:
        st.markdown(f'<p class="pfa-lede">{esc(t(lede_key, **fmt))}</p>', unsafe_allow_html=True)


def facts(items):
    """items: [(label, value, sub-text or None), ...]"""
    cells = "".join(
        f'<div class="pfa-fact"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div>'
        + (f'<div class="s">{esc(s)}</div>' if s else "")
        + "</div>"
        for k, v, s in items
    )
    st.markdown(f'<div class="pfa-facts">{cells}</div>', unsafe_allow_html=True)


def chip(text, kind="flat"):
    return f'<span class="pfa-chip {kind}">{esc(text)}</span>'


def trend_chip(direction, change_percent=None):
    kind = {"increasing": "up", "decreasing": "down"}.get(direction, "flat")
    label = t(f"trend.{direction}")
    if change_percent not in (None, 0) and direction in ("increasing", "decreasing"):
        label = f"{label} {change_percent:+.0f}%"
    return chip(label, kind)


def note(text, kind=""):
    st.markdown(f'<div class="pfa-note {kind}">{text}</div>', unsafe_allow_html=True)


def empty(title_key, body_key):
    st.markdown(
        f'<div class="pfa-empty"><b>{esc(t(title_key))}</b>{esc(t(body_key))}</div>',
        unsafe_allow_html=True,
    )


def goal_progress(goal):
    pct = max(0.0, min(100.0, goal["current_amount"] / goal["target_amount"] * 100))
    when = (
        t("goals.by_date", date=goal["target_date"]) if goal.get("target_date") else t("goals.no_date")
    )
    st.markdown(
        f"""<div class="pfa-goal">
  <div class="top"><span class="name">{esc(goal['name'])}</span>
  <span class="nums">{esc(money(goal['current_amount']))} / {esc(money(goal['target_amount']))}</span></div>
  <div class="pfa-track"><span style="width:{pct:.1f}%"></span></div>
  <div class="top nums"><span>{esc(t('goals.percent_done', pct=f'{pct:.0f}'))}</span><span>{esc(when)}</span></div>
</div>""",
        unsafe_allow_html=True,
    )


def money_split(categories, saved):
    """One bar: income split into each spending category plus what was kept."""
    parts = [(c, v, color_for(c)) for c, v in sorted(categories.items(), key=lambda kv: -kv[1]) if v > 0]
    if saved > 0:
        parts.append(("__saved__", saved, SAVED_COLOR))
    total = sum(v for _, v, _ in parts) or 1

    bar = "".join(
        f'<span style="width:{v / total * 100:.2f}%;background:{color}" '
        f'title="{esc(t("dash.saved") if c == "__saved__" else cat(c))}: {esc(money(v))}"></span>'
        for c, v, color in parts
    )
    legend = "".join(
        f'<span><i style="background:{color}"></i>'
        f'{esc(t("dash.saved") if c == "__saved__" else cat(c))} <b>{esc(money(v))}</b></span>'
        for c, v, color in parts
    )
    return f'<div class="pfa-split" role="img" aria-label="{esc(t("dash.split_aria"))}">{bar}</div>' \
           f'<div class="pfa-legend">{legend}</div>'

