#!/usr/bin/env bash
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$RQ4_ROOT/scripts/supervise_pipeline.py" "$@"
