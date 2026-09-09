from __future__ import annotations

import json
from pathlib import Path

from scripts.evaluate_rank_stages import evaluate


def test_rank_stage_evaluator_reports_early_accuracy(tmp_path: Path) -> None:
    trace = tmp_path / "agent_traces.jsonl"
    rows = [
        {
            "evaluation_3level": {"gold": {"files": ["src/target.py"]}},
            "rank_stage_snapshots": {
                "fast_seed": [{"path": "src/other.py"}, {"path": "src/target.py"}],
                "head_selector": [{"path": "src/target.py"}, {"path": "src/other.py"}],
            },
        },
        {
            "evaluation_3level": {"gold": {"files": ["src/second.py"]}},
            "rank_stage_snapshots": {
                "fast_seed": [{"path": "src/second.py"}],
                "head_selector": [{"path": "src/second.py"}],
            },
        },
    ]
    trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = evaluate(trace)
    assert report["stages"]["fast_seed"]["acc@1"] == 50.0
    assert report["stages"]["head_selector"]["acc@1"] == 100.0
    assert report["stages"]["fast_seed"]["acc@3"] == 100.0
