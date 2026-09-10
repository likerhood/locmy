#!/usr/bin/env bash
# Examples: --mode check; --mode generate --limit 1; --mode all --run-id swe50-v1
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$RQ4_ROOT/.venv/bin/python" ]]; then
  bash "$RQ4_ROOT/scripts/setup.sh"
fi
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/run_batch.py" "$@"
