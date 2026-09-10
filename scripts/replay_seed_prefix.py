#!/usr/bin/env python3
"""Frozen-ranking ablation, NOT an end-to-end evaluation of the seed policy.

Read compact agent traces one record at a time. Gold is used only for scoring.
No repository/source verification, model calls, or graph searches are replayed.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


KS = (1, 2, 3, 4, 5, 6, 7, 8, 15)


def replay(path: Path) -> dict:
    totals = {n: defaultdict(float) for n in range(4)}
    changes = []
    samples = 0
    skipped = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            loc = record.get("localization") or record
            stages = loc.get("rank_stage_snapshots") or record.get("rank_stage_snapshots") or {}
            gold = set((record.get("evaluation_3level") or {}).get("gold", {}).get("files", [])
                       or record.get("gold_files", []))
            final = stages.get("modification_closure")
            seeds = stages.get("fast_seed")
            if not gold or final is None or seeds is None:
                skipped += 1
                continue
            samples += 1
            final = [row["path"] for row in final]
            seeds = [row["path"] for row in seeds]
            ranks = {}
            for n, values in totals.items():
                ranked = list(dict.fromkeys(seeds[:n] + final))[:15]
                first = next((i for i, p in enumerate(ranked, 1) if p in gold), 0)
                ranks[n] = first
                values["mrr"] += 1 / first if first else 0
                for k in KS:
                    covered = gold.intersection(ranked[:k])
                    values[f"acc@{k}"] += bool(covered)
                    values[f"sl@{k}"] += covered == gold
                    values[f"recall@{k}"] += len(covered) / len(gold)
            if ranks[0] != ranks[2]:
                changes.append({"instance_id": record.get("instance_id"),
                                "final_first_hit": ranks[0], "prefix2_first_hit": ranks[2],
                                "seeds": seeds, "final_top3": final[:3]})
    return {
        "kind": "retrospective_frozen_prefix_ablation_not_policy_evaluation",
        "input": str(path), "samples": samples, "skipped": skipped,
        "metrics": {str(n): {key: round(100 * value / max(samples, 1), 2)
                              for key, value in values.items()} for n, values in totals.items()},
        "changed_cases": changes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.trace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"samples": report["samples"], "skipped": report["skipped"],
                      "metrics": report["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
