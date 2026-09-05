#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python -m pip install -r backend/requirements.txt
python backend/app/data_gen.py
python -m uvicorn app.main:app --app-dir backend --port 8000 &
SERVER_PID=$!
trap 'kill "$SERVER_PID"' EXIT
sleep 2
python -c 'import webbrowser; webbrowser.open("http://127.0.0.1:8000")'
wait "$SERVER_PID"