import json

from scripts.replay_seed_prefix import replay


def test_frozen_replay_distinguishes_hit_from_full_coverage(tmp_path):
    trace = tmp_path / "trace.jsonl"
    record = {
        "instance_id": "fixture", "gold_files": ["src/a.py", "src/b.py"],
        "rank_stage_snapshots": {
            "fast_seed": [{"path": "src/a.py"}, {"path": "src/b.py"}],
            "modification_closure": [{"path": "src/noise.py"}, {"path": "src/a.py"}],
        },
    }
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
    report = replay(trace)
    assert report["samples"] == 1
    assert report["metrics"]["0"]["acc@2"] == 100
    assert report["metrics"]["0"]["sl@2"] == 0
    assert report["metrics"]["2"]["sl@2"] == 100
    assert report["metrics"]["2"]["recall@1"] == 50
