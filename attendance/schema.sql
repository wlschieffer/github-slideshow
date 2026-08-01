-- Attendance & ID-check schema (SQLite)
-- One file database: easy to back up, no server to install.

-- The roster of known/authorized students. student_id is the value the
-- barcode/QR scanner types (badge number, ID string, etc.).
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    grade      TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);

-- The class periods attendance is taken against, matched by time of day.
CREATE TABLE IF NOT EXISTS periods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    start_time TEXT NOT NULL,   -- "HH:MM", 24h
    end_time   TEXT NOT NULL,   -- "HH:MM", 24h
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- One row per check-in. A student can only be marked present once per
-- period per day (enforced by the UNIQUE index below).
CREATE TABLE IF NOT EXISTS attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    period_id  INTEGER NOT NULL,
    day        TEXT NOT NULL,   -- "YYYY-MM-DD"
    scanned_at TEXT NOT NULL,   -- ISO timestamp
    method     TEXT NOT NULL DEFAULT 'scan',  -- 'scan' or 'manual'
    FOREIGN KEY (student_id) REFERENCES students(student_id),
    FOREIGN KEY (period_id)  REFERENCES periods(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_unique
    ON attendance (student_id, period_id, day);

CREATE INDEX IF NOT EXISTS idx_attendance_lookup
    ON attendance (day, period_id);
