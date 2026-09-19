import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import migrate_calypso_dash_prefix as migration


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


class MigrateCalypsoTests(unittest.TestCase):
    def test_evaluator_dataset_content_hash_is_not_treated_as_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = 'Automattic__wp-calypso-30240'
            run_id = 'resume-test'
            adapter = root / migration.ADAPTER
            adapter.parent.mkdir(parents=True)
            adapter.write_text('# deployed parser fix\n')
            stable = root / 'configs/protocol.json'
            stable.parent.mkdir(parents=True)
            stable.write_text('{}\n')

            batch = root / 'runs/batches' / run_id
            write_json(batch / 'manifest.json', {
                'hashes': {
                    str(migration.ADAPTER): '0' * 64,
                    'configs/protocol.json': hashlib.sha256(stable.read_bytes()).hexdigest(),
                    'evaluator_dataset': '1' * 64,
                },
            })
            write_json(batch / 'control_failure.json', {
                'instance_id': instance,
                'control': 'control_gold',
                'expected_resolved': True,
            })
            report_dir = (batch / 'evaluation/control_gold/logs/evaluation'
                          / 'run' / 'test' / instance)
            write_json(report_dir / 'report.json', {
                instance: {
                    'resolved': False,
                    'patch_successfully_applied': True,
                    'infra_failure': False,
                },
            })
            (report_dir / 'test_output.txt').write_text(
                'tests passed\n>>>>> Test Exit Code: 0\n'
            )

            output = io.StringIO()
            argv = [
                'migrate_calypso_dash_prefix.py',
                '--run-id', run_id,
                '--instance', instance,
            ]
            with patch.object(migration, 'ROOT', root), patch.object(sys, 'argv', argv):
                with contextlib.redirect_stdout(output):
                    migration.main()

            self.assertIn('Dry run complete', output.getvalue())


if __name__ == '__main__':
    unittest.main()
