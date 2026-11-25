"""Same-search-trajectory final-function ablation; not a new model run."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--instance-id', required=True)
    args = parser.parse_args()
    target = args.run_dir / (args.instance_id + '.reservoir_ablation.json')
    if target.exists():
        raise SystemExit('Preserve existing ablations')
    os.environ.update(LOAD_ENV_FILE='0', MYCODE_JOINT_EVIDENCE_V2='1')
    from mycode.dynamic_retrieval.search_agent import (
        RankedEntity, _reread_function_ledger, _joint_round_consensus, _closure_context_map, _issue_query_text)
    from mycode.repo_index.structure_index import RepositoryIndex
    from mycode.evidence.issue_sketch import semantic_issue_text
    from mycode.evaluation.localization_eval import evaluate_entity_ranking
    from compare_gala_v4_development import entity
    import hashlib
    payload = json.loads((args.run_dir / (args.instance_id + '.input.json')).read_text())
    structure = Path(payload['structure'])
    assert hashlib.sha256(structure.read_bytes()).hexdigest() == payload['structure_sha256']
    sample = payload['sample']
    index = RepositoryIndex(repo=sample['repo'], instance_id=args.instance_id,
        dataset='swebench_multimodal-full-dev', repo_root=ROOT / '_no_shared_checkout', structure_path=structure)
    saved = json.loads((args.run_dir / (args.instance_id + '.improved.json')).read_text())
    assert saved['valid'], 'Do not ablate an invalid model run'
    result = saved['result']
    rounds = result['dynamic_rounds']
    rankings = [[RankedEntity(**row) for row in rd['ranked_functions']] for rd in rounds]
    selected = next(ranking for ranking, rd in zip(rankings, rounds)
                    if rd['round_no'] == result['best_round_selection']['selected_round'])
    extra = [RankedEntity(**row) for rd in rounds
             for row in rd['verifier'].get('supplemental_function_candidates', [])]
    contexts = _closure_context_map([ctx for rd in rounds for ctx in rd['code_contexts']])
    issue = _issue_query_text(semantic_issue_text(sample['problem_statement']))
    report = dict(protocol='Same saved model trajectory; only final source-memory ablation, not new online accuracy',
                  instance_id=args.instance_id, supplemental_candidates=len(extra), arms={})
    for enabled in (False, True):
        ledger_rankings = rankings + ([extra[i:i+12] for i in range(0, len(extra), 12)] if enabled else [])
        ledger = _reread_function_ledger(index, ledger_rankings, issue,
                                       limit=48 if enabled and extra else 24)
        functions, log = _joint_round_consensus(selected, rankings, key=lambda row: row.id,
            contexts=contexts, top_k=15, entity_evidence=ledger, extra_candidates=extra if enabled else ())
        report['arms']['enabled' if enabled else 'disabled'] = dict(
            functions=[row.to_dict() for row in functions], log=log)
    assert [r['id'] for r in report['arms']['enabled']['functions']] == [
        r['id'] for r in result['ranked_functions']], 'Replay failed to reproduce saved final functions'
    # Evaluation labels are used only after both immutable predictions exist.
    workspace = Path(os.environ.get('LOC_CODE_ROOT', '/home/like/locCode'))
    gold = json.loads((workspace / 'collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json').read_text())
    for arm in report['arms'].values():
        arm['metrics'] = evaluate_entity_ranking([entity(row) for row in arm['functions']],
                                               [entity(row) for row in gold[args.instance_id]['functions']])
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(args.instance_id, 'extra candidates', len(extra))
    for name, arm in report['arms'].items():
        print(name, {key: arm['metrics'][key] for key in ('acc@1', 'acc@5', 'acc@15', 'set_rec@15')},
              'recovered', arm['log']['recovered'])


if __name__ == '__main__':
    main()
