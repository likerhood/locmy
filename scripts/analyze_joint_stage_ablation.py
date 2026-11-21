"""All-case deterministic function-stage ablations, not new model runs."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.dynamic_retrieval.search_agent import _closure_context_map, _joint_round_consensus
from mycode.evaluation.localization_eval import evaluate_entity_ranking
from score_fileloc_pairs import canonical_gold


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["MYCODE_JOINT_EVIDENCE_V2"] = "1"
    source = json.loads(args.scores.read_text())
    improved = [r for r in source["rows"] if r["arm"] == "improved"]
    if source.get("incomplete") or len(improved) != 8 or len({r["instance_id"] for r in improved}) != 8:
        raise SystemExit("This ablation requires all eight complete, distinct development pairs")
    swe = json.loads((args.workspace / "collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni = {r["instance_id"]: r["gold"] for r in map(json.loads, (args.workspace / "collected_results/omin296/data/reference_gold.jsonl").open())}
    rows = []
    for row in source["rows"]:
        if row["arm"] != "improved":
            continue
        loc = json.loads(Path(row["source_path"]).read_text().splitlines()[-1])["localization"]
        diagnostic = loc["best_round_selection"]["joint_evidence_v2"]["functions"]
        before = diagnostic["before"]
        rankings = [[SimpleNamespace(**i) for i in r["ranked_functions"]] for r in loc["dynamic_rounds"]]
        pool = {i.id: i for ranking in rankings for i in ranking}
        contexts = _closure_context_map(c for r in loc["dynamic_rounds"] for c in r["code_contexts"])
        no_ledger, _ = _joint_round_consensus([pool[i] for i in before], rankings,
            key=lambda i: i.id, contexts=contexts, top_k=15)
        variants = {"selected_round_only": before, "consensus_without_ledger": [i.id for i in no_ledger],
                    "live_consensus_with_ledger": diagnostic["after"]}
        gold = canonical_gold((swe if row["instance_id"] in swe else omni)[row["instance_id"]])["functions"]
        metrics = {name: evaluate_entity_ranking(ids, gold) for name, ids in variants.items()}
        rows.append({"instance_id": row["instance_id"], "gold_count": len(gold), "variants": metrics,
                     "reservoir_insertions": len(diagnostic["source_reservoir"]),
                     "reservoir_gold_hits": sum(i["id"] in gold for i in diagnostic["source_reservoir"])})
        print(row["instance_id"], {name: {k: m[k] for k in ["acc@1", "acc@15", "set_rec@15"]} for name, m in metrics.items()})
    with args.output.open("x") as handle:
        json.dump({"scope": "all eight saved trajectories, deterministic stage ablation only; empty function gold is not failure",
                   "rows": rows}, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
