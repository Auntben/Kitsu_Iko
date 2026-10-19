@echo off
rem Builds dist\KitsuIko.exe (single file, no Python needed to run it).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt || goto :error
".venv\Scripts\python.exe" build.py || goto :error
echo.
echo Done: dist\KitsuIko.exe
pause
exit /b 0
:error
echo Build failed.
pause
exit /b 1
