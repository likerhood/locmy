from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycode.agent.pipeline import run_localization_pipeline
from mycode.data.dataset_loader import load_samples


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run evidence-aware dynamic localization for one sample.")
    parser.add_argument("--samples", required=True, help="Path to samples.jsonl.")
    parser.add_argument("--dataset", default="clean15", help="Dataset label.")
    parser.add_argument("--instance-id", required=True, help="Instance id.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--allow-browser", action="store_true")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--use-llm-planning", action="store_true")
    parser.add_argument("--use-vlm", action="store_true")
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--cache-dir", default="outputs/tool_cache")
    parser.add_argument("--max-tool-rounds", type=int, default=2, help="Maximum evidence tool planning/execution rounds.")
    parser.add_argument("--dynamic-rounds", type=int, default=3, help="Maximum dynamic search/read/flow rounds.")
    parser.add_argument("--structure-only", action="store_true", help="Use benchmark repo_structures only; do not scan checked-out repos.")
    parser.add_argument("--lightweight", action="store_true", help="Use fast full-run localization without deep graph/flow rounds.")
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
    result = run_localization_pipeline(
        sample,
        allow_network=args.allow_network,
        allow_browser=args.allow_browser,
        use_llm=args.use_llm,
        use_llm_planning=args.use_llm_planning if args.use_llm else False,
        execute_tools=True,
        cache_dir=args.cache_dir,
        download_images=args.download_images,
        use_vlm=args.use_vlm,
        max_tool_rounds=args.max_tool_rounds,
        top_k=args.top_k,
        dynamic_rounds=args.dynamic_rounds,
        structure_only=args.structure_only,
        lightweight=args.lightweight,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = result["evaluation"]
    print(f"Wrote localization result: {output}")
    print(f"Top files: {[item['path'] for item in result['localization'].get('ranked_locations', [])[:5]]}")
    rounds = result["localization"].get("dynamic_rounds", [])
    print(f"Dynamic rounds: {len(rounds)}")
    for item in rounds:
        stop = item.get("stop_decision", {})
        print(
            f"  round {item.get('round_no')}: "
            f"queries={len(item.get('input_queries', []))} "
            f"next={len(item.get('next_queries', []))} "
            f"stop={stop.get('stop')} reason={stop.get('reason')}"
        )
    print(f"acc@1={metrics.get('acc@1')} acc@5={metrics.get('acc@5')} recall@15={metrics.get('recall@15')} mrr@15={metrics.get('mrr@15')}")
    three = result.get("evaluation_3level", {})
    if "file" in three:
        for level in ("file", "module", "function"):
            level_metrics = three.get(level, {})
            print(
                f"{level}: acc@1={level_metrics.get('acc@1')} "
                f"acc@5={level_metrics.get('acc@5')} "
                f"recall@15={level_metrics.get('recall@15')} "
                f"mrr@15={level_metrics.get('mrr@15')}"
            )


if __name__ == "__main__":
    main()
