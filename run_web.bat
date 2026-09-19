@echo off
REM CBMP Dashboard — run as a web app (Windows)
REM Double-click this file, then open the URL shown (usually http://localhost:8501)

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo Tick "Add Python to PATH" during install.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q

if not defined CBMP_BOOTSTRAP_PASSWORD (
  echo TIP: set CBMP_BOOTSTRAP_PASSWORD before first run for a known admin password.
)

echo.
echo Starting CBMP web app...
echo Open in your browser:  http://localhost:8501
echo On the same network others can use:  http://YOUR-PC-IP:8501
echo Press Ctrl+C to stop.
echo.

streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --browser.gatherUsageStats false

pause
