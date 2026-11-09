# scripts/generate_recommendation_dataset.py
# OPTION 2 — build the recommendation training set from SYNTHETIC FINANCIAL SCENARIOS.
#
# One row = ONE USER'S FINANCIAL STATE (never one transaction).
#
# Pipeline for every row:
#   1. draw a scenario   (income, sources, category shares, trends, goal)
#   2. render it as real income records + transactions across several months
#   3. run the SAME pure analysis functions the live app uses
#   4. run the SAME app/ml/feature_engineering.build_features   -> X
#   5. run the SAME app/ml/recommendation_rules.recommend_labels -> Y
#   6. validate coverage, then save to data/sample/recommendation_scenarios.csv
#
# Because steps 3-5 are the production modules, a training row and a live user
# row are produced by identical code. Labels are deterministic, never random.
import argparse
import csv
import random
from collections import Counter
from datetime import date

from app.config import CATEGORY_TREE, DATA_DIR, INCOME_SOURCES
from app.finance.analysis import (analyze_categories, analyze_income,
                                  analyze_spending_patterns)
from app.finance.calculations import (calculate_total_expenses, calculate_savings,
                                      calculate_savings_rate)
from app.engines.goals_engine import calculate_goal_gap
from app.ml.feature_engineering import FEATURE_COLUMNS, LABEL_COLUMNS, build_features
from app.ml.recommendation_rules import recommend_labels

OUT_PATH = DATA_DIR / "sample" / "recommendation_scenarios.csv"
REFERENCE_DATE = "2026-01-15"          # fixed so the dataset is reproducible

EXPENSE_CATEGORIES = ["Food", "Shopping", "Entertainment", "Transport",
                      "Bills", "Utilities", "Education", "Healthcare", "Others"]

# ---------------------------------------------------------------------------
# Scenario archetypes. Each one fixes the FINANCIAL SHAPE of the situation;
# the labels are still produced by the rules, not written down here.
# `shares` are fractions of total expenses, `rate` is the saving rate (%),
# `gap` is the monthly saving gap as a fraction of income (negative = on track).
# ---------------------------------------------------------------------------
ARCHETYPES = [
    {"name": "food_heavy",
     "shares": {"Food": (0.34, 0.60), "Shopping": (0.04, 0.13), "Entertainment": (0.01, 0.05),
                "Transport": (0.05, 0.12)},
     "rate": (12, 45), "gap": (0.01, 0.08), "sources": (1, 2),
     "trends": {"Food": "up"}},

    {"name": "shopping_heavy",
     "shares": {"Food": (0.14, 0.28), "Shopping": (0.16, 0.36), "Entertainment": (0.01, 0.05),
                "Transport": (0.05, 0.12)},
     "rate": (12, 45), "gap": (0.01, 0.06), "sources": (1, 2),
     "trends": {"Shopping": "up"}},

    {"name": "food_and_shopping",
     "shares": {"Food": (0.30, 0.58), "Shopping": (0.12, 0.30), "Entertainment": (0.01, 0.04),
                "Transport": (0.04, 0.10)},
     "rate": (12, 45), "gap": (0.02, 0.22), "sources": (1, 2),
     "trends": {"Food": "up", "Shopping": "up"}},

    {"name": "entertainment_heavy",
     "shares": {"Food": (0.14, 0.26), "Shopping": (0.04, 0.12), "Entertainment": (0.09, 0.25),
                "Transport": (0.05, 0.12)},
     "rate": (12, 45), "gap": (0.01, 0.06), "sources": (1, 2),
     "trends": {"Entertainment": "up"}},

    {"name": "three_way_overspend",
     "shares": {"Food": (0.28, 0.48), "Shopping": (0.14, 0.28), "Entertainment": (0.08, 0.18),
                "Transport": (0.04, 0.09)},
     "rate": (8, 40), "gap": (0.03, 0.14), "sources": (1, 2),
     "trends": {"Food": "up", "Shopping": "up", "Entertainment": "up"}},

    {"name": "transport_heavy",
     "shares": {"Food": (0.14, 0.27), "Shopping": (0.04, 0.12), "Entertainment": (0.01, 0.05),
                "Transport": (0.16, 0.34)},
     "rate": (12, 45), "gap": (0.01, 0.06), "sources": (1, 2),
     "trends": {"Transport": "up"}},

    {"name": "no_flexible_gap",       # nothing over-weight -> income side
     "shares": {"Food": (0.15, 0.22), "Shopping": (0.03, 0.07), "Entertainment": (0.01, 0.03),
                "Transport": (0.05, 0.10)},
     "rate": (12, 45), "gap": (0.02, 0.14), "sources": (1, 2),
     "trends": {}},

    {"name": "shopping_plus_income",  # one flag, but cutting alone cannot close the gap
     "shares": {"Food": (0.12, 0.22), "Shopping": (0.16, 0.28), "Entertainment": (0.01, 0.03),
                "Transport": (0.04, 0.08)},
     "rate": (10, 45), "gap": (0.07, 0.20), "sources": (1, 2),
     "trends": {"Shopping": "up"}},

    {"name": "severe_deficit",        # gap far beyond anything that can be cut or earned
     "shares": {"Food": (0.28, 0.36), "Shopping": (0.14, 0.20), "Entertainment": (0.06, 0.11),
                "Transport": (0.06, 0.12)},
     "rate": (-5, 8), "gap": (0.25, 0.45), "sources": (1, 2),
     "trends": {"Food": "up", "Shopping": "up"}},

    {"name": "low_saving_rate",       # on track for the goal but saving too little
     "shares": {"Food": (0.15, 0.22), "Shopping": (0.04, 0.09), "Entertainment": (0.01, 0.04),
                "Transport": (0.05, 0.11)},
     "rate": (11, 19), "gap": (-0.12, -0.01), "sources": (1, 3),
     "trends": {}},

    {"name": "healthy",               # on track, saving well, nothing rising
     "shares": {"Food": (0.14, 0.22), "Shopping": (0.03, 0.08), "Entertainment": (0.01, 0.04),
                "Transport": (0.04, 0.10)},
     "rate": (22, 42), "gap": (-0.25, -0.03), "sources": (1, 3),
     "trends": {"Food": "down"}},

    {"name": "diversified_extend",    # 3+ income sources, nothing to cut -> extend
     "shares": {"Food": (0.14, 0.21), "Shopping": (0.03, 0.08), "Entertainment": (0.01, 0.03),
                "Transport": (0.04, 0.09)},
     "rate": (16, 45), "gap": (0.02, 0.12), "sources": (3, 4),
     "trends": {}},
]

TREND_FACTOR = {"up": (1.06, 1.18), "down": (0.84, 0.94), "flat": (0.98, 1.02)}


def _add_months(start, months):
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _month_string(ref, months_back):
    d = _add_months(date(ref.year, ref.month, 1), -months_back)
    return f"{d.year:04d}-{d.month:02d}"


def _pick_subcategory(rng, category):
    subs = CATEGORY_TREE.get(category, [])
    return rng.choice(subs) if subs else None


def render_scenario(rng, archetype):
    """Turn one archetype into income records + transactions + a goal."""
    ref = date.fromisoformat(REFERENCE_DATE)
    n_months = rng.randint(3, 6)
    income = round(rng.uniform(3000, 25000), 2)
    rate = rng.uniform(*archetype["rate"])
    expenses_target = income * (1 - rate / 100.0)

    # --- category shares: the archetype fixes the flexible ones, the rest fill up ---
    shares = {}
    for cat, (lo, hi) in archetype["shares"].items():
        shares[cat] = rng.uniform(lo, hi)
    fixed_total = sum(shares.values())
    rest = [c for c in EXPENSE_CATEGORIES if c not in shares]
    weights = [rng.uniform(0.4, 1.6) for _ in rest]
    remaining_share = max(1.0 - fixed_total, 0.05)
    wsum = sum(weights)
    for cat, w in zip(rest, weights):
        shares[cat] = remaining_share * w / wsum
    total_share = sum(shares.values())
    shares = {c: s / total_share for c, s in shares.items()}

    current_amounts = {c: round(expenses_target * s, 2) for c, s in shares.items()}

    # --- monthly history so the trend analysis has something real to read ---
    trends = {c: archetype["trends"].get(c, "flat") for c in EXPENSE_CATEGORIES}
    factors = {c: rng.uniform(*TREND_FACTOR[t]) for c, t in trends.items()}

    transactions = []
    for back in range(n_months):
        month = _month_string(ref, back + 1)
        for cat, amount in current_amounts.items():
            value = amount / (factors[cat] ** back)
            value = round(value * rng.uniform(0.97, 1.03) if back else amount, 2)
            if value <= 0:
                continue
            n_lines = rng.randint(1, 3) if CATEGORY_TREE.get(cat) else 1
            parts = [value / n_lines] * n_lines
            for part in parts:
                transactions.append({
                    "description": f"{cat} spending",
                    "amount": round(part, 2),
                    "category": cat,
                    "subcategory": _pick_subcategory(rng, cat),
                    "date": f"{month}-15",
                })

    # --- income records: split the monthly income across the chosen sources ---
    n_sources = rng.randint(*archetype["sources"])
    sources = rng.sample(INCOME_SOURCES, k=min(n_sources, len(INCOME_SOURCES)))
    if "Salary" not in sources and rng.random() < 0.7:
        sources[0] = "Salary"
    weights = sorted((rng.uniform(0.5, 3.0) for _ in sources), reverse=True)
    wsum = sum(weights)
    income_records = []
    for back in range(n_months):
        month = _month_string(ref, back + 1)
        for src, w in zip(sources, weights):
            income_records.append({"amount": round(income * w / wsum, 2),
                                   "source": src, "date": f"{month}-01"})
    return transactions, income_records, n_months, ref


def build_analysis(transactions, income_records, ref, archetype, rng):
    """Run the production analysis functions over the rendered scenario."""
    months = sorted({t["date"][:7] for t in transactions})
    current_month = months[-1]
    current = [t for t in transactions if t["date"][:7] == current_month]

    income = round(sum(r["amount"] for r in income_records
                       if r["date"][:7] == current_month), 2)
    expenses = calculate_total_expenses(current)
    savings = calculate_savings(income, expenses)
    savings_rate = calculate_savings_rate(income, savings)

    # --- a goal that produces the archetype's intended saving gap ---
    goal = None
    goal_analysis = None
    if rng.random() < 0.92:
        months_remaining = rng.randint(2, 12)
        gap_ratio = rng.uniform(*archetype["gap"])
        required = max(savings + gap_ratio * income, 50.0)
        remaining = round(required * months_remaining, 2)
        current_saved = round(remaining * rng.uniform(0.0, 1.5), 2)
        goal = {"name": "Savings goal",
                "target_amount": round(remaining + current_saved, 2),
                "current_amount": current_saved,
                "target_date": _add_months(date(ref.year, ref.month, 1),
                                           months_remaining).isoformat()}
        goal_analysis = calculate_goal_gap(goal, savings, reference_date=ref.isoformat())

    analysis = {
        "income": income,
        "expenses": expenses,
        "savings": savings,
        "savings_rate": savings_rate,
        "category_analysis": analyze_categories(current, total_income=income,
                                                history_transactions=transactions),
        "income_analysis": analyze_income(income_records),
        "spending_patterns": analyze_spending_patterns(transactions),
        "goal_analysis": goal_analysis,
    }
    return analysis


def make_row(rng, archetype):
    transactions, income_records, n_months, ref = render_scenario(rng, archetype)
    analysis = build_analysis(transactions, income_records, ref, archetype, rng)
    features = build_features(analysis)
    labels, reasons = recommend_labels(features)
    row = dict(features)
    row.update(labels)
    row["archetype"] = archetype["name"]
    row["months_of_history"] = n_months
    row["rule_reason"] = " | ".join(reasons)
    return row


# ---------------------------------------------------------------------------
# combinations the assignment explicitly asks to see in the data
# ---------------------------------------------------------------------------
REQUIRED_COMBOS = [
    ("Reduce_Food",),
    ("Reduce_Shopping",),
    ("Reduce_Food", "Reduce_Shopping"),
    ("Reduce_Entertainment",),
    ("Reduce_Food", "Reduce_Shopping", "Reduce_Entertainment"),
    ("Increase_Income",),
    ("Reduce_Shopping", "Increase_Income"),
    ("Increase_Saving",),
    ("Maintain_Current_Plan",),
    ("Extend_Goal_Timeline",),
]


def combo_key(row):
    return tuple(sorted(name for name in LABEL_COLUMNS if row.get(name)))


def validate(rows, min_per_label=20, min_per_combo=5):
    """Check the dataset is usable before we train anything on it."""
    problems = []
    counts = Counter()
    combos = Counter()
    for row in rows:
        combos[combo_key(row)] += 1
        for name in LABEL_COLUMNS:
            counts[name] += row[name]

    for name in LABEL_COLUMNS:
        if counts[name] < min_per_label:
            problems.append(f"label {name} has only {counts[name]} positive rows")
        if counts[name] == len(rows):
            problems.append(f"label {name} is always 1 (useless for training)")

    for combo in REQUIRED_COMBOS:
        key = tuple(sorted(combo))
        if combos[key] < min_per_combo:
            problems.append(f"required combination {key} appears {combos[key]} time(s)")

    for row in rows:
        if row["Maintain_Current_Plan"] and sum(row[n] for n in LABEL_COLUMNS) > 1:
            problems.append("Maintain_Current_Plan combined with another action")
            break
        if sum(row[n] for n in LABEL_COLUMNS) == 0:
            problems.append("a row has no label at all")
            break

    for row in rows:
        leaked = [c for c in LABEL_COLUMNS if c in FEATURE_COLUMNS]
        if leaked:
            problems.append(f"leakage: {leaked}")
        break
    return problems, counts, combos


def generate(n_rows=3000, seed=42, out_path=OUT_PATH):
    rng = random.Random(seed)
    rows = []
    # 1) balanced pass over every archetype
    per_archetype = max(n_rows // len(ARCHETYPES), 1)
    for archetype in ARCHETYPES:
        for _ in range(per_archetype):
            rows.append(make_row(rng, archetype))
    # 2) top up any required combination that is still rare
    for _ in range(4000):
        combos = Counter(combo_key(r) for r in rows)
        missing = [c for c in REQUIRED_COMBOS if combos[tuple(sorted(c))] < 5]
        if not missing:
            break
        rows.append(make_row(rng, rng.choice(ARCHETYPES)))
    rng.shuffle(rows)

    problems, counts, combos = validate(rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = FEATURE_COLUMNS + LABEL_COLUMNS + ["archetype", "months_of_history", "rule_reason"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in header})

    print(f"Wrote {len(rows)} financial scenarios -> {out_path}")
    print(f"Columns: {len(FEATURE_COLUMNS)} features + {len(LABEL_COLUMNS)} labels")
    print("\nPositive rows per label:")
    for name in LABEL_COLUMNS:
        print(f"  {name:<24} {counts[name]:>5}  ({counts[name] / len(rows) * 100:5.1f}%)")
    print("\nMost common label combinations:")
    for combo, n in combos.most_common(12):
        print(f"  {n:>5}  {' + '.join(combo) if combo else '(none)'}")
    print("\nRequired combinations:")
    for combo in REQUIRED_COMBOS:
        key = tuple(sorted(combo))
        print(f"  {combos[key]:>5}  {' + '.join(combo)}")
    print("\nValidation: " + ("PASSED — dataset is usable" if not problems
                              else "FAILED\n  - " + "\n  - ".join(problems)))
    return out_path, problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the recommendation scenario dataset")
    parser.add_argument("--rows", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    generate(n_rows=args.rows, seed=args.seed)
