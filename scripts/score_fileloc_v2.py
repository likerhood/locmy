"""Post-hoc three-arm comparison; this module is never imported by inference."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_fileloc_pairs import canonical_gold, stage_ranks, valid_result
from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets


def reference_id(value, level):
    path, sep, entity = str(value).partition("::")
    if sep:
        if level == "module" and entity == "module:__file__":
            return path
        kind, colon, name = entity.partition(":")
        if colon and kind in {"function", "method", "class", "module"}:
            return path + "::" + name
    return str(value)


def score(loc, gold, dataset):
    if dataset != "Omni296":
        return evaluate_three_level_from_gold_sets(loc, gold)
    adapted, normalized = dict(loc), deepcopy(gold)
    for level, plural in [("function", "functions"), ("module", "modules")]:
        normalized[plural] = [reference_id(v, level) for v in gold[plural]]
        adapted["ranked_" + plural] = []
        for item in loc.get("ranked_" + plural, []) or []:
            value = item if isinstance(item, str) else item.get("id") or (
                str(item.get("path", "")) + "::" + str(item.get("kind", "entity")) + ":" + str(item.get("name", "")))
            adapted["ranked_" + plural].append(reference_id(value, level))
    return evaluate_three_level_from_gold_sets(adapted, normalized)


def assessment(rows, cases, arm):
    result = {}
    for dataset in ["SWE92", "Omni296"]:
        panel = [c for c in cases if c["dataset"] == dataset]
        indexed = {(r["instance_id"],r["arm"]):r for r in rows if r["dataset"] == dataset}
        paired = [c for c in panel if all((c["instance_id"],a) in indexed for a in ["baseline",arm])]
        keys = [f"acc@{k}" for k in [1,2,3,4,5,8,15]] + ["set_sl@15","set_rec@15"]
        deltas = {}
        for level in ["file","function"]:
            applicable = [c for c in paired if level != "function" or indexed[c["instance_id"],"baseline"]["function_applicable"]]
            deltas[level] = {k:sum(indexed[c["instance_id"],arm]["metrics"][level][k] - indexed[c["instance_id"],"baseline"]["metrics"][level][k] for c in applicable) for k in keys}
        protections = [c for c in panel if c["role"] == "protection"]
        new_complete=sum((c['instance_id'],arm) in indexed for c in panel)
        checks = dict(complete=len(panel) == new_complete == 16 and len(paired)>=15,
            file_front_gain=all(deltas["file"][f"acc@{k}"] >= 2-1e-12 for k in [1,3,5]),
            file_nonregressing=all(v >= -1e-12 for v in deltas["file"].values()),
            function_nonregressing=all(v >= -1e-12 for v in deltas["function"].values()),
            protection_A1=len(protections)==4 and all(indexed.get((c["instance_id"],arm),{}).get("first_rank")==1 for c in protections))
        result[dataset] = dict(N=len(paired),new_complete_N=new_complete,checks=checks, passed=all(checks.values()), net_metric_sums=deltas)
    return dict(datasets=result, passed=all(r["passed"] for r in result.values()))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--report-prefix", default="scores")
    p.add_argument("--reference-run-dir", type=Path, help="Frozen v1 records, never rerun or outcome-selected")
    args=p.parse_args()
    manifest=json.loads((args.run_dir/"manifest.json").read_text())
    reference=json.loads((args.reference_run_dir/'manifest.json').read_text()) if args.reference_run_dir else None
    if reference:
        references={c['instance_id']:c for c in reference['cases']}
        for c in manifest['cases']:
            old=references.get(c['instance_id'],{})
            if c['input_sha256'] != old.get('input_sha256',old.get('sha256')):
                raise SystemExit('Reference task input hash mismatch')
    swe=json.loads((args.workspace/"collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni={r["instance_id"]:r["gold"] for r in map(json.loads,(args.workspace/"collected_results/omin296/data/reference_gold.jsonl").open())}
    rows, incomplete=[],[]
    for case in manifest["cases"]:
        ident=case["instance_id"]
        gold=canonical_gold((swe if case["dataset"]=="SWE92" else omni)[ident])
        arms=manifest['arms']+(['baseline'] if reference and 'baseline' not in manifest['arms'] else [])
        for arm in arms:
            output=(args.reference_run_dir if reference and arm=='baseline' else args.run_dir)/ident/arm
            path=output/"localization_results.jsonl"
            if not (output/"execution.json").exists() or not path.exists() or not path.stat().st_size:
                incomplete.append(f"{ident}/{arm}:incomplete")
                continue
            raw=json.loads(path.read_text().splitlines()[-1])
            execution=json.loads((output/"execution.json").read_text())
            if execution.get("returncode") != 0 or not valid_result(raw):
                incomplete.append(f"{ident}/{arm}:invalid")
                continue
            loc=raw["localization"]
            before=loc.get('rank_stage_snapshots',{}).get('fileloc_listwise_before')
            before_loc=dict(loc,ranked_locations=before) if before is not None else None
            predicted=list(dict.fromkeys(x["path"] for x in loc.get("ranked_locations",[])))[:15]
            rows.append(dict(instance_id=ident,dataset=case["dataset"],role=case["role"],arm=arm,
                first_rank=next((i for i,x in enumerate(predicted,1) if x in gold["files"]),0),
                metrics=score(loc,gold,case["dataset"]),function_applicable=bool(gold["functions"]),
                stage_ranks=stage_ranks(loc.get("rank_stage_snapshots",{}),set(gold["files"])),
                source_path=str(path), token_usage=raw.get('token_usage_summary'),elapsed_seconds=raw.get('elapsed_seconds'),
                pre_listwise_metrics=score(before_loc,gold,case['dataset']) if before_loc else None,
                round_observations=[r.get("agent_observation",{}) for r in loc.get("dynamic_rounds",[])]))
    payload=dict(rows=rows,incomplete=incomplete,assessments={arm:assessment(rows,manifest["cases"],arm) for arm in manifest["arms"] if arm!="baseline"},
        entity_encoding="Omni exact typed-kind removal; reference build_baseline_report convention; SWE unchanged",
        warning="Only complete same-task pairs may be compared. Missing is not zero; sentinel panel cannot pass full gates.")
    payload['reference_run_dir']=str(args.reference_run_dir) if reference else None
    payload['reference_budget_warning']='New quality budget versus frozen old budget; not an equal-budget causal comparison' if reference else None
    import csv
    historical=args.workspace/'allresultwork/rq1/前列定位诊断_20261121/统计输出/per_instance_file_metrics.csv'
    if historical.exists():
        ids={c['instance_id'] for c in manifest['cases']}
        payload['frozen_external_baselines']=[{k:r[k] for k in ['dataset','model','method','instance_id','first_rank','A1','A2','A3','A4','A5','A8','A15']}
            for r in csv.DictReader(historical.open(encoding='utf-8-sig'))
            if r['instance_id'] in ids and r['method'] in {'CoSIL','LocAgent','GALA'} and r['model'] in {'MiMo','Kimi'}]
    with (args.run_dir/(args.report_prefix+".json")).open("x") as handle:
        json.dump(payload,handle,ensure_ascii=False,indent=2)
    lines=['# Fileloc A1–A5 development comparison','','Missing is not zero. Frozen external baselines use different models/budgets.','',
        '| Task | Frozen v1 rank | New rank | Before final rank |','| --- | ---: | ---: | ---: |']
    for c in manifest['cases']:
        def row(arm): return next((r for r in rows if r['instance_id']==c['instance_id'] and r['arm']==arm),{})
        new=row('improved');old=row('baseline')
        before=(new.get('stage_ranks') or {}).get('fileloc_listwise_before','missing')
        lines.append(f"| {c['instance_id']} | {old.get('first_rank','missing')} | {new.get('first_rank','missing')} | {before} |")
    lines+=['','0 = no hit in Top15, not rank 1.','','```json',json.dumps(payload['assessments'],indent=2),'```']
    with (args.run_dir/(args.report_prefix+'.md')).open('x') as handle:handle.write('\n'.join(lines)+'\n')
    print(json.dumps(dict(valid_arms=len(rows),incomplete=incomplete,assessments=payload["assessments"]),ensure_ascii=False))


if __name__ == "__main__":
    main()
