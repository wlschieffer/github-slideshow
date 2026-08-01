"""Attendance & ID-check web app.

Two interfaces, one internal server:
  * "/"       kiosk    -> students scan their ID to check in
  * "/admin"  staff    -> manual check-in + who-scanned / who-hasn't reports
  * "/roster" staff    -> import/view the roster of valid IDs

Run:  python app.py            (serves on 0.0.0.0:8000 via waitress)
      python app.py import roster.csv    (bulk-import a roster)
"""

import csv
import io
import os
import sys
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db

app = Flask(__name__)


def _load_secret_key():
    """Per-install random key stored next to the database (outside git), so
    signed session cookies can't be forged even though the code is public."""
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    path = os.path.join(os.path.dirname(db.DB_PATH), "secret_key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    key = os.urandom(32)
    with open(path, "wb") as f:
        f.write(key)
    return key


app.secret_key = _load_secret_key()
app.permanent_session_lifetime = timedelta(hours=12)

# Endpoints reachable without the staff PIN: the student kiosk, the scan
# endpoint it calls, the login page itself, and static files.
PUBLIC_ENDPOINTS = {"kiosk", "scan", "staff_login", "static"}


@app.before_request
def _ensure_db_and_auth():
    # Cheap and idempotent; guarantees tables/periods exist on first hit.
    db.init_db()
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if not session.get("staff_authed"):
        return redirect(url_for("staff_login", next=request.full_path))


def _safe_next(target):
    """Only allow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("admin")


# ---------------------------------------------------------------------------
# Staff PIN login
# ---------------------------------------------------------------------------
@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if session.get("staff_authed"):
        return redirect(_safe_next(request.args.get("next")))
    error = None
    if request.method == "POST":
        pin = request.form.get("pin", "")
        conn = db.get_db()
        pin_hash = db.get_setting(conn, "staff_pin_hash", "")
        conn.close()
        if pin_hash and check_password_hash(pin_hash, pin):
            session.permanent = True
            session["staff_authed"] = True
            return redirect(_safe_next(request.form.get("next")))
        error = "Incorrect PIN."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/staff/logout")
def staff_logout():
    session.pop("staff_authed", None)
    return redirect(url_for("kiosk"))


# ---------------------------------------------------------------------------
# Core check-in logic (shared by the kiosk scanner and manual entry)
# ---------------------------------------------------------------------------
def check_in(conn, student_id, period_id, method):
    """Record a check-in. Returns (status, student_row_or_None, timestamp).

    status is one of: 'ok', 'duplicate', 'unknown', 'inactive'.
    """
    student_id = (student_id or "").strip()
    student = conn.execute(
        "SELECT * FROM students WHERE student_id = ?", (student_id,)
    ).fetchone()

    if student is None:
        return "unknown", None, None
    if not student["active"]:
        return "inactive", student, None

    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT INTO attendance (student_id, period_id, day, scanned_at, method) "
            "VALUES (?, ?, ?, ?, ?)",
            (student_id, period_id, today, now, method),
        )
        conn.commit()
        return "ok", student, now
    except db.sqlite3.IntegrityError:
        # UNIQUE(student_id, period_id, day) -> already checked in this period.
        existing = conn.execute(
            "SELECT scanned_at FROM attendance "
            "WHERE student_id = ? AND period_id = ? AND day = ?",
            (student_id, period_id, today),
        ).fetchone()
        return "duplicate", student, existing["scanned_at"] if existing else None


# ---------------------------------------------------------------------------
# Kiosk (student-facing)
# ---------------------------------------------------------------------------
@app.route("/")
def kiosk():
    conn = db.get_db()
    periods = db.visible_periods(conn)
    sched = db.active_schedule(conn)
    cur = db.current_period(conn)
    selected = request.args.get("period_id", type=int) or (
        cur["id"] if cur else (periods[0]["id"] if periods else None)
    )
    cooldown = int(db.get_setting(conn, "scan_cooldown_ms", "1500") or 0)
    conn.close()
    return render_template(
        "kiosk.html",
        periods=periods,
        selected_period=selected,
        current=cur,
        schedule_name=sched["name"] if sched else None,
        scan_cooldown_ms=cooldown,
    )


@app.post("/scan")
def scan():
    """JSON endpoint the kiosk page calls when a badge is scanned."""
    student_id = request.form.get("student_id", "")
    period_id = request.form.get("period_id", type=int)
    conn = db.get_db()
    status, student, ts = check_in(conn, student_id, period_id, method="scan")
    conn.close()

    messages = {
        "ok": "Checked in",
        "duplicate": "Already checked in",
        "unknown": "ID not recognized",
        "inactive": "ID is inactive",
    }
    when = ""
    if ts:
        when = datetime.fromisoformat(ts).strftime("%-I:%M %p")
    return {
        "status": status,
        "name": student["name"] if student else "",
        "grade": student["grade"] if student else "",
        "message": messages.get(status, status),
        "time": when,
    }


# ---------------------------------------------------------------------------
# Admin: manual check-in + reports
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    conn = db.get_db()
    periods = db.visible_periods(conn)
    cur = db.current_period(conn)
    day = request.args.get("day") or date.today().isoformat()
    period_id = request.args.get("period_id", type=int) or (cur["id"] if cur else (periods[0]["id"] if periods else None))

    rows = []
    extras = []
    present_count = 0
    per_period = db.enrollments_exist(conn)
    if period_id is not None:
        if per_period:
            # Expected roster = students enrolled in this period.
            rows = conn.execute(
                """
                SELECT s.student_id, s.name, s.grade, e.section, e.room,
                       a.scanned_at, a.method
                FROM enrollments e
                JOIN students s ON s.student_id = e.student_id AND s.active = 1
                LEFT JOIN attendance a
                  ON a.student_id = s.student_id
                 AND a.period_id = e.period_id
                 AND a.day = ?
                WHERE e.period_id = ?
                ORDER BY (a.scanned_at IS NOT NULL), s.name
                """,
                (day, period_id),
            ).fetchall()
            # Students who scanned into this period but aren't enrolled in it.
            extras = conn.execute(
                """
                SELECT s.student_id, s.name, s.grade, a.scanned_at, a.method
                FROM attendance a
                JOIN students s ON s.student_id = a.student_id
                WHERE a.period_id = ? AND a.day = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM enrollments e
                      WHERE e.student_id = a.student_id
                        AND e.period_id = a.period_id)
                ORDER BY a.scanned_at
                """,
                (period_id, day),
            ).fetchall()
        else:
            # Whole-school mode: every active student is expected.
            rows = conn.execute(
                """
                SELECT s.student_id, s.name, s.grade, NULL AS section, NULL AS room,
                       a.scanned_at, a.method
                FROM students s
                LEFT JOIN attendance a
                  ON a.student_id = s.student_id
                 AND a.period_id = ?
                 AND a.day = ?
                WHERE s.active = 1
                ORDER BY (a.scanned_at IS NOT NULL), s.name
                """,
                (period_id, day),
            ).fetchall()
        present_count = sum(1 for r in rows if r["scanned_at"])
    conn.close()

    return render_template(
        "admin.html",
        periods=periods,
        selected_period=period_id,
        day=day,
        rows=rows,
        extras=extras,
        per_period=per_period,
        present_count=present_count,
        absent_count=len(rows) - present_count,
        total=len(rows),
    )


@app.post("/admin/mark")
def admin_mark():
    """Manual check-in (e.g. student forgot their ID)."""
    student_id = request.form.get("student_id", "")
    period_id = request.form.get("period_id", type=int)
    day = request.form.get("day") or date.today().isoformat()
    conn = db.get_db()
    # Manual marks may be for a past/selected day, so insert directly.
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT INTO attendance (student_id, period_id, day, scanned_at, method) "
            "VALUES (?, ?, ?, ?, 'manual')",
            (student_id.strip(), period_id, day, now),
        )
        conn.commit()
    except db.sqlite3.IntegrityError:
        pass  # already present; nothing to do
    conn.close()
    return redirect(url_for("admin", day=day, period_id=period_id))


@app.post("/admin/unmark")
def admin_unmark():
    """Undo a check-in (fix a mistake)."""
    student_id = request.form.get("student_id", "")
    period_id = request.form.get("period_id", type=int)
    day = request.form.get("day") or date.today().isoformat()
    conn = db.get_db()
    conn.execute(
        "DELETE FROM attendance WHERE student_id = ? AND period_id = ? AND day = ?",
        (student_id.strip(), period_id, day),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin", day=day, period_id=period_id))


@app.route("/admin/export.csv")
def admin_export():
    """Download the who-scanned / who-hasn't report for a day + period."""
    day = request.args.get("day") or date.today().isoformat()
    period_id = request.args.get("period_id", type=int)
    conn = db.get_db()
    period = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
    rows = conn.execute(
        """
        SELECT s.student_id, s.name, s.grade, a.scanned_at, a.method
        FROM students s
        LEFT JOIN attendance a
          ON a.student_id = s.student_id AND a.period_id = ? AND a.day = ?
        WHERE s.active = 1
        ORDER BY s.name
        """,
        (period_id, day),
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_id", "name", "grade", "status", "checked_in_at", "method"])
    for r in rows:
        w.writerow([
            r["student_id"], r["name"], r["grade"] or "",
            "Present" if r["scanned_at"] else "Absent",
            r["scanned_at"] or "", r["method"] or "",
        ])
    pname = period["name"] if period else f"period{period_id}"
    fname = f"attendance_{day}_{pname}.csv".replace(" ", "_")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------------------------------------------------------------------------
# Roster management
# ---------------------------------------------------------------------------
@app.route("/roster")
def roster():
    conn = db.get_db()
    students = conn.execute(
        "SELECT * FROM students ORDER BY active DESC, name"
    ).fetchall()
    conn.close()
    return render_template("roster.html", students=students)


@app.post("/roster/import")
def roster_import():
    """Import a CSV with columns: student_id, name, grade (grade optional)."""
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.")
        return redirect(url_for("roster"))
    text = file.read().decode("utf-8-sig")
    added = import_roster_rows(csv.DictReader(io.StringIO(text)))
    flash(f"Imported / updated {added} students.")
    return redirect(url_for("roster"))


@app.route("/roster/export.csv")
def roster_export():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM students ORDER BY name").fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_id", "name", "grade", "active"])
    for r in rows:
        w.writerow([r["student_id"], r["name"], r["grade"] or "", r["active"]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=roster.csv"},
    )


def import_roster_rows(reader):
    """Upsert rows from a DictReader. Header names are matched loosely."""
    conn = db.get_db()
    count = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        sid = row.get("student_id") or row.get("id") or row.get("badge")
        name = row.get("name") or row.get("student") or row.get("full_name")
        if not sid or not name:
            continue
        grade = row.get("grade") or row.get("homeroom") or None
        conn.execute(
            """
            INSERT INTO students (student_id, name, grade, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(student_id) DO UPDATE SET
                name = excluded.name,
                grade = excluded.grade,
                active = 1
            """,
            (sid, name, grade),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Schedule (per-period class rosters)
# ---------------------------------------------------------------------------
@app.route("/schedule")
def schedule():
    conn = db.get_db()
    periods = db.list_periods(conn)
    counts = {
        p["id"]: conn.execute(
            "SELECT COUNT(*) FROM enrollments e "
            "JOIN students s ON s.student_id = e.student_id AND s.active = 1 "
            "WHERE e.period_id = ?",
            (p["id"],),
        ).fetchone()[0]
        for p in periods
    }
    total = conn.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
    conn.close()
    return render_template(
        "schedule.html", periods=periods, counts=counts, total=total
    )


@app.post("/schedule/import")
def schedule_import():
    """Import class schedules. CSV columns: student_id, period, [section], [room]."""
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.")
        return redirect(url_for("schedule"))
    text = file.read().decode("utf-8-sig")
    added, skipped = import_enrollment_rows(csv.DictReader(io.StringIO(text)))
    msg = f"Imported / updated {added} enrollments."
    if skipped:
        msg += f" Skipped {skipped} rows (unknown student ID or period)."
    flash(msg)
    return redirect(url_for("schedule"))


@app.post("/schedule/clear")
def schedule_clear():
    """Remove all enrollments (reverts to whole-school mode)."""
    conn = db.get_db()
    conn.execute("DELETE FROM enrollments")
    conn.commit()
    conn.close()
    flash("Cleared all schedules. Back to whole-school mode.")
    return redirect(url_for("schedule"))


def import_enrollment_rows(reader):
    """Upsert enrollments. Returns (imported, skipped)."""
    conn = db.get_db()
    added = skipped = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        sid = row.get("student_id") or row.get("id")
        ptoken = row.get("period") or row.get("period_id")
        if not sid or not ptoken:
            continue
        known = conn.execute(
            "SELECT 1 FROM students WHERE student_id = ?", (sid,)
        ).fetchone()
        if not known:
            skipped += 1
            continue
        # Periods are driven by the import: create one on the fly if this
        # token doesn't match an existing period.
        pid = db.resolve_period(conn, ptoken)
        if pid is None:
            order = (conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM periods"
            ).fetchone()[0])
            cur = conn.execute(
                "INSERT INTO periods (name, start_time, end_time, sort_order) "
                "VALUES (?, '00:00', '00:00', ?)",
                (ptoken.strip().title(), order),
            )
            pid = cur.lastrowid
        conn.execute(
            """
            INSERT INTO enrollments (student_id, period_id, section, room)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, period_id) DO UPDATE SET
                section = excluded.section,
                room = excluded.room
            """,
            (sid, pid, row.get("section") or None, row.get("room") or None),
        )
        added += 1
    conn.commit()
    conn.close()
    return added, skipped


# ---------------------------------------------------------------------------
# Settings (bell schedule)
# ---------------------------------------------------------------------------
@app.route("/settings")
def settings():
    conn = db.get_db()
    schedules = db.list_schedules(conn)
    mode = db.get_setting(conn, "schedule_mode", "auto")
    weekday_map = {wd: db.get_setting(conn, f"wd_{wd}", "") for wd in range(7)}
    # Times per schedule, for the editor.
    sched_times = {}
    for s in schedules:
        sched_times[s["id"]] = conn.execute(
            "SELECT sp.period_id, sp.start_time, sp.end_time, p.name AS period_name "
            "FROM schedule_periods sp JOIN periods p ON p.id = sp.period_id "
            "WHERE sp.schedule_id = ? ORDER BY p.sort_order",
            (s["id"],),
        ).fetchall()
    active = db.active_schedule(conn)
    current = db.current_period(conn)
    cooldown = db.get_setting(conn, "scan_cooldown_ms", "1500")
    conn.close()
    return render_template(
        "settings.html",
        schedules=schedules,
        mode=mode,
        weekday_map=weekday_map,
        weekday_names=db.WEEKDAY_NAMES,
        sched_times=sched_times,
        active_name=active["name"] if active else None,
        current_name=current["name"] if current else None,
        scan_cooldown_ms=cooldown,
    )


@app.post("/settings/mode")
def settings_mode():
    mode = request.form.get("schedule_mode", "auto")
    conn = db.get_db()
    db.set_setting(conn, "schedule_mode", mode)
    conn.commit()
    conn.close()
    labels = {"auto": "Auto (by weekday)", "off": "Off (manual period)"}
    flash(f"Today's schedule set to: {labels.get(mode, mode)}.")
    return redirect(url_for("settings"))


@app.post("/settings/pin")
def settings_pin():
    """Change the staff PIN (requires the current PIN)."""
    current = request.form.get("current_pin", "")
    new = request.form.get("new_pin", "").strip()
    conn = db.get_db()
    pin_hash = db.get_setting(conn, "staff_pin_hash", "")
    if not (pin_hash and check_password_hash(pin_hash, current)):
        conn.close()
        flash("Current PIN is incorrect — PIN not changed.")
        return redirect(url_for("settings"))
    if len(new) < 4 or not new.isdigit():
        conn.close()
        flash("New PIN must be at least 4 digits — PIN not changed.")
        return redirect(url_for("settings"))
    db.set_setting(conn, "staff_pin_hash", generate_password_hash(new))
    conn.commit()
    conn.close()
    flash("Staff PIN updated.")
    return redirect(url_for("settings"))


@app.post("/settings/scan")
def settings_scan():
    """Save the double-scan cooldown (milliseconds)."""
    try:
        ms = max(0, min(10000, int(float(request.form.get("scan_cooldown_ms", "1500")))))
    except (TypeError, ValueError):
        ms = 1500
    conn = db.get_db()
    db.set_setting(conn, "scan_cooldown_ms", str(ms))
    conn.commit()
    conn.close()
    flash(f"Scan cooldown set to {ms} ms.")
    return redirect(url_for("settings"))


@app.post("/settings/weekdays")
def settings_weekdays():
    conn = db.get_db()
    for wd in range(7):
        db.set_setting(conn, f"wd_{wd}", request.form.get(f"wd_{wd}", ""))
    conn.commit()
    conn.close()
    flash("Weekday schedule map saved.")
    return redirect(url_for("settings"))


@app.post("/settings/times")
def settings_times():
    """Save edited period times for one schedule."""
    schedule_id = request.form.get("schedule_id", type=int)
    conn = db.get_db()
    rows = conn.execute(
        "SELECT period_id FROM schedule_periods WHERE schedule_id = ?",
        (schedule_id,),
    ).fetchall()
    for r in rows:
        pid = r["period_id"]
        start = request.form.get(f"start_{pid}", "").strip()
        end = request.form.get(f"end_{pid}", "").strip()
        if start and end:
            conn.execute(
                "UPDATE schedule_periods SET start_time = ?, end_time = ? "
                "WHERE schedule_id = ? AND period_id = ?",
                (start, end, schedule_id, pid),
            )
    conn.commit()
    conn.close()
    flash("Schedule times updated.")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Reports & history
# ---------------------------------------------------------------------------
@app.route("/reports")
def reports():
    """Per-period present/absent summary for a chosen day."""
    day = request.args.get("day") or date.today().isoformat()
    conn = db.get_db()
    periods = db.visible_periods(conn)
    per_period = db.enrollments_exist(conn)
    summary = []
    for p in periods:
        if per_period:
            expected = conn.execute(
                "SELECT COUNT(*) FROM enrollments e "
                "JOIN students s ON s.student_id = e.student_id AND s.active = 1 "
                "WHERE e.period_id = ?",
                (p["id"],),
            ).fetchone()[0]
            present = conn.execute(
                "SELECT COUNT(DISTINCT a.student_id) FROM attendance a "
                "WHERE a.period_id = ? AND a.day = ? AND EXISTS ("
                "  SELECT 1 FROM enrollments e "
                "  WHERE e.student_id = a.student_id AND e.period_id = a.period_id)",
                (p["id"], day),
            ).fetchone()[0]
        else:
            expected = conn.execute(
                "SELECT COUNT(*) FROM students WHERE active = 1"
            ).fetchone()[0]
            present = conn.execute(
                "SELECT COUNT(DISTINCT student_id) FROM attendance "
                "WHERE period_id = ? AND day = ?",
                (p["id"], day),
            ).fetchone()[0]
        rate = round(100 * present / expected) if expected else None
        summary.append(
            {
                "period": p,
                "expected": expected,
                "present": present,
                "absent": max(expected - present, 0),
                "rate": rate,
            }
        )
    conn.close()
    return render_template(
        "reports.html", day=day, summary=summary, per_period=per_period
    )


@app.route("/student/<student_id>")
def student_history(student_id):
    """A single student's check-in history + attendance rate over a range."""
    conn = db.get_db()
    student = conn.execute(
        "SELECT * FROM students WHERE student_id = ?", (student_id,)
    ).fetchone()
    if student is None:
        conn.close()
        return "Student not found", 404

    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (
        date.fromisoformat(end) - timedelta(days=13)
    ).isoformat()

    enrolled = conn.execute(
        "SELECT p.* FROM enrollments e JOIN periods p ON p.id = e.period_id "
        "WHERE e.student_id = ? ORDER BY p.sort_order",
        (student_id,),
    ).fetchall()
    # Proxy for "school days ran" = distinct days with any recorded attendance.
    school_days = conn.execute(
        "SELECT COUNT(DISTINCT day) FROM attendance WHERE day BETWEEN ? AND ?",
        (start, end),
    ).fetchone()[0]

    per_period = []
    for p in enrolled:
        present = conn.execute(
            "SELECT COUNT(DISTINCT day) FROM attendance "
            "WHERE student_id = ? AND period_id = ? AND day BETWEEN ? AND ?",
            (student_id, p["id"], start, end),
        ).fetchone()[0]
        per_period.append(
            {
                "period": p,
                "present": present,
                "school_days": school_days,
                "rate": round(100 * present / school_days) if school_days else None,
            }
        )

    records = conn.execute(
        "SELECT a.*, p.name AS period_name FROM attendance a "
        "JOIN periods p ON p.id = a.period_id "
        "WHERE a.student_id = ? AND a.day BETWEEN ? AND ? "
        "ORDER BY a.day DESC, p.sort_order",
        (student_id, start, end),
    ).fetchall()
    conn.close()
    return render_template(
        "student.html",
        student=student,
        enrolled=enrolled,
        per_period=per_period,
        records=records,
        start=start,
        end=end,
        school_days=school_days,
    )


@app.route("/reports/absences.csv")
def absences_export():
    """CSV of every expected-but-absent slot across a date range."""
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or end
    conn = db.get_db()
    per_period = db.enrollments_exist(conn)
    periods = db.list_periods(conn)
    school_days = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT day FROM attendance WHERE day BETWEEN ? AND ? ORDER BY day",
            (start, end),
        ).fetchall()
    ]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["day", "period", "student_id", "name", "grade"])
    for day in school_days:
        for p in periods:
            if per_period:
                expected = conn.execute(
                    "SELECT s.student_id, s.name, s.grade FROM enrollments e "
                    "JOIN students s ON s.student_id = e.student_id AND s.active = 1 "
                    "WHERE e.period_id = ?",
                    (p["id"],),
                ).fetchall()
            else:
                expected = conn.execute(
                    "SELECT student_id, name, grade FROM students WHERE active = 1"
                ).fetchall()
            for s in expected:
                present = conn.execute(
                    "SELECT 1 FROM attendance "
                    "WHERE student_id = ? AND period_id = ? AND day = ?",
                    (s["student_id"], p["id"], day),
                ).fetchone()
                if not present:
                    w.writerow(
                        [day, p["name"], s["student_id"], s["name"], s["grade"] or ""]
                    )
    conn.close()
    fname = f"absences_{start}_to_{end}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _cli_import(path):
    db.init_db()
    with open(path, newline="", encoding="utf-8-sig") as f:
        n = import_roster_rows(csv.DictReader(f))
    print(f"Imported / updated {n} students from {path}")


def _cli_import_schedule(path):
    db.init_db()
    with open(path, newline="", encoding="utf-8-sig") as f:
        added, skipped = import_enrollment_rows(csv.DictReader(f))
    print(f"Imported / updated {added} enrollments from {path} (skipped {skipped})")


def _cli_reset():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    print("Database reset and re-seeded with the Jordan B-Lunch schedule.")
    print("Re-import your roster and class schedule next.")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "reset":
        _cli_reset()
        sys.exit(0)
    db.init_db()
    if len(sys.argv) >= 3 and sys.argv[1] == "import":
        _cli_import(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "import-schedule":
        _cli_import_schedule(sys.argv[2])
    else:
        from waitress import serve

        host, port = "0.0.0.0", 8000
        print(f"Attendance app running at http://{host}:{port}  (Ctrl+C to stop)")
        print("  Kiosk: /   Attendance: /admin   Reports: /reports   "
              "Roster: /roster   Schedule: /schedule")
        serve(app, host=host, port=port)
