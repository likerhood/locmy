import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import restore_eval_data
import prepare_harness_dataset


class OfficialSnapshotTests(unittest.TestCase):
    def fixture(self, root):
        shutil.copytree(ROOT/'configs', root/'configs')
        shutil.copytree(ROOT/'manifests', root/'manifests')
        shutil.copytree(ROOT/'data/evaluation_seed', root/'data/evaluation_seed')
        with patch.object(restore_eval_data, 'ROOT', root), patch.object(sys, 'argv', ['restore']), contextlib.redirect_stdout(io.StringIO()):
            restore_eval_data.main()

    def test_all_50_validate_offline_and_replacement_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); self.fixture(root)
            with patch.object(prepare_harness_dataset, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                prepare_harness_dataset.main()
            rows=[json.loads(x) for x in (root/'data/evaluation_only/swe50.harness.jsonl').read_text().splitlines()]
            ids={x['instance_id'] for x in rows}
            self.assertEqual(len(ids), 50)
            self.assertIn('chartjs__Chart.js-10806', ids)
            self.assertNotIn('chartjs__Chart.js-8650', ids)

    def test_corrupt_official_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); self.fixture(root)
            (root/'data/evaluation_seed/swe50.harness.jsonl.gz').write_bytes(b'corrupt')
            with patch.object(prepare_harness_dataset, 'ROOT', root), self.assertRaisesRegex(SystemExit, 'hash mismatch'):
                prepare_harness_dataset.main()

    def test_arbitrary_existing_seed_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); self.fixture(root)
            target=root/'data/evaluation_only/swe50.jsonl';target.write_text('custom')
            with patch.object(restore_eval_data, 'ROOT', root), patch.object(sys, 'argv', ['restore']), self.assertRaisesRegex(SystemExit, 'refusing overwrite'):
                restore_eval_data.main()
            self.assertEqual(target.read_text(), 'custom')
