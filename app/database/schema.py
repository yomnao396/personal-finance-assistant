# app/database/schema.py
# Creates all tables if they do not already exist, and fills the reference
# `categories` table from the category tree in app/config.py.
# The schema includes transaction subcategories and two reference tables:
#   categories                 -> the official category/subcategory reference data
#   user_category_preferences  -> each user's essential / flexible choices

from app.database.connection import get_connection
from app.config import CATEGORY_TREE


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    email      TEXT,
    current_savings REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS income (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount  REAL NOT NULL,
    source  TEXT,
    date    TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    description TEXT,
    amount      REAL NOT NULL,
    category    TEXT,
    subcategory TEXT,
    date        TEXT,
    is_estimate INTEGER NOT NULL DEFAULT 0,   -- 1 = entered during onboarding, not real history
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS financial_goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    target_amount  REAL NOT NULL,
    current_amount REAL DEFAULT 0,
    target_date    TEXT,
    priority       INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS recurring_expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    description TEXT,
    amount      REAL NOT NULL,
    category    TEXT,
    subcategory TEXT,
    frequency   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Reference data: the official categories and subcategories.
CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    main_category TEXT NOT NULL,
    subcategory   TEXT NOT NULL DEFAULT '',
    is_income     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (main_category, subcategory)
);

-- Per-user runtime constraint: which spending is essential (protected).
CREATE TABLE IF NOT EXISTS user_category_preferences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    main_category TEXT NOT NULL,
    subcategory   TEXT NOT NULL DEFAULT '',
    is_essential  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, main_category, subcategory),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- What the user did with an advice card. Cards are rebuilt from the data on
-- every visit; only the user's choice is stored, once per (user, card key).
CREATE TABLE IF NOT EXISTS recommendation_status (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    rec_key    TEXT NOT NULL,
    status     TEXT NOT NULL,              -- accepted / completed / dismissed
    baseline   REAL,                       -- the tracked number when first marked
    month      TEXT,                       -- the data month of that baseline
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, rec_key),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def _add_column_if_missing(conn, table, column, decl):
    """Tiny migration helper so an existing finance.db keeps working."""
    cols = [
        r["name"]
        for r in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    ]

    if column not in cols:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
        )


def seed_categories():
    """Write the category tree from config into the categories table (idempotent)."""
    conn = get_connection()
    # Only missing rows are inserted: an ignored INSERT still advances the
    # AUTOINCREMENT counter, which rewrote the database file on every start.
    have = {(r["main_category"], r["subcategory"]) for r in conn.execute(
        "SELECT main_category, subcategory FROM categories").fetchall()}

    for main, subs in CATEGORY_TREE.items():

        if subs:
            for sub in subs:
                if (main, sub) in have:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO categories
                    (main_category, subcategory, is_income)
                    VALUES (?, ?, ?);
                    """,
                    (
                        main,
                        sub,
                        1 if main == "Income" else 0,
                    ),
                )

        elif (main, "") not in have:
            conn.execute(
                """
                INSERT OR IGNORE INTO categories
                (main_category, subcategory, is_income)
                VALUES (?, ?, ?);
                """,
                (
                    main,
                    "",
                    1 if main == "Income" else 0,
                ),
            )

    conn.commit()

    n = conn.execute(
        "SELECT COUNT(*) AS c FROM categories"
    ).fetchone()["c"]

    conn.close()

    return n


def create_tables():
    conn = get_connection()

    conn.executescript(SCHEMA_SQL)

    # migrations for databases created before this update
    _add_column_if_missing(
        conn,
        "transactions",
        "subcategory",
        "TEXT",
    )

    _add_column_if_missing(
        conn,
        "recurring_expenses",
        "subcategory",
        "TEXT",
    )

    _add_column_if_missing(
        conn,
        "financial_goals",
        "priority",
        "INTEGER",
    )

    _add_column_if_missing(conn, "users", "current_savings", "REAL DEFAULT 0")

    _add_column_if_missing(
        conn,
        "transactions",
        "is_estimate",
        "INTEGER NOT NULL DEFAULT 0",
    )

    conn.commit()
    conn.close()

    n = seed_categories()

    print(
        f"Tables created (or already existed). "
        f"{n} category rows in 'categories'"
    )


if __name__ == "__main__":
    create_tables()