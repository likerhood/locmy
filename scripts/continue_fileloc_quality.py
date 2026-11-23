"""Guarded staged development run, never full benchmark/server work or auto-push."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from score_fileloc_pairs import valid_result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['workspace','baseline-root','case-manifest','env-file','run-dir','full-reference-run-dir']:
        p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    initial=json.loads((args.run_dir/'manifest.json').read_text())
    def event(stage,**extra):
        value=dict(stage=stage,**extra)
        print(json.dumps(value,ensure_ascii=False),flush=True)
        with (args.run_dir/f'stage_event_{time.time_ns()}.json').open('x') as h:json.dump(value,h,ensure_ascii=False,indent=2)
    def unchanged():
        for rel,sha in initial['implementation_hashes'].items():
            if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()!=sha:
                raise RuntimeError('Source drift: use a fresh experiment directory')
        for name,sha in initial['harness_hashes'].items():
            if hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest()!=sha:
                raise RuntimeError('Harness drift: do not mix protocol versions')
    def inspect(run):
        manifest=json.loads((run/'manifest.json').read_text())
        issues=[]
        for c in manifest['cases']:
            directory=run/c['instance_id']/'improved'
            try:
                execution=json.loads((directory/'execution.json').read_text())
                raw=json.loads((directory/'localization_results.jsonl').read_text().splitlines()[-1])
                assert execution.get('returncode')==0 and valid_result(raw),'invalid_or_partial_execution'
                loc=raw['localization']
                assert loc.get('dynamic_rounds'),'no_rounds'
                for rd in loc['dynamic_rounds']:
                    review=rd['verifier']['llm_candidate_review']
                    assert review.get('coverage_batches'),'coverage_not_executed'
                    assert review.get('reviewed_count')==review.get('candidate_count'),'incomplete_review_group'
                ranking=loc['best_round_selection']['fileloc_listwise']
                assert ranking['status']=='ok','listwise_fallback_or_skipped'
                assert len(ranking['before'])==len(ranking['after']) and set(ranking['before'])==set(ranking['after']),'candidate_set_changed'
            except Exception as exc:
                issues.append(dict(instance_id=c['instance_id'],error=f'{type(exc).__name__}: {exc}'))
        return issues
    def score(run,reference):
        unchanged()
        subprocess.run([sys.executable,str(ROOT/'scripts/score_fileloc_v2.py'),'--workspace',str(args.workspace),
            '--run-dir',str(run),'--reference-run-dir',str(reference),'--report-prefix','final_scores'],cwd=ROOT,check=True)
        return json.loads((run/'final_scores.json').read_text())
    event('waiting_for_eight_quality_cases')
    started=time.monotonic()
    while not all((args.run_dir/c['instance_id']/'improved/execution.json').exists() for c in initial['cases']):
        unchanged()
        if time.monotonic()-started>15000:
            event('blocked',reason='initial_dispatch_watchdog');return
        time.sleep(20)
    first=score(args.run_dir,Path(initial['reference']['run_dir']))
    issues=inspect(args.run_dir)
    if issues:
        event('needs_mechanism_diagnosis',issues=issues,note='No automatic full32 dispatch or release');return
    common=[sys.executable,str(ROOT/'scripts/fileloc_v2_smoke.py'),'--workspace',str(args.workspace),
        '--baseline-root',str(args.baseline_root),'--case-manifest',str(args.case_manifest),
        '--env-file',str(args.env_file),'--model',initial['model'],'--quality','--workers','4']
    full=args.run_dir.parent/'quality_full_r1'
    event('dispatching_frozen_full32',output=str(full),note='Local development panel only; external methods unchanged')
    unchanged()
    subprocess.run(common+['--stage','full','--reference-run-dir',str(args.full_reference_run_dir),'--run-dir',str(full)],cwd=ROOT,check=True)
    full_scores=score(full,args.full_reference_run_dir)
    issues=inspect(full)
    if issues or not full_scores['assessments']['improved']['passed']:
        event('needs_accuracy_or_execution_diagnosis',issues=issues,assessment=full_scores['assessments'],
              note='No publication; retain all results');return
    repeat=args.run_dir.parent/'quality_sentinels_repeat_r1'
    event('dispatching_independent_eight_repeat',output=str(repeat))
    unchanged()
    subprocess.run(common+['--stage','sentinels','--reference-run-dir',initial['reference']['run_dir'],
                          '--run-dir',str(repeat)],cwd=ROOT,check=True)
    repeated=score(repeat,Path(initial['reference']['run_dir']))
    issues=inspect(repeat)
    deltas=[v['net_metric_sums'] for v in repeated['assessments']['improved']['datasets'].values()]
    stable=not issues and all(all(x>=-1e-12 for level in d.values() for x in level.values()) and
                             any(d['file'][f'acc@{k}']>0 for k in [1,3,5]) for d in deltas)
    event('ready_for_final_regression_and_manual_delivery' if stable else 'repeat_not_stable',
          issues=issues,assessment=repeated['assessments'],note='No automatic commit or push; verify regression, fingerprint and origin')


if __name__=='__main__':
    main()
