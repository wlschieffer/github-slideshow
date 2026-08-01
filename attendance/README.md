# Attendance & ID-Check App (first-run prototype)

A small internal web app to replace the Excel-based attendance/ID-check
workbook. One Python process serves two interfaces on your network:

- **Kiosk** (`/`) — students scan their ID badge to check in. Instant
  green / yellow / red feedback. A barcode/QR scanner just "types" the ID
  and presses Enter, so no drivers or special setup are needed.
- **Staff** (`/admin`) — see who *has* and *hasn't* checked in for each
  period, manually check in a student who forgot their ID, fix mistakes,
  and export the report to CSV/Excel.
- **Roster** (`/roster`) — import your list of valid IDs from a CSV
  (exported straight from your current Excel file).

Data lives in **one SQLite file** (`data/attendance.db`) — back it up by
copying that file. No database server to install or maintain.

## Run it (2 minutes)

```bash
cd attendance

# 1. One-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt         # Windows: .venv\Scripts\pip

# 2. (Optional) load the sample roster to try it out
.venv/bin/python app.py import sample_roster.csv  # Windows: .venv\Scripts\python

# 3. Start the server
.venv/bin/python app.py
```

Then open:

- Student scan station → `http://<this-computer-ip>:8000/`
- Your staff/reports machine → `http://<this-computer-ip>:8000/admin`

Both machines just point a browser at the same server — that's the
two-sided setup you asked for. Find `<this-computer-ip>` with `ipconfig`
(Windows) or `ip addr` (Linux/Mac).

## Loading your real students

Export your Excel roster to a CSV with these column headers and import it
on the **Roster** page (or with the `import` command above):

```
student_id,name,grade
```

`grade` is optional. Re-importing updates existing students by ID.

## Class periods

A default bell schedule (Period 1–7) is seeded on first run. The kiosk
picks the current period automatically from the computer's clock, and
staff can view any period/date. To change the schedule, edit
`DEFAULT_PERIODS` in `db.py` before first run, or edit the `periods` table.

## What this first run does / doesn't do yet

**Included:** valid-ID check, attendance logging (who + when),
per-period who's-in / who's-out reports, manual forgot-ID check-in,
mistake undo, CSV import/export, multi-station over the network.

**Deliberately deferred** (easy to add once you've tried it): per-student
class schedules (so each period expects only its own roster), ID
expiration rules, in/out direction, student photos on scan, dashboards,
and staff logins. These are the things worth deciding *after* a first run.

## Notes for real deployment

- The app serves via **waitress**, a production-grade WSGI server that
  runs well on Windows — fine to leave running on an internal server.
- Keep it on your internal network (no public internet exposure needed).
- Back up `data/attendance.db` on whatever schedule suits you.
