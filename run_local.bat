@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Centre de formation - version locale ===
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo Creation de l environnement Python...
  %PY% -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements-local.txt -q
set SECRET_KEY=cle-locale-de-test
set DATABASE_URL=
set APP_MODE=local
if not exist instance\centre.db (
  echo Creation de la base de demonstration...
  python seed_demo.py
)
echo.
echo Ouvrez http://localhost:5000  -  compte : admin@demo.dz / demo1234
echo Fermez cette fenetre pour arreter l application.
start "" http://localhost:5000
python app.py
pause
