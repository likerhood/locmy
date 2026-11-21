"""Replay saved precision facts at head_selector, without any API or new evidence."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.dynamic_retrieval.search_agent import _verified_frontier_order


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; choose a new path")
    gold = json.loads(args.gold.read_text())
    records = {}
    skipped = []
    for line in args.results.open():
        result = json.loads(line)
        ident = result["instance_id"]
        if ident not in gold:
            continue
        loc = result.get("localization", {})
        before = (loc.get("rank_stage_snapshots") or {}).get("head_selector", [])
        facts = {r["path"]: (r.get("belief") or {}).get("precision_rerank") for r in loc.get("ranked_locations", [])}
        if not before or any(not facts.get(r["path"]) for r in before):
            skipped.append(ident)
            continue
        rows = [(facts[r["path"]]["quality"], facts[r["path"]]["base_rank"], r, facts[r["path"]]) for r in before]
        ordered = _verified_frontier_order(rows, 3.0)
        old = [r["path"] for r in before]
        new = [r[2]["path"] for r in ordered]
        assert set(old) == set(new) and len(old) == len(new)
        gt = set(gold[ident]["files"])
        def rank(paths):
            return next((i for i,p in enumerate(paths,1) if p in gt),0)
        records[ident] = dict(instance_id=ident, before_rank=rank(old), after_rank=rank(new),
                              before=old, after=new, changed=old!=new,
                              evidence=[dict(path=r[2]["path"], quality=r[0], eligible=r[3].get("head_eligible")) for r in rows])
    rows = list(records.values())
    summary = {}
    for k in [1,2,3,4,5,6,7,8,10,15]:
        summary[f"A{k}"] = {arm:sum(0<r[f"{arm}_rank"]<=k for r in rows) for arm in ["before", "after"]}
    payload = dict(source=str(args.results), gold=str(args.gold), N=len(rows), skipped=skipped,
                   stage="head_selector only, before closure; NOT full pipeline new performance",
                   summary=summary, rows=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    print(json.dumps(dict(N=len(rows),skipped=len(skipped),changed=sum(r["changed"] for r in rows),summary=summary)))


if __name__ == "__main__":
    main()
