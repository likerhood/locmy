#!/usr/bin/env python3
"""Migrate one stopped RQ4 batch to the audited Calypso dash-prefix parser."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = Path('scripts/official_eval.py')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Back up a stale control_gold report and migrate its batch manifest.'
    )
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run_id):
        parser.error('Unsafe run-id')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.instance):
        parser.error('Unsafe instance ID')

    batch = ROOT / 'runs/batches' / args.run_id
    pipeline = ROOT / 'runs/pipeline' / args.run_id
    manifest_path = batch / 'manifest.json'
    failure_path = batch / 'control_failure.json'
    if not manifest_path.exists() or not failure_path.exists():
        raise SystemExit('Batch manifest or control_failure.json is missing')

    status_path = pipeline / 'status.json'
    if status_path.exists():
        status = json.loads(status_path.read_text())
        if status.get('state') == 'running' and process_alive(status.get('supervisor_pid')):
            raise SystemExit('Pipeline supervisor is still running; stop it before migration')

    failure = json.loads(failure_path.read_text())
    expected_failure = {
        'instance_id': args.instance,
        'control': 'control_gold',
        'expected_resolved': True,
    }
    if failure != expected_failure:
        raise SystemExit(f'Unexpected control failure: {failure!r}')

    manifest = json.loads(manifest_path.read_text())
    hashes = manifest.get('hashes', {})
    if str(ADAPTER) not in hashes:
        raise SystemExit(f'{ADAPTER} is not recorded in the batch manifest')
    mismatches = []
    for relative, recorded in hashes.items():
        path = ROOT / relative
        if not path.is_file():
            mismatches.append((relative, recorded, '<missing>'))
            continue
        current = sha256(path)
        if current != recorded and relative != str(ADAPTER):
            mismatches.append((relative, recorded, current))
    if mismatches:
        lines = '\n'.join(f'  {name}: recorded={old} current={new}'
                          for name, old, new in mismatches)
        raise SystemExit('Files other than the evaluator adapter changed:\n' + lines)

    reports = list((batch / 'evaluation/control_gold').glob(
        f'logs/evaluation/*/*/{args.instance}/report.json'
    ))
    outputs = list((batch / 'evaluation/control_gold').glob(
        f'logs/evaluation/*/*/{args.instance}/test_output.txt'
    ))
    if len(reports) != 1 or len(outputs) != 1 or reports[0].parent != outputs[0].parent:
        raise SystemExit(f'Expected one matching Gold report/output, found {len(reports)}/{len(outputs)}')
    report = json.loads(reports[0].read_text()).get(args.instance, {})
    if (report.get('resolved') is not False
            or report.get('patch_successfully_applied') is not True
            or report.get('infra_failure') is not False):
        raise SystemExit(f'Gold report is not the expected parser-failure shape: {report!r}')
    if '>>>>> Test Exit Code: 0' not in outputs[0].read_text(errors='replace'):
        raise SystemExit('Gold test output does not contain a successful test exit code')

    adapter_old = hashes[str(ADAPTER)]
    adapter_new = sha256(ROOT / ADAPTER)
    print(f'run_id={args.run_id}')
    print(f'instance={args.instance}')
    print(f'adapter_old={adapter_old}')
    print(f'adapter_new={adapter_new}')
    print(f'stale_gold_directory={reports[0].parent}')
    if adapter_old == adapter_new:
        raise SystemExit('Evaluator adapter hash has not changed; deploy the parser fix first')
    if not args.apply:
        print('Dry run complete. Repeat with --apply to perform the recoverable migration.')
        return

    stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')
    migration = batch / 'migrations' / f'{stamp}-calypso-dash-prefix-{args.instance}'
    migration.mkdir(parents=True)
    shutil.copy2(manifest_path, migration / 'manifest.before.json')
    shutil.copy2(failure_path, migration / 'control_failure.before.json')
    moved = migration / 'stale_control_gold'
    shutil.move(str(reports[0].parent), moved)

    manifest['hashes'][str(ADAPTER)] = adapter_new
    atomic_json(manifest_path, manifest)
    atomic_json(migration / 'migration.json', {
        'run_id': args.run_id,
        'instance_id': args.instance,
        'reason': 'Calypso parser emitted a literal dash suite prefix for all expected test IDs',
        'adapter': str(ADAPTER),
        'adapter_old_sha256': adapter_old,
        'adapter_new_sha256': adapter_new,
        'moved_gold_directory': str(moved.relative_to(batch)),
        'next_action': 'Repeat the original pipeline command with the same run-id',
    })
    print(f'Migration applied and backed up under {migration}')


if __name__ == '__main__':
    main()
