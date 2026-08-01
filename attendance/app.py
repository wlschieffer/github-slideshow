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
import sys
from datetime import date, datetime

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import db

app = Flask(__name__)
app.secret_key = "attendance-local-dev"  # only used for flash messages


@app.before_request
def _ensure_db():
    # Cheap and idempotent; guarantees tables/periods exist on first hit.
    db.init_db()


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
    periods = db.list_periods(conn)
    cur = db.current_period(conn)
    selected = request.args.get("period_id", type=int) or (cur["id"] if cur else None)
    conn.close()
    return render_template(
        "kiosk.html", periods=periods, selected_period=selected, current=cur
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
    periods = db.list_periods(conn)
    cur = db.current_period(conn)
    day = request.args.get("day") or date.today().isoformat()
    period_id = request.args.get("period_id", type=int) or (cur["id"] if cur else (periods[0]["id"] if periods else None))

    rows = []
    present_count = 0
    if period_id is not None:
        rows = conn.execute(
            """
            SELECT s.student_id, s.name, s.grade,
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
# Entry point
# ---------------------------------------------------------------------------
def _cli_import(path):
    db.init_db()
    with open(path, newline="", encoding="utf-8-sig") as f:
        n = import_roster_rows(csv.DictReader(f))
    print(f"Imported / updated {n} students from {path}")


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) >= 3 and sys.argv[1] == "import":
        _cli_import(sys.argv[2])
    else:
        from waitress import serve

        host, port = "0.0.0.0", 8000
        print(f"Attendance app running at http://{host}:{port}  (Ctrl+C to stop)")
        print("  Kiosk:  /        Admin: /admin       Roster: /roster")
        serve(app, host=host, port=port)
