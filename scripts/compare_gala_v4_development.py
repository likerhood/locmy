"""Post-hoc paired scoring and stage audit; never supplies gold to an agent."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mycode.evaluation.localization_eval import evaluate_file_ranking, evaluate_entity_ranking


def entity(value):
    if isinstance(value, dict):
        value = value.get('id') or f"{value['path']}::{value.get('name', '')}"
    path, sep, name = str(value).partition('::')
    if sep and name.split(':', 1)[0] in {'function', 'method', 'class', 'module'}:
        name = name.split(':', 1)[1]
    return path + sep + name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.run_dir / 'manifest.json').read_text())
    files = [args.run_dir / f'{iid}.{arm}.json'
             for iid in manifest['cases'] for arm in ('baseline', 'improved')]
    if not all(p.exists() for p in files):
        raise SystemExit('Incomplete run; do not produce a complete-pair assessment')
    gold_path = args.workspace / 'collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json'
    gold = json.loads(gold_path.read_text())

    def score(iid, paths, functions):
        gs = gold[iid]
        return dict(first_rank=next((n for n, p in enumerate(paths[:15], 1) if p in gs['files']), None),
                    file=evaluate_file_ranking(paths[:15], gs['files']),
                    function_gold_nonempty=bool(gs['functions']),
                    function=evaluate_entity_ranking([entity(v) for v in functions[:15]],
                                                    [entity(v) for v in gs['functions']]))

    rows = []
    for path in files:
        out = json.loads(path.read_text()); iid = out['instance_id']; loc = out['result']
        invalid_stages = []
        for rd in loc.get('dynamic_rounds', []):
            verifier = rd.get('verifier', {})
            bridge = verifier.get('nomination_bridge', {})
            for stage, review in [('merged', verifier.get('llm_candidate_review', {}))] + (
                    [('primary', bridge.get('primary_review', {})),
                     ('supplemental', bridge.get('supplemental_review', {}))]
                    if bridge.get('additive_review') else []):
                if review.get('status') != 'ok':
                    invalid_stages.append(dict(round=rd['round_no'], stage=stage,
                                               status=review.get('status')))
        row = dict(instance_id=iid, method='paired_' + out['arm'], model=manifest['model'],
                   valid=out['valid'] and not invalid_stages, invalid_stages=invalid_stages,
                   elapsed_seconds=out['elapsed_seconds'],
                   **score(iid, out['paths'], loc.get('ranked_functions', [])))
        gs = set(gold[iid]['files'])
        def first(paths):
            return next((n for n, p in enumerate(paths, 1) if p in gs), None)
        seeds = next((t for t in loc.get('search_trace', []) if t.get('step') == 'fast_seed_planner'), {})
        row['seed'] = {k: seeds.get(k) for k in ('seed_files', 'llm_status', 'global_file_selector')}
        row['stages'] = {}
        for key, stage in loc.get('rank_stage_snapshots', {}).items():
            stages = stage if isinstance(stage, dict) and key == 'rounds' else {key: stage}
            for name, values in stages.items():
                if isinstance(values, list):
                    paths = [v['path'] for v in values]
                    row['stages'][('round_' + name) if key == 'rounds' else name] = dict(
                        first_rank=first(paths), paths=paths)
        row['rounds'] = []
        for rd in loc.get('dynamic_rounds', []):
            contexts = rd.get('code_contexts', [])
            review = rd.get('verifier', {}).get('llm_candidate_review', {})
            row['rounds'].append(dict(round=rd['round_no'], review_status=review.get('status'),
                read_gold=[v['path'] for v in contexts if v['path'] in gs],
                gold_reviews=[v for v in review.get('candidates', []) if v.get('path') in gs],
                stop=rd.get('stop_decision')))
        rows.append(row)

    historical = args.workspace / 'collected_results/swe92_new/rq1详细分析/data/predictions.json'
    for pred in json.loads(historical.read_text()):
        if (pred['instance_id'] in manifest['cases'] and pred['method'] in {'CoSIL', 'LocAgent', 'GALA'}
                and pred['model'] in {'MiMo-v2.5', 'Qwen3.5-397B-A17B'}):
            rows.append(dict(instance_id=pred['instance_id'], method='historical_' + pred['method'],
                             model=pred['model'], **score(pred['instance_id'], pred['files'], pred['functions'])))
    bundle = args.workspace / ('collected_results/swe92_new/results/服务器导入_fileloc_v1_20261123/'
        'verified_extract/swe92_mimo26_magnet_fileloc_v1_complete_f5163fb_20261123/results/localization_results.jsonl')
    selected = {}
    for line in bundle.open():
        out = json.loads(line)
        if out['instance_id'] in manifest['cases'] and out.get('status') == 'ok':
            selected[out['instance_id']] = out
    for iid, out in selected.items():
        loc = out['localization']
        rows.append(dict(instance_id=iid, method='historical_MAGNET_v1', model='MiMo-v2.6-pro',
            **score(iid, [v['path'] for v in loc['ranked_locations']], loc.get('ranked_functions', []))))
    payload = dict(protocol='Post-hoc frozen-gold scoring; historical methods are not matched-budget reruns',
                   gold_sha256=hashlib.sha256(gold_path.read_bytes()).hexdigest(), rows=rows)
    destination = args.run_dir / 'paired_stage_comparison.json'
    if destination.exists():
        raise SystemExit('Assessment already exists; preserve it')
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    for row in rows:
        print(row['instance_id'], row['method'], row['model'], 'rank=', row['first_rank'],
              'valid=', row.get('valid', 'historical'))


if __name__ == '__main__':
    main()
