#!/usr/bin/env python3
"""Run no-op and gold controls for one staged SWE replacement candidate."""

import argparse
import fcntl
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_batch  # noqa: E402
from resources import Resources  # noqa: E402


def read_one(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    if len(rows) != 1:
        raise ValueError(f"Expected one row in {path}, got {len(rows)}")
    return rows[0]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    args_cli = parser.parse_args()

    stage = args_cli.stage.resolve()
    evaluator = read_one(stage / "assets/official_evaluator_row.jsonl")
    sample = read_one(stage / "assets/input_row.jsonl")
    if evaluator["instance_id"] != sample["instance_id"]:
        raise ValueError("Input and evaluator instance IDs differ")

    run_dir = stage / "controls"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_args = argparse.Namespace(
        dataset="swe",
        methods=[],
        mode="evaluate",
        env_file=ROOT / ".env.mimo1.local",
        candidate_workers=1,
        test_timeout=1800,
        min_free_gb=args_cli.min_free_gb,
        image_cache="budget",
    )

    lock_path = ROOT / "repos/.rq4.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_batch.ACTIVE_RESOURCES = Resources(
            ROOT, run_dir, run_args.min_free_gb, run_args.image_cache, docker=True
        )
        run_batch.ACTIVE_RESOURCES.ensure()

        row = dict(evaluator)
        digest = run_batch.ACTIVE_RESOURCES.image(row["image"])
        row["image"] = digest
        dataset_file = run_dir / "dataset.jsonl"
        dataset_file.parent.mkdir(parents=True, exist_ok=True)
        dataset_file.write_text(json.dumps(row) + "\n")

        noop = run_batch.evaluate_one(
            run_dir, run_args, row, dataset_file, "control_noop", gold=False
        )
        gold = run_batch.evaluate_one(
            run_dir, run_args, row, dataset_file, "control_gold", gold=True
        )
        run_batch.ACTIVE_RESOURCES.finish_sample()

    passed = noop is False and gold is True
    report = {
        "instance_id": row["instance_id"],
        "image": digest,
        "control_noop_resolved": noop,
        "control_gold_resolved": gold,
        "passed": passed,
    }
    save(stage / "control_result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not passed:
        raise SystemExit("Replacement candidate failed no-op/gold controls")


if __name__ == "__main__":
    main()
