"""Freeze and run eight development pairs; never supply patch/gold to the agent.

Use an explicit --run-dir (must not exist). Credentials stay in --env-file.
This is a development diagnostic, not an unbiased benchmark evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("SWE92", "processing__p5.js-3709", "negative"),
    ("SWE92", "Automattic__wp-calypso-21648", "negative"),
    ("SWE92", "Automattic__wp-calypso-22026", "negative"),
    ("SWE92", "chartjs__Chart.js-10806", "negative"),
    ("Omni296", "babel__babel-15349", "negative"),
    ("SWE92", "markedjs__marked-1262", "protection"),
    ("SWE92", "markedjs__marked-1435", "protection"),
    ("SWE92", "processing__p5.js-3068", "protection"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model-api-name", default="")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume only an identical frozen manifest; never overwrite an arm")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    parser.add_argument("--instance-id", action="append", default=[], help="Explicit subset, e.g. an infrastructure-failed pair retry")
    parser.add_argument("--dynamic-rounds", type=int, default=3)
    parser.add_argument("--max-react-steps", type=int, default=8)
    parser.add_argument("--sample-timeout-seconds", type=int, default=900)
    parser.add_argument("--function-reread-limit", type=int, default=48)
    args = parser.parse_args()
    if min(args.dynamic_rounds, args.max_react_steps) < 1 or args.sample_timeout_seconds < 60 or args.function_reread_limit < 0:
        parser.error("Invalid rounds, steps, timeout or reread limit")
    sys.path.insert(0, str(ROOT / "src"))
    from mycode.utils.env import load_env_file
    load_env_file(args.env_file)
    if args.model_api_name:
        os.environ["MODEL_API_NAME"] = args.model_api_name
        os.environ["MODEL_NAME"] = args.model_api_name
    required = ["BASE_URL", "API_KEY", "MODEL_API_NAME"]
    if any(not os.environ.get(key) for key in required):
        raise SystemExit("Missing model configuration (values redacted)")
    env = dict(os.environ)
    env.update(LOAD_ENV_FILE="0", LOC_CODE_ROOT=str(args.workspace),
               MYCODE_MODEL_PROFILE="mimo", MYCODE_FAST_SEED_PLANNER="1", MYCODE_FAST_SEED_LLM="1",
               MYCODE_LLM_CANDIDATE_REVIEW="1", MYCODE_LLM_RETRIES="1", MYCODE_PHASE_LOG="1")
    for stage in ["PLANNING", "EVIDENCE", "CONTROLLER", "VLM"]:
        env[f"{stage}_MODEL_API_NAME"] = env["MODEL_API_NAME"]
    args.run_dir.mkdir(parents=True, exist_ok=args.resume)
    sources = {
        "SWE92": args.workspace / "clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl",
        "Omni296": args.workspace / "collected_results/omin296/data/samples296.jsonl",
    }
    samples = {dataset: {r["instance_id"]: r for r in map(json.loads, path.open())}
               for dataset, path in sources.items()}
    # Whitelist task-time evidence only. No repair patch, test patch, files or test labels.
    allowed = {"repo", "instance_id", "base_commit", "problem_statement", "created_at",
               "image_assets", "version", "image_urls", "web_urls", "language", "website links"}
    inputs = []
    selected_cases = [case for case in CASES if not args.instance_id or case[1] in args.instance_id]
    if not selected_cases or set(args.instance_id) - {case[1] for case in CASES}:
        raise SystemExit("Unknown or empty case selection")
    for dataset, ident, role in selected_cases:
        raw = samples[dataset][ident]
        path = args.run_dir / f"{ident}.input.jsonl"
        text = json.dumps({k: v for k, v in raw.items() if k in allowed}, ensure_ascii=False) + "\n"
        if args.resume:
            if not path.exists() or path.read_text() != text:
                raise SystemExit("Frozen input mismatch; use a new run directory")
        else:
            path.write_text(text)
        inputs.append(dict(dataset=dataset, instance_id=ident, role=role,
                           path=str(path), sha256=digest(path)))
    manifest = dict(base_commit="fa574b260fc9fed4518a587912379acd4741d621", cases=inputs,
                    git_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    tracked_diff_sha256=hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
                    implementation_hashes={str(p.relative_to(ROOT)): digest(p) for p in
                        sorted((ROOT / "src/mycode/dynamic_retrieval").glob("*.py"))},
                    improved_strategy="joint_evidence_v3_source_ledger_no_file_rrf",
                    function_reread_limit=args.function_reread_limit,
                    source_hashes={str(p): digest(p) for p in sources.values()},
                    model=env["MODEL_API_NAME"], profile="mimo", top_k=15,
                    dynamic_rounds=args.dynamic_rounds, max_react_steps=args.max_react_steps, max_tool_rounds=1,
                    full_browser=False, vlm=True, task_timeout_seconds=args.sample_timeout_seconds,
                    warning="development cases; do not compare directly with historical Kimi scores")
    manifest_path = args.run_dir / "manifest.json"
    if args.resume:
        old = json.loads(manifest_path.read_text())
        for key in ["base_commit", "cases", "tracked_diff_sha256", "source_hashes", "model", "profile", "top_k",
                    "implementation_hashes", "improved_strategy", "function_reread_limit",
                    "dynamic_rounds", "max_react_steps", "max_tool_rounds", "task_timeout_seconds"]:
            if key == "tracked_diff_sha256" and old.get("implementation_hashes") and old["implementation_hashes"] == manifest["implementation_hashes"]:
                # Committing an identical working tree changes git diff, not
                # the frozen implementation. Preserve the old audit metadata.
                continue
            if old[key] != manifest[key]:
                raise SystemExit(f"Frozen manifest mismatch: {key}")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        snapshot = args.run_dir / "source_snapshot/dynamic_retrieval"
        snapshot.mkdir(parents=True)
        for path in (ROOT / "src/mycode/dynamic_retrieval").glob("*.py"):
            shutil.copyfile(path, snapshot / path.name)
        for name in ["fileloc_pair_smoke.py", "score_fileloc_pairs.py"]:
            shutil.copyfile(ROOT / "scripts" / name, snapshot.parent / name)
    if args.prepare_only:
        return
    # Fail before sixteen runs if the provider silently retired the configured model.
    import requests
    probe = requests.post(env["BASE_URL"].rstrip("/") + "/chat/completions",
                          headers={"Authorization": "Bearer " + env["API_KEY"]},
                          json={"model": env["MODEL_API_NAME"], "messages": [{"role": "user", "content": "Reply READY only."}],
                                "max_tokens": 128, "thinking": {"type": "disabled"}}, timeout=45)
    if not probe.ok or not probe.json().get("choices", [{}])[0].get("message", {}).get("content"):
        raise SystemExit(f"Model preflight failed: HTTP {probe.status_code}; inspect provider model support")
    preflight_name = f"resume_preflight_{time.time_ns()}.json" if args.resume else "model_preflight.json"
    (args.run_dir / preflight_name).write_text(json.dumps({"http_status":probe.status_code,
        "model":probe.json().get("model"), "usage":probe.json().get("usage")}, indent=2))
    (args.run_dir / f"dispatch_{time.time_ns()}.json").write_text(json.dumps({"workers":args.workers,"resume":args.resume}))
    def run_case(entry):
        i, item = entry
        # Alternate order to reduce time/order confounding. Separate caches, no answer reuse.
        arms = ["baseline", "improved"] if i % 2 == 0 else ["improved", "baseline"]
        for arm in arms:
            if any(digest(ROOT / path) != sha for path, sha in manifest["implementation_hashes"].items()):
                raise RuntimeError("Frozen implementation changed during run; refusing mixed-code results")
            output = args.run_dir / item["instance_id"] / arm
            if output.exists():
                results_path = output / "localization_results.jsonl"
                if args.resume and results_path.exists() and results_path.stat().st_size:
                    from score_fileloc_pairs import valid_result
                    saved = json.loads(results_path.read_text().splitlines()[-1])
                    if valid_result(saved):
                        print(f"SKIP completed {item['instance_id']} {arm}", flush=True)
                        continue
                raise RuntimeError(f"Existing incomplete arm; choose new run directory: {output}")
            output.mkdir(parents=True)
            arm_env = dict(env, MYCODE_READ_EXPLICIT_PATHS="1" if arm == "improved" else "0",
                           MYCODE_JOINT_EVIDENCE_V2="1" if arm == "improved" else "0",
                           MYCODE_FUNCTION_SOURCE_LEDGER="1" if arm == "improved" else "0",
                           MYCODE_FUNCTION_REREAD_LIMIT=str(args.function_reread_limit),
                           MYCODE_FILE_FRONT_RERANK="verified_frontier" if arm == "improved" else "head_only")
            command = [sys.executable, str(ROOT / "newtest/run_clean15_dataset.py"),
                       "--samples", item["path"], "--dataset", "swebench_multimodal-full-dev" if item["dataset"] == "SWE92" else "omnigirl-full-candidates",
                       "--output-dir", str(output), "--top-k", "15", "--dynamic-rounds", str(args.dynamic_rounds),
                       "--max-react-steps", str(args.max_react_steps), "--max-tool-rounds", "1", "--structure-only",
                       "--no-auto-fetch-repos", "--use-llm", "--use-llm-planning", "--use-llm-controller",
                       "--use-vlm", "--download-images", "--allow-network",
                       "--sample-timeout-seconds", str(args.sample_timeout_seconds), "--sample-heartbeat-interval", "30"]
            print(f"RUN {i+1}/{len(inputs)} {item['instance_id']} {arm}", flush=True)
            start = time.time()
            try:
                with (output / "console.log").open("w") as log:
                    result = subprocess.run(command, cwd=ROOT, env=arm_env, stdout=log, stderr=subprocess.STDOUT, timeout=args.sample_timeout_seconds + 60)
                status = dict(returncode=result.returncode)
            except subprocess.TimeoutExpired:
                status = dict(returncode=None, error="subprocess_timeout")
            status.update(elapsed_seconds=time.time()-start, arm=arm, instance_id=item["instance_id"])
            (output / "execution.json").write_text(json.dumps(status, indent=2))
            print(json.dumps(status), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_case, enumerate(inputs)))


if __name__ == "__main__":
    main()
