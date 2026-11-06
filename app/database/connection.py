# app/database/connection.py
# One function that opens a connection to the SQLite database file.

import sqlite3

from app import config


def get_connection():
    # Read DB_PATH at call time so tests can safely replace
    # config.DB_PATH with a temporary test database.
    conn = sqlite3.connect(config.DB_PATH)

    # row_factory=Row lets us read columns by name,
    # e.g. row["amount"].
    conn.row_factory = sqlite3.Row

    # Enforce the table relationships.
    conn.execute("PRAGMA foreign_keys = ON")

    return conn

