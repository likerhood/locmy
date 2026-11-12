#!/usr/bin/env python3
"""Normalize one replacement sample's upstream localization without gold data."""

import argparse
import hashlib
import json
from pathlib import Path


SPECS = {
    "locagent": ("found_files", "found_functions"),
    "cosil": ("found_files", "found_functions"),
    "gala": ("final_files", "final_functions"),
    "graphlocator": ("found_files", "found_functions"),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_source(path, instance_id):
    if path.suffix == ".jsonl":
        matches = [json.loads(line) for line in path.read_text().splitlines()
                   if line.strip() and json.loads(line).get("instance_id") == instance_id]
        if len(matches) != 1:
            raise ValueError(f"Expected one {instance_id} row in {path}, found {len(matches)}")
        return matches[0]
    value = json.loads(path.read_text())
    if isinstance(value, dict) and instance_id in value:
        return value[instance_id]
    if isinstance(value, list):
        matches = [row for row in value if row.get("instance_id") == instance_id]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f"Could not find {instance_id} in {path}")


def normalize_files(values):
    if not isinstance(values, list):
        raise ValueError("Predicted files must be a list")
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("path") or value.get("file") or ""
        path = str(value).strip().removeprefix("./")
        if not path or path.startswith("/") or ".." in path.split("/") or "\\" in path:
            continue
        if path not in result:
            result.append(path)
    return result


def normalize_functions(values, files):
    if not isinstance(values, list):
        return []
    allowed = set(files[:15])
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("name") or ""
        value = str(value).strip().removeprefix("./")
        if "::" not in value or value.split("::", 1)[0] not in allowed:
            continue
        if value not in result:
            result.append(value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=sorted(SPECS), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instance-id", default="processing__p5.js-3769")
    args = parser.parse_args()

    raw = load_source(args.source, args.instance_id)
    file_field, function_field = SPECS[args.method]
    files = normalize_files(raw.get(file_field, []))
    functions = normalize_functions(raw.get(function_field, []), files)
    row = {
        "instance_id": args.instance_id,
        "found_files": files,
        "found_functions": functions,
        "status": "available" if files else "empty_prediction",
        "provenance": {
            "method": args.method,
            "source": str(args.source.resolve()),
            "source_sha256": digest(args.source),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
