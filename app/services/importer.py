# app/services/importer.py
# Import a CSV of transactions (exported from a bank or a spreadsheet).
#
# Accepted columns (names are case-insensitive, common aliases understood):
#   date, description, amount, category, [subcategory], [type]
# A row is INCOME when type is income/credit/deposit or the category is Income;
# its source is read from subcategory/description. Expense categories that are
# not in our tree are mapped with the same deterministic rules as the Kaggle
# converter (e.g. "Groceries" -> Food, "Uber" -> Transport, unknown -> Others).
# Every row is validated; bad rows are reported, never silently dropped.

import io

import pandas as pd

from app.config import CATEGORY_TREE, INCOME_CATEGORY
from app.database import crud
from scripts.kaggle_to_scenarios import map_category, map_income_source, map_subcategory

COLUMN_ALIASES = {
    "date": ["date", "transaction date", "day", "التاريخ"],
    "description": ["description", "name", "note", "notes", "details", "merchant", "الوصف"],
    "amount": ["amount", "value", "sum", "price", "المبلغ"],
    "category": ["category", "type of expense", "الفئة"],
    "subcategory": ["subcategory", "sub category", "sub-category", "الفئة الفرعية"],
    "type": ["type", "kind", "income/expense", "debit/credit", "النوع"],
}
INCOME_TYPES = {"income", "credit", "deposit", "دخل"}
MAX_ROWS = 5000

TEMPLATE_CSV = (
    "date,description,amount,category,subcategory,type\n"
    "2026-10-01,Monthly salary,5000,Income,Salary,income\n"
    "2026-10-03,Lunch at work,45,Food,Lunch,expense\n"
    "2026-10-05,Groceries,320,Groceries,,expense\n"
    "2026-10-07,Electricity bill,150,Bills,Electricity,expense\n"
)


def _find_columns(df):
    lowered = {c.strip().lower(): c for c in df.columns}
    found = {}
    for key, aliases in COLUMN_ALIASES.items():
        found[key] = next((lowered[a] for a in aliases if a in lowered), None)
    return found


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def import_csv_text(user_id, csv_text):
    """Parse and import. Returns counts plus a list of rejected rows with reasons."""
    if crud.get_user(user_id) is None:
        raise ValueError(f"user {user_id} does not exist")
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    except Exception as exc:  # noqa: BLE001 — any parse failure is reported to the user
        raise ValueError(f"could not read the CSV: {exc}") from exc

    columns = _find_columns(df)
    missing = [c for c in ("date", "amount") if columns[c] is None]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")
    if len(df) > MAX_ROWS:
        raise ValueError(f"too many rows ({len(df)}); import at most {MAX_ROWS} at a time")

    added_expenses, added_income, errors, mapped = 0, 0, [], 0
    for i, row in df.iterrows():
        line = i + 2                                  # +1 for the header, +1 for 1-based
        get = lambda key: _clean(row[columns[key]]) if columns[key] else None  # noqa: E731
        try:
            when = pd.to_datetime(get("date"), errors="raise").date().isoformat()
        except (ValueError, TypeError):
            errors.append({"row": line, "error": f"unreadable date {get('date')!r}"})
            continue
        try:
            amount = float(str(get("amount")).replace(",", ""))
        except (ValueError, TypeError):
            errors.append({"row": line, "error": f"unreadable amount {get('amount')!r}"})
            continue

        raw_type = (get("type") or "").lower()
        raw_category = get("category")
        is_income = raw_type in INCOME_TYPES or (raw_category or "").lower() == INCOME_CATEGORY.lower()
        amount = abs(amount)
        if amount == 0:
            errors.append({"row": line, "error": "amount is 0"})
            continue

        if is_income:
            source = map_income_source(get("subcategory") or get("description"))
            crud.add_income(user_id, amount, source, when)
            added_income += 1
            continue

        category = raw_category if raw_category in CATEGORY_TREE else map_category(raw_category)
        if category != raw_category:
            mapped += 1
        subcategory = map_subcategory(category, get("subcategory") or raw_category)
        crud.add_transaction(user_id, get("description") or raw_category or "Imported",
                             amount, category, when, subcategory)
        added_expenses += 1

    return {
        "rows": int(len(df)),
        "added_expenses": added_expenses,
        "added_income": added_income,
        "categories_mapped": mapped,
        "errors": errors,
    }
