#!/usr/bin/env python3
"""Run the two Chart.js gap samples across all five localization methods."""

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METHODS = ("magnet", "locagent", "cosil", "gala", "graphlocator")
INSTANCES = ("chartjs__Chart.js-8567", "chartjs__Chart.js-9766")


def run_one(stage, instance_id, method):
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_magnet_gap_amendment.py"),
        "--stage", str(stage),
        "--instance-id", instance_id,
        "--method", method,
        "--candidate-workers", "1",
    ]
    print(f"[start] {instance_id} {method}", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"{instance_id} {method} failed with {result.returncode}")
    print(f"[done] {instance_id} {method}", flush=True)
    return instance_id, method


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    stage = args.stage.resolve()
    tasks = [(instance_id, method) for instance_id in INSTANCES for method in METHODS]
    failures = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_one, stage, instance_id, method): (instance_id, method)
            for instance_id, method in tasks
        }
        for future in as_completed(futures):
            instance_id, method = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append((instance_id, method, str(exc)))
                print(f"[failed] {instance_id} {method}: {exc}", flush=True)
    if failures:
        for failure in failures:
            print("failure:", *failure, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
