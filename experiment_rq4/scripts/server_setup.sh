#!/usr/bin/env bash
# Run from any directory after cloning the mycode repository on the server.
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RQ4_ENV_FILE="$RQ4_ROOT/.env.local"
if [[ $# -gt 0 ]]; then
  if [[ $# -ne 2 || "$1" != "--env-file" ]]; then
    echo 'Usage: server_setup.sh [--env-file /absolute/path]' >&2
    exit 2
  fi
  RQ4_ENV_FILE="$2"
fi
python3 "$RQ4_ROOT/scripts/verify_bundle.py"
bash "$RQ4_ROOT/scripts/setup.sh"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/setup_vendor.py" --env-file "$RQ4_ENV_FILE"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/restore_eval_data.py"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/preflight.py" --env-file "$RQ4_ENV_FILE"
echo 'Setup finished. Read preflight blockers before running formal evaluation.'
