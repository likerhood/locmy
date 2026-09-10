#!/usr/bin/env python3
"""Restore ignored evaluation-only subsets from tracked datasets and manifests.

No baseline result paths, API calls, or workstation-specific directories needed.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data/evaluation_only')
    args = parser.parse_args()
    audit = json.loads((ROOT / 'reports/preparation.json').read_text())
    for tag, filename in [('swe', 'swebench_multimodal-full-dev.clean15.samples.jsonl'),
                          ('omni', 'omnigirl-full-candidates.clean15.v458.samples.jsonl')]:
        source = ROOT.parent / 'data' / filename
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != audit['datasets'][tag]['sha256']:
            raise SystemExit(f'{tag}: dataset SHA mismatch; refusing silent replacement')
        with (ROOT / 'manifests' / f'{tag}_provisional50.csv').open() as handle:
            ids = [row['instance_id'] for row in csv.DictReader(handle)]
        if len(ids) != 50 or len(set(ids)) != 50:
            raise SystemExit(f'{tag}: invalid 50-case manifest')
        full = {r['instance_id']: r for r in map(json.loads, raw.decode().splitlines())}
        output = ''.join(json.dumps(full[i], ensure_ascii=False) + '\n' for i in ids)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / f'{tag}50.jsonl'
        if target.exists() and target.read_text() != output:
            raise SystemExit(f'{target}: existing content differs; refusing overwrite')
        target.write_text(output)
        print(f'{tag}: restored 50 evaluation-only records')


if __name__ == '__main__':
    main()
