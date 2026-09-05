@echo off
setlocal
cd /d "%~dp0.."
python -m pip install -r backend\requirements.txt
python backend\app\data_gen.py
start "GST Invoice Matcher" cmd /c "python -m uvicorn app.main:app --app-dir backend --port 8000"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
endlocal