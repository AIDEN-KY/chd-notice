@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Project Python environment is missing.
    exit /b 2
)
".venv\Scripts\python.exe" main.py send-now
exit /b %errorlevel%
