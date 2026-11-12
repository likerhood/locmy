#!/usr/bin/env python3
"""Run one isolated MAGNET v9 recovery and promote it only if resolved."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
VARIANT = Path(__file__).resolve().parent
MAIN_BATCH = ROOT / "runs/batches/swe50_mimo25_four_method_v8_patch_audit_20261007"
STAGING = ROOT / "runs/batches/magnet_v9_marked684_staging_20261103"
AMENDMENT = MAIN_BATCH / "amendments/magnet_v9_marked684_20261103"
GRAPH_RECOVERY = ROOT / "runs/graphlocator-asset-recovery-20261020/completed.json"
ENV_FILE = ROOT / ".env.mimo1.local"
EVAL_DATASET = ROOT / "data/evaluation_only/swe50.harness.jsonl"
INSTANCE_IDS = ("markedjs__marked-684",)

sys.path.insert(0, str(ROOT / "scripts"))
import analyze_batch  # noqa: E402
import run_batch  # noqa: E402
from preflight import load_env  # noqa: E402
from resources import Resources  # noqa: E402


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def graph_active():
    result = subprocess.run(
        ["systemctl", "--user", "show",
         "locmy-rq4-graph-asset-recovery.service", "-p", "ActiveState", "--value"],
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() in {"active", "activating", "reloading"}


def wait_for_graph():
    while graph_active() or not GRAPH_RECOVERY.exists():
        print(
            f"[magnet-v9-wait] graph_active={str(graph_active()).lower()} "
            f"completion_marker={str(GRAPH_RECOVERY.exists()).lower()}",
            flush=True,
        )
        time.sleep(60)


def args_for_run():
    return argparse.Namespace(
        dataset="swe",
        methods=["magnet"],
        mode="all",
        env_file=ENV_FILE,
        candidate_workers=1,
        test_timeout=1800,
        min_free_gb=20.0,
        image_cache="budget",
    )


def ensure_manifest():
    model = os.environ["RQ4_MODEL"]
    endpoint_hash = hashlib.sha256(os.environ["RQ4_BASE_URL"].encode()).hexdigest()
    manifest = {
        "dataset": "swe",
        "methods": ["magnet"],
        "instance_ids": list(INSTANCE_IDS),
        "model": model,
        "endpoint_hash": endpoint_hash,
        "protocol": json.loads((VARIANT / "protocol_v9.json").read_text())["name"],
        "candidate_workers": 1,
        "test_timeout": 1800,
        "source_batch": str(MAIN_BATCH),
        "targeted_selection": "Single user-requested case: LocAgent resolved and MAGNET unresolved under v8",
        "hashes": {
            "variants/magnet_v9/repair_v9.py": sha256(VARIANT / "repair_v9.py"),
            "variants/magnet_v9/protocol_v9.json": sha256(VARIANT / "protocol_v9.json"),
            "normalized/swe/magnet.jsonl": sha256(ROOT / "normalized/swe/magnet.jsonl"),
            "data/inputs/swe50.jsonl": sha256(ROOT / "data/inputs/swe50.jsonl"),
            "evaluator_dataset": sha256(EVAL_DATASET),
        },
        "note": "No LocAgent patch, gold patch, official test patch, or prior official outcome is supplied to repair_v9.",
    }
    path = STAGING / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise RuntimeError("MAGNET v9 staging manifest changed")
    save(path, manifest)


def generation_command(instance, repo, output):
    return [
        sys.executable, str(VARIANT / "repair_v9.py"),
        "--dataset", "swe",
        "--method", "magnet",
        "--instance-id", instance,
        "--repo", str(repo),
        "--env-file", str(ENV_FILE),
        "--output-dir", str(output),
        "--candidate-workers", "1",
        "--execute",
    ]


def run_instance(args, sample, evaluator, pins):
    instance = sample["instance_id"]
    completed = STAGING / "completed_samples" / f"{instance}.json"
    if completed.exists():
        marker = json.loads(completed.read_text())
        if isinstance(marker.get("resolved"), bool):
            print(f"[magnet-v9-skip] {instance} already complete", flush=True)
            return
        history = completed.with_name(
            f"{completed.stem}.invalid-{int(time.time())}{completed.suffix}"
        )
        completed.replace(history)
        print(f"[magnet-v9-retry] archived invalid completion marker: {history}", flush=True)

    row = dict(evaluator[instance])
    digest = run_batch.ACTIVE_RESOURCES.image(row["image"], pins.get(instance))
    pins[instance] = digest
    save(STAGING / "image_pins.json", pins)
    row["image"] = digest
    dataset_file = STAGING / "eval_inputs" / instance / "dataset.jsonl"
    dataset_file.parent.mkdir(parents=True, exist_ok=True)
    dataset_file.write_text(json.dumps(row) + "\n")

    for label, gold in (("control_noop", False), ("control_gold", True)):
        if run_batch.evaluate_one(STAGING, args, row, dataset_file, label, gold=gold) is not gold:
            raise RuntimeError(f"{instance}: {label} did not match expected outcome")

    record_path = STAGING / "records/magnet" / f"{instance}.json"
    output = STAGING / "attempts/magnet" / instance
    output.parent.mkdir(parents=True, exist_ok=True)
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    if (record and record.get("status") == "completed" and
            record.get("prediction", {}).get("status") == "infrastructure_failure"):
        save(record_path, {
            "status": "started",
            "instance_id": instance,
            "note": "Retrying definite HTTP failures; durable successful calls remain reusable.",
        })
        record = None
    start = time.monotonic()
    if not (record and record.get("status") == "completed"):
        repo = run_batch.repo_for(sample)
        save(record_path, {
            "status": "started",
            "instance_id": instance,
            "note": "MAGNET v9 durable calls; safe resume reuses completed responses.",
        })
        request_timeout = int(os.getenv("RQ4_REQUEST_TIMEOUT", "900"))
        run_batch.run_repair_resumably(
            generation_command(instance, repo, output),
            STAGING / "attempts/magnet" / f"{instance}.log",
            timeout=(request_timeout + 30) * 12 + 120,
            max_continuations=12,
        )
        prediction = read_rows(output / "prediction.jsonl")[0]
        if prediction.get("status") == "infrastructure_failure":
            save(record_path, {
                "status": "started",
                "instance_id": instance,
                "prediction": prediction,
                "note": "Retryable candidate API failure; do not evaluate or mark complete.",
            })
            raise RuntimeError(f"{instance}: candidate generation infrastructure failure")
        run_batch.ACTIVE_RESOURCES.run([
            sys.executable, str(ROOT / "scripts/check_patch.py"),
            "--run-dir", str(output),
        ])
        applied = json.loads((output / "application_check.json").read_text())["applied"]
        save(record_path, {
            "status": "completed",
            "prediction": prediction,
            "applied": applied,
            "elapsed_seconds": time.monotonic() - start,
            "variant": "magnet_v9_context_aligned",
        })

    outcome = run_batch.evaluate_one(STAGING, args, row, dataset_file, "magnet")
    if not isinstance(outcome, bool):
        raise RuntimeError(f"{instance}: official evaluation returned no trustworthy outcome")
    save(completed, {"image": digest, "methods": ["magnet"], "resolved": outcome})
    run_batch.ACTIVE_RESOURCES.finish_sample()
    analyze_batch.analyze(STAGING)
    print(f"[magnet-v9-complete] {instance} resolved={outcome}", flush=True)


def files_manifest(root):
    return {
        str(path.relative_to(root)): {"size": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def staged_report_dir(instance):
    reports = list((STAGING / "evaluation/magnet").rglob(f"{instance}/report.json"))
    if len(reports) != 1:
        raise RuntimeError(f"Expected one staged official report for {instance}, got {len(reports)}")
    return reports[0].parent


def staged_resolved(instance):
    report = json.loads((staged_report_dir(instance) / "report.json").read_text())
    return report[instance].get("resolved") is True and report[instance].get("infra_failure") is False


def promote(instance):
    if not staged_resolved(instance):
        print(f"[magnet-v9-not-promoted] {instance} did not resolve", flush=True)
        return False

    backup = AMENDMENT / "superseded_v8" / instance
    if backup.exists():
        raise RuntimeError(f"Promotion backup already exists for {instance}")
    backup.mkdir(parents=True)

    active_attempt = MAIN_BATCH / "attempts/magnet" / instance
    active_record = MAIN_BATCH / "records/magnet" / f"{instance}.json"
    active_input = MAIN_BATCH / "eval_inputs" / instance / "magnet.jsonl"
    old_reports = list((MAIN_BATCH / "evaluation/magnet").rglob(f"{instance}/report.json"))
    if len(old_reports) != 1:
        raise RuntimeError(f"Expected one active v8 report for {instance}, got {len(old_reports)}")

    shutil.move(str(active_attempt), str(backup / "attempt"))
    shutil.move(str(active_record), str(backup / "record.json"))
    shutil.move(str(active_input), str(backup / "magnet.jsonl"))
    shutil.move(str(old_reports[0].parent), str(backup / "official_evaluation"))

    shutil.copytree(STAGING / "attempts/magnet" / instance, active_attempt)
    shutil.copy2(STAGING / "records/magnet" / f"{instance}.json", active_record)
    shutil.copy2(STAGING / "eval_inputs" / instance / "magnet.jsonl", active_input)
    target_report = (MAIN_BATCH / "evaluation/magnet/logs/evaluation/"
                     "magnet_v9_targeted/rq4-magnet" / instance)
    target_report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged_report_dir(instance), target_report)

    save(backup / "MANIFEST.json", {
        "instance_id": instance,
        "superseded_at": time.time(),
        "files": files_manifest(backup),
    })
    save(AMENDMENT / f"{instance}.json", {
        "instance_id": instance,
        "status": "promoted",
        "old_result": "unresolved",
        "new_result": "resolved",
        "backup": str(backup),
        "staging": str(STAGING),
        "repair_sha256": sha256(VARIANT / "repair_v9.py"),
        "protocol_sha256": sha256(VARIANT / "protocol_v9.json"),
        "promoted_at": time.time(),
    })
    print(f"[magnet-v9-promoted] {instance}", flush=True)
    return True


def refresh_main_reports():
    analyze_batch.analyze(MAIN_BATCH)
    subprocess.run([
        sys.executable, str(ROOT / "scripts/analyze_downstream.py"),
        "--batch-dir", str(MAIN_BATCH),
    ], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-wait", action="store_true")
    args_cli = parser.parse_args()
    if not args_cli.no_wait:
        wait_for_graph()

    lock_path = ROOT / "repos/.rq4.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        load_env(ENV_FILE)
        ensure_manifest()
        args = args_for_run()
        samples = {row["instance_id"]: row for row in read_rows(ROOT / "data/inputs/swe50.jsonl")}
        evaluator = {row["instance_id"]: row for row in read_rows(EVAL_DATASET)}
        pins_path = STAGING / "image_pins.json"
        pins = json.loads(pins_path.read_text()) if pins_path.exists() else {}
        run_batch.ACTIVE_RESOURCES = Resources(
            ROOT, STAGING, args.min_free_gb, args.image_cache, docker=True,
        )
        run_batch.ACTIVE_RESOURCES.ensure()

        for instance in INSTANCE_IDS:
            run_instance(args, samples[instance], evaluator, pins)

        promoted = [instance for instance in INSTANCE_IDS if promote(instance)]
        if promoted:
            refresh_main_reports()
        save(STAGING / "completed.json", {
            "status": "completed",
            "instances": list(INSTANCE_IDS),
            "promoted": promoted,
            "finished_at": time.time(),
        })


if __name__ == "__main__":
    main()
