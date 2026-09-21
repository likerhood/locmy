#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dataset=swe
arm=full
tag=v1
profile=
dry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset|--arm|--tag|--env-file)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in
        --dataset) dataset="$2";;
        --arm) arm="$2";;
        --tag) tag="$2";;
        --env-file) profile="$2";;
      esac
      shift 2;;
    --dry-run) dry=1; shift;;
    -h|--help)
      echo 'Usage: bash scripts/run_ablation_qwen.sh --dataset swe|omni --arm ARM --env-file PATH [--tag v1] [--dry-run]'
      echo 'Arms: full no_visual no_graph no_flow no_graph_flow no_closure fixed_react no_head no_checkpoint'
      exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
[[ -n "$profile" ]] || { echo '--env-file is required' >&2; exit 2; }
source "$ROOT/newtest/model_config.sh"
parse_model_cli_args "$0" --env-file "$profile"
load_required_model_environment "$0"
export MAGNET_ABLATION="$arm"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo 'Set PYTHON_BIN to an existing environment interpreter' >&2; exit 2; }
exec "$PYTHON_BIN" "$ROOT/scripts/run_ablation_qwen.py" \
  --dataset "$dataset" --arm "$arm" --tag "$tag" --env-file "$profile" --dry-run "$dry"
