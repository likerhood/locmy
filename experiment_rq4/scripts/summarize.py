#!/usr/bin/env python3
"""Summarize official per-instance report.json files using the frozen denominator.

Unknown/missing reports remain explicit; conservative rate counts only resolved.
"""
import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dataset', required=True, choices=['swe', 'omni'])
    p.add_argument('--reports', type=Path, required=True, help='One method/attempt official reports directory')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    ids = [r['instance_id'] for r in csv.DictReader((ROOT / 'manifests' / f'{args.dataset}_provisional50.csv').open())]
    found = {}
    for path in args.reports.rglob('report.json'):
        obj = json.loads(path.read_text())
        for i in ids:
            if i in obj and isinstance(obj[i], dict) and isinstance(obj[i].get('resolved'), bool):
                if i in found:
                    raise ValueError(f'Multiple official reports for {i}; pass ONE method/attempt directory')
                found[i] = obj[i]
    count = sum(r['resolved'] for r in found.values())
    out = {'dataset': args.dataset, 'denominator': len(ids), 'resolved': count,
           'resolved_percent_conservative': count / len(ids) * 100,
           'missing_reports': [i for i in ids if i not in found],
           'status': 'complete' if len(found) == len(ids) else 'incomplete_do_not_claim_full_evaluation',
           'per_instance': {i: found.get(i, {'resolved': None, 'status': 'missing_report'}) for i in ids}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + '\n')
    print(f'{count}/{len(ids)} resolved; {len(ids)-len(found)} missing reports')


if __name__ == '__main__':
    main()
