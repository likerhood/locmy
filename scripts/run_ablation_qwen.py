"""Serial, isolated Qwen ablation launcher; never reads credentials into artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from mycode.ablation import ARMS, AblationConfig


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("swe", "omni"), required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--tag", default="v1")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--dry-run", choices=("0", "1"), default="0")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.tag):
        parser.error("tag must contain only letters, digits, _, . or -")
    model = os.environ.get("MODEL_API_NAME", "")
    if "qwen" not in model.lower():
        parser.error("The selected profile must resolve to a Qwen MODEL_API_NAME")
    for stage in ("PLANNING", "EVIDENCE", "CONTROLLER", "VLM"):
        value = os.environ.get(f"{stage}_MODEL_API_NAME", model)
        if value and "qwen" not in value.lower():
            parser.error(f"{stage}_MODEL_API_NAME is not Qwen")
    filename = ("swebench_multimodal-full-dev.clean15.samples.jsonl" if args.dataset == "swe"
                else "omnigirl-full-candidates.clean15.v458.samples.jsonl")
    candidates = [root / "data" / filename, root.parent / "clean_subsets_new" / filename,
                  root.parent.parent / "clean_subsets_new" / filename]
    samples = Path(os.environ["SAMPLES"]) if os.environ.get("SAMPLES") else next(
        (p for p in candidates if p.is_file()), candidates[0])
    if not samples.is_file():
        parser.error(f"Missing dataset: {samples}; set SAMPLES explicitly")
    ids = set()
    with samples.open() as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            instance = row.get("instance_id")
            if not instance or instance in ids:
                parser.error("Dataset has missing/duplicate instance_id")
            ids.add(instance)
    expected = 92 if args.dataset == "swe" else 458
    if len(ids) != expected:
        parser.error(f"Expected frozen {expected}-row dataset, found {len(ids)}; use MAX_SAMPLES for smoke tests")
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    output = root / "result" / "ablation_addtest32" / args.dataset / label / args.tag / args.arm
    if output.exists():
        parser.error(f"Output already exists: {output}. Use a new --tag; never mix arms or resume old evidence")
    env = os.environ.copy()
    env.update(MAGNET_ABLATION=args.arm, SAMPLES=str(samples.resolve()),
               RUN_ID=f"{args.dataset}_addtest32_{args.tag}_{args.arm}", OUTPUT_DIR=str(output),
               CACHE_DIR=str(output / "tool_cache"), RESUME="0", FORCE_RERUN="0",
               USE_LLM="1", USE_LLM_PLANNING="1", USE_LLM_CONTROLLER="1",
               DEEP_AGENT="1", LIGHTWEIGHT="0", STRUCTURE_ONLY="1", FULL_MM="1",
               MYCODE_AGENT_BOOTSTRAP_MODE="react_only")
    for key, value in {"DYNAMIC_ROUNDS": "12", "MAX_TOOL_ROUNDS": "6", "MAX_REACT_STEPS": "10",
                       "TOP_K": "15", "MYCODE_REQUIRE_SOURCE_CHECKOUT": "1"}.items():
        env.setdefault(key, value)
    if args.arm == "no_visual":
        env.update(USE_VLM="0", DOWNLOAD_IMAGES="0")
    command = ["bash", str(root / "scripts" / f"run_{args.dataset}_clean15_addtest.sh"),
               "--env-file", str(args.env_file.resolve())]
    def git(*argv):
        return subprocess.check_output(["git", "-C", str(root), *argv], text=True).strip()
    record = {"schema": 1, "ablation": AblationConfig(args.arm).to_dict(),
              "git_commit": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
              "dataset": str(samples.resolve()), "dataset_sha256": fingerprint(samples),
              "dataset_count": len(ids), "model_api_name": model,
              "provider_host": urlsplit(env.get("BASE_URL", "")).hostname,
              "endpoint_sha256": hashlib.sha256(env.get("BASE_URL", "").encode()).hexdigest(),
              "stage_models": {stage: env.get(f"{stage}_MODEL_API_NAME") or model
                               for stage in ("PLANNING", "EVIDENCE", "CONTROLLER", "VLM")},
              "llm_settings": {key: env.get(key, "<default>") for key in (
                  "MYCODE_LLM_THINKING", "MYCODE_LLM_TOKEN_FIELD", "MYCODE_LLM_MIN_COMPLETION_TOKENS",
                  "MYCODE_LLM_RETRIES", "MYCODE_LLM_RETRY_DELAYS", "MYCODE_VLM_IMAGE_TRANSPORT")},
              "python": sys.executable, "profile_path": str(args.env_file.resolve()),
              "output": str(output), "command": command,
              "selection": {key: env.get(key, "") for key in ("MAX_SAMPLES", "INSTANCE_ID")},
              "requested_runtime": {key: env[key] for key in sorted(env)
                                    if key.startswith("MYCODE_") and not any(
                                        word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))},
              "budgets": {key: env[key] for key in ("DYNAMIC_ROUNDS", "MAX_TOOL_ROUNDS", "MAX_REACT_STEPS", "TOP_K")}}
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run == "1":
        return 0
    output.mkdir(parents=True, exist_ok=False)
    (output / "ablation_manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    result = subprocess.run(command, cwd=root, env=env)
    (output / "launcher_status.json").write_text(json.dumps({"exit_code": result.returncode}) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
