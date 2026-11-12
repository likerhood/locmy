#!/usr/bin/env python3
"""Officially evaluate every generated repair candidate for one instance."""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def evaluate(candidate_path, dataset, output, instance_id, model_name, timeout):
    candidate = json.loads(candidate_path.read_text())
    index = int(candidate.get("candidate_index", candidate_path.stem))
    patch = candidate.get("model_patch") or ""
    item_dir = output / f"candidate_{index:02d}"
    item_dir.mkdir(parents=True, exist_ok=True)
    status_path = item_dir / "status.json"
    if candidate.get("status") != "generated" or not patch:
        status = {
            "candidate_index": index,
            "evaluated": False,
            "resolved": False,
            "reason": candidate.get("status") or "empty_patch",
        }
        status_path.write_text(json.dumps(status, indent=2) + "\n")
        return status

    prediction = item_dir / "prediction.jsonl"
    prediction.write_text(json.dumps({
        "instance_id": instance_id,
        "model_name_or_path": f"{model_name}-candidate-{index:02d}",
        "model_patch": patch,
    }) + "\n")
    run_id = f"oracle_{output.name}_{index:02d}"
    log_path = item_dir / "harness.log"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "official_eval.py"),
        "--dataset", str(dataset),
        "--predictions", str(prediction),
        "--workers", "1",
        "--timeout", str(timeout),
        "--run-id", run_id,
    ]
    with log_path.open("w") as log:
        result = subprocess.run(
            command,
            cwd=item_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    reports = list(item_dir.glob(f"logs/evaluation/{run_id}/**/report.json"))
    report = json.loads(reports[0].read_text()).get(instance_id) if reports else None
    status = {
        "candidate_index": index,
        "evaluated": report is not None,
        "resolved": report.get("resolved") if report else None,
        "infra_failure": report.get("infra_failure") if report else None,
        "patch_successfully_applied": (
            report.get("patch_successfully_applied") if report else None
        ),
        "returncode": result.returncode,
        "report": str(reports[0]) if reports else None,
    }
    status_path.write_text(json.dumps(status, indent=2) + "\n")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    candidates = sorted(args.candidates_dir.resolve().glob("*.json"))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    statuses = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                evaluate,
                path,
                args.dataset.resolve(),
                output,
                args.instance_id,
                args.model_name,
                args.timeout,
            ): path
            for path in candidates
        }
        for future in as_completed(futures):
            status = future.result()
            statuses.append(status)
            print(json.dumps(status), flush=True)

    statuses.sort(key=lambda item: item["candidate_index"])
    summary = {
        "instance_id": args.instance_id,
        "candidate_count": len(statuses),
        "evaluated": sum(item.get("evaluated") is True for item in statuses),
        "resolved_candidates": [
            item["candidate_index"] for item in statuses if item.get("resolved") is True
        ],
        "infrastructure_failures": [
            item["candidate_index"]
            for item in statuses
            if item.get("infra_failure") is True or item.get("evaluated") is None
        ],
        "statuses": statuses,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
