# ui/views/plan.py
# The recommendation plan as numbered steps. Every sentence is rebuilt here from
# the plan's numbers (not the engine's English text), so it reads naturally in
# both languages; the engine's own reasoning sits in the "how it was decided" panel.
# Below it: evidence-based advice cards (insights_engine) the user can accept,
# mark done or dismiss, and a read-only "what if" preview.

import streamlit as st

from ui import api, charts
from ui.i18n import cat, join, money, t
from ui.theme import chip, esc, facts, note, page_header, panel


def _b(value):
    return f"<b>{esc(value)}</b>"


def _summary(plan):
    gap = plan["financial_gap"]
    if plan["maintain_current_plan"]:
        note(esc(t("plan.sum_on_track", saving=money(plan["analysis_snapshot"]["savings"]),
                   rate=f'{plan["analysis_snapshot"]["savings_rate"]:.1f}')), "good")
    elif gap["target_amount"] <= 0:
        note(esc(t("plan.sum_no_gap")), "good")
    elif gap["gap_closed"]:
        note(t("plan.sum_closed", found=_b(money(gap["total_planned_reduction"])),
               target=_b(money(gap["target_amount"]))), "good")
    else:
        note(t("plan.sum_open", found=_b(money(gap["total_planned_reduction"])),
               target=_b(money(gap["target_amount"])),
               left=_b(money(gap["remaining_gap"]))), "bad")


def _steps(plan):
    steps = []
    for r in plan["reductions"]:
        subs = join([f"{cat(s['subcategory'])} −{money(s['reduce_by'])}"
                     for s in r["subcategory_plan"] if s["subcategory"] != "(unspecified)"])
        body = t("plan.reduce_body", cut=_b(money(r["reduce_by"])),
                 now=_b(money(r["current_spending"])), new=_b(money(r["new_category_spending"])))
        if subs:
            body += " " + esc(t("plan.reduce_subs", subs=subs))
        if r["protected_subcategories"]:
            body += " " + esc(t("plan.reduce_protected", subs=join(
                [cat(s["subcategory"]) for s in r["protected_subcategories"]])))
        steps.append((t("plan.reduce_title", category=cat(r["category"])), body))

    for a in plan["saving_actions"]:
        steps.append((t("plan.save_title"),
                      t("plan.save_body", amount=_b(money(a["suggested_monthly_saving"])))))

    for a in plan["income_actions"]:
        if a["required_extra_monthly_income"] > 0:
            body = t("plan.income_body", amount=_b(money(a["required_extra_monthly_income"])))
        else:
            body = esc(t("plan.income_buffer"))
        steps.append((t("plan.income_title"), body))

    for a in plan["timeline_actions"]:
        if a["months_needed_at_that_rate"] is None:
            body = esc(t("plan.timeline_impossible"))
        else:
            body = t("plan.timeline_body", saving=_b(money(a["achievable_monthly_saving"])),
                     months=_b(a["months_needed_at_that_rate"]),
                     extra=_b(a["extra_months_required"]))
        steps.append((t("plan.timeline_title", goal=a["goal_name"]), body))

    if plan["maintain_current_plan"]:
        steps.append((t("plan.keep_title"), esc(t("plan.keep_body"))))

    if not steps:
        return
    html = "".join(
        f'<div class="pfa-step"><div class="n">{i}</div><div>'
        f'<div class="title">{esc(title)}</div><div class="body">{body}</div></div></div>'
        for i, (title, body) in enumerate(steps, start=1)
    )
    with panel():
        st.subheader(t("plan.steps_title"))
        st.markdown(html, unsafe_allow_html=True)


def _how_decided(plan):
    with st.expander(t("plan.how_title")):
        source = plan.get("prediction_source")
        st.markdown(
            esc(t("plan.how_source")) + " " +
            chip(t("plan.source_rf") if source == "random_forest" else t("plan.source_rules")),
            unsafe_allow_html=True,
        )
        if plan.get("prediction_probabilities"):
            st.caption(t("plan.how_probs"))
            charts.probability_bars(plan["prediction_probabilities"])
        reasons = (
            [(r["action"], r["reason"]) for r in plan["filtered_out"]]
            + [(r["action"], r["reason"]) for r in plan["derived_actions"]]
            + [(r["action"], r["reason"]) for r in plan["substituted_actions"]]
        )
        if reasons:
            st.caption(t("plan.how_engine_notes"))
            for action, reason in reasons:
                st.markdown(f"- **{t(f'action.{action}')}**: {reason}")


# ---------- advice cards ----------

def _goal_line(link):
    if not link:
        return None
    kind = link.get("type", "toward")
    return t(f"ins.link_{kind}", goal=link["goal_name"], amount=money(link["monthly_saving"]),
             before=link.get("months_before"), after=link.get("months_after"),
             pct=f'{link.get("share_of_gap_pct", 0):.0f}', gap=money(link.get("gap", 0)))


def _goal_texts(e):
    body = t("ins.goal_plan.body", left=money(e["remaining_amount"]),
             target=money(e["target_amount"]), pct=f'{e["percent_complete"]:.0f}')
    status = e["deadline_status"]
    if status == "upcoming":
        body += " " + t("ins.goal_plan.due", date=e["target_date"],
                        need=money(e["required_monthly_saving"]))
    elif status == "deadline passed":
        body += " " + t("ins.goal_plan.passed", date=e["target_date"])
    else:
        body += " " + t("ins.goal_plan.no_date")
    if e["months_at_current_rate"] is not None:
        body += " " + t("ins.goal_plan.eta", saving=money(e["monthly_saving"]),
                        months=e["months_at_current_rate"], eta=e["estimated_completion"])
    else:
        body += " " + t("ins.goal_plan.never")
    if e["reachable_on_time"]:
        action = t("ins.goal_plan.keep", need=money(e["required_monthly_saving"]))
    elif e["alternatives"]:
        parts = [t("ins.goal_plan.save_more", extra=money(a["extra_per_month"]))
                 if a["option"] == "save_more" else t("ins.goal_plan.move_date", month=a["new_month"])
                 for a in e["alternatives"]]
        action = t("ins.goal_plan.either", options=t("ins.or").join(parts))
    else:
        action = t("ins.goal_plan.set_date")
    marks = [t("ins.milestone", pct=m["percent"], amount=money(m["amount"]), month=m["month"])
             for m in e["milestones"] if m["month"]]
    evidence = t("ins.milestones", items=join(marks)) if marks else None
    return body, evidence, action


def _texts(card):
    """(title, insight, evidence, action, impact) rebuilt from the card's numbers."""
    kind, e = card["kind"], card["evidence"]
    impact = card.get("impact") or {}
    saving = impact.get("monthly_saving")
    if kind == "money_leak":
        name = cat(e["subcategory"] or e["category"])
        return (t("ins.money_leak.title", name=name),
                t("ins.money_leak.body", count=e["count"], name=name, first=e["first_date"],
                  last=e["last_date"], total=money(e["total"]), per_month=e["per_month"],
                  avg=money(e["average_price"])),
                t("ins.money_leak.rule", limit=money(e["small_purchase_limit"])),
                t("ins.money_leak.action", fewer=e["suggested_fewer_per_month"],
                  week=e["suggested_fewer_per_week"], name=name),
                t("ins.money_leak.impact", saving=money(saving), fewer=e["suggested_fewer_per_month"],
                  avg=money(e["average_price"])))
    if kind in ("spending_increase", "essential_increase"):
        name = cat(e["subcategory"] or e["category"])
        series = " · ".join(f"{m}: {money(v)}" for m, v in e["monthly"].items())
        body = t("ins.rise.body", name=name, earlier=money(e["earlier_average"]),
                 recent=money(e["recent_average"]), change=f'{e["change_percent"]:+.0f}')
        if kind == "essential_increase":
            return t("ins.essential_increase.title", name=name), body, series, \
                t("ins.essential_increase.action"), None
        return (t("ins.spending_increase.title", name=name), body, series,
                t("ins.spending_increase.action", name=name, target=money(e["target"]),
                  now=money(e["this_month"])),
                t("ins.spending_increase.impact", saving=money(saving)))
    if kind == "weekly_allowance":
        body = t("ins.weekly.body", income=money(e["income"]), essential=money(e["essential"]),
                 committed=money(e["committed"]), saving=money(e["goal_saving"]),
                 allowance=money(e["flexible_allowance"]), month=e["month"],
                 spent=money(e["flexible_spent"]))
        if e["left"] < 0 and e["is_current_month"]:
            action = t("ins.weekly.over", over=money(-e["left"]))
        else:
            action = t("ins.weekly.action", weekly=money(e["weekly"]))
        note_ = (t("ins.weekly.days", days=e["days_left"]) if e["is_current_month"]
                 else t("ins.weekly.typical", month=e["month"]))
        return t("ins.weekly.title"), body, t("ins.weekly.rule") + " " + note_, action, None
    if kind == "goal_plan":
        body, evidence, action = _goal_texts(e)
        return t("ins.goal_plan.title", goal=e["name"]), body, evidence, action, None
    if kind == "surplus":
        return (t("ins.surplus.title"), t("ins.surplus.body", kept=money(e["kept"]), month=e["month"]),
                None, t("ins.surplus.action", goal=e["goal_name"], amount=money(e["amount"]),
                        before=f'{e["percent_before"]:.0f}', after=f'{e["percent_after"]:.0f}'), None)
    if kind == "safety":
        if e["missing"]:
            return t("ins.safety.title"), t("ins.safety.missing"), None, t("ins.safety.missing_action"), None
        body = t("ins.safety.body", available=money(e["available"]), months=e["months_covered"],
                 essential=money(e["essential_monthly"]), label=t(f'ins.safety.{e["label"]}'))
        evidence = t("ins.safety.counted", liquid=money(e["liquid_savings"]),
                     goals=money(e["emergency_goal_savings"]))
        action = (t("ins.safety.next", months=e["next_level_months"],
                    more=money(e["needed_for_next_level"]))
                  if e.get("next_level_months") else t("ins.safety.done"))
        return t("ins.safety.title"), body, evidence, action, None
    if kind == "irregular_income":
        return (t("ins.income.title"),
                t("ins.income.body", low=money(e["lowest"]), high=money(e["highest"]),
                  months=e["months"], spread=f'{e["spread_percent"]:.0f}'),
                None, t("ins.income.action", low=money(e["lowest"])),
                t("ins.income.impact", buffer=money(e["latest"] - e["lowest"])))
    if kind == "deficit":
        action = t("ins.deficit.action", cut=money(saving))
        if impact.get("remaining"):
            action += " " + t("ins.deficit.left", left=money(impact["remaining"]))
        return (t("ins.deficit.title"),
                t("ins.deficit.body", month=e["month"], spent=money(e["spent"]),
                  income=money(e["income"]), short=money(e["shortfall"])),
                t("ins.deficit.rule"), action, None)
    if kind == "no_income":
        return (t("ins.no_income.title"), t("ins.no_income.body", spent=money(e["spent"]),
                                            month=e["month"]), None, t("ins.no_income.action"), None)
    if kind == "insufficient_history":
        body = t("ins.history.body", months=e["months"], needed=e["needed"])
        if e["estimates_only"]:
            body += " " + t("ins.history.estimates")
        return t("ins.history.title"), body, None, t("ins.history.action"), None
    return card["title"], card["summary"], None, card.get("action"), None


def _set_status(key, status):
    if api.post("/recommendations/status", {"user_id": api.user_id(), "key": key,
                                            "status": status}):
        st.toast(t(f"ins.toast_{status}"))


def _progress_line(card):
    p = card.get("progress")
    if not p:
        return None
    metric = (card.get("tracking") or {}).get("metric")
    return t("ins.progress_goal" if metric == "goal_saved" else "ins.progress_spending",
             since=p["since"], before=money(p["baseline"]), now=money(p["now"]),
             change=money(p["change"], signed=True))


def _card(card):
    title, body, evidence, action, impact = _texts(card)
    chips = chip(t(f'ins.priority_{card["priority"]}'),
                 {"high": "up", "medium": "goal"}.get(card["priority"], "flat"))
    if card.get("difficulty"):
        chips += " " + chip(t(f'ins.difficulty_{card["difficulty"]}'))
    if card.get("optional"):
        chips += " " + chip(t("ins.optional"), "down")
    if card["status"] in ("accepted", "completed"):
        chips += " " + chip(t(f'ins.status_{card["status"]}'), "down")
    lines = [f'<div class="title">{esc(title)} {chips}</div>', f'<div>{esc(body)}</div>']
    for label, text in ((t("ins.evidence"), evidence), (t("ins.action"), action),
                        (t("ins.impact"), impact),
                        (t("ins.goal_link"), _goal_line(card.get("goal_connection"))),
                        (t("ins.tracking"), _progress_line(card))):
        if text:
            lines.append(f'<div class="body"><b>{esc(label)}:</b> {esc(text)}</div>')
    if (card.get("impact") or {}).get("assumption") and impact:
        lines.append(f'<div class="body">{esc(t("ins.estimate_note"))}</div>')
    st.markdown(f'<div class="pfa-step" style="grid-template-columns:1fr"><div>{"".join(lines)}'
                f'</div></div>', unsafe_allow_html=True)

    key = card["key"]
    buttons = {"new": [("accepted", "ins.accept"), ("dismissed", "ins.dismiss")],
               "accepted": [("completed", "ins.complete"), ("new", "ins.undo")],
               "completed": [("new", "ins.undo")]}.get(card["status"], [])
    cols = st.columns(len(buttons) + 2)
    for col, (status, label) in zip(cols, buttons):
        col.button(t(label), key=f"ins_{status}_{key}", on_click=_set_status, args=(key, status),
                   width="stretch")


def _insights():
    data = api.get("/recommendations/insights")
    if not data or data.get("empty"):
        return
    with panel():
        st.subheader(t("ins.title"))
        st.caption(t("ins.lede"))
        for card in data["insights"]:
            _card(card)
        if not data["insights"]:
            st.caption(t("ins.none"))
    if data["dismissed"]:
        with st.expander(t("ins.dismissed", n=len(data["dismissed"]))):
            for card in data["dismissed"]:
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                c1.markdown(esc(_texts(card)[0]))
                c2.button(t("ins.restore"), key=f"ins_restore_{card['key']}", on_click=_set_status,
                          args=(card["key"], "new"), width="stretch")


def _what_if(plan):
    with panel():
        st.subheader(t("whatif.title"))
        st.caption(t("whatif.lede"))
        saving = max(plan["analysis_snapshot"]["savings"], 0.0)
        c1, c2 = st.columns(2)
        spend_less = c1.number_input(t("whatif.spend_less"), min_value=0.0, step=50.0, value=0.0,
                                     key="whatif_cut")
        to_goal = c2.number_input(t("whatif.to_goal"), min_value=0.0, step=50.0,
                                  value=float(round(saving)), key="whatif_goal")
        r = api.get("/recommendations/what-if", spend_less=spend_less, to_goal=to_goal)
        if not r:
            return
        facts([
            (t("whatif.new_spending"), money(r["new_expenses"]),
             t("whatif.was", amount=money(r["expenses"]))),
            (t("whatif.left"), money(r["balance_after"]),
             t("whatif.was", amount=money(r["balance_before"]))),
            (t("whatif.after_goal"), money(r["left_after_goal"]), None),
        ])
        if r["spend_less_capped"]:
            note(esc(t("whatif.capped", amount=money(r["flexible_spending"]))))
        if r["left_after_goal"] < 0:
            note(esc(t("whatif.too_much")), "bad")
        g = r.get("goal")
        if g:
            if g["months_at_current_rate"] is None:
                note(esc(t("whatif.goal_never", goal=g["name"])), "bad")
            else:
                text = t("whatif.goal_eta", goal=g["name"], months=g["months_at_current_rate"],
                         eta=g["estimated_completion"])
                if g["deadline_status"] == "upcoming":
                    text += " " + t("whatif.on_time" if g["reachable_on_time"] else "whatif.late",
                                    date=g["target_date"], need=money(g["required_monthly_saving"]))
                note(esc(text), "good" if g["reachable_on_time"] else "")
        st.caption(t("whatif.assumptions"))


def render():
    page_header("plan.title", "plan.lede")
    plan = api.get("/recommendations/plan")
    if plan is None:
        return

    _summary(plan)
    gap = plan["financial_gap"]
    goal = gap.get("goal")
    items = [
        (t("plan.gap"), money(gap["target_amount"]),
         t("plan.gap_for", goal=goal["goal_name"]) if goal else t("plan.gap_rate")),
        (t("plan.found"), money(gap["total_planned_reduction"]),
         t("plan.capacity", amount=money(gap["total_reduction_capacity"]))),
        (t("plan.left"), money(gap["remaining_gap"]), None),
    ]
    facts(items)

    _steps(plan)

    for b in plan["blocked_by_preferences"]:
        note(esc(t("plan.blocked", category=cat(b["category"]),
                   amount=money(b["protected_amount"]))))

    _insights()
    _what_if(plan)
    _how_decided(plan)
