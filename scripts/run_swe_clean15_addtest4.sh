#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RUN_ID="${RUN_ID:-swe_clean15_addtest4_$(date +%Y%m%d_%H%M%S)}"
export RESUME="${RESUME:-0}"
exec bash "$ROOT/scripts/run_swe_clean15_addtest.sh" "$@"
