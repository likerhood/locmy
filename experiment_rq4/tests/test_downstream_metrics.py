import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_downstream import build_report  # noqa: E402


class DownstreamMetricsTests(unittest.TestCase):
    def test_candidate_and_instance_denominators_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = root / "runs" / "batches" / "demo"
            (root / "configs").mkdir(parents=True)
            (root / "configs" / "protocol.json").write_text('{"candidates_per_instance": 10}\n')
            batch.mkdir(parents=True)
            (batch / "manifest.json").write_text(json.dumps({
                "methods": ["magnet"], "instance_ids": ["a", "b"]
            }))
            (batch / "records" / "magnet").mkdir(parents=True)
            (batch / "records" / "magnet" / "a.json").write_text(json.dumps({
                "status": "completed", "applied": True,
                "prediction": {
                    "status": "generated", "model_patch": "patch",
                    "candidate_count": 1,
                    "unique_nonempty_patches": 1,
                    "selection": {"vote_count": 1},
                    "candidate_outcomes": {"generated": 1},
                },
            }))
            candidate_dir = batch / "attempts" / "magnet" / "a" / "candidates"
            candidate_dir.mkdir(parents=True)
            (candidate_dir / "00.json").write_text(json.dumps({
                "candidate_index": 0,
                "status": "generated", "model_patch": "patch", "patch_apply_ok": True,
                "validation": {"status": "passed"},
                "coverage_diagnostic": {"score": 1},
            }))
            (batch / "deferred_samples").mkdir()
            (batch / "deferred_samples" / "b.json").write_text("{}")
            eval_dir = batch / "evaluation" / "magnet" / "run"
            eval_dir.mkdir(parents=True)
            (eval_dir / "report.json").write_text(json.dumps({"a": {"resolved": True}}))

            report = build_report(batch)["methods"][0]

            self.assertEqual(report["candidate_count"], 1)
            self.assertEqual(report["valid_candidate_count"], 1)
            self.assertEqual(report["candidate_valid_rate"], 100.0)
            self.assertEqual(report["valid_at_k"], 50.0)
            self.assertEqual(report["complete_coverage_at_k"], 50.0)
            self.assertEqual(report["applied_at_1_assessed"], 100.0)
            self.assertEqual(report["resolved_at_1_known"], 100.0)
            self.assertEqual(report["resolved_at_1_lower_bound"], 50.0)
            self.assertIsNone(report["oracle_at_k"])


if __name__ == "__main__":
    unittest.main()
