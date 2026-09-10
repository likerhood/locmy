#!/usr/bin/env bash
# Full SWE workflow: environment setup, metadata validation, controls, repair, tests, analysis.
# Makes paid API calls only after metadata and environment controls pass.
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$RQ4_ROOT/scripts/server_setup.sh"
docker info >/dev/null
bash "$RQ4_ROOT/scripts/install_harness.sh"
exec "$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/run_batch.py" --mode all "$@"
