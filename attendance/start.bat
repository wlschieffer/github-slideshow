@echo off
REM Double-click this file to start the attendance server (Windows).
REM It sets up the virtual environment on first run, then starts the server.
cd /d "%~dp0"

REM Create the virtual environment if it doesn't exist yet (e.g. a fresh copy).
if not exist ".venv\Scripts\activate" (
  echo First-time setup: creating the environment ^(this runs only once^)...
  python -m venv .venv
  call .venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)

REM Make sure the required packages are present even if the venv is incomplete.
python -c "import flask, waitress" 2>nul || pip install -r requirements.txt

echo ===================================================
echo  Attendance server starting...
echo  Open http://localhost:8000 in your browser.
echo  To stop: close this window or press Ctrl+C.
echo ===================================================
python app.py
