@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -X utf8 -u "%~dp0hunter_menu.py"
pause
