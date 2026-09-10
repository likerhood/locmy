#!/usr/bin/env python3
"""Join frozen public evaluator metadata, refusing mismatched instances/commits.

Downloads official dataset to evaluation side only. Never supplies it to repair.
"""
import json
import ast
from pathlib import Path
from run_batch import validate_eval

ROOT=Path(__file__).resolve().parents[1]


def main():
    from datasets import load_dataset
    config=json.loads((ROOT/'configs/harness_lock.json').read_text())
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
        selected.append(remote)
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
