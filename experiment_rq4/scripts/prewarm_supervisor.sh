#!/usr/bin/env bash
set -uo pipefail

ROOT=/data2/like/locmy/experiment_rq4
RUN_ID=swe50-image-prewarm-v1
RUN_DIR="$ROOT/runs/prewarm/$RUN_ID"
STATE="$RUN_DIR/state.json"
LOG="$RUN_DIR/supervisor.log"
MAX_RESTARTS=10

mkdir -p "$RUN_DIR"
echo $$ > "$RUN_DIR/supervisor.pid"
exec >> "$LOG" 2>&1

for ((attempt=0; attempt<=MAX_RESTARTS; attempt++)); do
  echo "$(date -Is) worker_start attempt=$attempt"
  env DOCKER_HOST=unix:///run/user/1000/docker.sock \
    "$ROOT/.venv/bin/python" "$ROOT/scripts/prewarm_images.py" \
      --run-id "$RUN_ID" \
      --min-free-gb 25 \
      --env-file /data2/like/locmy/.envaliyun-qwen.local &
  worker=$!
  echo "$worker" > "$RUN_DIR/launcher.pid"
  wait "$worker"
  rc=$?
  status=$("$ROOT/.venv/bin/python" -c \
    'import json,sys; print(json.load(open(sys.argv[1])).get("status", "unknown"))' \
    "$STATE" 2>/dev/null || echo unknown)
  echo "$(date -Is) worker_exit rc=$rc status=$status attempt=$attempt"
  case "$status" in
    completed|paused_storage)
      exit 0
      ;;
  esac
  if (( attempt == MAX_RESTARTS )); then
    echo "$(date -Is) restart_limit_reached"
    exit 1
  fi
  sleep 600
done
