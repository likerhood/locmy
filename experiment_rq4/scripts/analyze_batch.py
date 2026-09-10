#!/usr/bin/env python3
"""Rebuild CSV/Markdown and paired counts. Missing evaluation is never resolved=False."""
import argparse
import csv
import json
from pathlib import Path


def official_reports(directory):
    found = {}
    for path in directory.rglob('report.json'):
        obj = json.loads(path.read_text())
        for key, value in obj.items():
            if isinstance(value, dict) and isinstance(value.get('resolved'), bool):
                if key in found:
                    raise ValueError(f'Duplicate evaluation for {key} in {directory}')
                found[key] = value
    return found


def analyze(batch):
    manifest = json.loads((batch / 'manifest.json').read_text())
    rows, summaries = [], []
    outcomes = {}
    for method in manifest['methods']:
        reports = official_reports(batch / 'evaluation' / method)
        method_rows = []
        for instance in manifest['instance_ids']:
            path = batch / 'records' / method / f'{instance}.json'
            record = json.loads(path.read_text()) if path.exists() else {'status': 'not_started'}
            pred = record.get('prediction', {})
            result = reports.get(instance)
            resolved = result['resolved'] if result else None
            # Explicit completed generation with no patch is a known system failure.
            if result is None and record.get('status') == 'completed' and not pred.get('model_patch'):
                resolved = False
            usage = pred.get('usage') or {}
            row = {'method': method, 'instance_id': instance, 'status': record['status'],
                   'generated': bool(pred.get('model_patch')), 'applied_check': record.get('applied'),
                   'resolved': resolved, 'official_report': result is not None,
                   'seconds': record.get('elapsed_seconds', 0),
                   'prompt_tokens': usage.get('prompt_tokens', 0),
                   'completion_tokens': usage.get('completion_tokens', 0),
                   'total_tokens': usage.get('total_tokens', 0)}
            rows.append(row)
            method_rows.append(row)
            outcomes[(method, instance)] = resolved
        n = len(method_rows)
        solved = sum(r['resolved'] is True for r in method_rows)
        unknown = sum(r['resolved'] is None for r in method_rows)
        summaries.append({'method': method, 'n': n, 'generated': sum(r['generated'] for r in method_rows),
                          'applied_check': sum(r['applied_check'] is True for r in method_rows),
                          'resolved': solved, 'unknown': unknown, 'resolved_percent_lower_bound': 100*solved/n,
                          'complete': unknown == 0, 'total_tokens': sum(r['total_tokens'] for r in method_rows),
                          'seconds': sum(r['seconds'] for r in method_rows)})
    paired = []
    if 'magnet' in manifest['methods']:
        for method in manifest['methods']:
            if method == 'magnet':
                continue
            counts = dict(ours_only=0, baseline_only=0, both=0, neither=0, unknown=0)
            for instance in manifest['instance_ids']:
                a, b = outcomes[('magnet', instance)], outcomes[(method, instance)]
                key = 'unknown' if a is None or b is None else 'both' if a and b else 'ours_only' if a else 'baseline_only' if b else 'neither'
                counts[key] += 1
            paired.append({'baseline': method, **counts})
    report = {'manifest': manifest, 'summary': summaries, 'paired': paired,
              'note': 'Token/time are this repair batch only, not end-to-end localization cost. Missing official evaluation stays unknown.'}
    (batch / 'analysis.json').write_text(json.dumps(report, indent=2) + '\n')
    with (batch / 'per_instance.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    lines = ['# RQ4 batch analysis', '', 'Unknown means no final test conclusion; lower bound is NOT a completed resolved rate.', '',
             '| Method | N | Generated | Applied check | Solved | Unknown | Tokens |', '|---|---:|---:|---:|---:|---:|---:|']
    for r in summaries:
        lines.append(f'| {r["method"]} | {r["n"]} | {r["generated"]} | {r["applied_check"]} | {r["resolved"]} | {r["unknown"]} | {r["total_tokens"]} |')
    lines += ['', 'Paired outcomes (unknown pairs excluded from win/loss counts):', '', '```json', json.dumps(paired, indent=2), '```']
    (batch / 'analysis.md').write_text('\n'.join(lines) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-dir', type=Path, required=True)
    args = parser.parse_args()
    analyze(args.batch_dir.resolve())
    print(args.batch_dir / 'analysis.md')
