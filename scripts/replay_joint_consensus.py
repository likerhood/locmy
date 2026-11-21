"""Fixed-trajectory replay of consensus only, not the changed read/exploration policy."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.dynamic_retrieval.search_agent import _joint_round_consensus
from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets
from score_fileloc_pairs import canonical_gold, valid_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    swe = json.loads((args.workspace / "collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni = {r["instance_id"]: r["gold"] for r in map(json.loads, (args.workspace / "collected_results/omin296/data/reference_gold.jsonl").open())}
    rows = []
    for path in sorted(args.run_dir.glob("*/improved/localization_results.jsonl")):
        result = json.loads(path.read_text().splitlines()[-1])
        if not valid_result(result):
            continue
        loc = result["localization"]
        after = dict(loc)
        contexts = {}
        for search_round in loc["dynamic_rounds"]:
            for context in search_round["code_contexts"]:
                contexts.setdefault(context["path"], {"snippets": []})["snippets"].extend(context.get("snippets", []))
        diagnostics = {}
        for field, identity in [("ranked_locations", "path"), ("ranked_functions", "id")]:
            selected = [SimpleNamespace(**item) for item in loc[field]]
            rankings = [[SimpleNamespace(**i) for i in r[field]] for r in loc["dynamic_rounds"]]
            items, diagnostics[field] = _joint_round_consensus(selected, rankings,
                key=lambda i: getattr(i, identity), contexts=contexts, top_k=15)
            after[field] = [vars(item) for item in items]
        # Labels enter only after ranking has finished.
        gold = canonical_gold((swe if result["instance_id"] in swe else omni)[result["instance_id"]])
        rows.append({"instance_id": result["instance_id"], "before": evaluate_three_level_from_gold_sets(loc, gold),
                     "after": evaluate_three_level_from_gold_sets(after, gold), "diagnostics": diagnostics})
    with args.output.open("x") as handle:
        json.dump({"scope": "consensus-only fixed trajectory; not live v2 performance", "rows": rows}, handle, ensure_ascii=False, indent=2)
    for row in rows:
        print(row["instance_id"], {level: {metric: [row[arm][level][metric] for arm in ["before", "after"]]
              for metric in ["acc@1", "acc@3", "acc@15", "set_rec@15"]} for level in ["file", "function"]})


if __name__ == "__main__":
    main()
