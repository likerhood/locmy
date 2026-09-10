#!/usr/bin/env python3
"""Read-only readiness check; no paid API requests or Docker mutations."""
import importlib.util
import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    # Preserve dedicated overrides; otherwise accept the existing mycode names.
    for target, sources in {
        'RQ4_BASE_URL': ['BASE_URL'],
        'RQ4_API_KEY': ['API_KEY'],
        'RQ4_MODEL': ['MODEL_API_NAME', 'MODEL_NAME'],
    }.items():
        if not os.getenv(target):
            for source in sources:
                if os.getenv(source):
                    os.environ[target] = os.environ[source]
                    break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.local')
    args = parser.parse_args()
    load_env(args.env_file)
    report = {'api_configured': all(os.getenv(k) for k in ['RQ4_BASE_URL', 'RQ4_API_KEY', 'RQ4_MODEL']),
              'predictions': {}, 'formal_blockers': []}
    try:
        r = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True, timeout=15)
        report['docker_available'] = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        report['docker_available'] = False
    vendor = ROOT / 'vendor/Agentless'
    r = subprocess.run(['git', '-C', str(vendor), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    report['agentless_commit'] = r.stdout.strip() if r.returncode == 0 else None
    report['swebench_import_available'] = importlib.util.find_spec('swebench') is not None
    for dataset in ['swe', 'omni']:
        for method in ['locagent', 'cosil', 'gala', 'graphlocator', 'magnet']:
            p = ROOT / 'normalized' / dataset / f'{method}.jsonl'
            rows = [json.loads(x) for x in p.open()] if p.exists() else []
            report['predictions'][f'{dataset}/{method}'] = {
                'rows': len(rows), 'available': sum(r['status'] == 'available' for r in rows)}
    report['formal_blockers'] = ['50+50 environment no-op/gold controls not executed',
        'MAGNET SWE front_rank_v1 and Omni r4 are different versions; choose common frozen method',
        'input compact evidence provenance and model identity require audit',
        'development repair uses byte budget; formal tokenizer-aware context policy not frozen']
    if not report['api_configured']:
        report['formal_blockers'].append('dedicated RQ4 API configuration missing')
    if not report['docker_available']:
        report['formal_blockers'].append('Docker daemon unavailable in this environment')
    for key, item in report['predictions'].items():
        if item['available'] != 50:
            report['formal_blockers'].append(f'{key}: missing predictions')
    (ROOT / 'reports').mkdir(exist_ok=True)
    (ROOT / 'reports/readiness.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
