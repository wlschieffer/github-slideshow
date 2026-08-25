#!/bin/bash
# Double-click this file in Finder to start the attendance server (macOS).
# It sets up the virtual environment on first run, then starts the server.
cd "$(dirname "$0")"

# Create the virtual environment if it doesn't exist yet (e.g. a fresh copy).
if [ ! -f ".venv/bin/activate" ]; then
  echo "First-time setup: creating the environment (this runs only once)..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
else
  source .venv/bin/activate
fi

# Make sure the required packages are present even if the venv is incomplete.
python -c "import flask, waitress" 2>/dev/null || pip install -r requirements.txt

echo "==================================================="
echo " Attendance server starting..."
echo " Open http://localhost:8000 in your browser."
echo " To stop: close this window or press Control-C."
echo "==================================================="
python app.py
