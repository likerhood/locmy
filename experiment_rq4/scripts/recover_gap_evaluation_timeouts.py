#!/usr/bin/env python3
"""Recover valid gap-amendment evaluations that timed out without a report."""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def find_report(candidate_dir, instance_id, run_id):
    reports = list(candidate_dir.glob(f"logs/evaluation/{run_id}/**/report.json"))
    if not reports:
        return None, None
    report_path = reports[0]
    return report_path, read_json(report_path).get(instance_id)


def evaluate_one(task, env, timeout):
    instance_id = task["instance_id"]
    index = task["index"]
    candidate_dir = task["candidate_dir"]
    status_path = candidate_dir / "status.json"
    backup = candidate_dir / "status.initial_timeout.json"
    if status_path.exists() and not backup.exists():
        shutil.copy2(status_path, backup)
    run_id = f"recovery_oracle_{index:02d}"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "official_eval.py"),
        "--dataset", str(task["dataset"]),
        "--predictions", str(candidate_dir / "prediction.jsonl"),
        "--workers", "1",
        "--timeout", str(timeout),
        "--run-id", run_id,
    ]
    print("+", " ".join(command), flush=True)
    with (candidate_dir / "recovery.harness.log").open("w") as log:
        result = subprocess.run(
            command,
            cwd=candidate_dir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    report_path, report = find_report(candidate_dir, instance_id, run_id)
    status = {
        "candidate_index": index,
        "evaluated": report is not None,
        "resolved": report.get("resolved") if report else None,
        "infra_failure": report.get("infra_failure") if report else None,
        "patch_successfully_applied": (
            report.get("patch_successfully_applied") if report else None
        ),
        "returncode": result.returncode,
        "report": str(report_path) if report_path else None,
        "recovery": "timeout retry with identical candidate patch",
    }
    write_json(status_path, status)
    print(json.dumps(status), flush=True)
    return task


def rebuild_summary(oracle_dir, instance_id):
    statuses = []
    for candidate_dir in sorted(oracle_dir.glob("candidate_*")):
        status_path = candidate_dir / "status.json"
        if status_path.exists():
            statuses.append(read_json(status_path))
    summary = {
        "instance_id": instance_id,
        "candidate_count": len(statuses),
        "evaluated": sum(status.get("evaluated") is True for status in statuses),
        "resolved_candidates": [
            status["candidate_index"]
            for status in statuses
            if status.get("resolved") is True
        ],
        "infrastructure_failures": [
            status["candidate_index"]
            for status in statuses
            if status.get("infra_failure") is True
        ],
        "statuses": statuses,
        "recovery": "merged after timeout-only official evaluation retries",
    }
    write_json(oracle_dir / "summary.json", summary)


def recover_selected(method_dir):
    selected_status = method_dir / "evaluation" / "selected" / "status.json"
    if not selected_status.exists() or read_json(selected_status).get("evaluated") is True:
        return
    repair_dirs = list((method_dir / "repair").glob("*_mimo26"))
    if len(repair_dirs) != 1:
        return
    prediction_rows = [
        json.loads(line)
        for line in (repair_dirs[0] / "prediction.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if len(prediction_rows) != 1:
        return
    selected_patch = prediction_rows[0].get("model_patch")
    matches = []
    for candidate_file in sorted((repair_dirs[0] / "candidates").glob("*.json")):
        candidate = read_json(candidate_file)
        if candidate.get("model_patch") == selected_patch:
            matches.append(candidate["candidate_index"])
    if len(matches) != 1:
        return
    index = matches[0]
    oracle_status = method_dir / "evaluation" / "oracle" / f"candidate_{index:02d}" / "status.json"
    if not oracle_status.exists():
        return
    source = read_json(oracle_status)
    if source.get("evaluated") is not True:
        return
    backup = selected_status.with_name("status.initial_timeout.json")
    if not backup.exists():
        shutil.copy2(selected_status, backup)
    recovered = {
        "instance_id": prediction_rows[0]["instance_id"],
        "evaluated": True,
        "resolved": source.get("resolved"),
        "infra_failure": source.get("infra_failure"),
        "patch_successfully_applied": source.get("patch_successfully_applied"),
        "report": source.get("report"),
        "recovery": (
            f"exact selected patch matched Oracle candidate {index:02d}; "
            "reused its official report"
        ),
    }
    write_json(selected_status, recovered)
    print(f"[selected-recovered] {method_dir.parent.name} {method_dir.name} candidate={index:02d}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    stage = args.stage.resolve()
    env = os.environ.copy()
    env["PATH"] = f"/data2/like/rq4-docker/bin:{env.get('PATH', '')}"
    env["DOCKER_HOST"] = "unix:///run/user/1000/docker.sock"
    tasks = []
    touched = set()
    for method_dir in sorted((stage / "instances").glob("*/*")):
        oracle_dir = method_dir / "evaluation" / "oracle"
        summary_path = oracle_dir / "summary.json"
        repair_dirs = list((method_dir / "repair").glob("*_mimo26"))
        if not summary_path.exists() or len(repair_dirs) != 1:
            continue
        instance_id = read_json(summary_path)["instance_id"]
        dataset = method_dir / "dataset.harness.jsonl"
        candidates_dir = repair_dirs[0] / "candidates"
        for status in read_json(summary_path).get("statuses", []):
            if status.get("evaluated") is True:
                continue
            index = status["candidate_index"]
            candidate_file = candidates_dir / f"{index:02d}.json"
            if not candidate_file.exists() or read_json(candidate_file).get("status") != "generated":
                continue
            tasks.append({
                "instance_id": instance_id,
                "index": index,
                "candidate_dir": oracle_dir / f"candidate_{index:02d}",
                "dataset": dataset,
                "method_dir": method_dir,
            })
            touched.add(method_dir)

    print(f"[recovery-plan] candidates={len(tasks)} workers={args.workers} timeout={args.timeout}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate_one, task, env, args.timeout) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    for method_dir in sorted(touched):
        instance_id = json.loads((method_dir / "dataset.harness.jsonl").read_text())["instance_id"]
        rebuild_summary(method_dir / "evaluation" / "oracle", instance_id)
    for method_dir in sorted((stage / "instances").glob("*/*")):
        recover_selected(method_dir)
    print("[recovery-done]", flush=True)


if __name__ == "__main__":
    main()
