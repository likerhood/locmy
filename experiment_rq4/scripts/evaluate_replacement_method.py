#!/usr/bin/env python3
"""Evaluate one staged replacement prediction without mutating the frozen dataset."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--method', choices=['locagent', 'cosil', 'gala', 'graphlocator', 'magnet'], required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()

    stage = args.stage.resolve()
    prediction_path = stage / 'repair' / f'{args.method}_mimo26' / 'prediction.jsonl'
    if not prediction_path.is_file():
        raise SystemExit(f'Missing completed prediction: {prediction_path}')
    prediction = json.loads(prediction_path.read_text().splitlines()[0])
    instance = prediction.get('instance_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', instance):
        raise SystemExit('Unsafe or missing replacement instance ID')

    status_path = stage / 'evaluation' / args.method / 'status.json'
    patch = prediction.get('model_patch', '')
    if prediction.get('status') != 'generated' or not patch:
        save(status_path, {
            'instance_id': instance,
            'method': args.method,
            'generation_status': prediction.get('status'),
            'evaluated': False,
            'resolved': False,
            'reason': 'no_generated_nonempty_patch',
        })
        return

    eval_dir = stage / 'evaluation' / args.method
    eval_dir.mkdir(parents=True, exist_ok=True)
    eval_input = eval_dir / 'prediction.jsonl'
    eval_input.write_text(json.dumps({
        'instance_id': instance,
        'model_name_or_path': prediction.get('model_name_or_path', f'rq4-{args.method}'),
        'model_patch': patch,
    }, ensure_ascii=False) + '\n')
    dataset = stage / 'dataset_v3' / 'swe50.harness.jsonl'
    run_id = 'rq4_v3_' + hashlib.sha256(f'{stage}:{args.method}'.encode()).hexdigest()[:16]
    log_path = eval_dir / 'harness.log'
    command = [
        sys.executable, str(ROOT / 'scripts' / 'official_eval.py'),
        '--dataset', str(dataset), '--predictions', str(eval_input),
        '--workers', '1', '--timeout', str(args.timeout), '--run-id', run_id,
    ]
    with log_path.open('a') as log:
        result = subprocess.run(command, cwd=eval_dir, stdout=log, stderr=subprocess.STDOUT)
    reports = list(eval_dir.glob(f'logs/evaluation/{run_id}/**/report.json'))
    report = json.loads(reports[0].read_text()).get(instance) if reports else None
    save(status_path, {
        'instance_id': instance,
        'method': args.method,
        'generation_status': prediction.get('status'),
        'evaluated': report is not None,
        'resolved': report.get('resolved') if report else None,
        'infra_failure': report.get('infra_failure') if report else None,
        'returncode': result.returncode,
        'report': str(reports[0]) if reports else None,
    })
    if result.returncode or report is None or report.get('infra_failure'):
        raise SystemExit('Official replacement evaluation did not produce a usable report')


if __name__ == '__main__':
    main()
