import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import run_batch
from analyze_batch import analyze
import migrate_deferred_evaluation as migration
import supervise_pipeline


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data)+'\n')


def fixture(root):
    rows = [dict(instance_id=i, repo='org/repo', base_commit='a'*40, problem_statement='issue',
                 patch='gold', test_patch='tests', FAIL_TO_PASS=['x'], PASS_TO_PASS=[],
                 image='org/image', eval_script='test', eval_type='unit', log_parser='parser') for i in 'abc']
    for relative in ('data/inputs/swe50.jsonl', 'eval.jsonl'):
        path = root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    seed = root/'data/evaluation_seed/swe50.jsonl.gz'
    seed.parent.mkdir(parents=True)
    seed.write_bytes(gzip.compress((root/'eval.jsonl').read_bytes()))
    put(root/'configs/protocol.json', {})
    (root/'scripts').mkdir()
    (root/'scripts/repair.py').write_text('# test')
    path = root/'normalized/swe/magnet.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps(dict(instance_id=i, status='available'))+'\n' for i in 'abc'))
    return ['batch', '--mode', 'all', '--methods', 'magnet', '--limit', '3', '--run-id', 'test',
            '--eval-dataset', str(root/'eval.jsonl')]


class DeferredTests(unittest.TestCase):
    def test_supervisor_reports_partial_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root/'runs/batches/demo/deferred_samples/a.json', {'reason':'test'})
            with patch.object(supervise_pipeline, 'ROOT', root):
                self.assertEqual(supervise_pipeline.supervise(
                    [sys.executable, '-c', 'pass'], root/'runs/pipeline/demo', .05), 0)
            state = json.loads((root/'runs/pipeline/demo/status.json').read_text())
            self.assertEqual(state['state'], 'completed_with_deferred')

    def exercise(self, failure, policy=True, limit=3, repeat=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = fixture(root)
            if policy:
                argv += ['--eval-failure-policy', 'defer', '--max-consecutive-eval-failures', str(limit)]
            resource = Mock()
            resource.image.return_value = 'org/image@sha256:fixed'
            generated = []
            def generate(batch, args, sample):
                generated.append(sample['instance_id'])
            def evaluate(batch, args, sample, dataset, method, gold=None):
                failure(sample['instance_id'], method)
                return gold if gold is not None else True
            with patch.object(run_batch, 'ROOT', root), patch('resources.Resources', return_value=resource), \
                 patch.object(run_batch, 'evaluate_one', side_effect=evaluate), \
                 patch.object(run_batch, 'generate_one', side_effect=generate), \
                 patch.dict(os.environ, {'RQ4_MODEL':'test', 'RQ4_API_KEY':'test', 'RQ4_BASE_URL':'https://invalid'}, clear=True), \
                 patch.object(sys, 'argv', argv):
                error = None
                try:
                    run_batch.main()
                except RuntimeError as exc:
                    error = exc
                before = list(generated)
                if repeat:
                    run_batch.main()
                    self.assertEqual(generated, before)
            batch = root/'runs/batches/test'
            return generated, error, sorted(p.stem for p in (batch/'deferred_samples').glob('*.json')), sorted(p.stem for p in (batch/'completed_samples').glob('*.json'))

    def test_bad_control_does_not_block_next_sample_or_retry_on_resume(self):
        def fail(instance, method):
            if instance == 'a':
                raise run_batch.EvaluationBlocked('browser disconnected')
        generated, error, deferred, completed = self.exercise(fail, repeat=True)
        self.assertIsNone(error)
        self.assertEqual(generated, ['b', 'c'])
        self.assertEqual(deferred, ['a'])
        self.assertEqual(completed, ['b', 'c'])

    def test_method_evaluation_error_retains_generation_and_continues(self):
        def fail(instance, method):
            if instance == 'a' and method == 'magnet':
                raise run_batch.EvaluationBlocked('missing report')
        generated, error, deferred, completed = self.exercise(fail, repeat=True)
        self.assertEqual(generated, ['a', 'b', 'c'])
        self.assertIsNone(error)
        self.assertEqual(deferred, ['a'])

    def test_default_policy_and_unexpected_bugs_still_stop(self):
        def blocked(*_):
            raise run_batch.EvaluationBlocked('broken control')
        self.assertIsInstance(self.exercise(blocked, policy=False)[1], run_batch.EvaluationBlocked)
        def bug(*_):
            raise RuntimeError('unexpected code bug')
        generated, error, deferred, completed = self.exercise(bug)
        self.assertEqual(str(error), 'unexpected code bug')
        self.assertEqual(deferred, [])

    def test_consecutive_failure_circuit_breaker(self):
        def fail(*_):
            raise run_batch.EvaluationBlocked('browser unavailable')
        generated, error, deferred, completed = self.exercise(fail, limit=2)
        self.assertIn('Consecutive', str(error))
        self.assertEqual(deferred, ['a', 'b'])
        self.assertEqual(generated, [])
        self.assertEqual(completed, [])

    def test_report_infrastructure_is_not_false_or_true(self):
        for value in (False, True):
            with self.assertRaises(run_batch.EvaluationBlocked):
                run_batch.report_outcome(dict(resolved=value, infra_failure=True), 'm', 'a')
        self.assertFalse(run_batch.report_outcome(dict(resolved=False), 'm', 'a'))

    def test_deferred_masks_all_methods_but_retains_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            put(batch/'manifest.json', dict(methods=['magnet', 'locagent'], instance_ids=['a']))
            put(batch/'deferred_samples/a.json', dict(reason='browser failure'))
            for method in ['magnet', 'locagent']:
                put(batch/'records'/method/'a.json', dict(status='completed', prediction=dict(model_patch='', usage=dict(total_tokens=12))))
                put(batch/'evaluation'/method/'a/report.json', {'a':dict(resolved=True)})
            report = analyze(batch)
            self.assertEqual(report['coverage']['deferred'], 1)
            for row in report['summary']:
                self.assertEqual(row['unknown'], 1)
                self.assertEqual(row['resolved'], 0)
                self.assertEqual(row['total_tokens'], 12)
            self.assertEqual(report['paired'][0]['unknown'], 1)

    def test_process_exit_and_missing_report_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            for error in (subprocess.CalledProcessError(1, 'test'), subprocess.TimeoutExpired('test', 1), None):
                with patch.object(run_batch, 'harness', side_effect=error):
                    with self.assertRaises(run_batch.EvaluationBlocked):
                        run_batch.evaluate_one(batch, Mock(test_timeout=1), dict(instance_id='a', patch=''), batch/'dataset', 'control_noop', gold=False)

    def test_migration_preserves_evidence_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'repos').mkdir()
            put(root/'runs/pipeline/demo/status.json', dict(state='failed'))
            batch = root/'runs/batches/demo'
            adapter = root/'scripts/official_eval.py'
            adapter.parent.mkdir()
            adapter.write_text('adapter')
            (root/migration.RUNNER).write_text('new runner')
            (root/migration.ANALYZER).write_text('new analyzer')
            manifest = dict(instance_ids=[migration.INSTANCE, 'old'], methods=['magnet'], hashes={migration.RUNNER:migration.OLD_RUNNER, 'scripts/official_eval.py':migration.sha(adapter)})
            put(batch/'manifest.json', manifest)
            put(batch/'completed_samples/old.json', {'preserved':True})
            put(batch/'records/magnet/old.json', {'preserved':True})
            evidence = batch/'evaluation/control_noop/logs/evaluation/run/model'/migration.INSTANCE/'test_output.txt'
            evidence.parent.mkdir(parents=True)
            evidence.write_text('DISCONNECTED')
            with patch.object(migration, 'EXPECTED_ADAPTER', migration.sha(adapter)):
                migration.migrate(root, 'demo')
                self.assertFalse((batch/'deferred_samples').exists())
                migration.migrate(root, 'demo', True)
                with self.assertRaisesRegex(ValueError, 'Already migrated'):
                    migration.migrate(root, 'demo', True)
            self.assertEqual(evidence.read_text(), 'DISCONNECTED')
            self.assertTrue((batch/'records/magnet/old.json').exists())
            self.assertTrue((batch/'completed_samples/old.json').exists())
            self.assertTrue((batch/'deferred_samples'/f'{migration.INSTANCE}.json').exists())


if __name__ == '__main__':
    unittest.main()
