#!/usr/bin/env bash

# Shared model/environment configuration for Clean15 launchers.

load_env_defaults() {
  local env_file="$1"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
      export "$key=$value"
    fi
  done < "$env_file"
}

print_model_cli_usage() {
  local command_name="$1"
  cat <<EOF
Usage: $command_name [model options]

Model options:
  --model NAME             Set both the display label and API model id.
  --model-label LABEL      Set MODEL_NAME (used in output directory names).
  --model-api-name ID      Set MODEL_API_NAME (sent to the API).
  --vlm-image-transport M  Set image transport: data_uri, url, or auto.
  --env-file PATH          Load defaults from PATH instead of .env.local.
  --no-env-file            Do not load an env file.
  --print-model-config     Print resolved non-secret model config and exit.
  -h, --help               Show this help and exit.

Explicit CLI options and inherited environment variables take precedence over
values loaded from the env file.
EOF
}

parse_model_cli_args() {
  local command_name="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)
        [[ $# -ge 2 ]] || { echo "Missing value for --model" >&2; return 2; }
        export MODEL_NAME="$2"
        export MODEL_API_NAME="$2"
        shift 2
        ;;
      --model-label)
        [[ $# -ge 2 ]] || { echo "Missing value for --model-label" >&2; return 2; }
        export MODEL_NAME="$2"
        shift 2
        ;;
      --model-api-name)
        [[ $# -ge 2 ]] || { echo "Missing value for --model-api-name" >&2; return 2; }
        export MODEL_API_NAME="$2"
        shift 2
        ;;
      --vlm-image-transport)
        [[ $# -ge 2 ]] || { echo "Missing value for --vlm-image-transport" >&2; return 2; }
        case "$2" in
          data_uri|url|auto) export MYCODE_VLM_IMAGE_TRANSPORT="$2" ;;
          *) echo "Invalid VLM image transport: $2 (expected data_uri, url, or auto)" >&2; return 2 ;;
        esac
        shift 2
        ;;
      --env-file)
        [[ $# -ge 2 ]] || { echo "Missing value for --env-file" >&2; return 2; }
        export ENV_FILE="$2"
        export LOAD_ENV_FILE=1
        shift 2
        ;;
      --no-env-file)
        export LOAD_ENV_FILE=0
        shift
        ;;
      --print-model-config)
        export PRINT_MODEL_CONFIG=1
        shift
        ;;
      -h|--help)
        print_model_cli_usage "$command_name"
        export MODEL_HELP_ONLY=1
        return 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        print_model_cli_usage "$command_name" >&2
        return 2
        ;;
    esac
  done
}

print_resolved_model_config() {
  local env_file="$1"
  local model_label="$2"
  echo "Env file: $env_file"
  echo "Load env file: ${LOAD_ENV_FILE:-1}"
  echo "Base URL: ${BASE_URL:-<unset>}"
  echo "Model label: $model_label"
  echo "Model API name: ${MODEL_API_NAME:-${MODEL_NAME:-<unset>}}"
  echo "Planning model: ${PLANNING_MODEL_API_NAME:-<global>}"
  echo "Evidence model: ${EVIDENCE_MODEL_API_NAME:-<global>}"
  echo "Controller model: ${CONTROLLER_MODEL_API_NAME:-<global>}"
  echo "VLM model: ${VLM_MODEL_API_NAME:-<global>}"
  echo "VLM image transport: ${MYCODE_VLM_IMAGE_TRANSPORT:-data_uri}"
}
