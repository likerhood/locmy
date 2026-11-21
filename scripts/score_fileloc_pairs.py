"""Post-hoc frozen-gold evaluation of fileloc_pair_smoke outputs (never agent input)."""
import argparse
import json
from pathlib import Path
import sys
import re
import hashlib
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets


def stage_ranks(snapshots, gold):
    ranks = {}
    def visit(value, prefix=""):
        if isinstance(value, list) and value and all(isinstance(r, dict) and "path" in r for r in value):
            ranks[prefix] = next((i for i, row in enumerate(value, 1) if row["path"] in gold), 0)
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{prefix}/{key}".lstrip("/"))
    visit(snapshots)
    return ranks


def model_configuration_errors(result):
    """Do not mistake a retired/unauthorized model's fallback output for a run."""
    errors = []
    def visit(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                location = f"{path}/{key}"
                if key.endswith("error") and isinstance(child, str) and any(
                    tag in location.lower() for tag in ["llm", "vlm", "candidate_review"]
                ) and any(marker in child for marker in ["HTTP 400", "HTTP 401", "HTTP 403", "Unsupported model"]):
                    errors.append(location)
                else:
                    visit(child, location)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, f"{path}/{i}")
    visit(result)
    return errors


def canonical_gold(gold):
    return {plural: gold.get(plural, gold.get(singular, []))
            for singular, plural in [("file", "files"), ("module", "modules"), ("function", "functions")]}


def valid_result(result):
    return bool(result.get("localization") and result.get("status") in {None, "ok", "success"}
                and (result.get("token_usage_summary") or {}).get("total_tokens")
                and not model_configuration_errors(result))


def merge_complement_manifest(primary, complement):
    """Combine only disjoint cases run with the exact same frozen implementation."""
    for key in ["base_commit", "tracked_diff_sha256", "source_hashes", "model", "profile", "top_k",
                "dynamic_rounds", "max_react_steps", "max_tool_rounds", "task_timeout_seconds",
                "implementation_hashes", "improved_strategy", "vlm", "full_browser", "function_reread_limit"]:
        if key == "tracked_diff_sha256" and primary.get("implementation_hashes") and primary["implementation_hashes"] == complement.get("implementation_hashes"):
            continue
        if primary.get(key) != complement.get(key):
            raise ValueError(f"Complement configuration mismatch: {key}")
    ids = [case["instance_id"] for manifest in [primary, complement] for case in manifest["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Complement must contain disjoint tasks; no choosing between valid outcomes")
    return dict(primary, cases=primary["cases"] + complement["cases"])


def experimental_release_assessment(rows, *, complete):
    """Explicit new user policy; never overwrite the historical strict gate."""
    gains, regressions = [], []
    if not complete:
        return {"policy": "either_granularity_gain_with_disclosed_regressions", "complete": False,
                "gain_metrics": [], "regressed_metrics": [], "eligible_for_experimental_push": False,
                "scope": "incomplete pairs are not compared for release"}
    keys = ["acc@1", "acc@3", "acc@5", "acc@8", "acc@15", "set_sl@15", "set_rec@15"]
    for level in ["file", "function"]:
        for key in keys:
            totals = {arm: sum(r["metrics"][level][key] for r in rows if r["arm"] == arm)
                      for arm in ["baseline", "improved"]}
            delta = totals["improved"] - totals["baseline"]
            if delta > 1e-12:
                gains.append(f"{level}/{key}")
            elif delta < -1e-12:
                regressions.append(f"{level}/{key}")
    return {"policy": "either_granularity_gain_with_disclosed_regressions",
            "complete": complete, "gain_metrics": gains, "regressed_metrics": regressions,
            "eligible_for_experimental_push": bool(complete and gains),
            "scope": "experimental branch only; unit/regression tests must also pass; not full benchmark validation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--retry-run-dir", type=Path)
    parser.add_argument("--complement-run-dir", type=Path, help="Disjoint remaining cases, identical frozen code/model/budgets")
    parser.add_argument("--report-prefix", default="paired")
    parser.add_argument("--release-policy", choices=["legacy-strict", "any-gain-experimental"], default="legacy-strict")
    parser.add_argument("--summary-output", type=Path, help="New lightweight repository-safe result summary; complete pairs only")
    parser.add_argument("--code-commit", default="", help="Git commit whose dynamic implementation hashes must match before summary export")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.report_prefix):
        parser.error("report prefix must be a simple name")
    manifest = json.loads((args.run_dir / "manifest.json").read_text())
    case_roots = {case["instance_id"]: args.run_dir for case in manifest["cases"]}
    if args.complement_run_dir:
        complement = json.loads((args.complement_run_dir / "manifest.json").read_text())
        manifest = merge_complement_manifest(manifest, complement)
        case_roots.update({case["instance_id"]: args.complement_run_dir for case in complement["cases"]})
    retry_cases = {}
    if args.retry_run_dir:
        retry = json.loads((args.retry_run_dir / "manifest.json").read_text())
        for key in ["base_commit", "tracked_diff_sha256", "source_hashes", "model", "profile", "top_k",
                    "dynamic_rounds", "max_react_steps", "max_tool_rounds", "task_timeout_seconds"]:
            if key == "tracked_diff_sha256" and manifest.get("implementation_hashes") and manifest["implementation_hashes"] == retry.get("implementation_hashes"):
                continue
            if retry[key] != manifest[key]:
                raise SystemExit(f"Retry configuration mismatch: {key}")
        for key in ["implementation_hashes", "improved_strategy", "function_reread_limit"]:
            if retry.get(key) != manifest.get(key):
                raise SystemExit(f"Retry implementation mismatch: {key}")
        originals = {c["instance_id"]: c for c in manifest["cases"]}
        for c in retry["cases"]:
            original = originals[c["instance_id"]]
            if any(c[k] != original[k] for k in ["dataset", "role", "sha256"]):
                raise SystemExit("Retry task input differs")
            previous = []
            for arm in ["baseline", "improved"]:
                p = case_roots[c["instance_id"]] / c["instance_id"] / arm / "localization_results.jsonl"
                previous.append(valid_result(json.loads(p.read_text().splitlines()[-1])) if p.exists() and p.stat().st_size else False)
            if all(previous):
                raise SystemExit("Refusing to replace a completed valid pair based on its outcome")
            retry_cases[c["instance_id"]] = True
    swe = json.loads((args.workspace / "collected_results/swe92_new/消融统一结果_20261015/frozen_gold.json").read_text())
    omni = {r["instance_id"]: r["gold"] for r in map(json.loads, (args.workspace / "collected_results/omin296/data/reference_gold.jsonl").open())}
    rows = []
    incomplete = []
    for case in manifest["cases"]:
        ident = case["instance_id"]
        gold = canonical_gold((swe if case["dataset"] == "SWE92" else omni)[ident])
        for arm in ["baseline", "improved"]:
            output = (args.retry_run_dir if ident in retry_cases else case_roots[ident]) / ident / arm
            path = output / "localization_results.jsonl"
            if not path.exists() or not path.stat().st_size:
                incomplete.append(f"{ident}/{arm}")
                continue
            result = json.loads(path.read_text().splitlines()[-1])
            loc = result.get("localization") or result.get("dynamic_localization") or {}
            usage = result.get("token_usage_summary") or {}
            if not valid_result(result):
                incomplete.append(f"{ident}/{arm}:invalid_or_partial")
                continue
            metrics = evaluate_three_level_from_gold_sets(loc, gold)
            predicted = list(dict.fromkeys(r["path"] for r in loc.get("ranked_locations", [])))
            gt = set(gold["files"])
            rank = next((i for i, p in enumerate(predicted, 1) if p in gt), 0)
            stages = stage_ranks(loc.get("rank_stage_snapshots", {}), gt)
            reads = [dict(step=step.get("round_no"), **step["observation"]["read_execution"])
                     for step in (loc.get("react_agent_trace") or {}).get("steps", [])
                     if (step.get("observation") or {}).get("read_execution")]
            target_evidence = [dict(path=item["path"], rank=i,
                                    precision=(item.get("belief") or {}).get("precision_rerank", {}))
                               for i, item in enumerate(loc.get("ranked_locations", []), 1) if item["path"] in gt]
            joint = (loc.get("best_round_selection") or {}).get("joint_evidence_v2") or {}
            function_ledger = (joint.get("functions") or {}).get("source_ledger", [])
            rows.append(dict(instance_id=ident, role=case["role"], arm=arm, first_rank=rank,
                             metrics=metrics, predicted_files=predicted, stage_ranks=stages,
                             read_execution=reads, target_evidence=target_evidence,
                             actual_rounds=len(loc.get("dynamic_rounds", [])),
                             main_react_steps=len((loc.get("react_agent_trace") or {}).get("steps", [])),
                             function_source_reads=sum(bool(e.get("source_read")) for e in function_ledger),
                             source_path=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             status=result.get("status"), elapsed_seconds=result.get("elapsed_seconds"),
                             token_usage=result.get("token_usage_summary")))
    # Evaluate all supplied function cutoffs, not just A15. No candidate pool or gold supplied to inference.
    def metric_sum(arm, level, key):
        return sum(float(r["metrics"][level].get(key, 0)) for r in rows if r["arm"] == arm)
    ks = [1,2,3,4,5,6,7,8,10,15]
    # Existing evaluator uses acc@k keys; verify instead of treating absent fields as zero.
    expected_keys = [f"acc@{k}" for k in ks]
    keys_valid = bool(rows) and all(k in rows[0]["metrics"]["file"] for k in expected_keys)
    gate = dict(complete=len(rows)==2*len(manifest["cases"]) and not incomplete, metric_keys_valid=keys_valid)
    paired = {(r["instance_id"],r["arm"]):r for r in rows}
    if gate["complete"] and keys_valid:
        gate["front_cutoffs_nonregressing"] = all(metric_sum("improved","file",k)>=metric_sum("baseline","file",k) for k in expected_keys)
        gate["net_A1_or_A3_gain"] = any(metric_sum("improved","file",f"acc@{k}")>metric_sum("baseline","file",f"acc@{k}") for k in [1,3])
        gate["negative_rank_improved"] = any(
            paired[c["instance_id"],"improved"]["first_rank"] > 0 and
            (paired[c["instance_id"],"baseline"]["first_rank"] == 0 or
             paired[c["instance_id"],"improved"]["first_rank"] < paired[c["instance_id"],"baseline"]["first_rank"])
            for c in manifest["cases"] if c["role"] == "negative")
        gate["protection_A1"] = all(paired[c["instance_id"],"improved"]["first_rank"] == 1 for c in manifest["cases"] if c["role"] == "protection")
        gate["function_cutoffs_nonregressing"] = all(metric_sum("improved","function",f"acc@{k}")>=metric_sum("baseline","function",f"acc@{k}") for k in range(1,16))
        gate["set_metrics_nonregressing"] = all(metric_sum("improved",level,k)>=metric_sum("baseline",level,k)
            for level in ["file", "function"] for k in ["set_sl@15", "set_rec@15"])
    required_ids = {"processing__p5.js-3709", "Automattic__wp-calypso-21648",
                    "Automattic__wp-calypso-22026", "chartjs__Chart.js-10806",
                    "babel__babel-15349", "markedjs__marked-1262",
                    "markedjs__marked-1435", "processing__p5.js-3068"}
    gate["full_eight_case_protocol"] = {c["instance_id"] for c in manifest["cases"]} == required_ids
    gate["eligible_for_push"] = bool(gate) and all(gate.values())
    paired_ids = {c["instance_id"] for c in manifest["cases"]
                  if all((c["instance_id"], arm) in paired for arm in ["baseline", "improved"])}
    aggregate = []
    for level in ["file", "function"]:
        for key in [f"acc@{k}" for k in range(1,16)] + ["mrr@15", "map@15", "set_sl@15", "set_rec@15"]:
            values = {arm: sum(r["metrics"][level][key] for r in rows
                               if r["arm"]==arm and r["instance_id"] in paired_ids) / len(paired_ids)
                      for arm in ["baseline", "improved"]} if paired_ids else {}
            aggregate.append(dict(level=level, metric=key, N=len(paired_ids), **values))
    release = experimental_release_assessment(rows, complete=gate["complete"] and keys_valid)
    payload = dict(rows=rows, incomplete=incomplete, gate=gate,
                   selected_release_policy=args.release_policy, experimental_release_assessment=release,
                   paired_aggregate=aggregate,
                   retry_run_dir=str(args.retry_run_dir) if args.retry_run_dir else None,
                   complement_run_dir=str(args.complement_run_dir) if args.complement_run_dir else None,
                   superseded_infrastructure_failed_pairs=list(retry_cases),
                   warning="8 development cases, same MiMo profile; not evidence of full Kimi/benchmark superiority")
    (args.run_dir / f"{args.report_prefix}_scores.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    lines = [f"# MiMo开发配对结果（计划{len(manifest['cases'])}例）", "", "开发案例，非全量性能证明。未完成任务不计作0分。", "",
             "| 任务 | 原版首个正确名次 | 改版首个正确名次 |", "| --- | ---: | ---: |"]
    for case in manifest["cases"]:
        def value(arm):
            row = paired.get((case["instance_id"],arm))
            return str(row["first_rank"]) if row else "未完成/无有效结果"
        lines.append(f"| {case['instance_id']} | {value('baseline')} | {value('improved')} |")
    lines += ["", "0表示预测列表未命中；不是名次第一。", "",
              f"互补任务目录：{args.complement_run_dir or '无'}。仅合并不重复任务，代码/模型/预算须相同，不跨版本择优。", "",
              f"独立重跑目录：{args.retry_run_dir or '无'}；仅替换原存在执行异常的整对任务：{', '.join(retry_cases) or '无'}。原始两组记录均保留。", "",
              f"## 已完成配对汇总（N={len(paired_ids)}，非全部运行条数）", "",
              "函数空gold按冻结协议计0；这种任务不提供函数优劣证据。未完成任务不当作失败0分。", "",
              "| 粒度 | 指标 | 原版 % | 改版 % | 差值 pp |", "| --- | --- | ---: | ---: | ---: |"]
    for item in aggregate:
        if item["N"]:
            a,b=item["baseline"]*100,item["improved"]*100
            lines.append(f"| {item['level']} | {item['metric']} | {a:.2f} | {b:.2f} | {b-a:+.2f} |")
    lines += ["", "## 推送门槛", "", "```json", json.dumps(gate, ensure_ascii=False, indent=2), "```"]
    lines += ["", "## 新授权实验分支发布口径（不覆盖旧严格门槛）", "",
              f"本次选择：{args.release_policy}；旧 gate 保留原值。", "", "```json",
              json.dumps(release, ensure_ascii=False, indent=2), "```"]
    (args.run_dir / f"{args.report_prefix}_results.md").write_text("\n".join(lines)+"\n")
    if args.summary_output:
        if not gate["complete"] or not args.code_commit:
            raise SystemExit("Summary export needs complete pairs and --code-commit")
        repo_root = Path(__file__).resolve().parents[1]
        commit = subprocess.check_output(["git", "rev-parse", "--verify", args.code_commit + "^{commit}"], cwd=repo_root, text=True).strip()
        for relative, expected in manifest["implementation_hashes"].items():
            blob = subprocess.check_output(["git", "show", f"{commit}:{relative}"], cwd=repo_root)
            if hashlib.sha256(blob).hexdigest() != expected:
                raise SystemExit(f"Commit does not match frozen source: {relative}")
        light_rows = []
        for row in rows:
            item = {k: row[k] for k in ["instance_id", "role", "arm", "first_rank", "metrics", "source_sha256",
                    "elapsed_seconds", "token_usage", "actual_rounds", "main_react_steps", "function_source_reads"]}
            metric_keys = [f"acc@{k}" for k in range(1,16)] + ["gold_count", "pred_count", "mrr@15", "map@15", "set_sl@15", "set_rec@15"]
            item["metrics"] = {level: {key: row["metrics"][level][key] for key in metric_keys if key in row["metrics"][level]}
                               for level in ["file", "function"]}
            item["source_run"] = Path(row["source_path"]).parents[2].name
            light_rows.append(item)
        summary = {"algorithm": manifest.get("improved_strategy"), "code_commit": commit,
                   "model": manifest["model"], "profile": manifest["profile"],
                   "budget": {k: manifest.get(k) for k in ["top_k", "dynamic_rounds", "max_react_steps", "task_timeout_seconds", "function_reread_limit"]},
                   "gate": gate, "experimental_release_assessment": release, "paired_aggregate": aggregate,
                   "rows": light_rows, "implementation_hashes": manifest["implementation_hashes"],
                   "note": "8 selected development cases, not a full benchmark claim; main_react_steps is not all pipeline actions; token records are not complete billing"}
        with args.summary_output.open("x") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(json.dumps(dict(completed=len(rows), incomplete=incomplete, gate=gate), ensure_ascii=False))


if __name__ == "__main__":
    main()
