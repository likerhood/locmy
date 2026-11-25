"""Paired development checks using fixed v1 task-time evidence.

This is a new localization/seed model run, with cached upstream evidence. It
is not a new end-to-end benchmark. Gold is loaded only after outputs are saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--model', default='mimo-v2.6-pro')
    parser.add_argument('--phase', choices=['seed', 'localize'], default='seed')
    parser.add_argument('--instance-id', action='append', required=True)
    args = parser.parse_args()
    if args.run_dir.exists():
        raise SystemExit('Use a new run directory; existing outputs are preserved')
    from mycode.utils.env import load_env_file
    load_env_file(args.env_file)
    os.environ.update(LOAD_ENV_FILE='0', LOC_CODE_ROOT=str(args.workspace),
        MODEL_API_NAME=args.model, CONTROLLER_MODEL_API_NAME=args.model,
        MYCODE_FAST_SEED_PLANNER='1', MYCODE_FAST_SEED_LLM='1',
        MYCODE_READ_EXPLICIT_PATHS='1', MYCODE_JOINT_EVIDENCE_V2='1',
        MYCODE_FUNCTION_SOURCE_LEDGER='1', MYCODE_FILE_FRONT_RERANK='verified_frontier',
        MYCODE_FUNCTION_FINAL_POLICY='consensus', MYCODE_LLM_CANDIDATE_REVIEW='1',
        MYCODE_LLM_REVIEW_CANDIDATES='6', MYCODE_LLM_THINKING='disabled',
        MYCODE_LLM_RETRIES='1')
    from mycode.agent.pipeline import _make_controller_llm
    from mycode.dynamic_retrieval.fast_seed_planner import plan_fast_seeds
    from mycode.dynamic_retrieval.search_agent import _build_localization_query_groups, dynamic_localize
    from mycode.evidence.issue_sketch import build_issue_sketch, semantic_issue_text
    from mycode.repo_index.structure_index import RepositoryIndex
    from mycode.repo_index.repo_locator import find_repo_structure
    from mycode.schemas.evidence import NormalizedSample

    bundle = args.workspace / ('collected_results/swe92_new/results/服务器导入_fileloc_v1_20261123/'
        'verified_extract/swe92_mimo26_magnet_fileloc_v1_complete_f5163fb_20261123')
    selected = {}
    for line in (bundle / 'results/localization_results.jsonl').open():
        row = json.loads(line)
        if row['instance_id'] in args.instance_id and row.get('status') == 'ok':
            selected[row['instance_id']] = row['evidence']
    source = args.workspace / 'clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl'
    whitelist = {'instance_id', 'repo', 'base_commit', 'problem_statement', 'image_assets',
                 'image_urls', 'web_urls', 'language'}
    samples = {row['instance_id']: {k: v for k, v in row.items() if k in whitelist}
               for row in map(json.loads, source.open()) if row['instance_id'] in args.instance_id}
    assert set(selected) == set(samples) == set(args.instance_id), 'Missing requested inputs'
    args.run_dir.mkdir(parents=True)
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / 'src/mycode').rglob('*.py'))}
    manifest = dict(base_commit='f5163fb', branch=subprocess.check_output(
        ['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        source_hashes=hashes, model=args.model, phase=args.phase, cases=args.instance_id,
        dynamic_rounds=2, react_steps=4, top_k=15,
        protocol='paired v1 localization with cached task-time upstream evidence; not full E2E')
    manifest['runner_sha256'] = sha(Path(__file__))
    manifest['fixed_environment'] = {key: os.environ[key] for key in (
        'MYCODE_FAST_SEED_PLANNER', 'MYCODE_FAST_SEED_LLM', 'MYCODE_READ_EXPLICIT_PATHS',
        'MYCODE_JOINT_EVIDENCE_V2', 'MYCODE_FUNCTION_SOURCE_LEDGER', 'MYCODE_FILE_FRONT_RERANK',
        'MYCODE_FUNCTION_FINAL_POLICY', 'MYCODE_LLM_CANDIDATE_REVIEW',
        'MYCODE_LLM_REVIEW_CANDIDATES', 'MYCODE_LLM_THINKING', 'MYCODE_LLM_RETRIES')}
    (args.run_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for relative in hashes:
        destination = args.run_dir / 'source_snapshot' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    shutil.copyfile(Path(__file__), args.run_dir / 'source_snapshot' / Path(__file__).name)
    outcomes = []
    for number, iid in enumerate(args.instance_id):
        raw = samples[iid]
        evidence = {k: selected[iid][k] for k in ('evidence_packet', 'evidence_synthesis',
            'tool_observations', 'deterministic_understanding', 'llm_understanding') if k in selected[iid]}
        structure = find_repo_structure(iid, 'swebench_multimodal-full-dev', raw['base_commit'])
        if not structure:
            raise RuntimeError('Missing canonical structure: ' + iid)
        index = RepositoryIndex(repo=raw['repo'], instance_id=iid,
            dataset='swebench_multimodal-full-dev', repo_root=ROOT / '_no_shared_checkout',
            structure_path=structure)
        assert index.ready
        sample = NormalizedSample(instance_id=iid, repo=raw['repo'],
            dataset='swebench_multimodal-full-dev', issue_text=semantic_issue_text(raw['problem_statement']), raw=raw)
        input_payload = dict(sample=raw, evidence=evidence, structure=str(structure), structure_sha256=sha(structure))
        (args.run_dir / f'{iid}.input.json').write_text(json.dumps(input_payload, ensure_ascii=False))
        for arm in (['baseline', 'improved'] if number % 2 == 0 else ['improved', 'baseline']):
            assert all(sha(ROOT / p) == value for p, value in hashes.items()), 'Source changed during run'
            os.environ['MYCODE_GALA_GLOBAL_FILES'] = '1' if arm == 'improved' else '0'
            events = []
            controller = _make_controller_llm(True)

            def logged(prompt):
                try:
                    response = controller(prompt, max_tokens=1600)
                except Exception as exc:
                    code = re.search(r'HTTP (\d{3})', str(exc))
                    events.append(dict(error_type=type(exc).__name__,
                        http_status=int(code.group(1)) if code else None))
                    raise
                events.append(dict(prompt=prompt, content=response['content'], usage=response['usage']))
                return response

            print('RUN', iid, arm, args.phase, flush=True)
            start = time.monotonic()
            if args.phase == 'seed':
                sketch = build_issue_sketch(sample, evidence)
                groups = _build_localization_query_groups(sample, evidence, sketch,
                    evidence.get('tool_observations', []))
                result = plan_fast_seeds(index=index, issue_text=sample.issue_text,
                    query_groups=groups, evidence_result=evidence, controller_llm=logged)
                paths = result['seed_files']
            else:
                result = dynamic_localize(sample, evidence, index=index, top_k=15,
                    max_rounds=2, max_react_steps=4, controller_llm=logged)
                paths = [r['path'] for r in result['ranked_locations']]
            output = dict(instance_id=iid, arm=arm, phase=args.phase,
                elapsed_seconds=time.monotonic() - start, paths=paths, result=result, events=events)
            output['valid'] = not any('error_type' in event for event in events)
            if args.phase == 'seed':
                output['valid'] = output['valid'] and result.get('llm_status') == 'ok'
            else:
                output['valid'] = output['valid'] and bool(result.get('dynamic_rounds')) and all(
                    r.get('verifier', {}).get('llm_candidate_review', {}).get('status') == 'ok'
                    for r in result.get('dynamic_rounds', []))
            (args.run_dir / f'{iid}.{arm}.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
            outcomes.append(output)
            print('DONE', iid, arm, round(output['elapsed_seconds'], 1), paths[:3], flush=True)
    # Post-hoc scoring only, after every model result has been saved.
    gold = json.loads((args.workspace / 'collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json').read_text())
    scored = []
    from mycode.evaluation.localization_eval import evaluate_file_ranking, evaluate_entity_ranking

    def entity(value):
        if isinstance(value, dict):
            value = value.get('id') or f"{value['path']}::{value.get('kind', 'entity')}:{value.get('name', '')}"
        path, sep, name = str(value).partition('::')
        if sep and name.split(':', 1)[0] in {'function', 'method', 'class', 'module'}:
            name = name.split(':', 1)[1]
        return path + sep + name

    for output in outcomes:
        gs = set(gold[output['instance_id']]['files'])
        rank = next((n for n, p in enumerate(output['paths'], 1) if p in gs), 0)
        score = dict(instance_id=output['instance_id'], arm=output['arm'], first_rank=rank,
            valid=output['valid'],
            **{f'A{k}': int(0 < rank <= k) for k in [1, 3, 5, 8, 15]})
        score['file_metrics'] = evaluate_file_ranking(output['paths'], list(gs))
        if args.phase == 'localize':
            functions = output['result'].get('ranked_functions', [])[:15]
            expected = gold[output['instance_id']]['functions']
            score['function_gold_nonempty'] = bool(expected)
            score['function_metrics'] = evaluate_entity_ranking(
                [entity(value) for value in functions], [entity(value) for value in expected])
        scored.append(score)
    (args.run_dir / 'posthoc_scores.json').write_text(json.dumps(scored, indent=2, ensure_ascii=False))
    print(json.dumps(scored, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
