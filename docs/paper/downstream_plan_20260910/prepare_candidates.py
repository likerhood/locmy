"""Offline planning only: audit frozen inputs and nominate paired 50-case subsets.

No LLM calls, no harness execution, no use of localization/repair outcomes.
Candidates are NOT an environment-validated or previously unseen test set.
"""
import ast
import collections
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SEED = "magnet-downstream-20260910-v1"


def parse_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        result = ast.literal_eval(value)
    if not isinstance(result, list):
        raise ValueError("Expected list")
    return result


def main():
    audits = {}
    for name, filename in [
        ("swe", "swebench_multimodal-full-dev.clean15.samples.jsonl"),
        ("omni", "omnigirl-full-candidates.clean15.v458.samples.jsonl"),
    ]:
        source = ROOT / "data" / filename
        rows = [json.loads(line) for line in source.read_text().splitlines() if line]
        assert len({row['instance_id'] for row in rows}) == len(rows)
        groups = collections.defaultdict(list)
        for row in rows:
            groups[row['repo']].append(row)
        quotas = {repo: 50 * len(group) // len(rows) for repo, group in groups.items()}
        remainder_order = sorted(groups, key=lambda repo: (-(50 * len(groups[repo]) % len(rows)), repo))
        for repo in remainder_order[:50 - sum(quotas.values())]:
            quotas[repo] += 1
        candidates = []
        for repo in sorted(groups):
            ordered = sorted(groups[repo], key=lambda r: hashlib.sha256(
                f"{SEED}|{name}|{r['instance_id']}".encode()).hexdigest())
            for rank, row in enumerate(ordered, 1):
                candidates.append({
                    "dataset": name, "instance_id": row['instance_id'], "repo": repo,
                    "base_commit": row['base_commit'], "version": row.get('version', ''),
                    "language": row.get('language', 'JavaScript/TypeScript'),
                    "has_image_url": bool(row.get('image_urls')),
                    "repo_order": rank, "repo_quota": quotas[repo],
                    "provisional_selected": rank <= quotas[repo],
                    "environment_status": "not_run", "gold_control_status": "not_run",
                })
        selected = [row for row in candidates if row['provisional_selected']]
        assert len(selected) == 50
        for suffix, data in [("candidate_order", candidates), ("provisional50", selected)]:
            with (OUT / f"{name}_{suffix}.csv").open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(candidates[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(data)
        audits[name] = {
            "source": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "n": len(rows), "repo_counts": dict(collections.Counter(r['repo'] for r in rows)),
            "language_counts": dict(collections.Counter(r.get('language', 'JavaScript/TypeScript') for r in rows)),
            "nonempty_test_patch": sum(bool(r.get('test_patch')) for r in rows),
            "empty_fail_to_pass": sum(not parse_list(r['FAIL_TO_PASS']) for r in rows),
            "empty_pass_to_pass": sum(not parse_list(r['PASS_TO_PASS']) for r in rows),
            "image_url_instances": sum(bool(r.get('image_urls')) for r in rows),
            "issue_marker_counts": dict(collections.Counter(str(r['problem_statement'].count('[Original Issue]')) for r in rows)),
            "compact_marker_counts": dict(collections.Counter(str(r['problem_statement'].count('[Multimodal Context - Compact]')) for r in rows)),
            "repo_quotas": quotas,
            "selected_languages": dict(collections.Counter(r['language'] for r in selected)),
            "selected_image_url_instances": sum(r['has_image_url'] for r in selected),
            "selection": "repository-proportional Hamilton allocation; seeded SHA256 ordering within repo",
            "seed": SEED, "status": "provisional_not_harness_validated",
        }
    (OUT / 'dataset_audit.json').write_text(json.dumps(audits, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: {'n': v['n'], 'quotas': v['repo_quotas'], 'selected_languages': v['selected_languages'], 'selected_images': v['selected_image_url_instances']} for k,v in audits.items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
