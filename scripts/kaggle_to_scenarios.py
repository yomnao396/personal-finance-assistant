# scripts/kaggle_to_scenarios.py
# OPTION 1 — build the recommendation training set from a REAL Kaggle
# personal-finance dataset of transactions.
#
# HOW TO GET A DATASET
#   1. Go to kaggle.com/datasets and search for one of:
#        "personal finance dataset", "daily household transactions",
#        "personal expenses", "budget tracker dataset", "bank transactions"
#      A usable dataset needs, at minimum: a DATE, an AMOUNT and a CATEGORY
#      column, and ideally an income/expense flag and a user/account column.
#   2. Download the CSV into  data/kaggle/  (create the folder).
#   3. Inspect the columns:
#        python -m scripts.kaggle_to_scenarios --csv data/kaggle/<file>.csv --inspect
#   4. Map the dataset's own category names onto our category tree by editing
#      CATEGORY_MAP below (anything unmapped falls into "Others").
#   5. Convert:
#        python -m scripts.kaggle_to_scenarios --csv data/kaggle/<file>.csv \
#               --date-col Date --amount-col Amount --category-col Category \
#               --type-col Income/Expense --income-label Income
#
# WHY A TRANSFORMATION IS NEEDED
#   Kaggle publishes TRANSACTIONS. The Random Forest must be trained on
#   FINANCIAL SCENARIOS: one row per user-month state. This script groups the
#   transactions into monthly states, runs the SAME analysis and feature code the
#   live app uses, attaches goal scenarios, and applies the SAME explicit
#   financial rules to produce the multi-label targets.
#
# HONEST LIMITATION
#   Public datasets contain no savings GOALS, so the goal (target amount and
#   deadline) is attached synthetically as a grid of realistic goals per scenario.
#   Everything else — income, spending, categories, trends — is real data.
import argparse
import csv
from collections import defaultdict
from datetime import date
 
import pandas as pd
 
from app.config import CATEGORY_TREE, DATA_DIR, INCOME_SOURCES
from app.finance.analysis import (analyze_categories, analyze_income,
                                  analyze_spending_patterns)
from app.finance.calculations import (calculate_savings, calculate_savings_rate,
                                      calculate_total_expenses)
from app.engines.goals_engine import calculate_goal_gap
from app.ml.feature_engineering import FEATURE_COLUMNS, LABEL_COLUMNS, build_features
from app.ml.recommendation_rules import recommend_labels
from scripts.generate_recommendation_dataset import REQUIRED_COMBOS, combo_key, validate
 
OUT_PATH = DATA_DIR / "sample" / "recommendation_scenarios_kaggle.csv"
 
# ---------------------------------------------------------------------------
# Map the dataset's category names onto OUR category tree.
# Keys are lower-case; edit this for your dataset.
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    # Food
    "food": "Food", "groceries": "Food", "grocery": "Food", "restaurant": "Food",
    "dining": "Food", "dining out": "Food", "cafe": "Food", "food & drinks": "Food",
    "fast food": "Food", "supermarket": "Food", "lunch": "Food", "breakfast": "Food",
    # Transport
    "transport": "Transport", "transportation": "Transport", "travel": "Transport",
    "fuel": "Transport", "petrol": "Transport", "gas": "Transport", "taxi": "Transport",
    "uber": "Transport", "public transport": "Transport", "car": "Transport",
    # Shopping
    "shopping": "Shopping", "clothing": "Shopping", "clothes": "Shopping",
    "electronics": "Shopping", "furniture": "Shopping", "apparel": "Shopping",
    "beauty": "Shopping", "personal care": "Shopping",
    # Entertainment
    "entertainment": "Entertainment", "movies": "Entertainment", "cinema": "Entertainment",
    "subscription": "Entertainment", "games": "Entertainment", "sports": "Entertainment",
    # Bills
    "bills": "Bills", "rent": "Bills", "electricity": "Bills", "water": "Bills",
    "internet": "Bills", "phone": "Bills", "mobile": "Bills", "housing": "Bills",
    "insurance": "Bills", "loan": "Bills", "emi": "Bills",
    # Utilities
    "utilities": "Utilities", "utility": "Utilities", "household": "Utilities",
    "maintenance": "Utilities",
    # Education
    "education": "Education", "tuition": "Education", "books": "Education",
    "course": "Education", "school": "Education",
    # Healthcare
    "health": "Healthcare", "healthcare": "Healthcare", "medical": "Healthcare",
    "pharmacy": "Healthcare", "doctor": "Healthcare", "fitness": "Healthcare",
    # Others
    "other": "Others", "others": "Others", "misc": "Others", "miscellaneous": "Others",
    "gift": "Others", "donation": "Others", "cash": "Others",
}
 
# Map dataset income descriptions onto our income sources.
INCOME_SOURCE_MAP = {
    "salary": "Salary", "wage": "Salary", "payroll": "Salary", "income": "Salary",
    "freelance": "Freelancer", "freelancer": "Freelancer", "business": "Freelancer",
    "gift": "Gifts", "gifts": "Gifts", "bonus": "Gifts",
    "family": "From Dad", "parents": "From Dad", "dad": "From Dad",
}
 
# Goal scenarios attached to every user-month (multiplier of monthly income, months).
GOAL_GRID = [(1.0, 3), (2.0, 4), (3.0, 6), (5.0, 6), (8.0, 12)]
 
 
def map_category(raw):
    """Dataset category name -> our category. Unknown names become 'Others'."""
    if raw is None:
        return "Others"
    key = str(raw).strip().lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    for name in CATEGORY_TREE:                       # exact tree name, e.g. "Food"
        if key == name.lower():
            return name
    for fragment, mapped in CATEGORY_MAP.items():    # partial match, e.g. "food-fast"
        if fragment in key:
            return mapped
    return "Others"
 
 
def map_subcategory(category, raw):
    """Keep the dataset's own label only when it is a real subcategory of ours."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    for sub in CATEGORY_TREE.get(category, []):
        if key == sub.lower():
            return sub
    return None
 
 
def map_income_source(raw):
    if raw is None:
        return "Other"
    key = str(raw).strip().lower()
    for fragment, source in INCOME_SOURCE_MAP.items():
        if fragment in key:
            return source
    return "Other" if "Other" in INCOME_SOURCES else INCOME_SOURCES[-1]
 
 
def inspect(csv_path):
    """Print the columns and a preview so you can choose the right column names."""
    df = pd.read_csv(csv_path)
    print(f"{csv_path}: {len(df)} rows, {len(df.columns)} columns\n")
    print("Columns:")
    for column in df.columns:
        sample = df[column].dropna().astype(str).head(3).tolist()
        print(f"  {column:<28} e.g. {sample}")
    print("\nFirst 5 rows:")
    print(df.head().to_string())
 
 
def load_rows(csv_path, date_col, amount_col, category_col, subcategory_col,
              type_col, income_label, user_col, source_col):
    """Read the CSV and split it into per-user expense transactions and income rows."""
    df = pd.read_csv(csv_path)
    for required in (date_col, amount_col):
        if required not in df.columns:
            raise SystemExit(f"column {required!r} not in {list(df.columns)}")
 
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    df[amount_col] = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
 
    expenses = defaultdict(list)
    incomes = defaultdict(list)
    for _, row in df.iterrows():
        user = str(row[user_col]) if user_col and user_col in df.columns else "user_1"
        stamp = row[date_col].strftime("%Y-%m-%d")
        amount = float(row[amount_col])
        raw_category = row[category_col] if category_col in df.columns else None
 
        # income vs expense
        is_income = False
        if type_col and type_col in df.columns:
            is_income = str(row[type_col]).strip().lower() == str(income_label).lower()
        elif amount < 0:
            is_income = False                     # negative = spending in many datasets
        if not is_income and str(raw_category).strip().lower() in (
                "income", "salary", "wage", "wages", "payroll"):
            is_income = True
 
        amount = abs(amount)
        if amount <= 0:
            continue
 
        if is_income:
            raw_source = row[source_col] if source_col and source_col in df.columns \
                else raw_category
            incomes[user].append({"amount": amount, "source": map_income_source(raw_source),
                                  "date": stamp})
        else:
            category = map_category(raw_category)
            raw_sub = row[subcategory_col] if subcategory_col and subcategory_col in df.columns \
                else raw_category
            expenses[user].append({"amount": amount, "category": category,
                                   "subcategory": map_subcategory(category, raw_sub),
                                   "date": stamp})
    return expenses, incomes
 
 
def scenarios_for_user(transactions, income_records, min_months=3):
    """One scenario per anchor month (using that month as 'current' and the
    earlier months as history), multiplied by the goal grid."""
    months = sorted({t["date"][:7] for t in transactions})
    rows = []
    for index in range(min_months - 1, len(months)):
        anchor = months[index]
        window = months[: index + 1]
        history = [t for t in transactions if t["date"][:7] in window]
        current = [t for t in transactions if t["date"][:7] == anchor]
        income_window = [r for r in income_records if r["date"][:7] in window]
 
        income = round(sum(r["amount"] for r in income_window
                           if r["date"][:7] == anchor), 2)
        if income <= 0:
            continue
        expenses = calculate_total_expenses(current)
        if expenses <= 0:
            continue
        savings = calculate_savings(income, expenses)
        savings_rate = calculate_savings_rate(income, savings)
 
        base = {
            "income": income, "expenses": expenses, "savings": savings,
            "savings_rate": savings_rate,
            "category_analysis": analyze_categories(current, total_income=income,
                                                    history_transactions=history),
            "income_analysis": analyze_income(income_window),
            "spending_patterns": analyze_spending_patterns(history),
        }
        anchor_date = date(int(anchor[:4]), int(anchor[5:7]), 1)
 
        for multiplier, months_ahead in GOAL_GRID:
            target = round(income * multiplier, 2)
            year = anchor_date.year + (anchor_date.month - 1 + months_ahead) // 12
            month_no = (anchor_date.month - 1 + months_ahead) % 12 + 1
            goal = {"name": "Goal", "target_amount": target, "current_amount": 0.0,
                    "target_date": f"{year:04d}-{month_no:02d}-01"}
            analysis = dict(base)
            analysis["goal_analysis"] = calculate_goal_gap(
                goal, savings, reference_date=anchor_date.isoformat())
            features = build_features(analysis)
            labels, reasons = recommend_labels(features)
            row = dict(features)
            row.update(labels)
            row["archetype"] = f"kaggle_{anchor}_goal{multiplier:g}x{months_ahead}m"
            row["months_of_history"] = index + 1
            row["rule_reason"] = " | ".join(reasons)
            rows.append(row)
    return rows
 
 
def convert(csv_path, out_path=OUT_PATH, **columns):
    expenses, incomes = load_rows(csv_path, **columns)
    rows = []
    for user, transactions in expenses.items():
        rows.extend(scenarios_for_user(transactions, incomes.get(user, [])))
    if not rows:
        raise SystemExit("no scenarios were produced — check the column names and that "
                         "the dataset has at least 3 months of data per user")
 
    problems, counts, combos = validate(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = FEATURE_COLUMNS + LABEL_COLUMNS + ["archetype", "months_of_history",
                                               "rule_reason"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in header})
 
    print(f"Wrote {len(rows)} scenarios from {len(expenses)} user(s) -> {out_path}")
    print("\nPositive rows per label:")
    for name in LABEL_COLUMNS:
        print(f"  {name:<24} {counts[name]:>5}  ({counts[name] / len(rows) * 100:5.1f}%)")
    print("\nRequired combinations found in this dataset:")
    for combo in REQUIRED_COMBOS:
        key = tuple(sorted(combo))
        print(f"  {combos[key]:>5}  {' + '.join(combo)}")
    if problems:
        print("\nValidation WARNINGS (real data is rarely balanced):")
        for problem in problems:
            print(f"  - {problem}")
        print("\nIf combinations are missing, either pick a richer Kaggle dataset or "
              "top the file up with synthetic rows from "
              "scripts/generate_recommendation_dataset.py.")
    else:
        print("\nValidation: PASSED — dataset is usable")
    return out_path, problems
 
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Turn a Kaggle transaction CSV into recommendation scenarios")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--inspect", action="store_true", help="just show the columns")
    parser.add_argument("--date-col", default="Date")
    parser.add_argument("--amount-col", default="Amount")
    parser.add_argument("--category-col", default="Category")
    parser.add_argument("--subcategory-col", default=None)
    parser.add_argument("--type-col", default=None,
                        help="column holding Income/Expense")
    parser.add_argument("--income-label", default="Income")
    parser.add_argument("--user-col", default=None)
    parser.add_argument("--source-col", default=None,
                        help="column naming the income source")
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()
 
    if args.inspect:
        inspect(args.csv)
    else:
        from pathlib import Path
        convert(args.csv, Path(args.out), date_col=args.date_col,
                amount_col=args.amount_col, category_col=args.category_col,
                subcategory_col=args.subcategory_col, type_col=args.type_col,
                income_label=args.income_label, user_col=args.user_col,
                source_col=args.source_col)

