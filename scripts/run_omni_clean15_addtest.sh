#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MAGNET_DATASET_KIND=omni
exec bash "$ROOT/scripts/run_swe_clean15_addtest.sh" "$@"
