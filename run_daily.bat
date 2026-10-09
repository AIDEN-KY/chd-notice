@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py send-now
exit /b %errorlevel%
