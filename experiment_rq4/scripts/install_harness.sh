#!/usr/bin/env bash
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/install_harness.py" "$@"
