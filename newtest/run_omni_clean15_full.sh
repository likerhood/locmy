#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOC_CODE_ROOT="${LOC_CODE_ROOT:-$(cd "$ROOT/../.." && pwd)}"
SAMPLES="${SAMPLES:-$LOC_CODE_ROOT/clean_subsets_new/omnigirl-full-candidates.clean15.v458.samples.jsonl}"
DATASET="${DATASET:-omnigirl-full-candidates-clean15}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  DEFAULT_PYTHON_BIN="$ROOT/.venv/bin/python"
else
  DEFAULT_PYTHON_BIN="python3"
fi
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON_BIN}"

# shellcheck disable=SC1091
source "$ROOT/newtest/model_config.sh"
ENV_FILE="${ENV_FILE:-$ROOT/.env.local}"

if [[ "${LOAD_ENV_FILE:-1}" == "1" && -f "$ENV_FILE" ]]; then
  load_env_defaults "$ENV_FILE"
fi

MODEL_LABEL="${MODEL_NAME:-${MODEL_API_NAME:-offline}}"
MODEL_LABEL="$(printf '%s' "$MODEL_LABEL" | sed 's/[^A-Za-z0-9_.-]/_/g')"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/newtest/results/$DATASET/${MODEL_LABEL}_$RUN_ID}"
CACHE_DIR="${CACHE_DIR:-$OUTPUT_DIR/tool_cache}"

if [[ "${DEEP_AGENT:-0}" == "1" && "${LIGHTWEIGHT:-0}" != "1" ]]; then
  export DYNAMIC_ROUNDS="${DYNAMIC_ROUNDS:-2}"
  export MAX_TOOL_ROUNDS="${MAX_TOOL_ROUNDS:-1}"
  export MAX_REACT_STEPS="${MAX_REACT_STEPS:-4}"
  export MYCODE_DEEP_GRAPH_SCOPE="${MYCODE_DEEP_GRAPH_SCOPE:-120}"
  export MYCODE_DEEP_PREFILTER_QUERY_BUDGET="${MYCODE_DEEP_PREFILTER_QUERY_BUDGET:-48}"
  export MYCODE_DEEP_PREFILTER_MULTIPLIER="${MYCODE_DEEP_PREFILTER_MULTIPLIER:-6}"
  export MYCODE_DEEP_SAME_DIR_PER_SEED="${MYCODE_DEEP_SAME_DIR_PER_SEED:-4}"
  export MYCODE_DEEP_SAME_DIR_BUDGET="${MYCODE_DEEP_SAME_DIR_BUDGET:-24}"
  export MYCODE_DEEP_PATH_PROBE_LIMIT="${MYCODE_DEEP_PATH_PROBE_LIMIT:-90}"
  export MYCODE_DEEP_PATH_SEMANTIC_BUDGET="${MYCODE_DEEP_PATH_SEMANTIC_BUDGET:-60}"
  export MYCODE_ROUND_POOL_LIMIT="${MYCODE_ROUND_POOL_LIMIT:-96}"
  export MYCODE_ROUND_GLOBAL_FILE_HITS="${MYCODE_ROUND_GLOBAL_FILE_HITS:-24}"
  export MYCODE_ROUND_GLOBAL_ENTITY_HITS="${MYCODE_ROUND_GLOBAL_ENTITY_HITS:-36}"
  export MYCODE_ROUND_READ_BUDGET="${MYCODE_ROUND_READ_BUDGET:-24}"
  export MYCODE_ROUND_PATH_PROBE_LIMIT="${MYCODE_ROUND_PATH_PROBE_LIMIT:-80}"
  export MYCODE_ROUND_PATH_PROBE_DIR_PER_SEED="${MYCODE_ROUND_PATH_PROBE_DIR_PER_SEED:-5}"
  export MYCODE_ROUND_PATH_PROBE_DIR_BUDGET="${MYCODE_ROUND_PATH_PROBE_DIR_BUDGET:-36}"
  export MYCODE_ROUND_PATH_SEMANTIC_LIMIT="${MYCODE_ROUND_PATH_SEMANTIC_LIMIT:-40}"
  export MYCODE_ROUND_SEMANTIC_DIR_SEEDS="${MYCODE_ROUND_SEMANTIC_DIR_SEEDS:-12}"
  export MYCODE_ROUND_SEMANTIC_DIR_PER_SEED="${MYCODE_ROUND_SEMANTIC_DIR_PER_SEED:-6}"
  export MYCODE_ROUND_SEMANTIC_DIR_BUDGET="${MYCODE_ROUND_SEMANTIC_DIR_BUDGET:-36}"
  export MYCODE_EVIDENCE_SEED_TARGET_MULTIPLIER="${MYCODE_EVIDENCE_SEED_TARGET_MULTIPLIER:-0.22}"
  export MYCODE_EVIDENCE_SEED_TARGET_CAP="${MYCODE_EVIDENCE_SEED_TARGET_CAP:-450}"
  export MYCODE_CODE_CONTEXT_LIMIT="${MYCODE_CODE_CONTEXT_LIMIT:-12}"
  export MYCODE_LLM_CANDIDATE_REVIEW="${MYCODE_LLM_CANDIDATE_REVIEW:-1}"
  export MYCODE_LLM_REVIEW_CANDIDATES="${MYCODE_LLM_REVIEW_CANDIDATES:-6}"
  export MYCODE_LLM_REVIEW_REPAIR_ATTEMPTS="${MYCODE_LLM_REVIEW_REPAIR_ATTEMPTS:-1}"
  export MYCODE_MULTI_CHANNEL_RECALL="${MYCODE_MULTI_CHANNEL_RECALL:-1}"
  export MYCODE_FLOW_POOL_LIMIT="${MYCODE_FLOW_POOL_LIMIT:-48}"
  export MYCODE_FLOW_TRACE_LIMIT="${MYCODE_FLOW_TRACE_LIMIT:-4}"
  export MYCODE_ARCHITECTURE_PATH_WEIGHT="${MYCODE_ARCHITECTURE_PATH_WEIGHT:-1.0}"
  export MYCODE_ROUND_ARCHITECTURE_PATH_LIMIT="${MYCODE_ROUND_ARCHITECTURE_PATH_LIMIT:-16}"
  export MYCODE_ARCHITECTURE_CORROBORATION="${MYCODE_ARCHITECTURE_CORROBORATION:-1}"
  export MYCODE_BEST_ROUND_CHECKPOINT="${MYCODE_BEST_ROUND_CHECKPOINT:-1}"
  export MYCODE_GRAPH_MAX_SEED_NODES="${MYCODE_GRAPH_MAX_SEED_NODES:-8}"
  export MYCODE_GRAPH_MAX_EDGES_PER_SEED="${MYCODE_GRAPH_MAX_EDGES_PER_SEED:-30}"
  export MYCODE_GRAPH_BEAM_WIDTH="${MYCODE_GRAPH_BEAM_WIDTH:-60}"
fi

args=(
  --samples "$SAMPLES"
  --dataset "$DATASET"
  --output-dir "$OUTPUT_DIR"
  --cache-dir "$CACHE_DIR"
  --top-k "${TOP_K:-15}"
  --max-tool-rounds "${MAX_TOOL_ROUNDS:-2}"
  --dynamic-rounds "${DYNAMIC_ROUNDS:-3}"
  --max-react-steps "${MAX_REACT_STEPS:-0}"
  --sample-timeout-seconds "${SAMPLE_TIMEOUT_SECONDS:-3000}"
)

if [[ "${FULL_MM:-0}" == "1" ]]; then
  export ALLOW_NETWORK="${ALLOW_NETWORK:-1}"
  export ALLOW_BROWSER="${ALLOW_BROWSER:-1}"
  export DOWNLOAD_IMAGES="${DOWNLOAD_IMAGES:-1}"
  export USE_VLM="${USE_VLM:-1}"
fi

if [[ "${MAX_SAMPLES:-0}" != "0" ]]; then
  args+=(--max-samples "$MAX_SAMPLES")
fi
if [[ -n "${INSTANCE_ID:-}" ]]; then
  args+=(--instance-id "$INSTANCE_ID")
fi
if [[ "${START_INDEX:-0}" != "0" ]]; then
  args+=(--start-index "$START_INDEX")
fi
if [[ "${RESUME:-1}" == "1" ]]; then
  args+=(--resume)
fi
if [[ "${FORCE_RERUN:-0}" == "1" ]]; then
  args+=(--force)
fi
if [[ "${FAIL_FAST:-0}" == "1" ]]; then
  args+=(--fail-fast)
fi
if [[ "${ALLOW_NETWORK:-0}" == "1" ]]; then
  args+=(--allow-network)
fi
if [[ "${ALLOW_BROWSER:-0}" == "1" ]]; then
  args+=(--allow-browser)
fi
if [[ "${DOWNLOAD_IMAGES:-0}" == "1" ]]; then
  args+=(--download-images)
fi
if [[ "${USE_VLM:-0}" == "1" ]]; then
  args+=(--use-vlm)
fi
if [[ "${USE_LLM:-0}" == "1" ]]; then
  args+=(--use-llm)
fi
if [[ "${USE_LLM_PLANNING:-${USE_LLM:-0}}" == "1" ]]; then
  args+=(--use-llm-planning)
fi
if [[ "${USE_LLM_CONTROLLER:-${USE_LLM:-0}}" == "1" ]]; then
  args+=(--use-llm-controller)
fi
if [[ "${STRUCTURE_ONLY:-1}" == "1" ]]; then
  args+=(--structure-only)
fi
if [[ "${LIGHTWEIGHT:-1}" == "1" ]]; then
  args+=(--lightweight)
fi
if [[ "${VERBOSE_AGENT_LOG:-0}" == "1" ]]; then
  args+=(--verbose-agent-log)
fi
if [[ -n "${VERBOSE_AGENT_LIMIT:-}" ]]; then
  args+=(--verbose-agent-limit "$VERBOSE_AGENT_LIMIT")
fi
if [[ -n "${VERBOSE_LLM_TEXT_LIMIT:-}" ]]; then
  args+=(--verbose-llm-text-limit "$VERBOSE_LLM_TEXT_LIMIT")
fi
if [[ "${SAMPLE_HEARTBEAT_INTERVAL:-30}" != "0" ]]; then
  args+=(--sample-heartbeat-interval "${SAMPLE_HEARTBEAT_INTERVAL:-30}")
fi

export LOC_CODE_ROOT
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
mkdir -p "$OUTPUT_DIR"

echo "Run mycode on OmniGIRL Clean15 full"
echo "Root: $ROOT"
echo "Samples: $SAMPLES"
echo "Dataset: $DATASET"
echo "Output: $OUTPUT_DIR"
echo "Model label: $MODEL_LABEL"
echo "Use LLM: ${USE_LLM:-0}; network/browser/vlm: ${ALLOW_NETWORK:-0}/${ALLOW_BROWSER:-0}/${USE_VLM:-0}; structure_only: ${STRUCTURE_ONLY:-1}; repo_auto_fetch: ${MYCODE_AUTO_FETCH_REPOS:-1}; lightweight: ${LIGHTWEIGHT:-1}"
echo "Budgets: top_k=${TOP_K:-15}; dynamic_rounds=${DYNAMIC_ROUNDS:-3}; max_tool_rounds=${MAX_TOOL_ROUNDS:-2}; max_react_steps=${MAX_REACT_STEPS:-auto}; full_mm=${FULL_MM:-0}; download_images=${DOWNLOAD_IMAGES:-0}"
echo "Deep graph: scope=${MYCODE_DEEP_GRAPH_SCOPE:-120}; prefilter_multiplier=${MYCODE_DEEP_PREFILTER_MULTIPLIER:-8}; same_dir_per_seed=${MYCODE_DEEP_SAME_DIR_PER_SEED:-6}; phase_log=${MYCODE_PHASE_LOG:-1}"
echo "Deep budgets: pool=${MYCODE_ROUND_POOL_LIMIT:-160}; global_file=${MYCODE_ROUND_GLOBAL_FILE_HITS:-36}; global_entity=${MYCODE_ROUND_GLOBAL_ENTITY_HITS:-60}; read=${MYCODE_ROUND_READ_BUDGET:-auto}; flow_pool=${MYCODE_FLOW_POOL_LIMIT:-80}; flow_limit=${MYCODE_FLOW_TRACE_LIMIT:-6}; graph_seed=${MYCODE_GRAPH_MAX_SEED_NODES:-10}; graph_edges=${MYCODE_GRAPH_MAX_EDGES_PER_SEED:-45}; graph_beam=${MYCODE_GRAPH_BEAM_WIDTH:-80}"
echo "CoSIL-style discipline: source_first=${MYCODE_SOURCE_FIRST_FILTER:-1}; noise_mult=${MYCODE_NOISE_PATH_MULTIPLIER:-0.18}; generated_mult=${MYCODE_GENERATED_PATH_MULTIPLIER:-0.06}; addon_mult=${MYCODE_ADDON_BUNDLE_MULTIPLIER:-0.24}; noise_cap=${MYCODE_NOISE_PATH_SCORE_CAP:-900}; generated_cap=${MYCODE_GENERATED_PATH_SCORE_CAP:-120}; addon_cap=${MYCODE_ADDON_BUNDLE_SCORE_CAP:-450}; source_boost=${MYCODE_SOURCE_FILE_BOOST:-1.18}; early_stop=${MYCODE_EARLY_STOP_HIGH_CONFIDENCE:-1}; high_conf=${MYCODE_HIGH_CONFIDENCE_THRESHOLD:-0.78}; skip_followup=${MYCODE_SKIP_FOLLOWUP_ON_EARLY_STOP:-1}; flow_mode=${MYCODE_FLOW_MODE:-light}; deep_flow_auto=${MYCODE_DEEP_FLOW_AUTO:-0}"
echo "Deep evidence policy: seed_multiplier=${MYCODE_EVIDENCE_SEED_TARGET_MULTIPLIER:-default}; seed_cap=${MYCODE_EVIDENCE_SEED_TARGET_CAP:-default}; semantic_dir_budget=${MYCODE_ROUND_SEMANTIC_DIR_BUDGET:-default}"
echo "Candidate convergence: multi_channel=${MYCODE_MULTI_CHANNEL_RECALL:-1}; review=${MYCODE_LLM_CANDIDATE_REVIEW:-0}; review_candidates=${MYCODE_LLM_REVIEW_CANDIDATES:-6}; repair_attempts=${MYCODE_LLM_REVIEW_REPAIR_ATTEMPTS:-1}"
echo "Terminal trace: verbose_agent_log=${VERBOSE_AGENT_LOG:-0}; verbose_agent_limit=${VERBOSE_AGENT_LIMIT:-5}; verbose_llm_text_limit=${VERBOSE_LLM_TEXT_LIMIT:-500}"
echo "Sample heartbeat: ${SAMPLE_HEARTBEAT_INTERVAL:-30}s (set SAMPLE_HEARTBEAT_INTERVAL=0 to disable)"

if [[ "${MYCODE_RUNTIME_PREFLIGHT:-1}" == "1" ]]; then
  preflight_args=(--json)
  if [[ "${ALLOW_BROWSER:-0}" == "1" && "${MYCODE_BROWSER_REQUIRED:-0}" == "1" ]]; then
    preflight_args+=(--require-browser)
  fi
  if ! "$PYTHON_BIN" "$ROOT/scripts/runtime_preflight.py" "${preflight_args[@]}" > "$OUTPUT_DIR/runtime_capabilities.json"; then
    cat "$OUTPUT_DIR/runtime_capabilities.json"
    echo "Runtime preflight failed. Install the reported dependencies or set MYCODE_BROWSER_REQUIRED=0 for an explicitly degraded run." >&2
    exit 2
  fi
  cat "$OUTPUT_DIR/runtime_capabilities.json"
fi

"$PYTHON_BIN" "$ROOT/newtest/run_clean15_dataset.py" "${args[@]}" 2>&1 | tee "$OUTPUT_DIR/run.log"
