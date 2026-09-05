#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.image_batch import build_image_understanding_batch


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch analyze problem_statement images with cache.")
    parser.add_argument("--samples", required=True, help="Input samples.jsonl.")
    parser.add_argument("--dataset", required=True, help="Dataset label, e.g. swe_clean15.")
    parser.add_argument("--output-cache", required=True, help="Output JSONL cache for image analyses.")
    parser.add_argument("--asset-cache-dir", required=True, help="Directory for downloaded/local image assets.")
    parser.add_argument("--summary", required=True, help="Output summary JSON.")
    parser.add_argument("--allow-network", action="store_true", help="Allow image downloads.")
    parser.add_argument("--use-vlm", action="store_true", help="Call configured VLM instead of heuristic-only analysis.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per VLM image request.")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep seconds between retries.")
    parser.add_argument("--no-reuse-cache", action="store_true", help="Ignore existing output cache.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    samples = list(load_samples(args.samples, dataset=args.dataset))
    summary = build_image_understanding_batch(
        samples,
        cache_path=args.output_cache,
        asset_cache_dir=args.asset_cache_dir,
        allow_network=args.allow_network,
        use_vlm=args.use_vlm,
        retries=args.retries,
        sleep_seconds=args.sleep,
        reuse_cache=not args.no_reuse_cache,
    )
    summary["dataset"] = args.dataset
    summary["samples"] = len(samples)
    summary["input"] = str(Path(args.samples))
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
