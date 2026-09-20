#!/usr/bin/env python3
"""Opt one known stopped batch into deferred evaluation, preserving every result."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = 'chartjs__Chart.js-10157'
RUNNER = 'scripts/run_batch.py'
ANALYZER = 'scripts/analyze_batch.py'
OLD_RUNNER = '9b5cfc734ca7582e0188f0f2c74ac21f7a4c551d360639230b0182444e6fb9e8'
EXPECTED_ADAPTER = 'f35a5a9ffa3494a3e25c54dc4f118f1686e9eafafc4713da4eac4c3c859269aa'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


def migrate(root, run_id, apply=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', run_id):
        raise ValueError('Unsafe run ID')
    pipeline = root/'runs/pipeline'/run_id
    batch = root/'runs/batches'/run_id
    # Also exclude supervisor setup, which happens before the runner takes its lock.
    with (pipeline/'supervisor.lock').open('a') as supervisor, (root/'repos/.rq4.lock').open('a') as runner:
        for handle in (supervisor, runner):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if json.loads((pipeline/'status.json').read_text()).get('state') != 'failed':
            raise ValueError('Expected stopped failed pipeline')
        manifest = json.loads((batch/'manifest.json').read_text())
        if 'evaluation_policy' in manifest:
            raise ValueError('Already migrated; do not migrate twice')
        hashes = manifest['hashes']
        if hashes.get(RUNNER) != OLD_RUNNER or hashes.get('scripts/official_eval.py') != EXPECTED_ADAPTER:
            raise ValueError('Unknown previous runner/adapter; inspect versions, do not overwrite hashes')
        for name, expected in hashes.items():
            if name in (RUNNER, 'evaluator_dataset'):
                continue
            path = (root/name).resolve()
            if not path.is_relative_to(root.resolve()) or sha(path) != expected:
                raise ValueError(f'Unexpected changed file: {name}')
        if INSTANCE not in manifest['instance_ids'] or (batch/'completed_samples'/f'{INSTANCE}.json').exists():
            raise ValueError('Target absent or already completed')
        evidence = list((batch/'evaluation/control_noop').glob(f'logs/evaluation/*/*/{INSTANCE}/test_output.txt'))
        if len(evidence) != 1 or 'DISCONNECTED' not in evidence[0].read_text(errors='replace'):
            raise ValueError('Expected Chart.js browser-disconnect evidence missing')
        if sha(root/RUNNER) == OLD_RUNNER:
            raise ValueError('Deploy new runner first')
        policy = dict(on_failure='defer', max_consecutive=3)
        print(f'Policy: {policy}; defer only {INSTANCE}; retain logs, completed markers and all API records')
        if not apply:
            print('Dry run complete. Repeat with --apply to enable.')
            return
        stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')
        backup = batch/'migrations'/f'{stamp}-deferred-evaluation'
        backup.mkdir(parents=True)
        for name in ('manifest.json', 'paused.json', 'control_failure.json'):
            if (batch/name).exists():
                shutil.copy2(batch/name, backup/name)
        marker = batch/'deferred_samples'/f'{INSTANCE}.json'
        if marker.exists():
            shutil.copy2(marker, backup/'deferred.before.json')
        write_json(marker, dict(instance_id=INSTANCE, status='deferred_evaluation',
                               reason='Chart.js browser disconnect in recorded no-op control',
                               evidence=str(evidence[0].relative_to(batch)), methods=manifest['methods']))
        manifest['evaluation_policy'] = policy
        manifest['hashes'][RUNNER] = sha(root/RUNNER)
        manifest['hashes'][ANALYZER] = sha(root/ANALYZER)
        write_json(backup/'migration.json', dict(policy=policy, instance_id=INSTANCE,
                                               previous_runner=OLD_RUNNER, new_runner=manifest['hashes'][RUNNER]))
        write_json(batch/'manifest.json', manifest)
        print(f'Applied. Backup: {backup}. Resume with --eval-failure-policy defer')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    migrate(ROOT, args.run_id, args.apply)
