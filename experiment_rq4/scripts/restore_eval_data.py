#!/usr/bin/env python3
"""Restore ignored evaluation-only subsets from bundled compressed records and manifests.

No baseline result paths, API calls, or workstation-specific directories needed.
"""
import argparse
import csv
import hashlib
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data/evaluation_only')
    args = parser.parse_args()
    seeds = json.loads((ROOT / 'configs/evaluation_seed.json').read_text())
    for tag, spec in seeds.items():
        packed = (ROOT / spec['path']).read_bytes()
        if hashlib.sha256(packed).hexdigest() != spec['sha256']:
            raise SystemExit(f'{tag}: bundled seed SHA mismatch')
        raw = gzip.decompress(packed)
        if hashlib.sha256(raw).hexdigest() != spec['uncompressed_sha256']:
            raise SystemExit(f'{tag}: uncompressed seed SHA mismatch')
        with (ROOT / 'manifests' / f'{tag}_provisional50.csv').open() as handle:
            ids = [row['instance_id'] for row in csv.DictReader(handle)]
        if len(ids) != 50 or len(set(ids)) != 50:
            raise SystemExit(f'{tag}: invalid 50-case manifest')
        rows = [json.loads(line) for line in raw.decode().splitlines()]
        full = {r['instance_id']: r for r in rows}
        if len(rows) != 50 or len(full) != 50 or set(full) != set(ids):
            raise SystemExit(f'{tag}: seed IDs differ from manifest')
        output = ''.join(json.dumps(full[i], ensure_ascii=False) + '\n' for i in ids)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / f'{tag}50.jsonl'
        if target.exists() and target.read_text() != output:
            previous = hashlib.sha256(target.read_bytes()).hexdigest()
            if previous not in spec.get('superseded_uncompressed_sha256', []):
                raise SystemExit(f'{target}: existing content differs; refusing overwrite')
            backup = target.with_name(target.name + '.superseded-' + previous[:12])
            if backup.exists() and backup.read_bytes() != target.read_bytes():
                raise SystemExit('Migration backup conflict')
            backup.write_bytes(target.read_bytes())
            print(f'Archived recognized previous selection: {backup}')
        target.write_text(output)
        print(f'{tag}: restored 50 evaluation-only records')


if __name__ == '__main__':
    main()
