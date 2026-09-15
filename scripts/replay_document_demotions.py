"""Offline diagnostic only: stable document demotion on frozen file candidates."""
import argparse
import json

from mycode.dynamic_retrieval.package_navigation import navigation_only_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--traces", required=True)
    args = parser.parse_args()
    issues = {}
    with open(args.samples) as stream:
        for line in stream:
            sample = json.loads(line)
            issues[sample["instance_id"]] = sample["problem_statement"]
    outcomes = {}
    with open(args.traces) as stream:
        for line in stream:
            record = json.loads(line)
            identifier = record["instance_id"]
            paths = [item["path"] for item in record.get("ranked", {}).get("files", [])]
            # Selection uses issue text and paths only; gold is used below for evaluation.
            blocked = {path for path in paths
                       if path.lower().endswith((".md", ".mdx", ".rst", ".adoc"))
                       and navigation_only_file(path, issues[identifier])}
            ordered = [path for path in paths if path not in blocked] + [path for path in paths if path in blocked]
            evaluation = record.get("evaluation_3level", {})
            if paths and "gold" not in evaluation:
                raise ValueError(f"Missing gold for nonempty prediction: {identifier}")
            gold = set(evaluation.get("gold", {}).get("files", []))
            outcomes[identifier] = [(int(bool(gold.intersection(paths[:k]))),
                                     int(bool(gold.intersection(ordered[:k])))) for k in (1, 3, 8, 15)]
    report = {"diagnostic_only": True, "samples": len(outcomes), "metrics": {}}
    for position, k in enumerate((1, 3, 8, 15)):
        pairs = [values[position] for values in outcomes.values()]
        report["metrics"][str(k)] = {
            "before_hits": sum(a for a, b in pairs), "after_hits": sum(b for a, b in pairs),
            "wins": sum(b > a for a, b in pairs), "losses": sum(b < a for a, b in pairs),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
