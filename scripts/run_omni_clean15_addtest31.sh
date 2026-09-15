#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DEEP_AGENT="${DEEP_AGENT:-1}"
export LIGHTWEIGHT="${LIGHTWEIGHT:-0}"
export STRUCTURE_ONLY="${STRUCTURE_ONLY:-1}"
export DYNAMIC_ROUNDS="${DYNAMIC_ROUNDS:-12}"
export MAX_TOOL_ROUNDS="${MAX_TOOL_ROUNDS:-6}"
export MAX_REACT_STEPS="${MAX_REACT_STEPS:-10}"
export MYCODE_REVIEW_BACKFILL_LIMIT="${MYCODE_REVIEW_BACKFILL_LIMIT:-2}"
if [[ -z "${SAMPLES:-}" && -f "$ROOT/data/omnigirl-full-candidates.clean15.v458.samples.jsonl" ]]; then
  export SAMPLES="$ROOT/data/omnigirl-full-candidates.clean15.v458.samples.jsonl"
fi
export RUN_ID="${RUN_ID:-omni_clean15_addtest31_$(date +%Y%m%d_%H%M%S)}"
export RESUME="${RESUME:-0}"
exec bash "$ROOT/newtest/run_omni_clean15_agent_full.sh" "$@"
