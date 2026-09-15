#!/usr/bin/env python3
"""Cache a pinned evaluation image's Git objects for read-only repair inputs.

Use only after the pipeline has stopped. No source checkout or test files are copied.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, errors='replace')
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-600:]
        raise RuntimeError(f'{command[0]} failed (exit {result.returncode}): {detail}')
    return result.stdout.strip()


def verify_git(path, commit, candidate_paths):
    run(['git', '-C', str(path), 'cat-file', '-e', commit + '^{commit}'])
    for relative in candidate_paths:
        result = subprocess.run(
            ['git', '-C', str(path), 'cat-file', '-e', f'{commit}:{relative}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return relative
    raise RuntimeError('Image Git objects contain no selected repair input at the frozen commit')


def selected_paths(dataset, instance):
    paths = []
    for method in ('locagent', 'cosil', 'gala', 'graphlocator', 'magnet'):
        source = ROOT / 'normalized' / dataset / f'{method}.jsonl'
        if not source.exists():
            continue
        for line in source.read_text().splitlines():
            row = json.loads(line)
            if row['instance_id'] == instance:
                paths.extend(row.get('found_files', []))
                break
    return list(dict.fromkeys(paths))


def cache(run_id, instance):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', run_id) or not re.fullmatch(r'[A-Za-z0-9_.-]+', instance):
        raise ValueError('Invalid run ID or instance ID')
    batch = ROOT / 'runs' / 'batches' / run_id
    status = json.loads((ROOT / 'runs' / 'pipeline' / run_id / 'status.json').read_text())
    if status['state'] in ('running', 'stopping'):
        raise RuntimeError('Stop the pipeline before importing repository objects')
    row = json.loads((batch / 'eval_inputs' / instance / 'dataset.jsonl').read_text().splitlines()[0])
    if row['instance_id'] != instance:
        raise RuntimeError('Evaluation input instance ID differs')
    repo, commit, image = row['repo'], row['base_commit'], row['image']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid frozen repository identity')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('Evaluation image must be pinned by digest')
    dataset = json.loads((batch / 'manifest.json').read_text())['dataset']
    candidates = selected_paths(dataset, instance)
    if not candidates:
        raise RuntimeError('No selected repair files to verify against the image')
    print(f'[image-cache] Checking pinned image {image}', flush=True)
    digests = json.loads(run(['docker', 'image', 'inspect', image, '--format', '{{json .RepoDigests}}']))
    if image not in digests:
        raise RuntimeError('Local Docker image does not match the pinned evaluation digest')
    repo_dir = ROOT / 'repos'
    repo_dir.mkdir(exist_ok=True)
    target = repo_dir / repo.replace('/', '__')
    with (repo_dir / '.rq4.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Repository cache is in use by another batch') from exc
        if target.exists():
            verified = verify_git(target, commit, candidates)
            print(f'Existing cache verified: {target}; file={verified}')
            return
        temporary = Path(tempfile.mkdtemp(prefix='.rq4-image-', dir=repo_dir))
        container = None
        try:
            container = run(['docker', 'create', '--entrypoint', '/bin/sh', image, '-c', 'true'])
            print('[image-cache] Copying /testbed/.git from local image; this may take several minutes.', flush=True)
            run(['docker', 'cp', f'{container}:/testbed/.git', str(temporary / '.git')])
            print('[image-cache] Verifying frozen commit and repair input.', flush=True)
            verified = verify_git(temporary, commit, candidates)
            (temporary / '.rq4-source.json').write_text(json.dumps({
                'source': 'pinned_evaluation_image_git_objects', 'image': image,
                'repo': repo, 'base_commit': commit, 'verified_file': verified,
                'run_id': run_id, 'imported_at': datetime.now(timezone.utc).isoformat(),
            }, indent=2) + '\n')
            os.rename(temporary, target)
            print(f'Image Git cache ready: {target}; commit={commit}; file={verified}')
        finally:
            if container:
                subprocess.run(['docker', 'rm', container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if temporary.exists():
                shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--instance-id', required=True)
    args = parser.parse_args()
    cache(args.run_id, args.instance_id)


if __name__ == '__main__':
    main()
