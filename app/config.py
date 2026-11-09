# app/config.py
# Central place for paths and constants so every file agrees on where things are.
# Categories are organised in a two-level tree of main categories and
# subcategories. They are reference data, not machine-learning predictions.
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent   # the personal_finance_ai/ folder
DATA_DIR = BASE_DIR / "data"
MODELS_DIR = BASE_DIR / "models"
DB_PATH = DATA_DIR / "finance.db"
# --------------------------------------------------------------------------
# The official category tree. A category with an empty list has no subcategories.
# --------------------------------------------------------------------------
CATEGORY_TREE = {
    # ----- income -----
       "Income":        ["Salary", "Gifts", "From Dad", "Freelancer", "Other"],
    # ----- expenses ----
    "Food":          ["Breakfast", "Lunch", "Dinner", "Restaurant", "Fast Food", "Other"],
    "Utilities":     [],
    "Transport":     [],
    "Entertainment": ["Movie"],
    "Shopping":      ["Clothes", "Electronics", "Furniture", "Other"],
    "Bills":         ["Rent", "Electricity", "Water", "Internet", "Other"],
    "Education":     [],
    "Healthcare":    [],
    "Others":        [],
}
INCOME_CATEGORY = "Income"
INCOME_SOURCES = list(CATEGORY_TREE[INCOME_CATEGORY])
EXPENSE_CATEGORIES = [c for c in CATEGORY_TREE if c != INCOME_CATEGORY]
ALL_CATEGORIES = list(CATEGORY_TREE.keys())
# Categories the recommendation model is allowed to ask the user to reduce.
REDUCIBLE_CATEGORIES = ["Food", "Shopping", "Entertainment", "Transport"]
# Suggested starting point for a new user's essential/flexible preferences.
# The user can change every one of these at any time (Phase E).
DEFAULT_ESSENTIAL_PREFERENCES = [
    ("Food", "Breakfast", 1),
    ("Food", "Lunch", 1),
    ("Food", "Dinner", 1),
    ("Food", "Restaurant", 0),
    ("Food", "Fast Food", 0),
    ("Food", "Other", 0),
    ("Bills", "Rent", 1),
    ("Bills", "Electricity", 1),
    ("Bills", "Water", 1),
    ("Bills", "Internet", 0),
    ("Bills", "Other", 0),
    ("Utilities", "", 1),
    ("Healthcare", "", 1),
    ("Education", "", 1),
    ("Transport", "", 0),
    ("Entertainment", "Movie", 0),
    ("Shopping", "Clothes", 0),
    ("Shopping", "Electronics", 0),
    ("Shopping", "Furniture", 0),
    ("Shopping", "Other", 0),
    ("Others", "", 0),
]
# --------------------------------------------------------------------------
# Validation helpers. Every write path (CRUD, API, UI) uses these.
# --------------------------------------------------------------------------
def is_valid_category(category):
    """True if the main category exists in the tree."""
    return category in CATEGORY_TREE
def get_subcategories(category):
    """The list of subcategories for a category ([] if it has none)."""
    return list(CATEGORY_TREE.get(category, []))
def is_valid_subcategory(category, subcategory):
    """A subcategory is valid when it is empty (not specified) or belongs to the category."""
    if not is_valid_category(category):
        return False
    if subcategory in (None, ""):
        return True                      # subcategory is optional
    return subcategory in CATEGORY_TREE[category]
def validate_category(category, subcategory=None):
    """Raise ValueError on an invalid pair; otherwise return the cleaned pair."""
    if not is_valid_category(category):
        raise ValueError(f"Unknown category: {category!r}. Valid: {ALL_CATEGORIES}")
    sub = subcategory if subcategory not in (None, "") else None
    if sub is not None and sub not in CATEGORY_TREE[category]:
        raise ValueError(
            f"{sub!r} is not a subcategory of {category!r}. "
            f"Valid: {CATEGORY_TREE[category] or 'this category has no subcategories'}")
    return category, sub
def validate_income_source(source):
    """Income sources are the subcategories of the Income category."""
    if source in (None, ""):
        return "Other"
    if source not in INCOME_SOURCES:
        raise ValueError(f"Unknown income source: {source!r}. Valid: {INCOME_SOURCES}")
    return source
MODELS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)