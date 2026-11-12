#!/usr/bin/env python3
"""Build a new SWE50 result set by replacing one invalid benchmark instance."""

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

METHODS = ("locagent", "cosil", "gala", "magnet", "graphlocator")


def load_json(path: Path):
    return json.loads(path.read_text())


def official_reports(directory: Path):
    reports = {}
    for path in directory.rglob("report.json"):
        payload = load_json(path)
        for instance_id, result in payload.items():
            if not isinstance(result, dict) or not isinstance(result.get("resolved"), bool):
                continue
            if instance_id in reports:
                raise ValueError(f"duplicate official report for {instance_id}: {path}")
            reports[instance_id] = result
    return reports


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replacement_record(stage: Path, method: str):
    prediction_path = stage / "repair" / f"{method}_mimo26" / "prediction.jsonl"
    status_path = stage / "evaluation" / method / "status.json"
    prediction = load_json(prediction_path)
    status = load_json(status_path)
    if prediction["instance_id"] != status["instance_id"]:
        raise ValueError(f"replacement instance mismatch for {method}")
    if not status.get("evaluated") or status.get("infra_failure"):
        raise ValueError(f"replacement evaluation is incomplete for {method}: {status}")
    return {
        "status": "completed",
        "prediction": prediction,
        "applied": bool(prediction.get("model_patch")),
        "elapsed_seconds": 0,
    }, Path(status["report"])


def old_rows(batch: Path, method: str, removed_instance: str):
    with (batch / "per_instance.csv").open(newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row["method"] == method and row["instance_id"] != removed_instance
        ]
    return rows


def replacement_row(record, report, method: str):
    prediction = record["prediction"]
    usage = prediction.get("usage") or {}
    result = report[prediction["instance_id"]]
    return {
        "method": method,
        "instance_id": prediction["instance_id"],
        "status": record["status"],
        "evaluation_deferred": False,
        "deferred_reason": "",
        "generated": bool(prediction.get("model_patch")),
        "applied_check": record.get("applied"),
        "fine_localization": prediction.get("fine_localization_status"),
        "candidate_count": prediction.get("candidate_count", 0),
        "valid_candidate_count": prediction.get("valid_candidate_count", 0),
        "candidate_outcomes": json.dumps(
            prediction.get("candidate_outcomes") or {}, sort_keys=True
        ),
        "missing_localization": prediction.get("status") == "missing_localization",
        "infrastructure_candidate_failures": prediction.get(
            "infrastructure_candidate_failures", 0
        ),
        "unique_nonempty_patches": prediction.get("unique_nonempty_patches", 0),
        "selected_candidate": prediction.get("selected_candidate"),
        "resolved": result["resolved"],
        "official_report": True,
        "seconds": record.get("elapsed_seconds", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def as_bool(value):
    if isinstance(value, bool):
        return value
    if value in ("True", "true", "1"):
        return True
    if value in ("False", "false", "0"):
        return False
    return None


def as_int(value):
    return int(value or 0)


def as_float(value):
    return float(value or 0)


def build_analysis(output: Path, manifest, rows):
    summaries = []
    outcomes = {}
    for method in METHODS:
        method_rows = [row for row in rows if row["method"] == method]
        candidate_outcomes = {}
        for row in method_rows:
            row_outcomes = json.loads(row.get("candidate_outcomes") or "{}")
            for key, value in row_outcomes.items():
                candidate_outcomes[key] = candidate_outcomes.get(key, 0) + value
            outcomes[(method, row["instance_id"])] = as_bool(row.get("resolved"))
        solved = sum(as_bool(row.get("resolved")) is True for row in method_rows)
        unknown = sum(as_bool(row.get("resolved")) is None for row in method_rows)
        n = len(method_rows)
        summaries.append({
            "method": method,
            "n": n,
            "generated": sum(as_bool(row.get("generated")) is True for row in method_rows),
            "applied_check": sum(
                as_bool(row.get("applied_check")) is True for row in method_rows
            ),
            "resolved": solved,
            "unknown": unknown,
            "resolved_percent_lower_bound": 100 * solved / n,
            "complete": unknown == 0,
            "resolved_percent": 100 * solved / n if unknown == 0 else None,
            "total_tokens": sum(as_int(row.get("total_tokens")) for row in method_rows),
            "repair_candidates": sum(
                as_int(row.get("candidate_count")) for row in method_rows
            ),
            "valid_repair_candidates": sum(
                as_int(row.get("valid_candidate_count")) for row in method_rows
            ),
            "candidate_outcomes": candidate_outcomes,
            "missing_localization": sum(
                as_bool(row.get("missing_localization")) is True for row in method_rows
            ),
            "infrastructure_candidate_failures": sum(
                as_int(row.get("infrastructure_candidate_failures"))
                for row in method_rows
            ),
            "unique_nonempty_patches": sum(
                as_int(row.get("unique_nonempty_patches")) for row in method_rows
            ),
            "seconds": sum(as_float(row.get("seconds")) for row in method_rows),
        })

    paired = []
    for method in METHODS:
        if method == "magnet":
            continue
        counts = dict(ours_only=0, baseline_only=0, both=0, neither=0, unknown=0)
        for instance_id in manifest["instance_ids"]:
            ours = outcomes[("magnet", instance_id)]
            baseline = outcomes[(method, instance_id)]
            key = (
                "unknown" if ours is None or baseline is None
                else "both" if ours and baseline
                else "ours_only" if ours
                else "baseline_only" if baseline
                else "neither"
            )
            counts[key] += 1
        paired.append({"baseline": method, **counts})

    report = {
        "manifest": manifest,
        "summary": summaries,
        "paired": paired,
        "coverage": {
            "planned": len(manifest["instance_ids"]),
            "method_instance_rows": len(rows),
            "official_outcomes": sum(
                as_bool(row.get("resolved")) is not None for row in rows
            ),
        },
        "note": (
            "The 49 legacy rows are preserved verbatim from the old per-instance CSVs; "
            "only processing__p5.js-3769 was added and aggregate metrics were recomputed."
        ),
    }
    (output / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")

    fieldnames = list(rows[0])
    with (output / "per_instance.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# RQ4 SWE50 v3 analysis",
        "",
        "The original 49 results are preserved; p5.js-3769 replaces invalid p5.js-4561.",
        "",
        "| Method | N | Generated | Candidates | Valid candidates | Applied | Solved | Unknown | Resolved lower bound | Tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            f'| {item["method"]} | {item["n"]} | {item["generated"]} | '
            f'{item["repair_candidates"]} | {item["valid_repair_candidates"]} | '
            f'{item["applied_check"]} | {item["resolved"]} | {item["unknown"]} | '
            f'{item["resolved_percent_lower_bound"]:.2f}% | {item["total_tokens"]} |'
        )
    lines += [
        "",
        "Candidate outcomes:",
        "",
        "```json",
        json.dumps({item["method"]: item["candidate_outcomes"] for item in summaries}, indent=2),
        "```",
        "",
        "Paired outcomes against MAGNET:",
        "",
        "```json",
        json.dumps(paired, indent=2),
        "```",
    ]
    (output / "analysis.md").write_text("\n".join(lines) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--four-method-batch", type=Path, required=True)
    parser.add_argument("--graphlocator-batch", type=Path, required=True)
    parser.add_argument("--replacement-stage", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--remove-instance", default="processing__p5.js-4561")
    parser.add_argument("--add-instance", default="processing__p5.js-3769")
    args = parser.parse_args()

    old4 = args.four_method_batch.resolve()
    oldg = args.graphlocator_batch.resolve()
    stage = args.replacement_stage.resolve()
    output = args.output_dir.resolve()
    if output in (old4, oldg, stage):
        raise ValueError("output must be a new directory")

    manifest4 = load_json(old4 / "manifest.json")
    old_ids = manifest4["instance_ids"]
    if args.remove_instance not in old_ids:
        raise ValueError(f"invalid instance is absent: {args.remove_instance}")
    instance_ids = [item for item in old_ids if item != args.remove_instance]
    if args.add_instance in instance_ids:
        raise ValueError(f"replacement is already present: {args.add_instance}")
    instance_ids.append(args.add_instance)
    if len(instance_ids) != len(old_ids):
        raise ValueError(f"replacement changed dataset size: {len(old_ids)} -> {len(instance_ids)}")

    if output.exists():
        shutil.rmtree(output)
    (output / "records").mkdir(parents=True)
    (output / "evaluation").mkdir()
    (output / "completed_samples").mkdir()
    (output / "deferred_samples").mkdir()

    sources = {}
    merged_rows = []
    for method in METHODS:
        old_batch = oldg if method == "graphlocator" else old4
        record_dir = output / "records" / method
        evaluation_dir = output / "evaluation" / method
        record_dir.mkdir()
        evaluation_dir.mkdir()

        old_reports = official_reports(old_batch / "evaluation" / method)
        merged_reports = {}
        inherited_rows = old_rows(old_batch, method, args.remove_instance)
        if len(inherited_rows) != len(instance_ids) - 1:
            raise ValueError(f"expected 49 inherited rows for {method}, got {len(inherited_rows)}")
        merged_rows.extend(inherited_rows)
        for instance_id in instance_ids[:-1]:
            source = old_batch / "records" / method / f"{instance_id}.json"
            if not source.exists():
                raise FileNotFoundError(source)
            shutil.copy2(source, record_dir / source.name)
            if instance_id in old_reports:
                merged_reports[instance_id] = old_reports[instance_id]

        record, new_report_path = replacement_record(stage, method)
        (record_dir / f"{args.add_instance}.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        new_report = load_json(new_report_path)
        if args.add_instance not in new_report:
            raise ValueError(f"replacement report missing {args.add_instance}: {new_report_path}")
        merged_reports[args.add_instance] = new_report[args.add_instance]
        merged_rows.append(replacement_row(record, new_report, method))
        (evaluation_dir / "report.json").write_text(
            json.dumps(merged_reports, indent=2) + "\n"
        )
        sources[method] = {
            "old_batch": str(old_batch),
            "old_records_kept": len(instance_ids) - 1,
            "old_official_reports_kept": len(merged_reports) - 1,
            "replacement_prediction": str(
                stage / "repair" / f"{method}_mimo26" / "prediction.jsonl"
            ),
            "replacement_report": str(new_report_path),
        }

    for instance_id in instance_ids:
        (output / "completed_samples" / f"{instance_id}.json").write_text(
            json.dumps({"instance_id": instance_id, "status": "completed"}) + "\n"
        )

    manifest = {
        "dataset": "swe",
        "dataset_version": "swe50_v3_p5_3769",
        "methods": list(METHODS),
        "instance_ids": instance_ids,
        "removed_instance": args.remove_instance,
        "replacement_instance": args.add_instance,
        "legacy_model": manifest4.get("model"),
        "replacement_model": "MiMo-v2.6-pro",
        "merge_policy": "preserve the old 49 records and replace only the invalid instance",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "sources.json").write_text(json.dumps(sources, indent=2) + "\n")

    report = build_analysis(output, manifest, merged_rows)
    audit = {
        "output": str(output),
        "instance_count": len(instance_ids),
        "method_count": len(METHODS),
        "per_instance_rows": len(instance_ids) * len(METHODS),
        "analysis_sha256": sha256(output / "analysis.json"),
        "per_instance_sha256": sha256(output / "per_instance.csv"),
        "summary": report["summary"],
    }
    (output / "merge_audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(output / "analysis.md")


if __name__ == "__main__":
    main()
