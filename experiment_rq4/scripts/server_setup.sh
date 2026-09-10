#!/usr/bin/env bash
# Run from any directory after cloning the mycode repository on the server.
set -euo pipefail
RQ4_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$RQ4_ROOT/scripts/setup.sh"
mkdir -p "$RQ4_ROOT/vendor"
if [[ ! -d "$RQ4_ROOT/vendor/Agentless" ]]; then
  git clone https://github.com/OpenAutoCoder/Agentless.git "$RQ4_ROOT/vendor/Agentless"
fi
RQ4_AGENTLESS_COMMIT="$("$RQ4_ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["Agentless"]["commit"])' "$RQ4_ROOT/configs/vendor_lock.json")"
if [[ -n "$(git -C "$RQ4_ROOT/vendor/Agentless" status --porcelain)" ]]; then
  echo 'Agentless contains local edits; preserve them before switching to the locked commit.' >&2
  exit 1
fi
if ! git -C "$RQ4_ROOT/vendor/Agentless" cat-file -e "$RQ4_AGENTLESS_COMMIT^{commit}" 2>/dev/null; then
  git -C "$RQ4_ROOT/vendor/Agentless" fetch origin "$RQ4_AGENTLESS_COMMIT"
fi
git -C "$RQ4_ROOT/vendor/Agentless" checkout --detach "$RQ4_AGENTLESS_COMMIT"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/restore_eval_data.py"
"$RQ4_ROOT/.venv/bin/python" "$RQ4_ROOT/scripts/preflight.py" --env-file "$RQ4_ROOT/.env.local"
echo 'Setup finished. Read preflight blockers before running formal evaluation.'
