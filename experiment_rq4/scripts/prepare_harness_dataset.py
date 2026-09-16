#!/usr/bin/env python3
"""Join frozen public evaluator metadata, refusing mismatched instances/commits.

Downloads official dataset to evaluation side only. Never supplies it to repair.
"""
import json
import ast
import gzip
import hashlib
from pathlib import Path
from run_batch import validate_eval

ROOT=Path(__file__).resolve().parents[1]


# The pinned multimodal snapshot uses this helper in every eval script. The
# recursive chmod is redundant because the locked SWE-bench harness executes
# /eval.sh as root, and it is extremely expensive for large node_modules trees
# on overlay2. Keep the upstream snapshot immutable and apply this exact,
# audited runtime adapter only to the generated evaluation-only dataset.
_DEPENDENCY_SETUP_OLD = (
    'if ! git diff --quiet HEAD -- package.json 2>/dev/null; then '
    'echo "package.json changed by patch; re-syncing dependencies"; '
    'export PUPPETEER_SKIP_DOWNLOAD=true PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true; '
    'if [ -f yarn.lock ]; then timeout 900 yarn install --silent > /dev/null 2>&1 || true; '
    'else timeout 900 npm install --silent > /dev/null 2>&1 || true; fi; '
    'chmod -R a+rX node_modules > /dev/null 2>&1 || true; fi'
)

_DEPENDENCY_SETUP_NEW = '''if ! git diff --quiet HEAD -- package.json 2>/dev/null; then
  echo "[rq4-phase] dependency_install_start $(date -Is)"
  export PUPPETEER_SKIP_DOWNLOAD=true PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
  if [ -f yarn.lock ]; then
    timeout 900 yarn install --silent
    RQ4_INSTALL_STATUS=$?
  else
    timeout 900 npm install --silent
    RQ4_INSTALL_STATUS=$?
  fi
  echo "[rq4-phase] dependency_install_end status=$RQ4_INSTALL_STATUS $(date -Is)"
  if [ "$RQ4_INSTALL_STATUS" -ne 0 ]; then
    echo "[rq4-infrastructure-error] dependency installation failed status=$RQ4_INSTALL_STATUS"
    exit "$RQ4_INSTALL_STATUS"
  fi
fi'''


def adapt_eval_script(row):
    """Remove one known overlay2-hostile helper without changing test logic."""
    script = row.get('eval_script', '')
    count = script.count(_DEPENDENCY_SETUP_OLD)
    if count != 1:
        raise SystemExit(
            f'{row.get("instance_id", "unknown")}: expected exactly one locked '
            f'dependency setup block, found {count}'
        )
    adapted = dict(row)
    adapted['eval_script'] = script.replace(
        _DEPENDENCY_SETUP_OLD, _DEPENDENCY_SETUP_NEW, 1
    )
    return adapted


def main():
    config=json.loads((ROOT/'configs/harness_lock.json').read_text())
    snapshot = ROOT/'configs/official_snapshot.json'
    if snapshot.exists():
        spec = json.loads(snapshot.read_text())
        if spec['revision'] != config['dataset_revision'] or spec['dataset'] != config['dataset']:
            raise SystemExit('Bundled official snapshot differs from harness lock')
        packed = (ROOT/spec['path']).read_bytes()
        if hashlib.sha256(packed).hexdigest() != spec['sha256']:
            raise SystemExit('Official snapshot hash mismatch')
        raw = gzip.decompress(packed)
        if hashlib.sha256(raw).hexdigest() != spec['uncompressed_sha256']:
            raise SystemExit('Official snapshot content hash mismatch')
        dataset = [json.loads(x) for x in raw.decode().splitlines()]
        if len(dataset) != spec['count'] or len({r['instance_id'] for r in dataset}) != len(dataset):
            raise SystemExit('Invalid official snapshot count/duplicate IDs')
        print('Using verified bundled official evaluator snapshot; no Hugging Face download', flush=True)
    else:
        from datasets import load_dataset
        dataset=load_dataset(config['dataset'],split='dev',revision=config['dataset_revision'])
    public={r['instance_id']:r for r in dataset}
    local=[json.loads(x) for x in (ROOT/'data/evaluation_only/swe50.jsonl').read_text().splitlines()]
    selected=[]
    for row in local:
        remote=public.get(row['instance_id'])
        if remote is None:
            raise SystemExit(f'{row["instance_id"]}: absent in pinned upstream. Supply a compatible historical evaluator snapshot; do not change sample IDs silently.')
        for key in ['repo','base_commit','patch','test_patch']:
            if remote.get(key)!=row.get(key):
                raise SystemExit(f'{row["instance_id"]}: upstream {key} differs; manual revision audit required')
        for key in ['FAIL_TO_PASS','PASS_TO_PASS']:
            def as_list(value):
                return ast.literal_eval(value) if isinstance(value,str) else value
            if as_list(remote.get(key)) != as_list(row.get(key)):
                raise SystemExit(f'{row["instance_id"]}: upstream {key} differs; test identity audit required')
        selected.append(adapt_eval_script(remote))
    target=ROOT/'data/evaluation_only/swe50.harness.jsonl'
    temp=target.with_suffix('.tmp.jsonl')
    temp.write_text(''.join(json.dumps(r)+'\n' for r in selected))
    try:
        validate_eval(temp,local)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    print(f'Validated {len(selected)} evaluator records: {target}')


if __name__=='__main__':
    main()
