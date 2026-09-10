#!/usr/bin/env python3
"""Thin wrapper around the locked official SWE-bench run_instances API."""
import argparse
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--predictions',type=Path,required=True)
    p.add_argument('--workers',type=int,default=1)
    p.add_argument('--timeout',type=int,default=1800)
    p.add_argument('--run-id',required=True)
    p.add_argument('--no-op',action='store_true')
    args=p.parse_args()
    from swebench.harness.run_evaluation import run_instances
    rows=[json.loads(x) for x in args.dataset.read_text().splitlines() if x]
    predictions={r['instance_id']:r for r in map(json.loads,args.predictions.read_text().splitlines())}
    rows=[r for r in rows if r['instance_id'] in predictions and
          (args.no_op or predictions[r['instance_id']].get('model_patch'))]
    if rows:
        run_instances(predictions,rows,args.workers,args.run_id,args.timeout,skip_patch=args.no_op)


if __name__=='__main__':
    main()
