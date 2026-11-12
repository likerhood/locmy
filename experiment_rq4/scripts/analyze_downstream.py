#!/usr/bin/env python3
"""Build downstream repair metrics without running models or official tests."""

import argparse
import json
from pathlib import Path


def percent(numerator, denominator):
    return 100.0 * numerator / denominator if denominator else None


def official_reports(directory):
    found = {}
    for path in directory.rglob("report.json"):
        payload = json.loads(path.read_text())
        for instance_id, value in payload.items():
            if not isinstance(value, dict) or not isinstance(value.get("resolved"), bool):
                continue
            if instance_id in found:
                raise ValueError(f"Duplicate official report for {instance_id} in {directory}")
            found[instance_id] = value
    return found


def load_candidates(batch, method, instance_id, expected_count):
    directory = batch / "attempts" / method / instance_id / "candidates"
    candidates = []
    for path in sorted(directory.glob("*.json")):
        candidate = json.loads(path.read_text())
        index = candidate.get("candidate_index")
        if isinstance(index, int) and index < expected_count:
            candidates.append(candidate)
    return candidates


def is_valid(candidate):
    return (
        candidate.get("status") == "generated"
        and bool(candidate.get("model_patch"))
        and candidate.get("patch_apply_ok") is True
        and candidate.get("validation", {}).get("status") == "passed"
    )


def method_report(batch, method, instance_ids, deferred):
    reports = official_reports(batch / "evaluation" / method)
    rows = []
    for instance_id in instance_ids:
        record_path = batch / "records" / method / f"{instance_id}.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else {"status": "not_started"}
        prediction = record.get("prediction") or {}
        expected_count = prediction.get("candidate_count", 0)
        candidates = load_candidates(batch, method, instance_id, expected_count)
        valid = [candidate for candidate in candidates if is_valid(candidate)]
        coverage_candidates = [
            candidate for candidate in valid
            if isinstance(candidate.get("coverage_diagnostic", {}).get("score"), (int, float))
        ]
        official = reports.get(instance_id)
        resolved = official.get("resolved") if official else None
        if (
            resolved is None
            and record.get("status") == "completed"
            and not prediction.get("model_patch")
            and prediction.get("status") in (None, "empty_patch", "missing_localization")
        ):
            resolved = False
        if instance_id in deferred or (official and official.get("infra_failure") is True):
            resolved = None
        selection = prediction.get("selection") or {}
        rows.append({
            "instance_id": instance_id,
            "deferred": instance_id in deferred,
            "candidate_count": len(candidates),
            "valid_candidate_count": len(valid),
            "valid_at_k": bool(valid),
            "complete_coverage_at_k": any(
                candidate["coverage_diagnostic"]["score"] == 1 for candidate in coverage_candidates
            ),
            "coverage_diagnostic_available": bool(coverage_candidates),
            "unique_nonempty_patches": prediction.get("unique_nonempty_patches", 0),
            "selected_vote_count": selection.get("vote_count"),
            "application_assessed": record.get("status") == "completed",
            "applied_at_1": record.get("applied") is True,
            "resolved_at_1": resolved,
            "candidate_outcomes": prediction.get("candidate_outcomes") or {},
        })

    planned = len(rows)
    known = sum(row["resolved_at_1"] is not None for row in rows)
    candidate_count = sum(row["candidate_count"] for row in rows)
    valid_count = sum(row["valid_candidate_count"] for row in rows)
    unique_count = sum(row["unique_nonempty_patches"] for row in rows)
    application_assessed = sum(row["application_assessed"] for row in rows)
    applied = sum(row["applied_at_1"] for row in rows)
    resolved = sum(row["resolved_at_1"] is True for row in rows)
    selected_votes = [
        row["selected_vote_count"] for row in rows
        if isinstance(row["selected_vote_count"], int)
    ]
    outcomes = {}
    for row in rows:
        for key, value in row["candidate_outcomes"].items():
            outcomes[key] = outcomes.get(key, 0) + value

    return {
        "method": method,
        "planned_instances": planned,
        "known_official_outcomes": known,
        "unknown_official_outcomes": planned - known,
        "candidate_count": candidate_count,
        "valid_candidate_count": valid_count,
        "candidate_valid_rate": percent(valid_count, candidate_count),
        "valid_at_k_count": sum(row["valid_at_k"] for row in rows),
        "valid_at_k": percent(sum(row["valid_at_k"] for row in rows), planned),
        "complete_coverage_at_k_count": sum(row["complete_coverage_at_k"] for row in rows),
        "complete_coverage_at_k": percent(
            sum(row["complete_coverage_at_k"] for row in rows), planned
        ),
        "coverage_diagnostic_available_count": sum(
            row["coverage_diagnostic_available"] for row in rows
        ),
        "applied_at_1_count": applied,
        "applied_at_1_lower_bound": percent(applied, planned),
        "application_assessed_count": application_assessed,
        "applied_at_1_assessed": percent(applied, application_assessed),
        "resolved_at_1_count": resolved,
        "resolved_at_1_lower_bound": percent(resolved, planned),
        "resolved_at_1_known": percent(resolved, known),
        "unique_nonempty_patches": unique_count,
        "unique_per_valid_candidate": percent(unique_count, valid_count),
        "selected_with_one_vote_count": sum(vote == 1 for vote in selected_votes),
        "selected_patch_count": len(selected_votes),
        "selected_with_one_vote_rate": percent(
            sum(vote == 1 for vote in selected_votes), len(selected_votes)
        ),
        "candidate_outcomes": outcomes,
        "oracle_at_k": None,
        "selection_gap": None,
        "rows": rows,
    }


def fmt(value):
    return "pending" if value is None else f"{value:.2f}%"


def markdown(report):
    lines = [
        "# Downstream repair metrics",
        "",
        "These metrics are post-hoc diagnostics. They do not alter generation, selection, or official evaluation.",
        "",
        "- CandidateValidRate: valid candidates / recorded candidate attempts.",
        "- Valid@K: planned instances with at least one valid candidate / all planned instances.",
        "- CompleteCoverage@K: planned instances with at least one valid candidate covering every file in the model-produced repair plan.",
        "- Applied@1: the selected patch passes the repository application check.",
        "- Resolved@1: the selected patch passes official tests. Known-rate excludes unknown/deferred outcomes.",
        "- Oracle@K remains pending until every valid candidate is evaluated independently with the official harness.",
        "",
        "| Method | Candidates | Candidate valid | Valid@K | CompleteCoverage@K | Applied@1 assessed | Applied lower bound | Resolved@1 known | Resolved lower bound | Singleton selection | Oracle@K |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["methods"]:
        lines.append(
            f'| {item["method"]} | {item["valid_candidate_count"]}/{item["candidate_count"]} '
            f'| {fmt(item["candidate_valid_rate"])} '
            f'| {item["valid_at_k_count"]}/{item["planned_instances"]} ({fmt(item["valid_at_k"])}) '
            f'| {item["complete_coverage_at_k_count"]}/{item["planned_instances"]} ({fmt(item["complete_coverage_at_k"])}) '
            f'| {item["applied_at_1_count"]}/{item["application_assessed_count"]} ({fmt(item["applied_at_1_assessed"])}) '
            f'| {item["applied_at_1_count"]}/{item["planned_instances"]} ({fmt(item["applied_at_1_lower_bound"])}) '
            f'| {item["resolved_at_1_count"]}/{item["known_official_outcomes"]} ({fmt(item["resolved_at_1_known"])}) '
            f'| {item["resolved_at_1_count"]}/{item["planned_instances"]} ({fmt(item["resolved_at_1_lower_bound"])}) '
            f'| {item["selected_with_one_vote_count"]}/{item["selected_patch_count"]} ({fmt(item["selected_with_one_vote_rate"])}) '
            "| pending |"
        )
    lines += [
        "",
        "CompleteCoverage@K is a diagnostic against the model-produced repair plan, not proof of semantic correctness.",
        "Oracle@K must be computed strictly post-hoc and must never be used to select the submitted patch.",
    ]
    return "\n".join(lines) + "\n"


def build_report(batch):
    manifest = json.loads((batch / "manifest.json").read_text())
    instance_ids = manifest["instance_ids"]
    deferred = {
        instance_id for instance_id in instance_ids
        if (batch / "deferred_samples" / f"{instance_id}.json").exists()
    }
    methods = [method_report(batch, method, instance_ids, deferred) for method in manifest["methods"]]
    return {
        "batch": str(batch),
        "k": json.loads((batch.parents[2] / "configs" / "protocol.json").read_text()).get(
            "candidates_per_instance", 10
        ) if (batch.parents[2] / "configs" / "protocol.json").exists() else 10,
        "planned_instances": len(instance_ids),
        "deferred_instances": len(deferred),
        "methods": methods,
        "oracle_policy": {
            "status": "not_run",
            "uses_official_tests_for_online_selection": False,
            "required_next_step": "Evaluate each valid candidate independently after the formal run.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    batch = args.batch_dir.resolve()
    output = (args.output_dir or batch / "reports").resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = build_report(batch)
    (output / "downstream_metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "downstream_metrics.md").write_text(markdown(report))
    print(output / "downstream_metrics.md")


if __name__ == "__main__":
    main()
