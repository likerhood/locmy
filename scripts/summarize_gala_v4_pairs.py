"""Combine disjoint development batches only when code and budgets match."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Existing assessment must be preserved')
    reference = None
    ids = set()
    rows = []
    for folder in args.run_dir:
        manifest = json.loads((folder / 'manifest.json').read_text())
        protocol = {k: manifest[k] for k in ('source_hashes', 'model', 'phase', 'variant',
                    'arms', 'fixed_environment', 'dynamic_rounds', 'react_steps', 'top_k')}
        if reference is None:
            reference = protocol
        assert protocol == reference, 'Different code/configuration: do not pool'
        assert not ids.intersection(manifest['cases']), 'Duplicate cases: do not double count'
        ids.update(manifest['cases'])
        comparison = json.loads((folder / 'paired_stage_comparison.json').read_text())
        paired = [r for r in comparison['rows'] if r['method'].startswith('paired_')]
        assert len(paired) == 2 * len(manifest['cases'])
        assert all(row.get('valid') for row in paired), 'Failed calls cannot count as misses'
        rows.extend(comparison['rows'])
    summary = []
    metrics = [f'acc@{k}' for k in range(1, 11)] + ['acc@15', 'set_sl@15', 'set_rec@15']
    for method, model in sorted({(r['method'], r['model']) for r in rows}):
        subset = [r for r in rows if (r['method'], r['model']) == (method, model)]
        assert {r['instance_id'] for r in subset} == ids, 'Historical task set differs'
        applicable = [r for r in subset if r['function_gold_nonempty']]
        row = dict(method=method, model=model, N=len(subset), function_N=len(applicable),
            file={k: sum(r['file'][k] for r in subset) / len(subset) * 100 for k in metrics},
            function={k: sum(r['function'][k] for r in applicable) / len(applicable) * 100
                      for k in metrics} if applicable else {})
        summary.append(row)
    result = dict(protocol='Diagnostic development batches, not population performance or matched historical reruns',
                  runs=[str(p) for p in args.run_dir], cases=sorted(ids), summary=summary, rows=rows)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
