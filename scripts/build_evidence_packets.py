#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_agent import build_evidence_packets
from mycode.evidence.packet_summary import summarize_packets
from mycode.utils.io import write_json, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Evidence Understanding Agent packets.")
    parser.add_argument("--dataset", required=True, help="Dataset name, e.g. swe_clean15 or omni_clean15.")
    parser.add_argument("--samples", required=True, help="Input samples.jsonl.")
    parser.add_argument("--output", required=True, help="Output evidence_packets.jsonl.")
    parser.add_argument("--summary", required=True, help="Output packet_summary.json.")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Fetch documentation/discussion pages for web snapshots. Disabled by default.",
    )
    parser.add_argument("--print-summary", action="store_true", help="Print summary JSON to stdout.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = list(load_samples(args.samples, dataset=args.dataset))
    packets = build_evidence_packets(samples, allow_network=args.allow_network)
    count = write_jsonl(args.output, (packet.to_dict() for packet in packets))
    summary = summarize_packets(packets)
    summary["dataset"] = args.dataset
    summary["input"] = str(Path(args.samples))
    summary["output"] = str(Path(args.output))
    summary["allow_network"] = args.allow_network
    write_json(args.summary, summary)

    print(f"Built evidence packets: {count}")
    print(f"Output: {args.output}")
    print(f"Summary: {args.summary}")
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

