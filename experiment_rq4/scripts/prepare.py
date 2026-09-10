#!/usr/bin/env python3
"""Snapshot existing predictions; create gold-isolated 50-case input files."""
import csv
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
WORKSPACE = PROJECT.parents[1]
PLAN = PROJECT / 'docs/paper/downstream_plan_20260910'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def normalize_files(files):
    if not isinstance(files, list) or any(not isinstance(x, str) for x in files):
        raise ValueError('Predicted files must be an ordered string list')
    result = []
    for path in files:
        path = path.removeprefix('./')
        if not path or path.startswith('/') or '..' in path.split('/') or '\\' in path:
            raise ValueError('Unsafe prediction path')
        if path not in result:
            result.append(path)
    return result


def main():
    (ROOT / 'manifests').mkdir(exist_ok=True)
    report = {'scope': 'provisional50; no environment controls executed', 'datasets': {}, 'predictions': []}
    for tag, filename in [('swe', 'swebench_multimodal-full-dev.clean15.samples.jsonl'),
                          ('omni', 'omnigirl-full-candidates.clean15.v458.samples.jsonl')]:
        candidate = PLAN / f'{tag}_provisional50.csv'
        ids = [r['instance_id'] for r in csv.DictReader(candidate.open())]
        assert len(ids) == len(set(ids)) == 50
        shutil.copy2(candidate, ROOT / 'manifests' / candidate.name)
        shutil.copy2(PLAN / f'{tag}_candidate_order.csv', ROOT / 'manifests' / f'{tag}_candidate_order.csv')
        source = PROJECT / 'data' / filename
        all_rows = {r['instance_id']: r for r in map(json.loads, source.open())}
        rows = [all_rows[i] for i in ids]
        # Full benchmark records exist only on the evaluation side.
        write_rows(ROOT / 'data/evaluation_only' / f'{tag}50.jsonl', rows)
        inputs = [{k: r[k] for k in ['instance_id', 'repo', 'base_commit', 'problem_statement']} for r in rows]
        write_rows(ROOT / 'data/inputs' / f'{tag}50.jsonl', inputs)
        report['datasets'][tag] = {'count': 50, 'source': str(source), 'sha256': digest(source),
                                  'input_policy': 'existing problem_statement unchanged; compact evidence provenance/dedup pending'}
        for method, spec in json.loads((ROOT / 'configs/sources.json').read_text())[tag].items():
            target = ROOT / 'normalized' / tag / f'{method}.jsonl'
            if spec is None:
                report['predictions'].append({'dataset': tag, 'method': method, 'status': 'missing_source'})
                if target.exists():
                    raise RuntimeError(f'Stale normalized output must be reviewed: {target}')
                continue
            path = WORKSPACE / spec['path']
            if not path.exists():
                raise FileNotFoundError(path)
            selected = {}
            if path.suffix == '.jsonl':
                with path.open() as handle:
                    for line in handle:
                        row = json.loads(line)
                        key = row['instance_id']
                        if key in ids:
                            if key in selected:
                                raise ValueError(f'Duplicate prediction: {key}')
                            if method == 'magnet' and spec['field'] == 'localization.ranked_locations':
                                selected[key] = {'localization': {'ranked_locations':
                                    [{'path': x['path']} for x in row['localization']['ranked_locations']]}}
                            else:
                                selected[key] = row
            else:
                value = json.loads(path.read_text())
                if isinstance(value, list):
                    raise ValueError('Expected instance-keyed JSON')
                selected = {i: value[i] for i in ids if i in value}
            if method != 'magnet':
                snapshot = ROOT / 'snapshots' / tag / method / path.name
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, snapshot)
            output = []
            for i in ids:
                raw = selected.get(i)
                if raw is None:
                    output.append({'instance_id': i, 'found_files': [], 'status': 'missing_prediction'})
                    continue
                files = raw
                for key in spec['field'].split('.'):
                    files = files[key]
                if spec.get('item_key'):
                    files = [x[spec['item_key']] for x in files]
                output.append({'instance_id': i, 'found_files': normalize_files(files), 'status': 'available'})
            write_rows(target, output)
            report['predictions'].append({'dataset': tag, 'method': method, 'status': 'exported',
                'source': str(path), 'source_sha256': digest(path), 'field': spec['field'],
                'matched': len(selected), 'count': 50, 'normalized_sha256': digest(target),
                'model_identity': 'directory_label; provider identity audit pending'})
    write_json(ROOT / 'reports/preparation.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
