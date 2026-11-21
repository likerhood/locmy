"""Offline ablation: retain the logged pre-consensus file order for every case.

No model calls, no candidate selection by gold; functions retain live outputs.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets
from score_fileloc_pairs import canonical_gold


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scores = json.loads(args.scores.read_text())
    swe = json.loads((args.workspace / "collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni = {r["instance_id"]: r["gold"] for r in map(json.loads, (args.workspace / "collected_results/omin296/data/reference_gold.jsonl").open())}
    rows = []
    for row in scores["rows"]:
        if row["arm"] != "improved":
            continue
        result = json.loads(Path(row["source_path"]).read_text().splitlines()[-1])
        loc = result["localization"]
        before = loc["best_round_selection"]["joint_evidence_v2"]["files"]["before"]
        alternate = dict(loc, ranked_locations=[{"path": path} for path in before])
        gold = canonical_gold((swe if row["instance_id"] in swe else omni)[row["instance_id"]])
        metrics = evaluate_three_level_from_gold_sets(alternate, gold)
        rank = next((i for i, path in enumerate(before, 1) if path in gold["files"]), 0)
        rows.append({"instance_id": row["instance_id"], "live_rank": row["first_rank"],
                     "pre_consensus_rank": rank, "live_metrics": row["metrics"], "ablated_metrics": metrics})
        print(row["instance_id"], row["first_rank"], "->", rank)
    with args.output.open("x") as handle:
        json.dump({"scope": "fixed trajectory offline ablation, not a new model run", "rows": rows}, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
