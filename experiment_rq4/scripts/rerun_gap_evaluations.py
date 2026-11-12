#!/usr/bin/env python3
"""Rerun selected and Oracle evaluations for an existing gap amendment."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_row(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError(f"Expected one row in {path}")
    return rows[0]


def run(command, *, cwd, env, log_path=None, check=True):
    print("+", " ".join(map(str, command)), flush=True)
    if log_path is None:
        subprocess.run(command, cwd=cwd, env=env, check=check)
        return
    with log_path.open("w") as log:
        subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=check,
        )


def write_selected_status(selected, instance_id):
    reports = list(selected.glob("logs/evaluation/**/report.json"))
    report = json.loads(reports[0].read_text()).get(instance_id) if reports else None
    status = {
        "instance_id": instance_id,
        "evaluated": report is not None,
        "resolved": report.get("resolved") if report else None,
        "infra_failure": report.get("infra_failure") if report else None,
        "patch_successfully_applied": (
            report.get("patch_successfully_applied") if report else None
        ),
        "report": str(reports[0]) if reports else None,
    }
    (selected / "status.json").write_text(json.dumps(status, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    stage = args.stage.resolve()
    env = os.environ.copy()
    env["PATH"] = f"/data2/like/rq4-docker/bin:{env.get('PATH', '')}"
    env["DOCKER_HOST"] = "unix:///run/user/1000/docker.sock"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for instance_dir in sorted((stage / "instances").iterdir()):
        if not instance_dir.is_dir():
            continue
        for method_dir in sorted(instance_dir.iterdir()):
            repair_roots = sorted((method_dir / "repair").glob("*_mimo26"))
            if len(repair_roots) != 1:
                continue
            repair_dir = repair_roots[0]
            dataset = method_dir / "dataset.harness.jsonl"
            row = read_row(dataset)
            if not row.get("image"):
                raise RuntimeError(f"Missing pinned image in {dataset}")

            evaluation = method_dir / "evaluation"
            if evaluation.exists():
                archived = method_dir / f"evaluation.missing_image_{stamp}"
                shutil.move(evaluation, archived)
            selected = evaluation / "selected"
            selected.mkdir(parents=True)
            instance_id = row["instance_id"]
            method = method_dir.name
            run_id = f"{method}_gap_selected_retry_{instance_id.replace('/', '_').replace('__', '_')}"
            run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "official_eval.py"),
                    "--dataset", str(dataset),
                    "--predictions", str(repair_dir / "prediction.jsonl"),
                    "--workers", "1",
                    "--timeout", "1800",
                    "--run-id", run_id,
                ],
                cwd=selected,
                env=env,
                log_path=selected / "harness.log",
                check=False,
            )
            write_selected_status(selected, instance_id)
            run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "evaluate_candidate_oracle.py"),
                    "--candidates-dir", str(repair_dir / "candidates"),
                    "--dataset", str(dataset),
                    "--output-dir", str(evaluation / "oracle"),
                    "--instance-id", instance_id,
                    "--model-name", f"rq4-{method}-mimo-v2.6-pro-gap-amendment",
                    "--workers", str(args.workers),
                    "--timeout", "1800",
                ],
                cwd=ROOT,
                env=env,
            )
            print(f"[done] {instance_id} {method}", flush=True)


if __name__ == "__main__":
    main()
