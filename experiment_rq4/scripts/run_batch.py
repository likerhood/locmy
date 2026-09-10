#!/usr/bin/env python3
"""Sequential, restartable repair+test batch. Default mode check makes no API calls."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from analyze_batch import analyze, official_reports
from preflight import load_env

ROOT = Path(__file__).resolve().parents[1]


def read_rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def repo_for(sample):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', sample['repo']):
        raise ValueError('Invalid repo name')
    path = ROOT / 'repos' / sample['repo'].replace('/', '__')
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        subprocess.run(['git', 'clone', '--no-checkout', 'https://github.com/' + sample['repo'] + '.git', str(path)], check=True)
    probe = subprocess.run(['git', '-C', str(path), 'cat-file', '-e', sample['base_commit']+'^{commit}'], capture_output=True)
    if probe.returncode:
        subprocess.run(['git', '-C', str(path), 'fetch', 'origin', sample['base_commit']], check=True)
    return path


def harness(batch, method, predictions, dataset_file, workers, timeout):
    directory = batch / 'evaluation' / method
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT/'scripts/official_eval.py'),
               '--dataset', str(dataset_file), '--predictions', str(predictions),
               '--workers', str(workers), '--timeout', str(timeout),
               '--run-id', 'rq4_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16]]
    if method == 'control_noop':
        command.append('--no-op')
    with (directory / 'harness.log').open('a') as handle:
        subprocess.run(command, cwd=directory, stdout=handle, stderr=subprocess.STDOUT, check=True)


def validate_eval(dataset_file, samples):
    rows = {r['instance_id']: r for r in read_rows(dataset_file)}
    for sample in samples:
        row = rows[sample['instance_id']]
        if row['base_commit'] != sample['base_commit'] or row['repo'] != sample['repo']:
            raise ValueError('Evaluator dataset revision mismatch')
        for field in ['image', 'eval_script', 'log_parser', 'eval_type', 'FAIL_TO_PASS', 'PASS_TO_PASS', 'patch']:
            if field not in row:
                raise ValueError(f'Missing evaluator field {field}; run prepare_harness_dataset.py')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', choices=['swe', 'omni'], default='swe')
    p.add_argument('--methods', nargs='+', default=['locagent', 'cosil', 'gala', 'graphlocator', 'magnet'])
    p.add_argument('--mode', choices=['check', 'generate', 'evaluate', 'all'], default='check')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--run-id', default='swe_qwen_k1_v1')
    p.add_argument('--env-file', type=Path, default=ROOT/'.env.local')
    p.add_argument('--eval-dataset', type=Path, default=ROOT/'data/evaluation_only/swe50.harness.jsonl')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--test-timeout', type=int, default=1800)
    args = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run_id) or not 1 <= args.limit <= 50 or args.workers < 1:
        p.error('Invalid run-id, limit, or workers')
    if len(set(args.methods)) != len(args.methods) or not set(args.methods) <= {'magnet','locagent','cosil','gala','graphlocator'}:
        p.error('Invalid/duplicate methods')
    load_env(args.env_file)
    samples = read_rows(ROOT/'data/inputs'/f'{args.dataset}50.jsonl')[:args.limit]
    hashes = {}
    source_paths = [ROOT/'configs/protocol.json', ROOT/'data/inputs'/f'{args.dataset}50.jsonl', ROOT/'scripts/repair.py']
    for method in args.methods:
        path = ROOT/'normalized'/args.dataset/f'{method}.jsonl'
        if not path.exists():
            p.error(f'Missing {args.dataset}/{method} Qwen predictions; no paid calls started')
        rows = {r['instance_id']: r for r in read_rows(path)}
        if any(rows.get(r['instance_id'],{}).get('status') != 'available' for r in samples):
            p.error('Incomplete prediction coverage; no paid calls started')
        source_paths.append(path)
    for path in source_paths:
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if args.mode == 'check':
        print(json.dumps({'dataset':args.dataset,'samples':len(samples),'methods':args.methods,
                          'planned_api_calls':len(samples)*len(args.methods),'api_configured':bool(os.getenv('RQ4_API_KEY')),
                          'eval_dataset_exists':args.eval_dataset.exists(), 'status':'offline_check_no_calls'},indent=2))
        return
    if args.mode in ['generate','all'] and not all(os.getenv(k) for k in ['RQ4_MODEL','RQ4_API_KEY','RQ4_BASE_URL']):
        p.error('API configuration missing')
    if args.mode in ['evaluate','all']:
        if args.dataset != 'swe':
            p.error('Omni official harness adapter not configured; generation-only is available after predictions supplied')
        validate_eval(args.eval_dataset, samples)
        subprocess.run(['docker','info'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable,'-c','import swebench.harness.run_evaluation'],check=True)
        hashes['evaluator_dataset'] = hashlib.sha256(args.eval_dataset.read_bytes()).hexdigest()
    batch = ROOT/'runs/batches'/args.run_id
    batch.mkdir(parents=True,exist_ok=True)
    with (batch/'.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest = {'dataset':args.dataset,'methods':args.methods,'instance_ids':[r['instance_id'] for r in samples],
                    'hashes':hashes,'model':os.getenv('RQ4_MODEL',''), 'endpoint_hash':hashlib.sha256(os.getenv('RQ4_BASE_URL','').encode()).hexdigest(),
                    'protocol':'development_byte_budget_K1','test_timeout':args.test_timeout}
        existing = batch/'manifest.json'
        if existing.exists():
            previous = json.loads(existing.read_text())
            # A generation-only batch can later acquire evaluator metadata, but not change it.
            candidate = dict(manifest); candidate['hashes'] = dict(hashes)
            if 'evaluator_dataset' not in previous['hashes']:
                candidate['hashes'].pop('evaluator_dataset',None)
            if previous != candidate:
                raise ValueError('Batch configuration changed; use a new run-id')
        save(existing,manifest)
        # Validate environments BEFORE paying for generation in all mode.
        if args.mode == 'all':
            full = {r['instance_id']:r for r in read_rows(args.eval_dataset)}
            subset = batch/'eval_subset.jsonl'
            subset.write_text(''.join(json.dumps(full[r['instance_id']])+'\n' for r in samples))
            for label,gold in [('control_gold',True),('control_noop',False)]:
                predictions = batch/f'{label}.jsonl'
                # no-op uses official run_instances(skip_patch=True), not an invalid fake diff.
                predictions.write_text(''.join(json.dumps({'instance_id':r['instance_id'], 'model_name_or_path':label,
                    'model_patch':full[r['instance_id']]['patch'] if gold else ''})+'\n' for r in samples))
                harness(batch,label,predictions,subset,args.workers,args.test_timeout)
                reports=official_reports(batch/'evaluation'/label)
                if any(reports.get(r['instance_id'],{}).get('resolved') is not gold for r in samples):
                    save(batch/'control_failure.json',{'control':label,'expected_resolved':gold,
                         'failed_or_missing':[r['instance_id'] for r in samples if reports.get(r['instance_id'],{}).get('resolved') is not gold]})
                    analyze(batch)
                    raise RuntimeError(f'{label} failed/missing. See harness.log; no model generation started')
        try:
            if args.mode in ['generate','all']:
                for method in args.methods:
                    for sample in samples:
                        instance = sample['instance_id']
                        record_path = batch/'records'/method/f'{instance}.json'
                        if record_path.exists():
                            print(f'Skip recorded attempt {method}/{instance}',flush=True)
                            continue
                        out = batch/'attempts'/method/instance
                        start=time.monotonic()
                        save(record_path,{'status':'started','instance_id':instance})
                        try:
                            repo=repo_for(sample)
                            command=[sys.executable,str(ROOT/'scripts/repair.py'),'--dataset',args.dataset,'--method',method,
                                '--instance-id',instance,'--repo',str(repo),'--env-file',str(args.env_file.resolve()),
                                '--output-dir',str(out),'--execute']
                            out.parent.mkdir(parents=True,exist_ok=True)
                            with (out.parent/f'{instance}.log').open('w') as log:
                                result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
                            if not (out/'prediction.jsonl').exists():
                                raise RuntimeError(f'No prediction; subprocess exit={result.returncode}')
                            prediction=json.loads((out/'prediction.jsonl').read_text())
                            subprocess.run([sys.executable,str(ROOT/'scripts/check_patch.py'),'--run-dir',str(out)],check=True,stdout=subprocess.DEVNULL)
                            applied=json.loads((out/'application_check.json').read_text())['applied']
                            record={'status':'completed','prediction':prediction,'applied':applied}
                        except Exception as exc:
                            record={'status':'execution_error','error_type':type(exc).__name__,'instance_id':instance}
                        record['elapsed_seconds']=time.monotonic()-start
                        save(record_path,record)
                        print(f'{method}/{instance}: {record["status"]}',flush=True)
            if args.mode in ['evaluate','all']:
                for method in args.methods:
                    predictions=[]
                    for sample in samples:
                        record_path=batch/'records'/method/f'{sample["instance_id"]}.json'
                        record=json.loads(record_path.read_text()) if record_path.exists() else {}
                        pred=record.get('prediction',{})
                        predictions.append({'instance_id':sample['instance_id'],'model_name_or_path':f'rq4-{method}',
                                            'model_patch':pred.get('model_patch','')})
                    path=batch/f'{method}.predictions.jsonl'
                    path.write_text(''.join(json.dumps(r)+'\n' for r in predictions))
                    harness(batch,method,path,args.eval_dataset,args.workers,args.test_timeout)
        finally:
            analyze(batch)
        print(f'Analysis: {batch / "analysis.md"}')


if __name__=='__main__':
    main()
