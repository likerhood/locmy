#!/usr/bin/env python3
"""Thin wrapper around the locked official SWE-bench run_instances API."""
import argparse
import json
from pathlib import Path


def run_with_git_mode(container, command, timeout, original, *, workdir, user):
    """Ignore image-layer permission changes before the unchanged official eval.sh."""
    if command == '/bin/bash /eval.sh':
        result = container.exec_run(
            ['/bin/bash', '-c',
             'git config --local core.filemode false && git diff --quiet HEAD -- package.json'],
            workdir=workdir, user=user,
        )
        if result.exit_code:
            detail = result.output.decode('utf-8', errors='replace')[:500].strip()
            raise RuntimeError(
                'Cannot normalize Git file-mode tracking in the test container, '
                'or package.json has a real content change. '
                f'Inspect the image baseline. {detail}'
            )
        print('[rq4] Git file-mode tracking disabled in test container; '
              'official eval.sh unchanged.', flush=True)
    return original(container, command, timeout)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--predictions',type=Path,required=True)
    p.add_argument('--workers',type=int,default=1)
    p.add_argument('--timeout',type=int,default=1800)
    p.add_argument('--run-id',required=True)
    p.add_argument('--no-op',action='store_true')
    args=p.parse_args()
    from swebench.harness import run_evaluation
    original = run_evaluation.exec_run_with_timeout
    def normalized_exec(container, command, timeout):
        return run_with_git_mode(
            container, command, timeout, original,
            workdir=run_evaluation.CONTAINER_WORKDIR,
            user=run_evaluation.CONTAINER_USER,
        )
    run_evaluation.exec_run_with_timeout = normalized_exec
    rows=[json.loads(x) for x in args.dataset.read_text().splitlines() if x]
    predictions={r['instance_id']:r for r in map(json.loads,args.predictions.read_text().splitlines())}
    rows=[r for r in rows if r['instance_id'] in predictions and
          (args.no_op or predictions[r['instance_id']].get('model_patch'))]
    if rows:
        run_evaluation.run_instances(
            predictions, rows, args.workers, args.run_id, args.timeout,
            skip_patch=args.no_op,
        )


if __name__=='__main__':
    main()
