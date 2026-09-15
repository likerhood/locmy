import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import git_cache
import run_batch
from resources import Resources, Paused
from analyze_batch import analyze


def put(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj)+'\n')


class SerialTests(unittest.TestCase):
    def test_git_mirror_failure_official_fallback_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            subprocess.run(['git','init','-q',str(source)],check=True)
            (source/'x').write_text('original')
            subprocess.run(['git','-C',str(source),'add','x'],check=True)
            subprocess.run(['git','-C',str(source),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
            sha=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
            resource=Mock()
            resource.run.side_effect=lambda cmd,**kw:subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            sample={'repo':'org/project','base_commit':sha}
            with patch.object(git_cache,'urls',return_value=[str(root/'missing-mirror'),str(source)]),patch.dict(os.environ,{'RQ4_GIT_RETRIES':'1'}):
                result=git_cache.repo_for(root,sample,resource)
                self.assertTrue(git_cache.has_commit(result,sha))
                calls=resource.run.call_count
                self.assertEqual(calls,2)
                self.assertEqual(git_cache.repo_for(root,sample,resource),result)
                self.assertEqual(resource.run.call_count,calls)
                self.assertEqual(list((root/'repos').glob('.rq4-clone-*')),[])

    def test_repo_failure_does_not_mark_paid_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch=Path(tmp)
            args=argparse.Namespace(methods=['magnet'])
            with patch.object(run_batch,'repo_for',side_effect=RuntimeError('network')):
                with self.assertRaises(RuntimeError):
                    run_batch.generate_one(batch,args,{'instance_id':'a'})
            self.assertFalse((batch/'records/magnet/a.json').exists())

    def test_disk_cleanup_protects_foreign_active_and_current_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Resources(Path(tmp),Path(tmp)/'batch',min_free_gb=1)
            r.client=Mock();r.owned={'active':{},'current':{},'old':{}};r.protected='current'
            r.client.containers.list.side_effect=lambda **kw: [Mock()] if kw['filters']['ancestor']=='active' else []
            self.assertFalse(r.drop('foreign'))
            self.assertFalse(r.drop('current'))
            self.assertFalse(r.drop('active'))
            self.assertTrue(r.drop('old'))
            r.client.images.remove.assert_called_once_with('old',force=False,noprune=True)
            with patch.object(r,'space',return_value={'disk':0}):
                with self.assertRaises(Paused):r.ensure()

    def test_monitor_stops_process_when_disk_falls(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Resources(Path(tmp),Path(tmp)/'batch',min_free_gb=1)
            with patch.object(r,'space',side_effect=[{'disk':2*1024**3},{'disk':0},{'disk':0}]):
                with self.assertRaises(Paused):
                    r.run([sys.executable,'-c','import time;time.sleep(30)'])

    def test_api_failure_is_unknown_not_zero_success_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            put(root/'manifest.json',{'methods':['magnet'],'instance_ids':['a']})
            put(root/'records/magnet/a.json',{'status':'completed','prediction':{'status':'failed','model_patch':''}})
            result=analyze(root)['summary'][0]
            self.assertEqual(result['unknown'],1)
            self.assertIsNone(result['resolved_percent'])

    def test_pin_is_reused_and_preexisting_image_is_not_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Resources(Path(tmp),Path(tmp)/'batch',min_free_gb=1)
            r.client=Mock()
            image=Mock(id='sha256:existing',attrs={'RepoDigests':['org/image@sha256:abc']})
            r.client.images.get.return_value=image
            self.assertEqual(r.image('org/image:latest'),'org/image@sha256:abc')
            self.assertEqual(r.owned,{})
            self.assertEqual(r.image('org/image:latest','org/image@sha256:abc'),'org/image@sha256:abc')
            with self.assertRaises(Paused):r.image('org/image:latest','org/image@sha256:changed')

    def test_pipeline_selected_env_reaches_setup_and_install(self):
        import pipeline
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=root/'mimo.env'
            env.write_text('RQ4_MODEL=mimo-test\nRQ4_API_KEY=test\nRQ4_BASE_URL=https://example.invalid/v1\n')
            args=['pipeline','--env-file',str(env),'--eval-dataset',str(root/'eval.jsonl')]
            with patch.object(pipeline,'ROOT',root),patch.dict(os.environ,{},clear=True),patch.object(sys,'argv',args),patch.object(pipeline.subprocess,'check_output',return_value=str(root)),patch.object(pipeline.subprocess,'run') as run,patch.object(pipeline.os,'execv') as execute:
                pipeline.main()
                self.assertEqual(os.environ['RQ4_MODEL'],'mimo-test')
                self.assertIn(str(env),run.call_args_list[0].args[0])
                self.assertIn(str(env),run.call_args_list[1].args[0])
                self.assertIn('--skip-dataset',run.call_args_list[1].args[0])
                self.assertIn(str(env),execute.call_args.args[1])

    def test_sample_order_controls_reuse_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sha='a'*40
            # Frozen selection order is preserved in manifest, execution groups repositories.
            rows=[{'instance_id':i,'repo':repo,'base_commit':sha,'problem_statement':'issue','patch':'gold','test_patch':'test','FAIL_TO_PASS':['x'],'PASS_TO_PASS':['y'],'image':'org/image:tag','eval_script':'test','eval_type':'unit','log_parser':'parser'} for i,repo in [('b','z/repo'),('a','a/repo')]]
            p=root/'data/inputs/swe50.jsonl';p.parent.mkdir(parents=True)
            p.write_text(''.join(json.dumps({k:r[k] for k in ['instance_id','repo','base_commit','problem_statement']})+'\n' for r in rows))
            seed=root/'data/evaluation_seed/swe50.jsonl.gz';seed.parent.mkdir(parents=True)
            raw=''.join(json.dumps(r)+'\n' for r in rows).encode();seed.write_bytes(gzip.compress(raw))
            evaluator=root/'eval.jsonl';evaluator.write_bytes(raw)
            put(root/'configs/protocol.json',{})
            (root/'scripts').mkdir();(root/'scripts/repair.py').write_text('# fixture')
            for method in ['locagent','magnet']:
                p=root/'normalized/swe'/f'{method}.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(''.join(json.dumps({'instance_id':r['instance_id'],'status':'available','found_files':['x']})+'\n' for r in rows))
            events=[]
            resource=Mock();resource.image.side_effect=lambda ref,pin:pin or 'org/image@sha256:fixed'
            def execute(command,**kw):
                if 'repair.py' in command[1]:
                    instance=command[command.index('--instance-id')+1];method=command[command.index('--method')+1]
                    events.append((instance,'generate',method))
                    out=Path(command[command.index('--output-dir')+1]);out.mkdir(parents=True)
                    put(out/'prediction.jsonl',{'status':'generated','model_patch':'diff'})
                else:
                    out=Path(command[command.index('--run-dir')+1]);put(out/'application_check.json',{'applied':True})
            resource.run.side_effect=execute
            fail_once=[True]
            def harness(batch,method,predictions,dataset_file,workers,timeout):
                instance=json.loads(dataset_file.read_text())['instance_id'];events.append((instance,'test',method))
                if instance=='a' and method=='magnet' and fail_once[0]:
                    fail_once[0]=False;raise RuntimeError('simulated test infrastructure failure')
                put(batch/'evaluation'/method/instance/'report.json',{instance:{'resolved':method!='control_noop'}})
            argv=['batch','--mode','all','--methods','locagent','magnet','--limit','2','--run-id','serial','--eval-dataset',str(evaluator)]
            with patch.object(run_batch,'ROOT',root),patch('resources.Resources',return_value=resource),patch.object(run_batch,'repo_for',return_value=root),patch.object(run_batch,'harness',side_effect=harness),patch.dict(os.environ,{'RQ4_MODEL':'m','RQ4_API_KEY':'test','RQ4_BASE_URL':'https://invalid'},clear=True),patch.object(sys,'argv',argv):
                with self.assertRaises(RuntimeError):run_batch.main()
                self.assertEqual(events[:6],[('a','test','control_noop'),('a','test','control_gold'),('a','generate','locagent'),('a','test','locagent'),('a','generate','magnet'),('a','test','magnet')])
                run_batch.main()
                # Retry test, never regenerate paid patches or repeat cached controls.
                self.assertEqual(events.count(('a','generate','magnet')),1)
                self.assertEqual(events.count(('a','test','control_gold')),1)
                before=len(events);run_batch.main();self.assertEqual(len(events),before)
            batch=root/'runs/batches/serial'
            self.assertFalse((batch/'paused.json').exists())
            result=json.loads((batch/'analysis.json').read_text())
            self.assertEqual(result['manifest']['instance_ids'],['b','a'])
            self.assertTrue(all(s['resolved_percent']==100 for s in result['summary']))


if __name__=='__main__':unittest.main()
