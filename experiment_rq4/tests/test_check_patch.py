import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_patch


class CheckPatchTests(unittest.TestCase):
    def test_empty_patch_does_not_require_repair_request_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / 'prediction.jsonl').write_text(json.dumps({
                'instance_id': 'Automattic__wp-calypso-30240',
                'model_patch': '',
                'status': 'empty_patch',
            }) + '\n')
            # This is the current localization request schema. It deliberately
            # has no legacy `messages` field and no repair_request.json because
            # no editable context or repair patch was produced.
            (run_dir / 'request.json').write_text(json.dumps({
                'localization_messages': [],
                'candidate_files': ['client/example.js'],
            }) + '\n')

            argv = ['check_patch.py', '--run-dir', str(run_dir)]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                check_patch.main()

            report = json.loads((run_dir / 'application_check.json').read_text())
            self.assertEqual(report['instance_id'], 'Automattic__wp-calypso-30240')
            self.assertFalse(report['nonempty'])
            self.assertFalse(report['applied'])
            self.assertFalse(report['official_tests_run'])
            self.assertIsNone(report['resolved'])


if __name__ == '__main__':
    unittest.main()
