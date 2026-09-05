from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.understanding_agent import run_evidence_understanding


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run first-step evidence understanding for one sample.")
    parser.add_argument("--samples", required=True, help="Path to samples.jsonl.")
    parser.add_argument("--dataset", default="clean15", help="Dataset label for normalized samples.")
    parser.add_argument("--instance-id", required=True, help="Instance id to analyze.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    parser.add_argument("--allow-network", action="store_true", help="Allow fetching docs/web pages.")
    parser.add_argument("--allow-browser", action="store_true", help="Allow Playwright browser execution if installed.")
    parser.add_argument(
        "--execute-tools",
        action="store_true",
        help="Execute planned evidence tools and include tool_observations.",
    )
    parser.add_argument(
        "--cache-dir",
        default="outputs/tool_cache",
        help="Cache directory for executed evidence tools.",
    )
    parser.add_argument(
        "--no-image-download",
        action="store_true",
        help="Do not download image assets during tool execution.",
    )
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM understanding.")
    parser.add_argument(
        "--no-llm-planning",
        action="store_true",
        help="Disable the first-round LLM evidence planning agent only.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    sample = None
    for item in load_samples(args.samples, dataset=args.dataset):
        if item.instance_id == args.instance_id:
            sample = item
            break
    if sample is None:
        raise SystemExit(f"Instance not found: {args.instance_id}")

    result = run_evidence_understanding(
        sample,
        allow_network=args.allow_network,
        use_llm=not args.no_llm,
        use_llm_planning=False if args.no_llm or args.no_llm_planning else True,
        execute_tools=args.execute_tools,
        cache_dir=args.cache_dir,
        allow_browser=args.allow_browser,
        download_images=not args.no_image_download,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evidence understanding: {output}")


if __name__ == "__main__":
    main()
