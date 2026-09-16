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


ACTIVE_RESOURCES = None


def repo_for(sample):
    from git_cache import repo_for as cached
    return cached(ROOT, sample, ACTIVE_RESOURCES)


def harness(batch, method, predictions, dataset_file, workers, timeout):
    directory = batch / 'evaluation' / method
    directory.mkdir(parents=True, exist_ok=True)
    run_id = 'rq4_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    command = [sys.executable, str(ROOT/'scripts/official_eval.py'),
               '--dataset', str(dataset_file), '--predictions', str(predictions),
               '--workers', '1', '--timeout', str(timeout), '--run-id', run_id]
    if method == 'control_noop':
        command.append('--no-op')
    try:
        with (directory / 'harness.log').open('a') as handle:
            ACTIVE_RESOURCES.run(command, cwd=directory, stdout=handle, stderr=subprocess.STDOUT,
                                 timeout=timeout + 300)
    finally:
        # A terminated harness may not execute its own finally. Remove only its exact containers.
        client = ACTIVE_RESOURCES.client
        if client:
            for container in client.containers.list(all=True, filters={'name': 'sweb.eval.'}):
                if container.name.startswith('sweb.eval.') and (container.name.endswith('.'+run_id) or '.'+run_id+'.' in container.name):
                    container.remove(force=True)


def generate_one(batch, args, sample):
    instance = sample['instance_id']
    for method in args.methods:
        record_path = batch/'records'/method/f'{instance}.json'
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        if record and record.get('status') == 'completed':
            print(f'Skip recorded attempt {method}/{instance}', flush=True)
            continue
        if record and record.get('status') == 'started' and 'note' not in record:
            print(f'Skip legacy uncertain paid attempt {method}/{instance}; inspect before manual recovery', flush=True)
            continue
        # Retryable preparation is deliberately BEFORE the paid-attempt record.
        repo = repo_for(sample)
        ACTIVE_RESOURCES.ensure()
        out = batch/'attempts'/method/instance
        out.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        if record is None:
            save(record_path, {'status':'started', 'instance_id':instance,
                               'note':'Durable paid subcalls are resumable only when response and result both exist.'})
        command = [sys.executable, str(ROOT/'scripts/repair.py'), '--dataset', args.dataset,
                   '--method', method, '--instance-id', instance, '--repo', str(repo),
                   '--env-file', str(args.env_file.resolve()), '--output-dir', str(out), '--execute']
        with (out.parent/f'{instance}.log').open('w') as log:
            protocol = json.loads((ROOT/'configs/protocol.json').read_text())
            paid_calls = 1 + protocol.get('candidates_per_instance', 1)
            request_timeout = int(os.getenv('RQ4_REQUEST_TIMEOUT','180'))
            ACTIVE_RESOURCES.run(command, stdout=log, stderr=subprocess.STDOUT,
                                 timeout=(request_timeout + 30) * paid_calls + 120)
        prediction = read_rows(out/'prediction.jsonl')[0]
        ACTIVE_RESOURCES.run([sys.executable, str(ROOT/'scripts/check_patch.py'), '--run-dir', str(out)])
        applied = json.loads((out/'application_check.json').read_text())['applied']
        save(record_path, {'status':'completed', 'prediction':prediction, 'applied':applied,
                           'elapsed_seconds':time.monotonic()-start})
        print(f'{method}/{instance}: {prediction.get("status")}, applied={applied}', flush=True)
        analyze(batch)


def evaluate_one(batch, args, sample, dataset_file, method, gold=None):
    instance = sample['instance_id']
    reports = official_reports(batch/'evaluation'/method)
    if instance in reports:
        return reports[instance]['resolved']
    if gold is not None:
        patch_text = sample['patch'] if gold else ''
    else:
        path = batch/'records'/method/f'{instance}.json'
        record = json.loads(path.read_text()) if path.exists() else {}
        prediction = record.get('prediction', {})
        if record.get('status') != 'completed' or prediction.get('status') not in ['generated','empty_patch']:
            raise RuntimeError(f'{method}/{instance}: generation incomplete; inspect record, no automatic paid retry')
        patch_text = prediction.get('model_patch','')
        if not patch_text:
            return False
    prediction_file = batch/'eval_inputs'/instance/f'{method}.jsonl'
    prediction_file.parent.mkdir(parents=True, exist_ok=True)
    prediction_file.write_text(json.dumps({'instance_id':instance, 'model_name_or_path':'rq4-'+method,
                                           'model_patch':patch_text})+'\n')
    harness(batch, method, prediction_file, dataset_file, 1, args.test_timeout)
    report = official_reports(batch/'evaluation'/method).get(instance)
    if report is None:
        raise RuntimeError(f'{method}/{instance}: official report missing; inspect harness.log')
    return report['resolved']


def validate_eval(dataset_file, samples):
    rows = {r['instance_id']: r for r in read_rows(dataset_file)}
    for sample in samples:
        row = rows[sample['instance_id']]
        if row['base_commit'] != sample['base_commit'] or row['repo'] != sample['repo']:
            raise ValueError('Evaluator dataset revision mismatch')
        for field in ['image', 'eval_script', 'log_parser', 'eval_type', 'FAIL_TO_PASS', 'PASS_TO_PASS', 'patch', 'test_patch']:
            if field not in row:
                raise ValueError(f'Missing evaluator field {field}; run prepare_harness_dataset.py')


def main():
    global ACTIVE_RESOURCES
    from resources import Resources
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', choices=['swe','omni'], default='swe')
    p.add_argument('--methods', nargs='+', default=['locagent','cosil','gala','graphlocator','magnet'])
    p.add_argument('--mode', choices=['check','generate','evaluate','all'], default='check')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--run-id', default='swe_mimo_serial_v2')
    p.add_argument('--env-file', type=Path, default=ROOT/'.env.local')
    p.add_argument('--eval-dataset', type=Path, default=ROOT/'data/evaluation_only/swe50.harness.jsonl')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--test-timeout', type=int, default=1800)
    p.add_argument('--min-free-gb', type=float)
    p.add_argument('--image-cache', choices=['budget','sample'])
    args = p.parse_args()
    load_env(args.env_file)
    args.min_free_gb = args.min_free_gb if args.min_free_gb is not None else float(os.getenv('RQ4_MIN_FREE_GB','10'))
    args.image_cache = args.image_cache or os.getenv('RQ4_IMAGE_CACHE','budget')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run_id) or not 1 <= args.limit <= 50 or args.workers != 1:
        p.error('Use a valid run-id, limit 1..50, and --workers 1 (serial mode only)')
    if not 0 < args.min_free_gb < float('inf') or args.image_cache not in ['budget','sample'] or args.test_timeout <= 0:
        p.error('Invalid resource limits')
    if len(set(args.methods)) != len(args.methods) or not set(args.methods) <= {'magnet','locagent','cosil','gala','graphlocator'}:
        p.error('Invalid/duplicate methods')
    samples = read_rows(ROOT/'data/inputs'/f'{args.dataset}50.jsonl')[:args.limit]
    for sample in samples:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',sample['instance_id']):
            p.error('Unsafe instance ID')
    source_paths = [ROOT/'configs/protocol.json', ROOT/'data/inputs'/f'{args.dataset}50.jsonl',
                    ROOT/'scripts/repair.py']
    source_paths += [ROOT/'scripts/preflight.py'] if (ROOT/'scripts/preflight.py').exists() else []
    source_paths += [ROOT/'scripts'/name for name in ['run_batch.py','official_eval.py','resources.py','git_cache.py'] if (ROOT/'scripts'/name).exists()]
    source_paths += [ROOT/'configs/harness_lock.json'] if (ROOT/'configs/harness_lock.json').exists() else []
    for method in args.methods:
        path = ROOT/'normalized'/args.dataset/f'{method}.jsonl'
        if not path.exists():
            p.error(f'Missing {args.dataset}/{method} Qwen predictions; no paid calls started')
        rows = {r['instance_id']:r for r in read_rows(path)}
        if any(rows.get(r['instance_id'],{}).get('status') != 'available' for r in samples):
            p.error('Incomplete prediction coverage; no paid calls started')
        source_paths.append(path)
    hashes = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    protocol = json.loads((ROOT/'configs/protocol.json').read_text())
    if args.mode == 'check':
        calls_per_method = 1 + protocol.get('candidates_per_instance', 1)
        print(json.dumps({'dataset':args.dataset,'samples':len(samples),'methods':args.methods,
            'planned_api_calls':len(samples)*len(args.methods)*calls_per_method,
            'calls_per_sample_method':calls_per_method,
            'fine_localization_calls':len(samples)*len(args.methods),
            'repair_candidate_calls':len(samples)*len(args.methods)*protocol.get('candidates_per_instance', 1),
            'api_configured':all(os.getenv(k) for k in ['RQ4_API_KEY','RQ4_MODEL','RQ4_BASE_URL']),
            'eval_dataset_exists':args.eval_dataset.exists(),'min_free_gb':args.min_free_gb,'image_cache':args.image_cache,
            'schedule':'repo_then_sample_then_method','status':'offline_check_no_calls'},indent=2))
        return
    if args.mode in ['generate','all'] and not all(os.getenv(k) for k in ['RQ4_MODEL','RQ4_API_KEY','RQ4_BASE_URL']):
        p.error('API configuration missing')
    testing = args.mode in ['evaluate','all']
    full = {}
    if testing:
        if args.dataset != 'swe':
            p.error('Omni official adapter not configured')
        validate_eval(args.eval_dataset,samples)
        # Caller-supplied metadata must match the bundled historical gold/tests too.
        import ast, gzip
        seed = ROOT/'data/evaluation_seed'/f'{args.dataset}50.jsonl.gz'
        gold_rows = {r['instance_id']:r for r in map(json.loads,gzip.decompress(seed.read_bytes()).decode().splitlines())}
        evaluator = {r['instance_id']:r for r in read_rows(args.eval_dataset)}
        for sample in samples:
            a,b = gold_rows[sample['instance_id']],evaluator[sample['instance_id']]
            for field in ['repo','base_commit','patch','test_patch','FAIL_TO_PASS','PASS_TO_PASS']:
                x,y = a[field],b[field]
                if field in ['FAIL_TO_PASS','PASS_TO_PASS']:
                    x = ast.literal_eval(x) if isinstance(x,str) else x
                    y = ast.literal_eval(y) if isinstance(y,str) else y
                if x != y:
                    p.error(f'{sample["instance_id"]}: evaluator {field} differs from bundled data')
        full = {r['instance_id']:r for r in read_rows(args.eval_dataset)}
        hashes['evaluator_dataset'] = hashlib.sha256(args.eval_dataset.read_bytes()).hexdigest()
    batch = ROOT/'runs/batches'/args.run_id
    batch.mkdir(parents=True,exist_ok=True)
    # Serialize ALL batches sharing the cache, not just one run-id.
    (ROOT/'repos').mkdir(exist_ok=True)
    with (ROOT/'repos/.rq4.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest = {'dataset':args.dataset,'methods':args.methods,'instance_ids':[r['instance_id'] for r in samples],
            'hashes':hashes,'model':os.getenv('RQ4_MODEL',''),'endpoint_hash':hashlib.sha256(os.getenv('RQ4_BASE_URL','').encode()).hexdigest(),
            'api_direct':os.getenv('RQ4_API_DIRECT','').strip().lower() in ('1','true','yes'),
            'protocol':protocol.get('name','legacy_test_fixture'),'test_timeout':args.test_timeout}
        existing = batch/'manifest.json'
        if existing.exists():
            previous = json.loads(existing.read_text())
            candidate = dict(manifest); candidate['hashes'] = dict(hashes)
            if 'evaluator_dataset' not in previous['hashes']:
                candidate['hashes'].pop('evaluator_dataset',None)
            if previous != candidate:
                raise ValueError('Batch configuration changed; use a new run-id')
        save(existing,manifest)
        try:
            ACTIVE_RESOURCES = Resources(ROOT,batch,args.min_free_gb,args.image_cache,docker=testing)
            ACTIVE_RESOURCES.ensure()
            pins_path = batch/'image_pins.json'
            pins = json.loads(pins_path.read_text()) if pins_path.exists() else {}
            for sample in sorted(samples,key=lambda r:(r['repo'],r['instance_id'])):
                instance = sample['instance_id']
                marker = batch/'completed_samples'/f'{instance}.json'
                if testing and marker.exists():
                    print(f'Skip fully evaluated sample {instance}',flush=True)
                    continue
                if testing:
                    row = dict(full[instance])
                    digest = ACTIVE_RESOURCES.image(row['image'],pins.get(instance))
                    pins[instance] = digest; save(pins_path,pins)
                    row['image'] = digest
                    dataset_file = batch/'eval_inputs'/instance/'dataset.jsonl'
                    dataset_file.parent.mkdir(parents=True,exist_ok=True)
                    dataset_file.write_text(json.dumps(row)+'\n')
                    for label,gold in [('control_noop',False),('control_gold',True)]:
                        if evaluate_one(batch,args,row,dataset_file,label,gold=gold) is not gold:
                            save(batch/'control_failure.json',{'instance_id':instance,'control':label,'expected_resolved':gold})
                            raise RuntimeError(f'{instance}: environment control failed; no new repair calls for this sample')
                for method in args.methods:
                    if args.mode in ['generate','all']:
                        # Keep the original argument object intact while generating one method.
                        one = argparse.Namespace(**vars(args)); one.methods=[method]
                        generate_one(batch,one,sample)
                    if testing:
                        evaluate_one(batch,args,row,dataset_file,method)
                        analyze(batch)
                if testing:
                    save(marker,{'image':digest,'methods':args.methods})
                    ACTIVE_RESOURCES.finish_sample()
                analyze(batch)
            (batch/'paused.json').unlink(missing_ok=True)
            (batch/'control_failure.json').unlink(missing_ok=True)
        except Exception as exc:
            save(batch/'paused.json',{'error_type':type(exc).__name__,'message':'Stopped; inspect resource events, generation records and harness logs. Re-run identical command after resolving infrastructure issues; started API requests are never retried automatically.'})
            raise
        finally:
            analyze(batch)
        print(f'Analysis: {batch / "analysis.md"}')


if __name__ == '__main__':
    main()
