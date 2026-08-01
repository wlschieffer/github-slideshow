@echo off
REM Double-click this file to start the attendance server (Windows).
REM It moves into its own folder, activates the virtual environment, and runs.
cd /d "%~dp0"
call .venv\Scripts\activate
echo ===================================================
echo  Attendance server starting...
echo  Open http://localhost:8000 in your browser.
echo  To stop: close this window or press Ctrl+C.
echo ===================================================
python app.py
