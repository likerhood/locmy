import gzip
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from official_eval import bundled_assets, resolve_required_asset, parse_complete_chartjs, run_with_git_mode
import migrate_chartjs_assets as migration

ROOT = Path(__file__).resolve().parents[1]


class ChartAssetsTests(unittest.TestCase):
    def test_modified_bundle_is_rejected(self):
        with patch.object(Path, 'read_bytes', return_value=b'{}'):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                bundled_assets()

    def test_all_six_blobs_match_official_patch_and_urls(self):
        with gzip.open(ROOT / 'data/evaluation_seed/swe50.harness.jsonl.gz', 'rt') as handle:
            row = next(r for r in map(json.loads, handle) if r['instance_id'] == migration.INSTANCE)
        bundle = bundled_assets()
        self.assertEqual(len(bundle), 6)
        original = Mock(side_effect=AssertionError('Network must not be used'))
        for asset in row['image_assets']['test_patch']:
            data = resolve_required_asset(dict(asset, instance_id=row['instance_id']), None,
                                          logging.getLogger(), original, bundle)
            section = next(s for s in row['test_patch'].split('diff --git ')
                           if s.startswith('a/' + asset['path'] + ' '))
            expected = re.search(r'index [0-9a-f]+\.\.([0-9a-f]+)', section)[1]
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            self.assertTrue(blob.startswith(expected))
        original.assert_not_called()

    def test_unknown_missing_asset_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Required evaluation asset unavailable'):
            resolve_required_asset(dict(instance_id='other', path='a.png'), None,
                                   logging.getLogger(), lambda *_: None, {})

    def test_parser_rejects_partial_disconnected_and_permission_failure(self):
        original = Mock(return_value={'test': 'FAILED'})
        for log in ('Executed 54 of 1545 (18 FAILED) DISCONNECTED',
                    'Executed 54 of 1545 (18 FAILED)', 'no suite output',
                    'Executed 1545 of 1545 SUCCESS\nEACCES mkdir coverage'):
            with self.assertRaises(RuntimeError):
                parse_complete_chartjs(log, None, original)
        original.assert_not_called()
        log = 'Executed 54 of 1545\rExecuted 1545 of 1545 (4 FAILED)'
        self.assertEqual(parse_complete_chartjs(log, None, original), {'test': 'FAILED'})
        self.assertEqual(parse_complete_chartjs('Executed 1545 SUCCESS', None, original), {'test': 'FAILED'})

    def test_coverage_preparation_is_chartjs_only(self):
        container = Mock()
        container.exec_run.return_value.exit_code = 0
        original = Mock(return_value='done')
        self.assertEqual(run_with_git_mode(container, '/bin/bash /eval.sh', 1, original,
                                         workdir='/testbed', user='root', chartjs=True), 'done')
        self.assertEqual(container.exec_run.call_count, 2)
        command = container.exec_run.call_args.args[0][-1]
        self.assertIn('test ! -L', command)
        self.assertNotIn('chmod -R', command)

    def test_migration_dry_run_and_archive_preserve_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'repos').mkdir()
            batch = root / 'runs/batches/demo'
            batch.mkdir(parents=True)
            status = root / 'runs/pipeline/demo/status.json'
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({'state': 'failed'}))
            adapter = root / migration.ADAPTER
            adapter.parent.mkdir(parents=True)
            adapter.write_text('new adapter')
            manifest = {'hashes': {migration.ADAPTER: migration.OLD_ADAPTER}}
            (batch / 'manifest.json').write_text(json.dumps(manifest))
            (batch / 'control_failure.json').write_text(json.dumps(dict(
                instance_id=migration.INSTANCE, control='control_noop', expected_resolved=False)))
            old = batch / 'evaluation/control_noop/logs/evaluation/run/model' / migration.INSTANCE
            old.mkdir(parents=True)
            (old / 'test_output.txt').write_text('DISCONNECTED')
            completed = batch / 'completed_samples/earlier.json'
            completed.parent.mkdir()
            completed.write_text('unchanged')
            migration.migrate(root, 'demo')
            self.assertTrue(old.exists())
            self.assertEqual(json.loads((batch / 'manifest.json').read_text()), manifest)
            status.write_text(json.dumps({'state': 'running'}))
            with self.assertRaisesRegex(ValueError, 'stopped, failed'):
                migration.migrate(root, 'demo', True)
            status.write_text(json.dumps({'state': 'failed'}))
            record = batch / 'records/magnet' / f'{migration.INSTANCE}.json'
            record.parent.mkdir(parents=True)
            record.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'generation records'):
                migration.migrate(root, 'demo', True)
            record.unlink()
            migration.migrate(root, 'demo', True)
            self.assertFalse(old.exists())
            self.assertEqual(completed.read_text(), 'unchanged')
            self.assertEqual(len(list((batch / 'migrations').glob('*/stale_control_noop/test_output.txt'))), 1)
            with self.assertRaisesRegex(ValueError, 'Unrecognized previous evaluator'):
                migration.migrate(root, 'demo', True)


if __name__ == '__main__':
    unittest.main()
