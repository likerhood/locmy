import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from progress_view import describe,snapshot,render,tail


class ProgressTests(unittest.TestCase):
    def test_controls_and_generation_are_distinct(self):
        result=describe(['python','official_eval.py','--predictions','/batch/eval_inputs/case/control_noop.jsonl'])
        self.assertIn('无补丁',result[1]);self.assertEqual(result[2:],('case','control_noop'))
        result=describe(['python','repair.py','--instance-id','case','--method','magnet'])
        self.assertIn('生成补丁',result[1]);self.assertEqual(result[2:],('case','magnet'))

    def test_reads_active_test_log_and_completed_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'runs/pipeline/demo';batch=root/'runs/batches/demo'
            log=batch/'evaluation/control_gold/logs/case/test_output.txt';log.parent.mkdir(parents=True);log.write_text('test A passed\nwaiting for B\n')
            (batch/'manifest.json').write_text(json.dumps({'instance_ids':['case','other']}))
            (batch/'completed_samples').mkdir();(batch/'completed_samples/other.json').write_text('{}')
            argv=['python','official_eval.py','--predictions','/eval_inputs/case/control_gold.jsonl']
            with patch('progress_view.descendants',return_value=[(123,argv)]): data=snapshot(root,directory,123)
            self.assertEqual(data['completed_samples'],1)
            self.assertEqual(data['total_samples'],2)
            self.assertIn('标准补丁',render(data))
            self.assertIn('waiting for B',render(data))

    def test_log_tail_bounds_and_redacts_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'pull.log'
            path.write_text('x'*10000+'\nhttps://user:secret@host/path?token=secret\n')
            result=tail(path)
            self.assertNotIn('secret',result['tail'])
            self.assertLessEqual(len(result['tail']),500)
