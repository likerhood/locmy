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
            put(root/'records/magnet/a.json',{'status':'completed','prediction':{'model_patch':'diff','usage':{'total_tokens':12}}})
            put(root/'records/magnet/b.json',{'status':'completed','prediction':{'model_patch':''}})
            put(root/'evaluation/magnet/a/report.json',{'a':{'resolved':True}})
            put(root/'evaluation/locagent/a/report.json',{'a':{'resolved':False}})
            result=analyze(root)
            self.assertEqual(result['summary'][0]['n'],3)
            self.assertEqual(result['summary'][0]['resolved'],1)
            self.assertEqual(result['summary'][0]['unknown'],1)
            self.assertEqual(result['paired'][0]['ours_only'],1)
            self.assertEqual(result['paired'][0]['unknown'],2)

    def test_duplicate_official_reports_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for sub in ['try1','try2']:
                put(root/sub/'report.json',{'a':{'resolved':True}})
            with self.assertRaises(ValueError):official_reports(root)

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
