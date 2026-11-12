#!/usr/bin/env python3
"""Resumably cache pinned SWE-bench images without invoking any model API."""
import argparse
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from resources import Paused, Resources, save


ROOT = Path(__file__).resolve().parents[1]
PULL_ENV_KEYS = {
    'RQ4_IMAGE_PULL_TIMEOUT',
    'RQ4_IMAGE_PULL_RETRIES',
    'RQ4_IMAGE_PULL_RETRY_DELAY',
}


class RetainingResources(Resources):
    """Enforce the reserve by stopping; never delete an already cached image."""

    def ensure(self, cleanup=True):
        return super().ensure(cleanup=False)


def load_pull_settings(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key in PULL_ENV_KEYS:
            os.environ[key] = value.strip().strip('"').strip("'")


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def now():
    return datetime.now(timezone.utc).astimezone().isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path,
                        default=ROOT / 'data/evaluation_only/swe50.harness.jsonl')
    parser.add_argument('--env-file', type=Path,
                        default=ROOT.parent / '.envaliyun-qwen.local')
    parser.add_argument('--run-id', default='swe50-image-prewarm-v1')
    parser.add_argument('--min-free-gb', type=float, default=25)
    args = parser.parse_args()
    if not args.run_id.replace('-', '').replace('_', '').isalnum():
        parser.error('Unsafe run ID')
    if args.min_free_gb < 20:
        parser.error('--min-free-gb must be at least 20')

    dataset = args.dataset.resolve()
    rows = read_rows(dataset)
    if len(rows) != 50 or len({row['instance_id'] for row in rows}) != 50:
        raise ValueError('Expected exactly 50 unique SWE instances')
    if len({row['image'] for row in rows}) != 50:
        raise ValueError('Expected exactly 50 unique evaluation images')
    rows.sort(key=lambda row: (row['repo'], row['instance_id']))
    load_pull_settings(args.env_file.resolve())

    output = ROOT / 'runs/prewarm' / args.run_id
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / 'prewarm.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(f'Prewarm already running: {output}') from exc

    dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
    state_path = output / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'status': 'starting',
        'started_at': now(),
        'dataset': str(dataset),
        'dataset_sha256': dataset_sha256,
        'total': len(rows),
        'completed': {},
        'failures': {},
        'api_calls': 0,
        'min_free_gib': args.min_free_gb,
    }
    if state['dataset_sha256'] != dataset_sha256:
        raise ValueError('Dataset changed; use a new run ID')
    if state['min_free_gib'] != args.min_free_gb:
        raise ValueError('Reserve changed; use the identical command or a new run ID')

    resources = RetainingResources(
        ROOT, output, args.min_free_gb, policy='budget', docker=True)
    state['daemon_id'] = resources.daemon_id
    state['status'] = 'running'
    state['updated_at'] = now()
    save(state_path, state)
    print(f'[prewarm] 0 API calls; {len(rows)} images; reserve={args.min_free_gb} GiB',
          flush=True)

    try:
        for index, row in enumerate(rows, 1):
            instance = row['instance_id']
            recorded = state['completed'].get(instance)
            if recorded:
                try:
                    resources.client.images.get(recorded['digest'])
                    print(f'[prewarm] {index}/50 cached {instance}', flush=True)
                    continue
                except Exception:
                    state['completed'].pop(instance)
            state['current'] = instance
            state['updated_at'] = now()
            save(state_path, state)
            print(f'[prewarm] {index}/50 pulling {instance}', flush=True)
            digest = resources.image(row['image'])
            image = resources.client.images.get(digest)
            state['completed'][instance] = {
                'repo': row['repo'],
                'digest': digest,
                'image_id': image.id,
                'size_bytes': image.attrs.get('Size'),
                'completed_at': now(),
            }
            state['failures'].pop(instance, None)
            state['current'] = None
            state['updated_at'] = now()
            state['free_bytes'] = resources.space()
            save(state_path, state)
            print(f'[prewarm] {index}/50 ready {instance} {digest}', flush=True)
        state['status'] = 'completed'
    except Paused as exc:
        state['status'] = 'paused_storage'
        state['error'] = str(exc)
        raise
    except Exception as exc:
        instance = state.get('current')
        if instance:
            state['failures'][instance] = {
                'type': type(exc).__name__, 'message': str(exc), 'time': now()}
        state['status'] = 'failed'
        state['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        state['current'] = None
        state['updated_at'] = now()
        state['completed_count'] = len(state['completed'])
        state['api_calls'] = 0
        try:
            state['free_bytes'] = resources.space()
        finally:
            save(state_path, state)


if __name__ == '__main__':
    main()
