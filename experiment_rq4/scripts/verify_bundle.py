#!/usr/bin/env python3
"""Verify portable RQ4 inputs without network, credentials, or external repos."""
import csv
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    audit = json.loads((ROOT / 'reports/preparation.json').read_text())
    seeds = json.loads((ROOT / 'configs/evaluation_seed.json').read_text())
    ids_by_tag = {}
    for tag in ['swe', 'omni']:
        with (ROOT / 'manifests' / f'{tag}_provisional50.csv').open() as f:
            ids = [r['instance_id'] for r in csv.DictReader(f)]
        require(len(ids) == len(set(ids)) == 50, f'{tag}: invalid manifest')
        ids_by_tag[tag] = ids
        spec = seeds[tag]
        packed = (ROOT / spec['path']).read_bytes()
        require(hashlib.sha256(packed).hexdigest() == spec['sha256'], f'{tag}: seed hash mismatch')
        raw = gzip.decompress(packed)
        require(hashlib.sha256(raw).hexdigest() == spec['uncompressed_sha256'], f'{tag}: raw seed hash mismatch')
        rows = [json.loads(x) for x in raw.decode().splitlines()]
        require([r['instance_id'] for r in rows] == ids, f'{tag}: seed order mismatch')
        inputs = [json.loads(x) for x in (ROOT / 'data/inputs' / f'{tag}50.jsonl').read_text().splitlines()]
        expected = [{k: r[k] for k in ['instance_id', 'repo', 'base_commit', 'problem_statement']} for r in rows]
        require(inputs == expected, f'{tag}: repair inputs differ from bundled records')
    official = json.loads((ROOT/'configs/official_snapshot.json').read_text())
    packed = (ROOT/official['path']).read_bytes()
    require(hashlib.sha256(packed).hexdigest() == official['sha256'], 'Official snapshot hash mismatch')
    raw = gzip.decompress(packed)
    require(hashlib.sha256(raw).hexdigest() == official['uncompressed_sha256'], 'Official raw snapshot hash mismatch')
    require([json.loads(x)['instance_id'] for x in raw.decode().splitlines()] == ids_by_tag['swe'], 'Official snapshot IDs/order mismatch')
    exported = []
    missing = []
    for item in audit['predictions']:
        label = f"{item['dataset']}/{item['method']}"
        if item['status'] != 'exported':
            missing.append(label)
            continue
        p = ROOT / 'normalized' / item['dataset'] / f"{item['method']}.jsonl"
        require(hashlib.sha256(p.read_bytes()).hexdigest() == item['normalized_sha256'], f'{label}: prediction hash mismatch')
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        require([r['instance_id'] for r in rows] == ids_by_tag[item['dataset']], f'{label}: IDs/order mismatch')
        require(all(set(r) == {'instance_id', 'found_files', 'status'} for r in rows), f'{label}: unexpected fields')
        exported.append(label)
    print(json.dumps({'bundle_integrity': 'ok', 'exported': exported,
                      'missing_localization': missing,
                      'note': 'Integrity only; no API/Docker or official dataset compatibility verified.'}, indent=2))


if __name__ == '__main__':
    main()
