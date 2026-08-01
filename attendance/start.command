#!/bin/bash
# Double-click this file in Finder to start the attendance server (macOS).
# It moves into its own folder, activates the virtual environment, and runs.
cd "$(dirname "$0")"
source .venv/bin/activate
echo "==================================================="
echo " Attendance server starting..."
echo " Open http://localhost:8000 in your browser."
echo " To stop: close this window or press Control-C."
echo "==================================================="
python app.py
