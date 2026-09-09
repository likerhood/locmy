#!/usr/bin/env python3
"""Compute file-ranking metrics for each recorded MAGNET ranking stage."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


KS = (1, 2, 3, 4, 5, 6, 15)


def _paths(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [str(row.get("path") or "") for row in rows if str(row.get("path") or "")]


def _stage_rows(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    localization = record.get("localization") or record
    raw = localization.get("rank_stage_snapshots") or record.get("rank_stage_snapshots") or {}
    stages: dict[str, list[dict[str, Any]]] = {}
    for name, rows in raw.items():
        if name == "rounds" and isinstance(rows, dict):
            for round_no, round_rows in rows.items():
                if isinstance(round_rows, list):
                    stages[f"round_{round_no}"] = round_rows
        elif isinstance(rows, list):
            stages[str(name)] = rows
    return stages


def evaluate(path: Path) -> dict[str, Any]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    malformed = 0
    records = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            records += 1
            gold = set(
                (record.get("evaluation_3level") or {}).get("gold", {}).get("files", [])
                or record.get("gold_files", [])
            )
            if not gold:
                continue
            for stage, rows in _stage_rows(record).items():
                predicted = _paths(rows)
                bucket = totals[stage]
                bucket["samples"] += 1
                first_rank = next((rank for rank, item in enumerate(predicted, start=1) if item in gold), 0)
                if first_rank:
                    bucket["mrr_sum"] += 1.0 / first_rank
                for k in KS:
                    bucket[f"hits@{k}"] += float(any(item in gold for item in predicted[:k]))
    metrics: dict[str, Any] = {}
    for stage, values in sorted(totals.items()):
        samples = int(values["samples"])
        metrics[stage] = {
            "samples": samples,
            **{
                f"acc@{k}": round(100.0 * values[f"hits@{k}"] / max(1, samples), 2)
                for k in KS
            },
            "mrr": round(100.0 * values["mrr_sum"] / max(1, samples), 2),
        }
    return {"input": str(path), "records": records, "malformed": malformed, "stages": metrics}


def _markdown(report: dict[str, Any]) -> str:
    header = "| Stage | N | Acc@1 | Acc@3 | Acc@6 | Acc@15 | MRR |"
    lines = ["# Ranking Stage Metrics", "", header, "|---|---:|---:|---:|---:|---:|---:|"]
    for stage, row in report["stages"].items():
        lines.append(
            f"| {stage} | {row['samples']} | {row['acc@1']:.2f} | {row['acc@3']:.2f} | "
            f"{row['acc@6']:.2f} | {row['acc@15']:.2f} | {row['mrr']:.2f} |"
        )
    lines.extend(["", f"Records: {report['records']}; malformed lines: {report['malformed']}."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--input", type=Path, default=None)
    args = parser.parse_args()
    result_dir = args.result_dir.expanduser().resolve()
    source = args.input
    if source is None:
        compact = result_dir / "agent_traces.jsonl"
        source = compact if compact.exists() else result_dir / "localization_results.jsonl"
    source = source.expanduser().resolve()
    report = evaluate(source)
    (result_dir / "rank_stage_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (result_dir / "rank_stage_metrics.md").write_text(_markdown(report), encoding="utf-8")
    print(result_dir / "rank_stage_metrics.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
