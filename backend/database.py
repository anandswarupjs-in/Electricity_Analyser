"""
database.py - SQLite storage layer (Python's built-in sqlite3 module / "PySqlite").

Syllabus concepts shown here:
  * Database processing: creating tables, inserting, querying, updating, deleting
  * Parameterized SQL (the "?" placeholders) - values are NEVER joined into the SQL text
  * Dictionaries (rows are converted to dicts), lists, tuples, user-defined functions
"""
import os
import sqlite3
from datetime import datetime

# The database file lives next to this script. DB_PATH can override it (handy for tests).
DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "electricity.db"
)

# ---------------------------------------------------------------------------
# Table definitions (created automatically on first run)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    consumer_type TEXT,                              -- 'house' or 'factory' (NULL until profile setup)
    billing_cycle INTEGER NOT NULL DEFAULT 1,        -- 1 or 2 months
    tariff_mode   TEXT NOT NULL DEFAULT 'default',   -- 'default' slab or 'custom' rate
    custom_rate   REAL,                              -- rupees per unit (used when tariff_mode='custom')
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,                     -- appliance category OR machine type
    wattage       REAL NOT NULL,
    quantity      INTEGER NOT NULL,
    hours_per_day REAL NOT NULL,
    days_per_month INTEGER NOT NULL,
    device_type   TEXT NOT NULL,                     -- 'appliance' or 'machine'
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    period            TEXT NOT NULL,                 -- e.g. 'October 2026'
    total_kwh         REAL NOT NULL,                 -- consumption for the whole billing cycle
    approx_bill       REAL NOT NULL,
    connected_load_kw REAL NOT NULL,
    highest_consumer  TEXT,
    snapshot_json     TEXT,                          -- data needed to REGENERATE the PDF later
    created_at        TEXT NOT NULL,
    UNIQUE (user_id, period),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS update_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id);
CREATE INDEX IF NOT EXISTS idx_history_user ON update_history(user_id);
"""

# Only these user columns may be changed through update_user() (protects the dynamic SET clause).
ALLOWED_USER_FIELDS = ("consumer_type", "billing_cycle", "tariff_mode", "custom_rate")

# Columns used when copying device data between dicts and SQL.
DEVICE_FIELDS = ("name", "category", "wattage", "quantity", "hours_per_day", "days_per_month")


def get_connection():
    """Open a new SQLite connection. Each call gets its own connection (safe for threads)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row               # lets us read columns by name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_text():
    """Current time as text, the format SQLite stores for us."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _execute(sql, params=(), mode="none"):
    """
    Run ONE parameterized statement.
    mode: 'all'    -> list of dicts
          'one'    -> a dict or None
          'insert' -> id of the new row
          'none'   -> number of rows changed
    """
    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        if mode == "all":
            result = [dict(row) for row in cursor.fetchall()]
        elif mode == "one":
            row = cursor.fetchone()
            result = dict(row) if row else None
        elif mode == "insert":
            result = cursor.lastrowid
        else:
            result = cursor.rowcount
        conn.commit()
        return result
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create every table if it does not exist yet (no manual SQL setup needed)."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------
def create_user(name, email, password_hash):
    """Insert a user. Raises sqlite3.IntegrityError if the email already exists."""
    return _execute(
        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (name, email.lower(), password_hash, now_text()),
        "insert",
    )


def get_user_by_email(email):
    return _execute("SELECT * FROM users WHERE email = ?", (email.lower(),), "one")


def get_user_by_id(user_id):
    return _execute("SELECT * FROM users WHERE id = ?", (user_id,), "one")


def update_user(user_id, **fields):
    """Update profile/tariff columns. Column names come from a whitelist; values are parameters."""
    columns = [col for col in fields if col in ALLOWED_USER_FIELDS]
    if not columns:
        return 0
    set_clause = ", ".join(f"{col} = ?" for col in columns)       # safe: names are whitelisted
    values = [fields[col] for col in columns] + [user_id]
    return _execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)


# ---------------------------------------------------------------------------
# DEVICES  (every query includes user_id so user A can never touch user B's rows)
# ---------------------------------------------------------------------------
def add_device(user_id, data, device_type):
    stamp = now_text()
    return _execute(
        """INSERT INTO devices (user_id, name, category, wattage, quantity, hours_per_day,
                                days_per_month, device_type, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, data["name"], data["category"], data["wattage"], data["quantity"],
         data["hours_per_day"], data["days_per_month"], device_type, stamp, stamp),
        "insert",
    )


def get_devices(user_id):
    return _execute("SELECT * FROM devices WHERE user_id = ? ORDER BY id", (user_id,), "all")


def get_device(user_id, device_id):
    return _execute("SELECT * FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id), "one")


def count_devices(user_id):
    row = _execute("SELECT COUNT(*) AS total FROM devices WHERE user_id = ?", (user_id,), "one")
    return row["total"]


def update_device(user_id, device_id, data):
    return _execute(
        """UPDATE devices SET name = ?, category = ?, wattage = ?, quantity = ?,
                              hours_per_day = ?, days_per_month = ?, updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (data["name"], data["category"], data["wattage"], data["quantity"],
         data["hours_per_day"], data["days_per_month"], now_text(), device_id, user_id),
    )


def delete_device(user_id, device_id):
    return _execute("DELETE FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id))


def delete_devices(user_id, device_ids):
    """Delete several devices at once (used by 'Undo Last Import')."""
    if not device_ids:
        return 0
    placeholders = ", ".join("?" for _ in device_ids)             # only '?' characters are joined
    return _execute(
        f"DELETE FROM devices WHERE user_id = ? AND id IN ({placeholders})",
        [user_id] + list(device_ids),
    )


# ---------------------------------------------------------------------------
# REPORTS  (one row per user per period; saving again in the same month updates it)
# ---------------------------------------------------------------------------
def save_report(user_id, period, total_kwh, approx_bill, connected_load_kw, highest_consumer, snapshot_json):
    existing = _execute(
        "SELECT id FROM reports WHERE user_id = ? AND period = ?", (user_id, period), "one"
    )
    if existing:
        _execute(
            """UPDATE reports SET total_kwh = ?, approx_bill = ?, connected_load_kw = ?,
                                  highest_consumer = ?, snapshot_json = ?, created_at = ?
               WHERE id = ?""",
            (total_kwh, approx_bill, connected_load_kw, highest_consumer, snapshot_json,
             now_text(), existing["id"]),
        )
        return existing["id"]
    return _execute(
        """INSERT INTO reports (user_id, period, total_kwh, approx_bill, connected_load_kw,
                                highest_consumer, snapshot_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, period, total_kwh, approx_bill, connected_load_kw, highest_consumer,
         snapshot_json, now_text()),
        "insert",
    )


def get_reports(user_id):
    """List of report summaries, newest first (the heavy snapshot column is left out)."""
    return _execute(
        """SELECT id, period, total_kwh, approx_bill, connected_load_kw, highest_consumer, created_at
           FROM reports WHERE user_id = ? ORDER BY id DESC""",
        (user_id,), "all",
    )


def get_report(user_id, report_id):
    return _execute(
        "SELECT * FROM reports WHERE id = ? AND user_id = ?", (report_id, user_id), "one"
    )


# ---------------------------------------------------------------------------
# UPDATE HISTORY
# ---------------------------------------------------------------------------
def add_history(user_id, description):
    return _execute(
        "INSERT INTO update_history (user_id, description, created_at) VALUES (?, ?, ?)",
        (user_id, description, now_text()), "insert",
    )


def get_history(user_id, limit=15):
    return _execute(
        "SELECT id, description, created_at FROM update_history WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit), "all",
    )
