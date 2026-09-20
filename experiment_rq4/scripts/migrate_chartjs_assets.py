#!/usr/bin/env python3
"""Explicit, recoverable migration of the stopped Chart.js no-op control only."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
from datetime import datetime

from official_eval import bundled_assets

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = 'chartjs__Chart.js-10157'
ADAPTER = 'scripts/official_eval.py'
OLD_ADAPTER = 'a8a8d936326f61d5b7b8c79c79aa74e4632efc33af7965b3059715dbdbef2283'


def migrate(root, run_id, apply=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', run_id):
        raise ValueError('Unsafe run ID')
    # Same lock as run_batch: never migrate a concurrently running batch.
    with (root / 'repos/.rq4.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        batch = root / 'runs/batches' / run_id
        status = json.loads((root / 'runs/pipeline' / run_id / 'status.json').read_text())
        if status.get('state') != 'failed':
            raise ValueError('Migration requires a stopped, failed pipeline')
        manifest_path = batch / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        failure = json.loads((batch / 'control_failure.json').read_text())
        if failure != dict(instance_id=INSTANCE, control='control_noop', expected_resolved=False):
            raise ValueError('Not the expected Chart.js no-op failure')
        if (batch / 'completed_samples' / f'{INSTANCE}.json').exists():
            raise ValueError('Target sample is marked completed; refusing migration')
        if list((batch / 'records').glob(f'*/{INSTANCE}.json')):
            raise ValueError('Target sample has generation records; manual audit required')
        hashes = manifest['hashes']
        if hashes.get(ADAPTER) != OLD_ADAPTER:
            raise ValueError('Unrecognized previous evaluator; do not bypass this check')
        for name, expected in hashes.items():
            if name in (ADAPTER, 'evaluator_dataset'):
                continue
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f'Unrelated file changed: {name}')
        directories = list((batch / 'evaluation/control_noop').glob(f'logs/evaluation/*/*/{INSTANCE}'))
        if len(directories) != 1 or directories[0].is_symlink():
            raise ValueError('Expected exactly one real no-op evidence directory')
        old = directories[0]
        output = (old / 'test_output.txt').read_text(errors='replace')
        if 'DISCONNECTED' not in output and 'EACCES' not in output:
            raise ValueError('Expected infrastructure failure evidence missing')
        bundled_assets()  # Verify shipped resources before authorizing migration.
        new_hash = hashlib.sha256((root / ADAPTER).read_bytes()).hexdigest()
        if new_hash == OLD_ADAPTER:
            raise ValueError('Deploy the new adapter first')
        print(f'Target: {INSTANCE}; archive: {old}; preserve all completed samples and API records')
        if not apply:
            print('Dry run only. Repeat with --apply after reviewing.')
            return
        stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')
        backup = batch / 'migrations' / f'{stamp}-chartjs-assets'
        backup.mkdir(parents=True)
        for name in ('manifest.json', 'control_failure.json', 'paused.json'):
            if (batch / name).exists():
                shutil.copy2(batch / name, backup / name)
        shutil.move(str(old), backup / 'stale_control_noop')
        manifest['hashes'][ADAPTER] = new_hash
        audit = dict(reason='Verified offline Chart.js assets and fail-closed suite grading',
                     instance=INSTANCE, old_adapter=OLD_ADAPTER, new_adapter=new_hash,
                     archived=str(old.relative_to(batch)))
        (backup / 'migration.json').write_text(json.dumps(audit, indent=2) + '\n')
        temporary = manifest_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.replace(manifest_path)
        print(f'Applied. Backup: {backup}. Resume the identical original command.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    migrate(ROOT, args.run_id, args.apply)


if __name__ == '__main__':
    main()
