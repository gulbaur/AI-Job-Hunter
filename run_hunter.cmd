@echo off
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0hunter_web.py"
