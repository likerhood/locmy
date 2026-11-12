#!/usr/bin/env python3
"""Run one post-hoc localization-method repair amendment."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def run(command, *, cwd=ROOT, log_path=None, env=None):
    print("+", " ".join(map(str, command)), flush=True)
    if log_path is None:
        subprocess.run(command, cwd=cwd, env=env, check=True)
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def normalized_localization(trace_path, instance_id):
    matches = [row for row in read_jsonl(trace_path) if row.get("instance_id") == instance_id]
    if not matches:
        raise RuntimeError(f"No MAGNET trace found for {instance_id}")
    ranked = matches[-1].get("ranked") or {}
    files = []
    for item in ranked.get("files") or []:
        path = str(item.get("path") or "").strip().removeprefix("./")
        if path and path not in files:
            files.append(path)
        if len(files) == 15:
            break
    if not files:
        raise RuntimeError(f"MAGNET returned no files for {instance_id}")
    allowed = set(files)
    functions = []
    for item in ranked.get("functions") or []:
        value = str(item.get("id") or "").strip().removeprefix("./")
        if "::" in value and value.split("::", 1)[0] in allowed and value not in functions:
            functions.append(value)
    return {
        "instance_id": instance_id,
        "found_files": files,
        "found_functions": functions,
        "status": "available",
        "provenance": {
            "method": "magnet",
            "source": str(trace_path.resolve()),
            "policy": "fresh MAGNET rerun; top-15 ranked files; no baseline localization copied",
        },
    }


def selected_status(eval_dir, instance_id, returncode):
    reports = list(eval_dir.glob("logs/evaluation/**/report.json"))
    report = json.loads(reports[0].read_text()).get(instance_id) if reports else None
    status = {
        "instance_id": instance_id,
        "evaluated": report is not None,
        "resolved": report.get("resolved") if report else None,
        "infra_failure": report.get("infra_failure") if report else None,
        "patch_successfully_applied": (
            report.get("patch_successfully_applied") if report else None
        ),
        "returncode": returncode,
        "report": str(reports[0]) if reports else None,
    }
    (eval_dir / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument(
        "--method",
        choices=("magnet", "locagent", "cosil", "gala", "graphlocator"),
        default="magnet",
    )
    parser.add_argument("--candidate-workers", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()

    stage = args.stage.resolve()
    instance_dir = (
        stage / "instances" / args.instance_id.replace("/", "_") / args.method
    )
    instance_dir.mkdir(parents=True, exist_ok=True)
    source_rows = {row["instance_id"]: row for row in read_jsonl(stage / "samples.source.jsonl")}
    sample = source_rows[args.instance_id]

    if args.method == "magnet":
        localization = normalized_localization(
            stage / "localization" / "agent_traces.jsonl", args.instance_id
        )
    else:
        matches = [
            row for row in read_jsonl(ROOT / "normalized" / "swe" / f"{args.method}.jsonl")
            if row.get("instance_id") == args.instance_id
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one {args.method} localization for {args.instance_id}"
            )
        localization = matches[0]
        localization["provenance"] = {
            "method": args.method,
            "policy": "frozen original RQ4 localization; repair rerun only",
        }
    localization_path = instance_dir / f"{args.method}.localization.jsonl"
    write_jsonl(localization_path, [localization])
    write_jsonl(
        instance_dir / "sample.repair.jsonl",
        [{key: sample[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")}],
    )
    dataset_rows = [
        row for row in read_jsonl(stage / "dataset.harness.jsonl")
        if row.get("instance_id") == args.instance_id
    ]
    if len(dataset_rows) != 1:
        raise RuntimeError(f"Expected one harness row for {args.instance_id}")
    if not dataset_rows[0].get("image"):
        raise RuntimeError(
            f"Harness row for {args.instance_id} is missing its pinned evaluation image"
        )
    dataset_path = instance_dir / "dataset.harness.jsonl"
    write_jsonl(dataset_path, dataset_rows)

    repo_path = (
        PROJECT
        / ".mycode_cache"
        / "checkouts"
        / sample["repo"].replace("/", "_")
        / sample["base_commit"]
    )
    if not repo_path.is_dir():
        raise RuntimeError(f"MAGNET checkout is missing: {repo_path}")

    repair_dir = instance_dir / "repair" / f"{args.method}_mimo26"
    run([
        sys.executable,
        str(ROOT / "scripts" / "repair.py"),
        "--dataset", "swe",
        "--method", args.method,
        "--instance-id", args.instance_id,
        "--repo", str(repo_path),
        "--env-file", str(ROOT / ".env.mimo1.local"),
        "--sample-file", str(instance_dir / "sample.repair.jsonl"),
        "--localization-file", str(localization_path),
        "--output-dir", str(repair_dir),
        "--candidate-workers", str(args.candidate_workers),
        "--execute",
    ])

    env = os.environ.copy()
    env["PATH"] = f"/data2/like/rq4-docker/bin:{env.get('PATH', '')}"
    env["DOCKER_HOST"] = "unix:///run/user/1000/docker.sock"
    selected_dir = instance_dir / "evaluation" / "selected"
    selected_dir.mkdir(parents=True, exist_ok=True)
    run_id = (
        f"{args.method}_gap_selected_"
        + args.instance_id.replace("/", "_").replace("__", "_")
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "official_eval.py"),
        "--dataset", str(dataset_path),
        "--predictions", str(repair_dir / "prediction.jsonl"),
        "--workers", "1",
        "--timeout", "1800",
        "--run-id", run_id,
    ]
    log_path = selected_dir / "harness.log"
    with log_path.open("w") as log:
        result = subprocess.run(
            command,
            cwd=selected_dir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    status = selected_status(selected_dir, args.instance_id, result.returncode)
    print(json.dumps(status, indent=2), flush=True)

    run([
        sys.executable,
        str(ROOT / "scripts" / "evaluate_candidate_oracle.py"),
        "--candidates-dir", str(repair_dir / "candidates"),
        "--dataset", str(dataset_path),
        "--output-dir", str(instance_dir / "evaluation" / "oracle"),
        "--instance-id", args.instance_id,
        "--model-name", f"rq4-{args.method}-mimo-v2.6-pro-gap-amendment",
        "--workers", "2",
        "--timeout", "1800",
    ], env=env)


if __name__ == "__main__":
    main()
