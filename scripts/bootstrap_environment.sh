#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  python3 -m venv "$ROOT/.venv"
fi

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r "$ROOT/requirements.lock"
"$PYTHON_BIN" -m pip install --no-deps -e "$ROOT"
"$PYTHON_BIN" -m playwright install chromium

cat <<EOF
Python dependencies and Chromium are installed.

Install Linux browser libraries once (this command requests sudo):
  $PYTHON_BIN -m playwright install-deps chromium

Then verify the complete runtime:
  $PYTHON_BIN $ROOT/scripts/runtime_preflight.py --require-browser
EOF
