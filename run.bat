@echo off
rem Kitsu Iko launcher: creates the virtualenv on first run, then starts the app.
rem Pass --demo to try it against a fake in-memory Kitsu.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :error
)
start "" ".venv\Scripts\pythonw.exe" -m kitsu_iko %*
exit /b 0
:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
exit /b 1
