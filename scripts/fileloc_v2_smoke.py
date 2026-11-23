"""Frozen three-arm diagnosis against the actual clean f5163fb worktree.

Only task-evidence whitelist enters inference. No scoring labels are loaded here.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = "f5163fbcd05d1ccf9084e679e2b9bb3055ca82b0"
SENTINELS = ["processing__p5.js-3709", "Automattic__wp-calypso-21648",
    "Automattic__wp-calypso-22026", "chartjs__Chart.js-10806", "statsmodels__statsmodels-7380",
    "babel__babel-15349", "prettier__prettier-15256", "assertj__assertj-2364"]
FLAGS = {"baseline": {"MYCODE_FILELOC_READ_RECONCILE": "0", "MYCODE_FILELOC_OWNER_CHALLENGE": "0"},
         "read_only": {"MYCODE_FILELOC_READ_RECONCILE": "1", "MYCODE_FILELOC_OWNER_CHALLENGE": "0"},
         "improved": {"MYCODE_FILELOC_READ_RECONCILE": "1", "MYCODE_FILELOC_OWNER_CHALLENGE": "1"}}
WHITELIST = {"repo", "instance_id", "base_commit", "problem_statement", "created_at", "image_assets",
             "version", "image_urls", "web_urls", "language", "website links"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted((root / "src/mycode").rglob("*.py"))}


def task_input(raw):
    return {k: v for k, v in raw.items() if k in WHITELIST}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ["workspace", "baseline-root", "case-manifest", "env-file", "run-dir"]:
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--model", default="mimo-v2.6-pro")
    p.add_argument("--stage", choices=["sentinels", "full"], default="sentinels")
    p.add_argument("--workers", type=int, choices=[1, 2, 4], default=4)
    p.add_argument("--prepare-only", action="store_true")
    p.add_argument("--quality", action="store_true", help="Only new MAGNET: 6 rounds/24 steps/18 contexts/3600s, coverage and listwise")
    p.add_argument("--reference-run-dir", type=Path, help="Freeze existing v1 records without rerunning them")
    args = p.parse_args()
    if args.quality and not args.reference_run_dir:
        p.error('--quality requires a frozen --reference-run-dir')
    base = args.baseline_root.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=base, text=True).strip() != BASE:
        raise SystemExit("Baseline is not f5163fb")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=base, text=True).strip():
        raise SystemExit("Baseline worktree is not clean")
    sys.path.insert(0, str(ROOT / "src"))
    from mycode.utils.env import load_env_file
    load_env_file(args.env_file)
    env = dict(os.environ)
    if any(not env.get(k) for k in ["BASE_URL", "API_KEY"]):
        raise SystemExit("Missing model configuration (redacted)")
    # Freeze the established v1 settings on ALL arms; arm flags are the only intervention.
    env.update(LOAD_ENV_FILE="0", LOC_CODE_ROOT=str(args.workspace), MODEL_API_NAME=args.model,
        MODEL_NAME=args.model, MYCODE_MODEL_PROFILE="mimo", MYCODE_VLM_IMAGE_TRANSPORT="data_uri",
        MYCODE_FAST_SEED_PLANNER="1", MYCODE_FAST_SEED_LLM="1", MYCODE_LLM_CANDIDATE_REVIEW="1",
        MYCODE_LLM_RETRIES="1", MYCODE_PHASE_LOG="1", MYCODE_READ_EXPLICIT_PATHS="1",
        MYCODE_JOINT_EVIDENCE_V2="1", MYCODE_FUNCTION_SOURCE_LEDGER="1",
        MYCODE_FUNCTION_REREAD_LIMIT="48", MYCODE_FILE_FRONT_RERANK="verified_frontier",
        MYCODE_FUNCTION_FINAL_POLICY="consensus", MYCODE_CONCERN_MATCH_V4="0",
        MYCODE_TOOL_EVIDENCE_HANDOFF="0", MYCODE_RESPONSIBILITY_GUARD_V5="0",
        MYCODE_WORKING_EVIDENCE_V5="0", MYCODE_DIRECT_ENTRY_V5="0")
    for stage in ["PLANNING", "EVIDENCE", "CONTROLLER", "VLM"]:
        env[f"{stage}_MODEL_API_NAME"] = args.model
    rounds,steps,timeout= (6,24,3600) if args.quality else (4,12,1800)
    env.update(MYCODE_FILELOC_REVIEW_COVERAGE="1" if args.quality else "0",
        MYCODE_FILELOC_LISTWISE_RANK="1" if args.quality else "0")
    if args.quality:
        env.update(MYCODE_CODE_CONTEXT_LIMIT="18",MYCODE_CANDIDATE_REVIEW_MAX_TOKENS="4000")
    panel = json.loads(args.case_manifest.read_text())["cases"]
    cases = [c for c in panel if args.stage == "full" or c["instance_id"] in SENTINELS]
    if len(cases) != (32 if args.stage == "full" else 8):
        raise SystemExit("Unexpected frozen panel size")
    args.run_dir.mkdir(parents=True, exist_ok=False)
    sources = {"SWE92": args.workspace / "clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl",
               "Omni296": args.workspace / "collected_results/omin296/data/samples296.jsonl"}
    samples = {ds: {r["instance_id"]: r for r in map(json.loads, path.open())} for ds, path in sources.items()}
    for case in cases:
        path = args.run_dir / (case["instance_id"] + ".input.jsonl")
        with path.open("x") as handle:
            handle.write(json.dumps(task_input(samples[case["dataset"]][case["instance_id"]]), ensure_ascii=False) + "\n")
        case.update(input_path=str(path), input_sha256=digest(path))
    arms = ["improved"] if args.quality else ["baseline", "improved"] if args.stage == "full" else list(FLAGS)
    manifest = dict(cases=cases, arms=arms, model=args.model, profile="mimo", baseline_commit=BASE,
        current_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        implementation_hashes=source_hashes(ROOT), baseline_implementation_hashes=source_hashes(base),
        case_manifest_sha256=digest(args.case_manifest), sources={str(v):digest(v) for v in sources.values()},
        protocol=dict(rounds=rounds, steps=steps, top_k=15, timeout_seconds=timeout, image_transport="data_uri",
                      code_context_limit=18 if args.quality else 10,review_batch_size=5 if args.quality else 6),
        quality=args.quality,
        effective_flags={k:env[k] for k in ["MYCODE_MODEL_PROFILE", "MYCODE_VLM_IMAGE_TRANSPORT",
            "MYCODE_FAST_SEED_PLANNER", "MYCODE_FAST_SEED_LLM", "MYCODE_LLM_CANDIDATE_REVIEW",
            "MYCODE_LLM_RETRIES", "MYCODE_PHASE_LOG", "MYCODE_READ_EXPLICIT_PATHS", "MYCODE_JOINT_EVIDENCE_V2",
            "MYCODE_FUNCTION_SOURCE_LEDGER", "MYCODE_FUNCTION_REREAD_LIMIT", "MYCODE_FILE_FRONT_RERANK",
            "MYCODE_FUNCTION_FINAL_POLICY", "MYCODE_CONCERN_MATCH_V4", "MYCODE_TOOL_EVIDENCE_HANDOFF",
            "MYCODE_RESPONSIBILITY_GUARD_V5", "MYCODE_WORKING_EVIDENCE_V5", "MYCODE_DIRECT_ENTRY_V5",
            "MYCODE_FILELOC_REVIEW_COVERAGE", "MYCODE_FILELOC_LISTWISE_RANK"]}, arm_flags=FLAGS,
        warning="Preselected diagnostic development tasks, not independent full-benchmark evidence")
    manifest['harness_hashes']={name:digest(ROOT/'scripts'/name) for name in
        ['fileloc_v2_smoke.py','score_fileloc_v2.py','score_fileloc_pairs.py','continue_fileloc_quality.py']}
    if args.reference_run_dir:
        reference=json.loads((args.reference_run_dir/'manifest.json').read_text())
        refs={c['instance_id']:c for c in reference['cases']}
        for c in cases:
            old=refs.get(c['instance_id'],{})
            if c['input_sha256'] != old.get('input_sha256',old.get('sha256')):
                raise SystemExit('Reference input hash mismatch; never compare different task inputs')
        manifest['reference']=dict(run_dir=str(args.reference_run_dir.resolve()),
            manifest_sha256=digest(args.reference_run_dir/'manifest.json'),
            outputs={c['instance_id']:digest(args.reference_run_dir/c['instance_id']/'baseline/localization_results.jsonl')
                     for c in cases if (args.reference_run_dir/c['instance_id']/'baseline/localization_results.jsonl').exists()},
            warning='Frozen historical MAGNET and external results unchanged; budget increased only for new MAGNET')
    (args.run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for name in manifest['harness_hashes']:
        target=args.run_dir/'source_snapshot/harness'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/'scripts'/name,target)
    for name, root in [("baseline", base), ("improved", ROOT)]:
        for rel in source_hashes(root):
            target = args.run_dir / "source_snapshot" / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, target)
    if args.prepare_only:
        return
    import requests
    probe = requests.post(env["BASE_URL"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + env["API_KEY"]},
        json={"model":args.model, "messages":[{"role":"user", "content":"Reply READY only."}],
              "max_tokens":128, "thinking":{"type":"disabled"}}, timeout=45)
    if not probe.ok:
        raise SystemExit(f"Preflight HTTP {probe.status_code}")
    (args.run_dir / "preflight.json").write_text(json.dumps({"http_status":probe.status_code,
        "model":probe.json().get("model"), "usage":probe.json().get("usage")}))
    def run_case(entry):
        n, case = entry
        order = arms[n % len(arms):] + arms[:n % len(arms)]
        for arm in order:
            if source_hashes(ROOT) != manifest["implementation_hashes"] or source_hashes(base) != manifest["baseline_implementation_hashes"]:
                raise RuntimeError("Frozen source drift: stop, never mix implementations")
            if any(digest(ROOT/'scripts'/name)!=sha for name,sha in manifest['harness_hashes'].items()):
                raise RuntimeError('Frozen harness drift: stop, do not mix protocols')
            arm_root = base if arm == "baseline" else ROOT
            out = args.run_dir / case["instance_id"] / arm
            out.mkdir(parents=True, exist_ok=False)
            arm_env = dict(env, **FLAGS[arm], PYTHONPATH=str(arm_root / "src"))
            cmd = [sys.executable, str(arm_root / "newtest/run_clean15_dataset.py"),
                "--samples",case["input_path"], "--dataset", "swebench_multimodal-full-dev" if case["dataset"] == "SWE92" else "omnigirl-full-candidates",
                "--output-dir",str(out), "--top-k","15", "--dynamic-rounds",str(rounds), "--max-react-steps",str(steps),
                "--max-tool-rounds","1", "--structure-only", "--no-auto-fetch-repos", "--use-llm",
                "--use-llm-planning", "--use-llm-controller", "--use-vlm", "--download-images", "--allow-network",
                "--sample-timeout-seconds",str(timeout), "--sample-heartbeat-interval","30"]
            print(f"RUN {case['instance_id']} {arm}", flush=True)
            started = time.monotonic()
            with (out / "console.log").open("x") as log:
                try:
                    result = subprocess.run(cmd, cwd=arm_root, env=arm_env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout+60)
                    status = dict(returncode=result.returncode)
                except subprocess.TimeoutExpired:
                    status = dict(returncode=None, error="subprocess_timeout")
            status.update(instance_id=case["instance_id"], arm=arm, elapsed_seconds=time.monotonic()-started)
            (out / "execution.json").write_text(json.dumps(status, indent=2))
            print(json.dumps(status), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_case, enumerate(cases)))


if __name__ == "__main__":
    main()
