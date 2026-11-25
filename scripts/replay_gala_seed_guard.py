"""Replay the seed-prior guard against frozen real v1/v2 stage trajectories.

No model call or new search. Scores measure this exact saved-stage permutation,
not final online accuracy. Gold is used only after guard outputs are produced.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mycode.dynamic_retrieval.seed_frontier import apply_verified_seed_guard
from mycode.dynamic_retrieval.search_agent import _merge_closure_candidate_reviews
from mycode.evaluation.localization_eval import evaluate_file_ranking


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(ROOT / 'src/mycode/dynamic_retrieval/seed_frontier.py',
                    args.output_dir / 'seed_frontier_source.py')
    provenance = []
    all_outputs = []
    for version, date, commit in [('v1', '20261123', 'f5163fb'), ('v2', '20261124', '30d1e32')]:
        folder = args.workspace / f'collected_results/swe92_new/results/服务器导入_fileloc_{version}_{date}/verified_extract'
        bundle = folder / f'swe92_mimo26_magnet_fileloc_{version}_complete_{commit}_{date}'
        audit = args.workspace / f'allresultwork/rq1/服务器全量{version}审计_{date}/统计输出_复核版/per_task_metrics.json'
        selected = {r['instance_id']: r['source_line'] for r in json.loads(audit.read_text())
                    if r['method'].startswith('MAGNET')}
        found = set()
        for number, line in enumerate((bundle / 'results/localization_results.jsonl').open(), 1):
            record = json.loads(line)
            ident = record['instance_id']
            if selected.get(ident) != number:
                continue
            loc = record['localization']
            stages = loc['rank_stage_snapshots']
            incumbent = [SimpleNamespace(**r) for r in stages['best_round']]
            restored = [SimpleNamespace(**r) for r in stages['seed_retention']]
            retained = loc['best_round_selection']['seed_retention']['retained']
            review = _merge_closure_candidate_reviews([r.get('verifier', {}) for r in loc['dynamic_rounds']])
            guarded, trace = apply_verified_seed_guard(restored, incumbent, retained, review)
            before, after = [r.path for r in restored], [r.path for r in guarded]
            assert len(before) == len(after) and set(before) == set(after)
            all_outputs.append(dict(version=version, instance_id=ident, source_line=number,
                before=before, after=after, trace=trace,
                protected_reviews=[r for r in review['candidates'] if r.get('path') in
                                   {change['path'] for change in trace['changes']}]))
            found.add(ident)
        assert found == set(selected) and len(found) == 92
        provenance.append(dict(version=version, audit_sha256=hashlib.sha256(audit.read_bytes()).hexdigest()))
    (args.output_dir / 'stage_outputs_before_scoring.json').write_text(json.dumps(all_outputs, ensure_ascii=False, indent=2))
    gold = json.loads((args.workspace / 'collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json').read_text())
    summary = []
    for version in ['v1', 'v2']:
        subset = [r for r in all_outputs if r['version'] == version]
        for arm in ['before', 'after']:
            scores = [evaluate_file_ranking(r[arm], gold[r['instance_id']]['files']) for r in subset]
            summary.append(dict(version=version, arm=arm, N=len(scores), changed=sum(r['before']!=r['after'] for r in subset),
                **{key: sum(s[key] for s in scores)/len(scores)*100 for key in [
                    'acc@1', 'acc@3', 'acc@5', 'acc@8', 'acc@15', 'set_sl@15', 'set_rec@15']}))
        for row in subset:
            for arm in ['before', 'after']:
                row[arm + '_metrics'] = evaluate_file_ranking(row[arm], gold[row['instance_id']]['files'])
    module = ROOT / 'src/mycode/dynamic_retrieval/seed_frontier.py'
    assessment = dict(protocol='frozen real seed-retention stage replay; not online or final pipeline accuracy',
        guard_source_sha256=hashlib.sha256(module.read_bytes()).hexdigest(), provenance=provenance,
        metrics=summary, cases=all_outputs)
    (args.output_dir / 'assessment.json').write_text(json.dumps(assessment, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
