# scripts/demo_outputs.py
# Walks the whole pipeline on screen, section by section — analysis, features,
# preferences, model, plan, forecast, goal assessment, what-if and the knowledge
# base. Uses a throw-away database with the sample user, so nothing is changed.
#
#   python -m scripts.demo_outputs

import json
import tempfile
from pathlib import Path

from app import config


def section(n, title):
    print("\n" + "=" * 70)
    print(f"{n}. {title}")
    print("=" * 70)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def main():
    original = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp()) / "demo.db"
    try:
        _walk()
    finally:
        config.DB_PATH = original


def _walk():
    from app.database.schema import create_tables
    from app.engines.financial_engine import analyze_finances
    from app.engines.recommendation_engine import (generate_recommendation_plan,
                                                   generate_recommendations)
    from app.ml.feature_engineering import build_features
    from app.ml.goal_assessment import assess_goal
    from app.ml.prediction import forecast_spending, predict_spending
    from app.ml.recommendation_model import get_feature_importance
    from app.preferences.user_preferences import list_preferences
    from app.rag.knowledge import search
    from app.services.scenario import rank_goals, simulate_scenario
    from scripts.seed_sample import seed

    create_tables()
    uid = seed()
    a = analyze_finances(uid)

    section(1, "OVERALL FINANCIAL STATE")
    for key in ("current_month", "income", "expenses", "savings", "savings_rate"):
        print(f"  {key:16} {a[key]}")

    section(2, "CATEGORY ANALYSIS")
    ca = a["category_analysis"]
    print(f"  window: {ca['window']}   total expenses: {ca['total_expenses']}")
    print(f"  {'rank':5} {'category':15} {'amount':>10} {'% exp':>8} {'% income':>9}")
    for r in ca["ranking"]:
        print(f"  {r['rank']:<5} {r['category']:15} {r['amount']:>10.2f} "
              f"{r['percent_of_expenses']:>8.2f} {r['percent_of_income']:>9.2f}")
    print(f"\n  highest: {ca['highest_category']['category']} ({ca['highest_category']['amount']})")
    print(f"  lowest:  {ca['lowest_category']['category']} ({ca['lowest_category']['amount']})")
    print(f"\n  subcategories of Food: {dumps(ca['by_subcategory']['Food'])}")
    dom = ca["dominant_subcategory"]["Food"]
    print(f"  dominant Food subcategory: {dom['subcategory']} "
          f"({dom['amount']}, {dom['percent_of_category']}% of Food)")

    section(3, "INCOME ANALYSIS")
    ia = a["income_analysis"]
    print(f"  total income recorded: {ia['total_income']}")
    print(f"  by source:  {dumps(ia['by_source'])}")
    print(f"  percentages:{dumps(ia['source_percentages'])}")
    print(f"  primary source: {ia['primary_source']['source']} "
          f"({ia['primary_source']['percent']}%)   sources: {ia['num_sources']}")
    print(f"  monthly income: {dumps(ia['monthly_income'])}")
    print(f"  trend: {ia['trend']['direction']} ({ia['trend']['change_percent']}%)")
    print(f"  stability claim supported: {ia['stability']['is_stable_claim_supported']}")
    print(f"    reason: {ia['stability']['reason']}")

    section(4, "SPENDING PATTERN ANALYSIS")
    sp = a["spending_patterns"]
    print(f"  monthly spending: {dumps(sp['monthly_spending'])}")
    print(f"  average month: {sp['average_monthly_spending']}")
    print(f"  highest: {sp['highest_month']}   lowest: {sp['lowest_month']}")
    print(f"  increasing: {sp['increasing_categories']}")
    print(f"  decreasing: {sp['decreasing_categories']}")
    print(f"  stable:     {sp['stable_categories']}")
    print(f"  overall trend: {sp['recent_trend']['direction']} ({sp['recent_trend']['change_percent']}%)")

    section(5, "SAVINGS / GOAL / GAP ANALYSIS")
    print(f"  {dumps(a['savings_analysis'])}")
    print("  " + json.dumps(a["goal_analysis"], indent=2, ensure_ascii=False).replace("\n", "\n  "))

    section(6, "FEATURE ENGINEERING (X for the Random Forest)")
    for k, v in build_features(a).items():
        print(f"  {k:28} {v}")

    section(7, "USER ESSENTIAL PREFERENCES")
    for p in list_preferences(uid):
        tag = "ESSENTIAL" if p["is_essential"] else "flexible "
        name = f"{p['main_category']}/{p['subcategory']}" if p["subcategory"] else p["main_category"]
        print(f"  {tag}  {name}")

    section(8, "RANDOM FOREST + RECOMMENDATION ENGINE")
    plan = generate_recommendation_plan(uid)
    gap = plan["financial_gap"]
    print(f"  prediction source: {plan['prediction_source']}")
    print(f"  predicted actions: {plan['predicted_actions']}")
    print(f"  probabilities:     {dumps(plan['prediction_probabilities'])}")
    print(f"  filtered out:      {[f['action'] for f in plan['filtered_out']]}")
    print(f"  added by engine:   {[d['action'] for d in plan['derived_actions']]}")
    print(f"  applied actions:   {plan['applied_actions']}")
    print(f"\n  gap to close: {gap['target_amount']}  ({gap['target_reason']})")
    print(f"  freed by the plan: {gap['total_planned_reduction']}  remaining: "
          f"{gap['remaining_gap']}  closed: {gap['gap_closed']}")
    for r in plan["reductions"]:
        print(f"\n  {r['action_name']}: cut {r['reduce_by']} from {r['category']} "
              f"(spend {r['current_spending']}, protected {r['protected_spending']}, "
              f"flexible {r['eligible_spending']})")
        for s in r["subcategory_plan"]:
            print(f"      {s['subcategory']:15} {s['current_spending']:>8.2f}  ->  "
                  f"{s['new_spending']:>8.2f}   (-{s['reduce_by']})")
        if r["protected_subcategories"]:
            print("      never reduced: " + ", ".join(
                f"{s['subcategory']} ({s['amount']})" for s in r["protected_subcategories"]))
    for x in plan["income_actions"]:
        print(f"\n  {x['action_name']}: {x['explanation']}")
    print(f"\n  SUMMARY: {plan['summary']}")

    section(9, "TEXT RECOMMENDATIONS (what Qwen explains)")
    for line in generate_recommendations(uid):
        print(f"  - {line}")

    section(10, "TOP FEATURE IMPORTANCES")
    try:
        overall = get_feature_importance(top_n=8)["overall"]
        for name, value in list(overall.items())[:8]:
            print(f"  {name:28} {value}")
    except Exception as exc:  # noqa: BLE001 — model file may be missing
        print(f"  (model not available: {exc})")

    section(11, "SPENDING FORECAST (mu, sigma)")
    f = forecast_spending(uid)
    print(f"  next month total: {f['mu']} +/- {f['sigma']}   state: {f['state']}")
    print(f"  {f['based_on']} — {f['method']}")
    per_cat = predict_spending(uid)
    print(f"  per category {per_cat['month']}: " + ", ".join(
        f"{c} {v['predicted']}" for c, v in per_cat["by_category"].items()))

    section(12, "CAN I AFFORD IT? — 5,000 laptop in 4 months")
    r = assess_goal(uid, target_amount=5000, months=4)
    for key in ("predicted_monthly_spending", "spending_uncertainty", "expected_monthly_saving",
                "projected_savings", "projected_spread", "probability", "label", "action",
                "forecast_based_on"):
        print(f"  {key:27} {r[key]}")

    section(13, "WHAT-IF SCENARIOS and GOAL RANKING (stored goal)")
    for label, kwargs in [("cut Shopping 120", {"cut_category": "Shopping", "cut_amount": 120}),
                          ("save 500 more", {"extra_saving": 500}),
                          ("wait 3 more months", {"extra_months": 3}),
                          ("income -10%", {"income_change_pct": -10})]:
        s = simulate_scenario(uid, **kwargs)
        print(f"  {label:20} {s['baseline']['probability']:.3f} -> "
              f"{s['scenario']['probability']:.3f}   ({s['label_change']})")
    ranking = rank_goals(uid)
    for g in ranking["ranked_goals"]:
        print(f"  #{g['rank']} {g['goal_name']}: importance {g['importance']} x urgency "
              f"{g['urgency']} = {g['priority_score']}")

    section(14, "KNOWLEDGE BASE (RAG)")
    for q in ("How much should I keep in an emergency fund?", "ما هي الفائدة المركبة؟",
              "What will the dollar rate be next month?"):
        hit = search(q)
        if hit["found"]:
            top = hit["results"][0]
            print(f"  Q: {q}\n     -> {top['title']} (score {top['score']}) — "
                  f"{top['source']['publisher']}")
        else:
            print(f"  Q: {q}\n     -> nothing relevant: the assistant says it cannot answer")


if __name__ == "__main__":
    main()
