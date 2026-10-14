#!/usr/bin/env bash
# Version locale (Linux / macOS)
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements-local.txt
export SECRET_KEY=cle-locale-de-test
unset DATABASE_URL
export APP_MODE=local
[ -f instance/centre.db ] || python seed_demo.py
echo "Ouvrez http://localhost:5000 — compte : admin@demo.dz / demo1234"
python app.py
