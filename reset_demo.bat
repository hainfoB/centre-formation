@echo off
chcp 65001 >nul
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set DATABASE_URL=
set APP_MODE=local
echo Cette operation efface la base locale et recree les donnees de demonstration.
pause
python seed_demo.py
pause
