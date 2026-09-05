#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter

from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_builder import collect_urls
from mycode.evidence.url_classifier import classify_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect URL distribution for a samples.jsonl file.")
    parser.add_argument("--samples", required=True)
    parser.add_argument("--dataset", default="unknown")
    parser.add_argument("--examples", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    type_counts = Counter()
    role_counts = Counter()
    risk_counts = Counter()
    examples: dict[str, list[tuple[str, str]]] = {}
    sample_count = 0

    for sample in load_samples(args.samples, dataset=args.dataset):
        sample_count += 1
        for raw_url in collect_urls(sample):
            evidence = classify_url(raw_url)
            type_counts[evidence.url_type] += 1
            role_counts[evidence.role] += 1
            risk_counts[evidence.leakage_risk] += 1
            examples.setdefault(evidence.url_type, [])
            if len(examples[evidence.url_type]) < args.examples:
                examples[evidence.url_type].append((sample.instance_id, raw_url))

    print(f"samples: {sample_count}")
    print("\nURL types:")
    for key, value in type_counts.most_common():
        print(f"  {key}: {value}")
    print("\nURL roles:")
    for key, value in role_counts.most_common():
        print(f"  {key}: {value}")
    print("\nLeakage risk:")
    for key, value in risk_counts.most_common():
        print(f"  {key}: {value}")
    print("\nExamples:")
    for key, rows in sorted(examples.items()):
        print(f"  {key}:")
        for instance_id, url in rows:
            print(f"    {instance_id}: {url}")


if __name__ == "__main__":
    main()

