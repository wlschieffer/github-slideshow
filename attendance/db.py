"""Database helpers for the attendance app.

Everything lives in a single SQLite file (data/attendance.db by default),
so backups are a one-file copy and there is no database server to run.
"""

import os
import re
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ATTENDANCE_DB", os.path.join(BASE_DIR, "data", "attendance.db"))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# ---------------------------------------------------------------------------
# Seed data — Jordan High School bell schedule, B Lunch (from the PDF).
# Periods are the canonical list; each schedule gives them different times.
# Times are 24h "HH:MM". Lunch is a time marker, not a scannable period, so
# it is intentionally omitted.
# ---------------------------------------------------------------------------
# Period display names, formatted "NN - Ordinal Period" (e.g. "01 - First Period").
ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh"]


def period_name(n):
    return f"{n:02d} - {ORDINALS[n - 1]} Period"


ENRICHMENT = "Enrichment"
_P = {n: period_name(n) for n in range(1, 8)}

# (name, sort_order) — Enrichment sits between 3rd and 4th.
SEED_PERIODS = [
    (_P[1], 1), (_P[2], 2), (_P[3], 3), (ENRICHMENT, 4),
    (_P[4], 5), (_P[5], 6), (_P[6], 7), (_P[7], 8),
]

ENRICHMENT_TIMES = {  # Mon, Tue, Fri
    _P[1]: ("07:15", "08:02"), _P[2]: ("08:09", "08:56"),
    _P[3]: ("09:03", "09:50"), ENRICHMENT: ("09:50", "10:29"),
    _P[4]: ("10:36", "11:23"), _P[5]: ("12:00", "12:47"),
    _P[6]: ("12:54", "13:41"), _P[7]: ("13:48", "14:35"),
}
REGULAR_TIMES = {  # Wed, Thu
    _P[1]: ("07:15", "08:08"), _P[2]: ("08:15", "09:08"),
    _P[3]: ("09:15", "10:08"), _P[4]: ("10:15", "11:07"),
    _P[5]: ("11:44", "12:35"), _P[6]: ("12:42", "13:35"),
    _P[7]: ("13:42", "14:35"),
}
PEP_RALLY_TIMES = {
    _P[1]: ("07:15", "08:04"), _P[2]: ("08:11", "09:00"),
    _P[3]: ("09:07", "09:56"), _P[4]: ("10:03", "10:50"),
    _P[5]: ("11:27", "12:14"), _P[6]: ("12:21", "13:08"),
    _P[7]: ("13:15", "14:02"),
}
# Map old short names -> new format, for migrating existing databases.
LEGACY_PERIOD_NAMES = {
    "1st": _P[1], "2nd": _P[2], "3rd": _P[3], "4th": _P[4],
    "5th": _P[5], "6th": _P[6], "7th": _P[7],
}
# (name, sort_order, times)
SEED_SCHEDULES = [
    ("Enrichment", 1, ENRICHMENT_TIMES),
    ("Regular", 2, REGULAR_TIMES),
    ("Pep Rally", 3, PEP_RALLY_TIMES),
]

# Which schedule each weekday defaults to under "auto" mode. Python's
# weekday(): Monday=0 .. Sunday=6. Blank = no bell schedule that day.
WEEKDAY_DEFAULT = {
    0: "Enrichment", 1: "Enrichment", 2: "Regular",
    3: "Regular", 4: "Enrichment", 5: "", 6: "",
}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]


def get_db():
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables (idempotent) and seed the bell schedule once."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    # Seed periods.
    if conn.execute("SELECT COUNT(*) FROM periods").fetchone()[0] == 0:
        for name, order in SEED_PERIODS:
            # periods.start_time/end_time are a fallback; real times live in
            # schedule_periods. Use the Regular (or Enrichment) time here.
            ft = REGULAR_TIMES.get(name) or ENRICHMENT_TIMES.get(name) or ("00:00", "00:00")
            conn.execute(
                "INSERT INTO periods (name, start_time, end_time, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (name, ft[0], ft[1], order),
            )

    # Migrate any legacy short period names ("1st") to the new format
    # ("01 - First Period"). References are by period_id, so this is safe.
    if conn.execute(
        "SELECT 1 FROM periods WHERE name IN "
        "('1st','2nd','3rd','4th','5th','6th','7th') LIMIT 1"
    ).fetchone():
        for old, new in LEGACY_PERIOD_NAMES.items():
            conn.execute("UPDATE periods SET name = ? WHERE name = ?", (new, old))

    # Seed schedules and their per-period times.
    if conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0:
        for sname, sorder, times in SEED_SCHEDULES:
            cur = conn.execute(
                "INSERT INTO schedules (name, sort_order) VALUES (?, ?)",
                (sname, sorder),
            )
            sid = cur.lastrowid
            for pname, (start, end) in times.items():
                prow = conn.execute(
                    "SELECT id FROM periods WHERE name = ?", (pname,)
                ).fetchone()
                if prow:
                    conn.execute(
                        "INSERT INTO schedule_periods "
                        "(schedule_id, period_id, start_time, end_time) "
                        "VALUES (?, ?, ?, ?)",
                        (sid, prow["id"], start, end),
                    )

    # Seed settings (mode + weekday map + scanner cooldown) if not present.
    _seed_setting(conn, "schedule_mode", "auto")
    _seed_setting(conn, "scan_cooldown_ms", "1500")
    for wd, sname in WEEKDAY_DEFAULT.items():
        _seed_setting(conn, f"wd_{wd}", sname)

    # Seed the default staff PIN (1234) only if none is set. Compute the hash
    # lazily so we don't re-hash on every startup.
    if not conn.execute(
        "SELECT 1 FROM settings WHERE key = 'staff_pin_hash'"
    ).fetchone():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('staff_pin_hash', ?)",
            (generate_password_hash("1234"),),
        )

    conn.commit()
    conn.close()


def _seed_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Periods & schedules
# ---------------------------------------------------------------------------
def list_periods(conn):
    return conn.execute(
        "SELECT * FROM periods ORDER BY sort_order, name"
    ).fetchall()


def visible_periods(conn):
    """Periods that appear in scanning/reports. When schedules (enrollments)
    have been imported, that's the periods students are actually enrolled in
    — so the period list is driven by the roster/schedule import. Otherwise
    (whole-school mode) it's every period."""
    if enrollments_exist(conn):
        return conn.execute(
            "SELECT DISTINCT p.* FROM periods p "
            "JOIN enrollments e ON e.period_id = p.id "
            "JOIN students s ON s.student_id = e.student_id AND s.active = 1 "
            "ORDER BY p.sort_order, p.name"
        ).fetchall()
    return list_periods(conn)


def list_schedules(conn):
    return conn.execute("SELECT * FROM schedules ORDER BY sort_order, name").fetchall()


def schedule_by_name(conn, name):
    return conn.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()


def active_schedule(conn, now=None):
    """The schedule in effect right now, honoring the mode setting:
      'auto'  -> the weekday's mapped schedule
      'off'   -> None (no bell schedule; periods chosen manually)
      <name>  -> that schedule is forced (e.g. a Pep Rally day)
    Returns a schedule row or None."""
    mode = get_setting(conn, "schedule_mode", "auto")
    if mode == "off":
        return None
    if mode == "auto":
        now = now or datetime.now()
        name = get_setting(conn, f"wd_{now.weekday()}", "")
        return schedule_by_name(conn, name) if name else None
    return schedule_by_name(conn, mode)


def current_period(conn, now=None):
    """The period whose time window (in the active schedule) contains now,
    or None if the bell schedule is off / we're between periods."""
    sched = active_schedule(conn, now)
    if not sched:
        return None
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    return conn.execute(
        "SELECT p.* FROM schedule_periods sp "
        "JOIN periods p ON p.id = sp.period_id "
        "WHERE sp.schedule_id = ? AND sp.start_time <= ? AND ? <= sp.end_time "
        "ORDER BY sp.start_time LIMIT 1",
        (sched["id"], hhmm, hhmm),
    ).fetchone()


def enrollments_exist(conn):
    """True once any class schedule has been imported. Switches the app from
    whole-school mode to per-period class rosters."""
    return conn.execute("SELECT 1 FROM enrollments LIMIT 1").fetchone() is not None


def resolve_period(conn, token):
    """Map a CSV period value ("4", "4th", "Enrichment") to a period id."""
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
        for p in list_periods(conn):
            pm = re.match(r"\s*0*(\d+)", p["name"])
            if pm and int(pm.group(1)) == n:
                return p["id"]
    return None
