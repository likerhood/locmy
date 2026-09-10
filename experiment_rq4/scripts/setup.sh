#!/usr/bin/env bash
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$RQ4_ROOT/.venv"
if [[ ! -f "$RQ4_ROOT/.env.local" ]]; then
  cp "$RQ4_ROOT/.env.example" "$RQ4_ROOT/.env.local"
  chmod 600 "$RQ4_ROOT/.env.local"
fi
"$RQ4_ROOT/.venv/bin/python" -c 'import sys; assert sys.version_info >= (3, 10); print("RQ4 standard-library environment ready", sys.version.split()[0])'
# Upstream dependencies are intentionally isolated from mycode. The standalone
# adapter does not import Agentless's Python-only processing or require an SDK.
