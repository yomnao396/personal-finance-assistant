# app/database/crud.py
# All Create/Read/Update/Delete operations. Uses parameterized SQL (the ? marks)
# which prevents SQL injection.
# UPDATED: transactions/recurring expenses carry a validated subcategory,
#          income carries a validated source, and there is a full CRUD set for
#          the `categories` reference table and `user_category_preferences`.
from app.database.connection import get_connection
from app.config import validate_category, validate_income_source
# ---------- users ---------
def add_user(username, email=None):
    conn = get_connection()
    cur = conn.execute("INSERT OR IGNORE INTO users (username, email) VALUES (?, ?)", (username, email))
    conn.commit(); uid = cur.lastrowid; conn.close()
    return uid
def list_users():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]
def set_current_savings(user_id, amount):
    conn = get_connection()
    cur = conn.execute("UPDATE users SET current_savings = ? WHERE id = ?", (amount, user_id))
    conn.commit(); changed = cur.rowcount; conn.close()
    return changed > 0
def get_user(user_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
# ---------- categories (reference data, NOT ML) ---------
def get_categories():
    """[{'main_category': 'Food', 'subcategory': 'Lunch', 'is_income': 0}, ...]"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT main_category, subcategory, is_income FROM categories"
        " ORDER BY is_income DESC, main_category, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]
def get_category_tree():
    """{'Food': ['Breakfast', ...], 'Transport': [], ...} read from the DATABASE."""
    tree = {}
    for row in get_categories():
        tree.setdefault(row["main_category"], [])
        if row["subcategory"]:
            tree[row["main_category"]].append(row["subcategory"])
    return tree
# ---------- income ---------
def add_income(user_id, amount, source=None, date=None):
    source = validate_income_source(source)
    conn = get_connection()
    conn.execute("INSERT INTO income (user_id, amount, source, date) VALUES (?, ?, ?, ?)",
                 (user_id, amount, source, date))
    conn.commit(); conn.close()
def get_income(user_id):
    """The user's MONTHLY income: the total of all income rows in the most recent
    month that has data (so Salary 5000 + Freelancer 1000 = 6000). 0 if none."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(substr(date,1,7),'') AS m, SUM(amount) AS total"
        " FROM income WHERE user_id = ? GROUP BY m ORDER BY m DESC LIMIT 1",
        (user_id,)).fetchone()
    conn.close()
    return row["total"] if row and row["total"] is not None else 0
def get_income_records(user_id):
    """Every income row, oldest first — the input to the Income Analysis."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM income WHERE user_id = ? ORDER BY COALESCE(date,''), id",
        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
def update_income(user_id, amount):
    """Change the amount of the user's most recent income row (or add one)."""
    conn = get_connection()
    row = conn.execute("SELECT id FROM income WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                       (user_id,)).fetchone()
    if row:
        conn.execute("UPDATE income SET amount = ? WHERE id = ?", (amount, row["id"]))
    else:
        conn.execute("INSERT INTO income (user_id, amount) VALUES (?, ?)", (user_id, amount))
    conn.commit(); conn.close()
# ---------- transactions ---------
def add_transaction(user_id, description, amount, category, date=None, subcategory=None,
                    is_estimate=False):
    """Category and subcategory are validated against the category tree and stored.
    They are DATA supplied by the user/importer — nothing is predicted here.
    is_estimate marks a monthly figure the user ENTERED during onboarding."""
    category, subcategory = validate_category(category, subcategory)
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO transactions (user_id, description, amount, category, subcategory, date,"
        " is_estimate) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, description, amount, category, subcategory, date, 1 if is_estimate else 0))
    conn.commit(); tid = cur.lastrowid; conn.close()
    return tid
def get_transactions(user_id, month=None):
    # month is a string like "2026-08"; if given, filter by it.
    conn = get_connection()
    if month:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND substr(date,1,7) = ?",
            (user_id, month)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM transactions WHERE user_id = ?",
                              (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
def delete_estimates(user_id):
    """Remove onboarding estimates (e.g. when the user re-runs onboarding)."""
    conn = get_connection()
    cur = conn.execute("DELETE FROM transactions WHERE user_id = ? AND is_estimate = 1", (user_id,))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted
def get_expense_transactions(user_id, month=None):
    """Transactions that are expenses (income is stored in the `income` table,
    but this guard keeps an Income-categorised row out of the expense maths)."""
    return [t for t in get_transactions(user_id, month) if t.get("category") != "Income"]
def update_transaction(transaction_id, description=None, amount=None,
                       category=None, subcategory=None, date=None):
    conn = get_connection()
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    cur = dict(row)
    new_cat = category if category is not None else cur["category"]
    new_sub = subcategory if subcategory is not None else cur["subcategory"]
    new_cat, new_sub = validate_category(new_cat, new_sub)
    conn.execute(
        "UPDATE transactions SET description = ?, amount = ?, category = ?, subcategory = ?,"
        " date = ? WHERE id = ?",
        (description if description is not None else cur["description"],
         amount if amount is not None else cur["amount"],
         new_cat, new_sub,
         date if date is not None else cur["date"], transaction_id))
    conn.commit(); conn.close()
    return True
def delete_transaction(transaction_id):
    conn = get_connection()
    conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    conn.commit(); conn.close()
# ---------- goals ---------
def add_goal(user_id, name, target_amount, current_amount=0, target_date=None):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO financial_goals (user_id, name, target_amount, current_amount, target_date)"
        " VALUES (?, ?, ?, ?, ?)", (user_id, name, target_amount, current_amount, target_date))
    conn.commit(); gid = cur.lastrowid; conn.close()
    return gid
def get_goals(user_id):
    conn = get_connection()
    # Goals the user ranked come first (1 = top priority), then the rest by id.
    rows = conn.execute("SELECT * FROM financial_goals WHERE user_id = ?"
                        " ORDER BY priority IS NULL, priority, id",
                        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
def get_goal(goal_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM financial_goals WHERE id = ?", (goal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
# ---------- recurring expenses ---------
def add_recurring_expense(user_id, description, amount, category,
                          frequency="monthly", subcategory=None):
    category, subcategory = validate_category(category, subcategory)
    conn = get_connection()
    conn.execute(
        "INSERT INTO recurring_expenses (user_id, description, amount, category, subcategory,"
        " frequency) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, description, amount, category, subcategory, frequency))
    conn.commit(); conn.close()
def get_recurring_expenses(user_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM recurring_expenses WHERE user_id = ?",
                        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
# ---------- user category preferences (essential / flexible) ---------
def add_preference(
    user_id, main_category, subcategory=None, is_essential=False
):
    """Create or replace one preference row. Returns its id."""
    main_cat, sub = validate_category(main_category, subcategory)
    sub = sub or ""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO user_category_preferences (user_id, main_category, subcategory, "
        "is_essential) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, main_category, subcategory) "
        "DO UPDATE SET is_essential = excluded.is_essential",
        (user_id, main_cat, sub, 1 if is_essential else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM user_category_preferences WHERE user_id = ? AND main_category = ? "
        "AND subcategory = ?",
        (user_id, main_cat, sub),
    ).fetchone()
    conn.close()
    return row["id"] if row else cur.lastrowid
def get_preferences(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, user_id, main_category, subcategory, is_essential"
        " FROM user_category_preferences WHERE user_id = ?"
        " ORDER BY main_category, subcategory", (user_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["is_essential"] = bool(d["is_essential"])
        out.append(d)
    return out
def get_preference(user_id, main_category, subcategory=None):
    sub = subcategory or ""
    conn = get_connection()
    row = conn.execute(
        "SELECT id, user_id, main_category, subcategory, is_essential"
        " FROM user_category_preferences WHERE user_id = ? AND main_category = ?"
        " AND subcategory = ?", (user_id, main_category, sub)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["is_essential"] = bool(d["is_essential"])
    return d
def update_preference(user_id, main_category, subcategory=None, is_essential=False):
    """Change an existing preference. Returns True if a row was changed."""
    sub = subcategory or ""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE user_category_preferences SET is_essential = ?"
        " WHERE user_id = ? AND main_category = ? AND subcategory = ?",
        (1 if is_essential else 0, user_id, main_category, sub))
    conn.commit(); changed = cur.rowcount; conn.close()
    return changed > 0
def delete_preference(user_id, main_category, subcategory=None):
    sub = subcategory or ""
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM user_category_preferences WHERE user_id = ? AND main_category = ?"
        " AND subcategory = ?", (user_id, main_category, sub))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted > 0
def delete_all_preferences(user_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM user_category_preferences WHERE user_id = ?", (user_id,))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted
def get_categories_by_type(main_cat):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM categories WHERE main_category = ?", (main_cat,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
# ---------- income: single rows ---------
def delete_income(income_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM income WHERE id = ?", (income_id,))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted > 0
# ---------- goals: update / delete ---------
def update_goal(goal_id, name=None, target_amount=None, current_amount=None, target_date=None):
    goal = get_goal(goal_id)
    if goal is None:
        return False
    conn = get_connection()
    conn.execute(
        "UPDATE financial_goals SET name = ?, target_amount = ?, current_amount = ?,"
        " target_date = ? WHERE id = ?",
        (name if name is not None else goal["name"],
         target_amount if target_amount is not None else goal["target_amount"],
         current_amount if current_amount is not None else goal["current_amount"],
         target_date if target_date is not None else goal["target_date"], goal_id))
    conn.commit(); conn.close()
    return True
def set_goal_priorities(user_id, ordered_goal_ids):
    """Store the user's drag-and-drop order: the first id becomes priority 1."""
    conn = get_connection()
    for rank, goal_id in enumerate(ordered_goal_ids, start=1):
        conn.execute("UPDATE financial_goals SET priority = ? WHERE id = ? AND user_id = ?",
                     (rank, goal_id, user_id))
    conn.commit(); conn.close()
def contribute_to_goal(goal_id, amount):
    """Add money saved toward a goal. Returns the updated goal or None."""
    goal = get_goal(goal_id)
    if goal is None:
        return None
    update_goal(goal_id, current_amount=round(goal["current_amount"] + amount, 2))
    return get_goal(goal_id)
def delete_goal(goal_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM financial_goals WHERE id = ?", (goal_id,))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted > 0
# ---------- recurring expenses: delete ---------
def delete_recurring_expense(expense_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM recurring_expenses WHERE id = ?", (expense_id,))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted > 0
# ---------- advice card status ---------
def get_recommendation_statuses(user_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM recommendation_status WHERE user_id = ?",
                        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
def set_recommendation_status(user_id, rec_key, status, baseline=None, month=None):
    """One row per (user, card): a new status updates it; the first baseline is kept."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO recommendation_status (user_id, rec_key, status, baseline, month)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, rec_key) DO UPDATE SET status = excluded.status,"
        " updated_at = CURRENT_TIMESTAMP",
        (user_id, rec_key, status, baseline, month))
    conn.commit(); conn.close()
def delete_recommendation_status(user_id, rec_key):
    conn = get_connection()
    cur = conn.execute("DELETE FROM recommendation_status WHERE user_id = ? AND rec_key = ?",
                       (user_id, rec_key))
    conn.commit(); deleted = cur.rowcount; conn.close()
    return deleted > 0
# ---------- demo reset ---------
USER_DATA_TABLES = ["recommendation_status", "user_category_preferences", "recurring_expenses",
                    "financial_goals", "transactions", "income", "users"]
def clear_user_data():
    """Delete every user's data (the categories reference table is kept) and restart
    the id counters, so the next seeded user is user 1 again."""
    conn = get_connection()
    for table in USER_DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        f"DELETE FROM sqlite_sequence WHERE name IN ({','.join('?' * len(USER_DATA_TABLES))})",
        USER_DATA_TABLES)
    conn.commit(); conn.close()
