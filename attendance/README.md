# Attendance & ID-Check App (first-run prototype)

A small internal web app to replace the Excel-based attendance/ID-check
workbook. One Python process serves two interfaces on your network:

- **Kiosk** (`/`) — students scan their ID badge to check in. Instant
  green / yellow / red feedback. A barcode/QR scanner just "types" the ID
  and presses Enter, so no drivers or special setup are needed.
- **Live** (`/admin`) — see who *has* and *hasn't* checked in for each
  period, manually check in a student who forgot their ID, fix mistakes,
  and export the report to CSV/Excel.
- **Reports** (`/reports`) — a daily per-period present/absent/rate
  summary, per-student history with attendance rates, and an absence
  export over any date range.
- **Roster** (`/roster`) — import your list of valid IDs from a CSV
  (exported straight from your current Excel file).
- **Schedule** (`/schedule`) — import each student's class schedule so a
  period expects only its enrolled students (per-period class rosters).

The staff pages are protected by a **PIN** (default `1234` — change it on
the Settings page). The kiosk and scanning stay open for students.

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

### One-click start (after the one-time setup above)

Instead of typing the run command each time, double-click the launcher in
the `attendance` folder:

- **macOS:** `start.command`
- **Windows:** `start.bat`

Each one activates the virtual environment and starts the server in a
window; close the window (or press Control-C / Ctrl+C) to stop it. On the
first macOS double-click you may need to right-click → Open once to get
past Gatekeeper.

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
student_id,name
```

You can also include an optional `period` column to enroll students at the
same time (space-separated for several, e.g. `1 3 4`):

```
student_id,name,period
100001,Ava Martinez,2
100002,Liam Johnson,1 3 4
```

Re-importing updates existing students by ID.

## Bell schedule (Settings page)

The app ships pre-loaded with the Jordan High School **B-Lunch** bell
schedule as three named day-schedules, each with its own period times:

- **Enrichment** — Monday, Tuesday, Friday
- **Regular** — Wednesday, Thursday
- **Pep Rally** — special days

The kiosk figures out the current period automatically from the computer's
clock **and the day of week** — it uses the Enrichment times on Mon/Tue/Fri
and the Regular times on Wed/Thu.

Scans in the **passing period before a class** count for the **upcoming**
class: a scan from `scan_lead_minutes` (default 7) before a period starts
through its end is attributed to that period — including before 1st period.
Set the lead to your passing-period length under **Settings → Scanner**. All of this is managed on the
**Settings** page (`/settings`):

- **Today's schedule / mode:**
  - **Auto (by weekday)** — normal behavior, follows the weekday map.
  - **Force a schedule** — pin a specific one for a special day (e.g. Pep
    Rally).
  - **Off** — turns off automatic period detection so staff pick the
    period on the kiosk. Use this for **non-traditional days**; remember to
    switch back to Auto afterward.
- **Weekday map** — which schedule each day of the week uses in Auto mode.
- **Edit schedule times** — adjust any period's start/end time in any
  schedule, right in the browser (24-hour, e.g. 13:48 = 1:48 PM).

Periods themselves are **driven by your import**: whatever periods appear
in the class-schedule CSV are the ones that show up in scanning and
reports (see below). The seeded bell times cover 1st–7th plus Enrichment.

### Starting over with a clean schedule

To wipe the database and re-seed the bell schedule from scratch:

```bash
python app.py reset
```

Then re-import your roster and class schedule.

## Per-period class rosters (schedules)

By default the app runs in **whole-school mode**: every active student is
"expected" every period. Import class schedules to switch to **per-period
rosters**, so each period only expects (and counts absences for) its own
enrolled students.

Export a CSV with these columns and import it on the **Schedule** page
(or with the command below). `section` and `room` are optional; `period`
may be a number (`3`) or a name (`Period 3`):

```
student_id,period,section,room
```

```bash
.venv/bin/python app.py import-schedule sample_schedule.csv
```

## Import from Canvas LMS

The **Roster** page can pull students straight from Canvas (reading the
**People** list / enrollments — not the gradebook, so an inactive gradebook
doesn't matter). Because cross-listed courses hold a **section per period**,
you map **sections** to periods.

1. In Canvas: Account → Settings → **+ New Access Token**, and copy it.
2. On the Roster page, enter the **Canvas site URL**, paste the **API token**
   (never written to disk), and enter your **Course IDs** (the number in each
   course's Canvas URL).
3. Click **List sections** — it shows every section's ID, name, student
   count, and how many students the app can actually **retrieve**, with a
   **Period** box next to each section that is **pre-filled from the section
   name** (e.g. "Bio - P3" → 3). Check/adjust the periods; leave one blank to
   skip that section.
4. Pick the **ID to use for scanning** (default **SIS ID**; **Login ID** is a
   common alternative if it holds the badge number).
5. Click **Preview**. It shows a sample of students with *all* their ID
   fields side by side so you can confirm the highlighted column matches a
   real badge — nothing is imported yet. If that column is blank, your token
   may lack permission to read it; pick a different ID field (Login ID,
   Canvas ID) that is populated.
6. Click **Import** (or "Looks right — import these" under the preview).

Pending/invited enrollments are included, so courses that haven't started or
aren't published yet still work. Re-running updates existing students and
enrollments, so it's safe to run again when your Canvas rosters change.
Students whose chosen ID field is empty are skipped and **listed by name** so
you can see who and why.

IDs are gathered from three sources in order — the bulk course roster (the
same data the People page shows, which carries SIS), then each student's
profile, then their login records — so a student is only skipped if none of
those expose an ID. That usually means a not-yet-activated account: Canvas
may show the SIS on the People page before the API will hand it to a teacher
token. For those, use **Add a student manually** below (type the SIS you see
in People); the import's skipped list has an **Add manually** button that
pre-fills the name and period.

Students who scan into a period they aren't enrolled in still check in
fine — they appear in an "Also checked in — not on this period's roster"
section on the Live page. Use **Clear all schedules** on the Schedule page
to revert to whole-school mode.

## Reports

- **Daily summary** (`/reports`) — present / absent / rate for every
  period on a chosen date, with a per-day absence CSV export.
- **Student history** (`/student/<id>`, or click a name on the Live page)
  — that student's check-in log and per-period attendance rate over a date
  range.
- **Absence export** (`/reports/absences.csv?start=…&end=…`) — every
  expected-but-absent slot across a date range, as CSV.

Attendance *rates* are computed over "school days that actually ran",
approximated as the distinct days with any recorded attendance in the
range (so weekends/holidays with no scans don't count against anyone).

## What's included / what's next

**Included:** valid-ID check, attendance logging (who + when), per-period
who's-in / who's-out live view, **per-period class rosters (schedules)**,
**day-of-week bell schedules with auto-switching + on/off/force modes**,
manual forgot-ID check-in, mistake undo, **daily summary + per-student
history + absence exports**, roster/schedule CSV import/export,
**Canvas LMS import**, **roster management (clear all / activate-deactivate /
delete)**, and multi-station use over the network.

Periods are named `NN - Ordinal Period` (e.g. `01 - First Period`), with
`Enrichment` between 3rd and 4th. A student can belong to several periods —
that's expected; the Roster page's **Periods** column shows each student's
periods so multi-period students are visible, and attendance is tracked
independently per period.

**Not built yet** (easy to add): ID expiration / active-status rules,
in-vs-out direction, student photos on scan, and dashboards/charts.

## Staff PIN

The staff pages (Live, Reports, Roster, Schedule, Settings, and exports)
require a **PIN**; the kiosk (`/`) and scanning stay open for students. The
default PIN is **1234** — log in once and change it under **Settings →
Staff PIN**. The PIN is stored only as a salted hash, and each install
generates its own random session key in `data/secret_key` (kept out of git)
so login cookies can't be forged. Forgot the PIN? Run `python app.py reset`
(wipes data) or ask to have it reset.

## Notes for real deployment

- The app serves via **waitress**, a production-grade WSGI server that
  runs well on Windows — fine to leave running on an internal server.
- Keep it on your internal network (no public internet exposure needed).
- Back up `data/attendance.db` on whatever schedule suits you.
