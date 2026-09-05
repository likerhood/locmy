#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter

from mycode.data.dataset_loader import load_samples
from mycode.evidence.image_extractor import classify_image, extract_image_urls


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect image distribution for a samples.jsonl file.")
    parser.add_argument("--samples", required=True)
    parser.add_argument("--dataset", default="unknown")
    parser.add_argument("--examples", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    type_counts = Counter()
    source_counts = Counter()
    examples: dict[str, list[tuple[str, str]]] = {}
    sample_count = 0
    samples_with_images = 0

    for sample in load_samples(args.samples, dataset=args.dataset):
        sample_count += 1
        image_pairs = extract_image_urls(sample.raw, sample.issue_text)
        if image_pairs:
            samples_with_images += 1
        for raw_url, source in image_pairs:
            evidence = classify_image(raw_url, source, sample.issue_text)
            type_counts[evidence.image_type] += 1
            source_counts[source] += 1
            examples.setdefault(evidence.image_type, [])
            if len(examples[evidence.image_type]) < args.examples:
                examples[evidence.image_type].append((sample.instance_id, raw_url))

    print(f"samples: {sample_count}")
    print(f"samples_with_images: {samples_with_images}")
    print("\nImage types:")
    for key, value in type_counts.most_common():
        print(f"  {key}: {value}")
    print("\nImage source fields:")
    for key, value in source_counts.most_common():
        print(f"  {key}: {value}")
    print("\nExamples:")
    for key, rows in sorted(examples.items()):
        print(f"  {key}:")
        for instance_id, url in rows:
            print(f"    {instance_id}: {url}")


if __name__ == "__main__":
    main()

