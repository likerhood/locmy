"""Deterministic v3 final-stage replay on frozen v2.1 model trajectories.

This does not simulate longer exploration or claim new live model scores.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.dynamic_retrieval.search_agent import _closure_context_map, _joint_round_consensus, _reread_function_ledger
from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets
from mycode.repo_index.structure_index import RepositoryIndex
from score_fileloc_pairs import canonical_gold


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["MYCODE_JOINT_EVIDENCE_V2"] = "1"
    source = json.loads(args.scores.read_text())
    swe = json.loads((args.workspace / "collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni = {r["instance_id"]: r["gold"] for r in map(json.loads, (args.workspace / "collected_results/omin296/data/reference_gold.jsonl").open())}
    output = []
    for row in source["rows"]:
        if row["arm"] != "improved":
            continue
        path = Path(row["source_path"])
        loc = json.loads(path.read_text().splitlines()[-1])["localization"]
        sample = json.loads((path.parents[2] / f"{row['instance_id']}.input.jsonl").read_text())
        meta = loc["index"]
        index = RepositoryIndex(repo=meta["repo"], instance_id=meta["instance_id"], dataset=meta["dataset"],
                                repo_root=Path("/__joint_replay_no_checkout__"), structure_path=Path(meta["structure_path"]))
        rankings = [[SimpleNamespace(**i) for i in r["ranked_functions"]] for r in loc["dynamic_rounds"]]
        selected_ids = loc["best_round_selection"]["joint_evidence_v2"]["functions"]["before"]
        entities = {i.id: i for ranking in rankings for i in ranking}
        selected = [entities[i] for i in selected_ids]
        contexts = _closure_context_map(c for r in loc["dynamic_rounds"] for c in r["code_contexts"])
        ledger = _reread_function_ledger(index, rankings, sample["problem_statement"], limit=48)
        functions, diagnostics = _joint_round_consensus(selected, rankings, key=lambda i: i.id,
            contexts=contexts, top_k=15, entity_evidence=ledger)
        files = loc["best_round_selection"]["joint_evidence_v2"]["files"]["before"]
        alternate = dict(loc, ranked_locations=[{"path": p} for p in files], ranked_functions=[vars(i) for i in functions])
        # Gold only enters after all inference/replay decisions above.
        gold = canonical_gold((swe if row["instance_id"] in swe else omni)[row["instance_id"]])
        metrics = evaluate_three_level_from_gold_sets(alternate, gold)
        output.append({"instance_id": row["instance_id"], "replay_metrics": metrics, "live_v2_1_metrics": row["metrics"],
                       "function_diagnostics": diagnostics, "file_order": files})
        print(row["instance_id"], {level: {key: metrics[level][key] for key in ["acc@1", "acc@3", "acc@15", "set_rec@15"]}
              for level in ["file", "function"]}, flush=True)
    with args.output.open("x") as handle:
        json.dump({"scope": "v3 final-stage replay only, not longer-budget live inference", "rows": output}, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
