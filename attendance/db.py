"""Database helpers for the attendance app.

Everything lives in a single SQLite file (data/attendance.db by default),
so backups are a one-file copy and there is no database server to run.
"""

import os
import re
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ATTENDANCE_DB", os.path.join(BASE_DIR, "data", "attendance.db"))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# Default bell schedule seeded on first run. Edit these (or the periods
# table) to match your real schedule.
DEFAULT_PERIODS = [
    ("Period 1", "08:00", "08:50", 1),
    ("Period 2", "08:55", "09:45", 2),
    ("Period 3", "09:50", "10:40", 3),
    ("Period 4", "10:45", "11:35", 4),
    ("Period 5", "11:40", "12:30", 5),
    ("Period 6", "12:35", "13:25", 6),
    ("Period 7", "13:30", "14:20", 7),
]


def get_db():
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables (idempotent) and seed the default periods once."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    if conn.execute("SELECT COUNT(*) FROM periods").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO periods (name, start_time, end_time, sort_order) "
            "VALUES (?, ?, ?, ?)",
            DEFAULT_PERIODS,
        )
    conn.commit()
    conn.close()


def list_periods(conn):
    return conn.execute("SELECT * FROM periods ORDER BY sort_order, start_time").fetchall()


def current_period(conn, now=None):
    """Return the period whose time window contains `now` (default: local now),
    or None if we are outside all periods."""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    for p in list_periods(conn):
        if p["start_time"] <= hhmm <= p["end_time"]:
            return p
    return None


def enrollments_exist(conn):
    """True once any class schedule has been imported. Switches the app from
    whole-school mode to per-period class rosters."""
    return conn.execute("SELECT 1 FROM enrollments LIMIT 1").fetchone() is not None


def resolve_period(conn, token):
    """Map a CSV period value ("3", "Period 3", "period 3") to a period id."""
    token = (token or "").strip()
    if not token:
        return None
    row = conn.execute(
        "SELECT id FROM periods WHERE lower(name) = lower(?)", (token,)
    ).fetchone()
    if row:
        return row["id"]
    m = re.search(r"\d+", token)
    if m:
        n = int(m.group())
        row = conn.execute(
            "SELECT id FROM periods WHERE sort_order = ?", (n,)
        ).fetchone()
        if row:
            return row["id"]
        for p in list_periods(conn):
            pm = re.search(r"\d+", p["name"])
            if pm and int(pm.group()) == n:
                return p["id"]
    return None
