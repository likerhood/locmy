#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_builder import build_evidence_sketches
from mycode.evidence.summary import summarize_evidence
from mycode.utils.io import write_json, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build structured evidence sketches for Clean15 samples.")
    parser.add_argument("--dataset", required=True, help="Dataset name, e.g. swe or omni.")
    parser.add_argument("--samples", required=True, help="Input samples.jsonl.")
    parser.add_argument("--output", required=True, help="Output evidence.jsonl.")
    parser.add_argument("--summary", required=True, help="Output summary.json.")
    parser.add_argument("--print-summary", action="store_true", help="Print summary JSON to stdout.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = list(load_samples(args.samples, dataset=args.dataset))
    sketches = build_evidence_sketches(samples)
    count = write_jsonl(args.output, (sketch.to_dict() for sketch in sketches))
    summary = summarize_evidence(sketches)
    summary["dataset"] = args.dataset
    summary["input"] = str(Path(args.samples))
    summary["output"] = str(Path(args.output))
    write_json(args.summary, summary)

    print(f"Built evidence sketches: {count}")
    print(f"Output: {args.output}")
    print(f"Summary: {args.summary}")
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

