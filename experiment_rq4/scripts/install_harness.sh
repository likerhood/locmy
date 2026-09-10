#!/usr/bin/env bash
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RQ4_COMMIT="$("$RQ4_ROOT/.venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1]))["commit"])' "$RQ4_ROOT/configs/harness_lock.json")"
"$RQ4_ROOT/.venv/bin/python" -m pip install "git+https://github.com/SWE-bench/SWE-bench.git@$RQ4_COMMIT"
mkdir -p "$RQ4_ROOT/reports"
"$RQ4_ROOT/.venv/bin/python" -m pip freeze > "$RQ4_ROOT/reports/server_dependencies.txt"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/prepare_harness_dataset.py"
