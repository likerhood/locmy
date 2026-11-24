"""Frozen eight-case new-only revision test, paired to published-v2 source fingerprints."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from fileloc_v2_smoke import source_hashes, digest, task_input
from score_fileloc_pairs import canonical_gold, stage_ranks, valid_result
from score_fileloc_v2 import score
from mycode.utils.env import load_env_file

NEW_FLAGS=['MYCODE_FILELOC_EXECUTION_V3','MYCODE_CANDIDATE_CONTINUITY_V3',
           'MYCODE_FUNCTION_CALIBRATION_V3','MYCODE_FILELOC_PAIRWISE_V3']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['workspace','reference-run-dir','run-dir','env-file']:
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--workers',type=int,choices=[1,2,4],default=4)
    p.add_argument('--prepare-only',action='store_true')
    args=p.parse_args();reference=args.reference_run_dir
    ref=json.loads((reference/'manifest.json').read_text())
    assert ref['protocol']['rounds']==6 and ref['protocol']['steps']==24 and ref['model']=='mimo-v2.6-pro'
    for rel,sha in ref['implementation_hashes'].items():
        blob=subprocess.check_output(['git','show','30d1e32:'+rel],cwd=ROOT)
        assert hashlib.sha256(blob).hexdigest()==sha, f'Published reference source mismatch: {rel}'
    args.run_dir.mkdir(parents=True,exist_ok=False)
    cases=[]
    for c in ref['cases']:
        inp=reference/(c['instance_id']+'.input.jsonl')
        assert digest(inp)==c['input_sha256']
        data=json.loads(inp.read_text());assert task_input(data)==data,'Non-whitelisted inference field'
        out=args.run_dir/inp.name;shutil.copyfile(inp,out)
        cases.append(dict(c,input_path=str(out)))
    assert len(cases)==8
    load_env_file(args.env_file)
    env=dict(os.environ)
    assert env.get('BASE_URL') and env.get('API_KEY'),'Missing private model configuration'
    env.update(LOAD_ENV_FILE='0',LOC_CODE_ROOT=str(args.workspace),PYTHONPATH=str(ROOT/'src'),
        MODEL_API_NAME='mimo-v2.6-pro',MODEL_NAME='mimo-v2.6-pro',MYCODE_MODEL_PROFILE='mimo',
        MYCODE_VLM_IMAGE_TRANSPORT='data_uri',MYCODE_FAST_SEED_PLANNER='1',MYCODE_FAST_SEED_LLM='1',
        MYCODE_LLM_CANDIDATE_REVIEW='1',MYCODE_LLM_RETRIES='1',MYCODE_PHASE_LOG='1',
        MYCODE_READ_EXPLICIT_PATHS='1',MYCODE_JOINT_EVIDENCE_V2='1',MYCODE_FUNCTION_SOURCE_LEDGER='1',
        MYCODE_FUNCTION_REREAD_LIMIT='48',MYCODE_FILE_FRONT_RERANK='verified_frontier',
        MYCODE_FUNCTION_FINAL_POLICY='consensus',MYCODE_CONCERN_MATCH_V4='0',MYCODE_TOOL_EVIDENCE_HANDOFF='0',
        MYCODE_RESPONSIBILITY_GUARD_V5='0',MYCODE_WORKING_EVIDENCE_V5='0',MYCODE_DIRECT_ENTRY_V5='0',
        MYCODE_FILELOC_READ_RECONCILE='1',MYCODE_FILELOC_OWNER_CHALLENGE='1',
        MYCODE_FILELOC_REVIEW_COVERAGE='1',MYCODE_FILELOC_LISTWISE_RANK='1',
        MYCODE_CODE_CONTEXT_LIMIT='18',MYCODE_CANDIDATE_REVIEW_MAX_TOKENS='4000')
    env.update({k:'1' for k in NEW_FLAGS})
    for stage in ['PLANNING','EVIDENCE','CONTROLLER','VLM']:env[stage+'_MODEL_API_NAME']='mimo-v2.6-pro'
    sha=source_hashes(ROOT)
    harness=digest(Path(__file__))
    manifest=dict(cases=cases,model='mimo-v2.6-pro',reference_commit='30d1e3254e9940b768796202a743f62c69ef4a81',
        reference_run=str(reference),reference_manifest_sha256=digest(reference/'manifest.json'),
        current_parent=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        implementation_hashes=sha,harness_sha256=harness,protocol=ref['protocol'],
        config={k:v for k,v in env.items() if k.startswith('MYCODE_') or k.endswith('_MODEL_API_NAME') or k=='MODEL_API_NAME'})
    # Private config path is never copied into the public configuration snapshot.
    manifest['config']={k:v for k,v in manifest['config'].items() if not any(t in k for t in ['ENV_FILE','API_KEY','AUTH_TOKEN','URL'])}
    (args.run_dir/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    for rel in sha:
        target=args.run_dir/'source_snapshot'/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/rel,target)
    shutil.copyfile(__file__,args.run_dir/'source_snapshot/harness.py')
    if args.prepare_only:return
    import requests
    probe=requests.post(env['BASE_URL'].rstrip('/')+'/chat/completions',headers={'Authorization':'Bearer '+env['API_KEY']},
        json=dict(model='mimo-v2.6-pro',messages=[dict(role='user',content='Reply READY only.')],max_tokens=128,thinking={'type':'disabled'}),timeout=45)
    assert probe.ok,f'Preflight HTTP {probe.status_code}'
    (args.run_dir/'preflight.json').write_text(json.dumps(dict(status=probe.status_code,model=probe.json().get('model'),usage=probe.json().get('usage'))))
    swe=json.loads((args.workspace/'collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json').read_text())
    omni={r['instance_id']:r['gold'] for r in map(json.loads,(args.workspace/'collected_results/omin296/data/reference_gold.jsonl').open())}
    result_rows=[]
    def run(c):
        if source_hashes(ROOT)!=sha or digest(Path(__file__))!=harness:raise RuntimeError('Source drift; refusing mixed code run')
        out=args.run_dir/c['instance_id']/'improved';out.mkdir(parents=True,exist_ok=False)
        cmd=[sys.executable,str(ROOT/'newtest/run_clean15_dataset.py'),'--samples',c['input_path'],
            '--dataset','swebench_multimodal-full-dev' if c['dataset']=='SWE92' else 'omnigirl-full-candidates',
            '--output-dir',str(out),'--top-k','15','--dynamic-rounds','6','--max-react-steps','24',
            '--max-tool-rounds','1','--structure-only','--no-auto-fetch-repos','--use-llm','--use-llm-planning',
            '--use-llm-controller','--use-vlm','--download-images','--allow-network','--sample-timeout-seconds','3600',
            '--sample-heartbeat-interval','30']
        start=time.monotonic();print('RUN',c['instance_id'],flush=True)
        with (out/'console.log').open('x') as log:
            try:ret=subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=3660);execution=dict(returncode=ret.returncode)
            except subprocess.TimeoutExpired:execution=dict(returncode=None,error='timeout')
        execution.update(instance_id=c['instance_id'],elapsed_seconds=time.monotonic()-start)
        (out/'execution.json').write_text(json.dumps(execution,indent=2))
        print('DONE',json.dumps(execution),flush=True)
        return c,execution
    def summarize(completed):
        rows=[];missing=[]
        for c,execution in completed:
            ident=c['instance_id'];gold=canonical_gold((swe if c['dataset']=='SWE92' else omni)[ident])
            for arm,base in [('published_v2',reference),('revision_v3',args.run_dir)]:
                file=base/ident/'improved/localization_results.jsonl'
                if not file.exists():missing.append(f'{ident}/{arm}:missing');continue
                raw=json.loads(file.read_text().splitlines()[-1])
                if not valid_result(raw) or (arm=='revision_v3' and execution['returncode']!=0):
                    missing.append(f'{ident}/{arm}:invalid');continue
                loc=raw['localization'];pred=[r['path'] for r in loc['ranked_locations'][:15]]
                rows.append(dict(instance_id=ident,dataset=c['dataset'],arm=arm,
                    first_rank=next((n for n,p in enumerate(pred,1) if p in gold['files']),0),
                    metrics=score(loc,gold,c['dataset']),native_metrics=raw.get('evaluation_3level'),
                    stage_ranks=stage_ranks(loc.get('rank_stage_snapshots',{}),set(gold['files'])),
                    pairwise=loc.get('best_round_selection',{}).get('fileloc_pairwise_v3'),
                    elapsed_seconds=raw.get('elapsed_seconds'),tokens=raw.get('token_usage_summary',{}).get('total_tokens')))
        summary=[]
        for ds in ['SWE92','Omni296']:
            for arm in ['published_v2','revision_v3']:
                rs=[r for r in rows if r['dataset']==ds and r['arm']==arm]
                summary.append(dict(dataset=ds,arm=arm,N=len(rs),file={k:sum(r['metrics']['file'][k] for r in rs) for k in
                    ['acc@1','acc@3','acc@5','acc@8','acc@15','set_sl@15','set_rec@15']},
                    function={k:sum(r['metrics']['function'][k] for r in rs) for k in
                    ['acc@1','acc@3','acc@5','acc@8','acc@15','set_sl@15','set_rec@15']}))
        payload=dict(completed=len(completed),expected=8,rows=rows,summary=summary,missing=missing,
            warning='Historical published-v2 runs, not fresh randomized pairs; inference inputs/source identities and nominal budgets match; model/index service drift remains possible.')
        (args.run_dir/f'progress_{len(completed):02d}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    completed=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(run,c) for c in cases]
        for future in as_completed(futures):
            completed.append(future.result());summarize(completed)
    print('COMPLETE',len(completed),flush=True)


if __name__=='__main__':main()
