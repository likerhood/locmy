import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from analyze_batch import analyze, official_reports
import run_batch


def put(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj))


class BatchTests(unittest.TestCase):
    def test_unknown_not_success_and_fixed_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            put(root/'manifest.json',{'methods':['magnet','locagent'],'instance_ids':['a','b','c']})
            put(root/'records/magnet/a.json',{'status':'completed','prediction':{
                'model_patch':'diff','candidate_count':10,'valid_candidate_count':7,
                'candidate_outcomes':{'generated':7,'failed:response_parse':2,'failed:api_request':1},
                'infrastructure_candidate_failures':1,
                'unique_nonempty_patches':5,'usage':{'total_tokens':12}}})
            put(root/'records/magnet/b.json',{'status':'completed','prediction':{'model_patch':''}})
            put(root/'evaluation/magnet/a/report.json',{'a':{'resolved':True}})
            put(root/'evaluation/locagent/a/report.json',{'a':{'resolved':False}})
            result=analyze(root)
            self.assertEqual(result['summary'][0]['n'],3)
            self.assertEqual(result['summary'][0]['resolved'],1)
            self.assertEqual(result['summary'][0]['unknown'],1)
            self.assertEqual(result['summary'][0]['repair_candidates'],10)
            self.assertEqual(result['summary'][0]['valid_repair_candidates'],7)
            self.assertEqual(result['summary'][0]['candidate_outcomes'],
                             {'generated':7,'failed:response_parse':2,'failed:api_request':1})
            self.assertEqual(result['summary'][0]['infrastructure_candidate_failures'],1)
            self.assertEqual(result['paired'][0]['ours_only'],1)
            self.assertEqual(result['paired'][0]['unknown'],2)

    def test_duplicate_official_reports_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for sub in ['try1','try2']:
                put(root/sub/'report.json',{'a':{'resolved':True}})
            with self.assertRaises(ValueError):official_reports(root)

    def test_infrastructure_only_generation_stays_unknown_without_official_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            instance = 'a'
            put(batch/'records/locagent/a.json', {
                'status': 'completed',
                'prediction': {
                    'status': 'infrastructure_failure',
                    'model_patch': '',
                    'infrastructure_candidate_failures': 10,
                },
            })
            result = run_batch.evaluate_one(
                batch, object(), {'instance_id': instance}, batch/'dataset.jsonl', 'locagent',
            )
            self.assertIsNone(result)

    def test_missing_localization_is_known_unsolved_without_official_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            put(batch/'records/cosil/a.json', {
                'status': 'completed',
                'prediction': {'status': 'missing_localization', 'model_patch': ''},
            })
            result = run_batch.evaluate_one(
                batch, object(), {'instance_id': 'a'}, batch/'dataset.jsonl', 'cosil',
            )
            self.assertFalse(result)

    def test_generate_skips_api_when_upstream_file_localization_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root/'runs/batches/test'
            put(root/'configs/protocol.json', {'name': 'p'})
            put(root/'normalized/swe/cosil.jsonl', {
                'instance_id': 'a', 'found_files': [], 'found_functions': [], 'status': 'available',
            })
            put(batch/'manifest.json', {'methods': ['cosil'], 'instance_ids': ['a']})
            args = type('Args', (), {'methods': ['cosil'], 'dataset': 'swe'})()
            with patch.object(run_batch, 'ROOT', root), patch.object(run_batch, 'repo_for') as repo:
                run_batch.generate_one(batch, args, {'instance_id': 'a'})
            repo.assert_not_called()
            record = json.loads((batch/'records/cosil/a.json').read_text())
            self.assertEqual(record['prediction']['status'], 'missing_localization')
            self.assertEqual(record['prediction']['candidate_count'], 0)
            self.assertEqual(record['prediction']['usage']['total_tokens'], 0)
            report = json.loads((batch/'analysis.json').read_text())
            self.assertEqual(report['summary'][0]['missing_localization'], 1)
            self.assertEqual(report['summary'][0]['resolved'], 0)
            self.assertEqual(report['summary'][0]['unknown'], 0)

    def test_missing_predictions_prevents_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=root/'data/inputs/omni50.jsonl'
            put(path,{'instance_id':'a'})
            with patch.object(run_batch,'ROOT',root),patch.object(sys,'argv',['run_batch','--dataset','omni','--mode','all']),patch.object(run_batch.subprocess,'run') as command:
                with self.assertRaises(SystemExit):run_batch.main()
                command.assert_not_called()

    def test_interrupted_attempt_not_paid_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            put(root/'data/inputs/swe50.jsonl',{'instance_id':'a','repo':'a/b','base_commit':'abc'})
            put(root/'configs/protocol.json',{})
            (root/'scripts').mkdir();(root/'scripts/repair.py').write_text('# fixture')
            put(root/'normalized/swe/locagent.jsonl',{'instance_id':'a','found_files':['x'],'status':'available'})
            put(root/'runs/batches/test/records/locagent/a.json',{'status':'started'})
            with patch.object(run_batch,'ROOT',root),patch.dict(run_batch.os.environ,{'RQ4_MODEL':'m','RQ4_API_KEY':'test','RQ4_BASE_URL':'https://invalid'},clear=True),patch.object(sys,'argv',['run_batch','--mode','generate','--limit','1','--methods','locagent','--run-id','test']),patch.object(run_batch,'repo_for') as repo:
                run_batch.main()
                repo.assert_not_called()
            result=json.loads((root/'runs/batches/test/analysis.json').read_text())
            self.assertEqual(result['summary'][0]['unknown'],1)


if __name__=='__main__':unittest.main()
