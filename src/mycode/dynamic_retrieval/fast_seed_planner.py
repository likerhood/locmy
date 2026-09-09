from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from mycode.repo_index.structure_index import RepositoryIndex


LLMController = Callable[[str], str | dict[str, Any]]
NOISE_PARTS = {"docs", "doc", "test", "tests", "examples", "example", "fixtures", "fixture"}


def _source_candidate(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lower()
    parts = set(Path(normalized).parts)
    filename = Path(normalized).name
    if parts & (NOISE_PARTS | {"demo", "demos", "dist", "build", "vendor", "generated"}):
        return False
    if filename.endswith((".min.js", ".bundle.js", ".umd.js", ".map")):
        return False
    if normalized.startswith("lib/") and filename.endswith((".esm.js", ".esm.mjs", ".esm.cjs")):
        return False
    return True


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = str(value or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path and path not in seen:
            seen.add(path)
            out.append(path)
            if len(out) >= limit:
                break
    return out


def _local_code_anchors(evidence_result: dict[str, Any], index: RepositoryIndex) -> list[str]:
    anchors: list[str] = []
    for observation in evidence_result.get("tool_observations", []) or []:
        if observation.get("tool") != "github_url_parser":
            continue
        extracted = observation.get("extracted", {}) or {}
        if extracted.get("local_resolution_status") != "resolved":
            continue
        path = str(extracted.get("local_path") or extracted.get("path") or "")
        if path in index.files:
            anchors.append(path)
    return _dedupe(anchors, limit=12)


def _parse_selected_paths(
    raw: str | dict[str, Any],
    allowed: set[str],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if isinstance(raw, dict):
        raw = raw.get("content", raw)
    if isinstance(raw, dict):
        payload = raw
    else:
        text = str(raw or "").strip()
        match = re.search(r"\{[\s\S]*\}", text)
        try:
            payload = json.loads(match.group(0) if match else text)
        except (json.JSONDecodeError, AttributeError):
            return [], {}
    entries = payload.get("seed_files", []) if isinstance(payload, dict) else []
    paths: list[str] = []
    metadata: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict):
            path = str(entry.get("path") or "")
            detail = {
                "role": str(entry.get("role") or "responsibility_candidate"),
                "evidence_channels": [str(value) for value in entry.get("evidence_channels", []) or []][:6],
                "expected_mechanism": str(entry.get("expected_mechanism") or "")[:300],
                "verification_query": str(entry.get("verification_query") or "")[:240],
            }
        else:
            path = str(entry or "")
            detail = {}
        if path in allowed:
            paths.append(path)
            metadata[path] = detail
    return _dedupe(paths, limit=len(allowed)), metadata


def plan_fast_seeds(
    *,
    index: RepositoryIndex,
    issue_text: str,
    query_groups: dict[str, list[str]],
    evidence_result: dict[str, Any],
    controller_llm: LLMController | None,
) -> dict[str, Any]:
    """Create a small GALA-style global entry set before iterative navigation.

    This planner is additive: it never replaces MAGNET's concern/call/flow
    search. It supplies a stable entry set and records the channels supporting
    each path so later rounds can preserve corroborated candidates.
    """

    if os.environ.get("MYCODE_FAST_SEED_PLANNER", "0").strip().lower() in {"0", "false", "off", "no"}:
        return {
            "enabled": False,
            "strategy": "disabled",
            "candidate_files": [],
            "responsibility_candidates": [],
            "seed_files": [],
            "persistent_seed_files": [],
            "local_code_anchors": [],
            "llm_status": "disabled",
            "llm_selected_files": [],
            "llm_promoted_files": [],
            "llm_selections": {},
            "evidence": {},
        }

    candidate_limit = _env_int("MYCODE_FAST_SEED_CANDIDATES", 10)
    shortlist_limit = min(candidate_limit, _env_int("MYCODE_FAST_SEED_SHORTLIST", 6))
    seed_limit = min(shortlist_limit, _env_int("MYCODE_FAST_SEED_LIMIT", 3))
    pool_limit = max(candidate_limit * 4, 24)
    scores: dict[str, float] = defaultdict(float)
    channels: dict[str, set[str]] = defaultdict(set)
    reasons: dict[str, list[str]] = defaultdict(list)
    local_anchors = _local_code_anchors(evidence_result, index)

    for rank, path in enumerate(local_anchors, start=1):
        scores[path] += 8.0 / rank
        channels[path].add("local_code_url")
        reasons[path].append("resolved_base_commit_code_url")

    weights = {
        "symbol": 1.8,
        "local_code": 2.0,
        "concern": 1.2,
        "visual": 0.7,
        "reproduction": 0.8,
        "flow": 1.1,
        "effect": 1.1,
        "path": 1.7,
    }
    for group, queries in query_groups.items():
        if group == "all" or not queries:
            continue
        weight = weights.get(group, 1.0)
        for rank, hit in enumerate(index.search_files(queries, limit=pool_limit), start=1):
            scores[hit.path] += weight / (12.0 + rank)
            channels[hit.path].add(group)
            reasons[hit.path].extend(hit.reasons[:2])

    issue_normalized = str(issue_text or "").replace("\\", "/").lower()
    for path in list(scores):
        parts = {part.lower() for part in Path(path).parts}
        if path.lower() in issue_normalized:
            scores[path] += 4.0
            channels[path].add("exact_issue_path")
            reasons[path].append("exact_issue_path")
        if parts & NOISE_PARTS:
            scores[path] *= 0.30
            reasons[path].append("noise_path_penalty")

    ordered = sorted(scores, key=lambda path: (-scores[path], path))
    candidate_files = _dedupe(local_anchors + ordered, limit=candidate_limit)
    selected: list[str] = []
    selection_metadata: dict[str, dict[str, Any]] = {}
    llm_status = "disabled"
    use_llm = os.environ.get("MYCODE_FAST_SEED_LLM", "1").strip().lower() not in {"0", "false", "off", "no"}
    if controller_llm is not None and use_llm:
        summaries: list[str] = []
        entities_by_path: dict[str, list[str]] = defaultdict(list)
        for entity in index.entities:
            if entity.path in candidate_files and len(entities_by_path[entity.path]) < 8:
                entities_by_path[entity.path].append(entity.name)
        for path in candidate_files:
            summaries.append(
                f"- {path} | channels={','.join(sorted(channels[path])) or 'global'} "
                f"| symbols={','.join(entities_by_path[path]) or 'none'}"
            )
        prompt = (
            "You are the global repository entry-point selector. Select responsibility entry points from "
            "Candidate Files using only exact paths and the supplied channel and symbol facts. This stage "
            "does not verify a patch mechanism. Do not claim that a file contains behavior that has not been read.\n"
            "Prefer editable implementation files supported by independent path, symbol, workflow, or local-code "
            "evidence. Treat images and reproduction URLs as navigation evidence only. Generated artifacts, "
            "bundles, demos, tests, and docs are navigation-only unless the issue explicitly targets them.\n"
            "Rank files by edit responsibility, not lexical similarity. Return compact JSON only.\n\n"
            f"Issue:\n{issue_text[:5000]}\n\nCandidate Files:\n" + "\n".join(summaries) +
            "\n\nChoose responsibility candidates that should be source-verified next. "
            f"Return at most {shortlist_limit} entries using this schema: "
            '{"seed_files":[{"path":"exact/path","role":"implementation|supporting|navigation",'
            '"evidence_channels":["symbol","path","visual","workflow"],'
            '"verification_query":"specific symbol or behavior to verify"}]}.'
        )
        try:
            selected, selection_metadata = _parse_selected_paths(
                controller_llm(prompt), set(candidate_files)
            )
            for path, metadata in selection_metadata.items():
                metadata["evidence_channels"] = [
                    channel
                    for channel in metadata.get("evidence_channels", [])
                    if channel in channels[path]
                ]
                if metadata.get("role") not in {"implementation", "supporting", "navigation"}:
                    metadata["role"] = "navigation"
            selected = selected[:shortlist_limit]
            llm_status = "ok" if selected else "invalid_or_empty"
        except Exception as exc:  # noqa: BLE001 - deterministic order is the fallback.
            llm_status = f"error:{type(exc).__name__}"
    best_score = max((scores[path] for path in candidate_files), default=0.0)
    selected_supported = [
        path
        for path in selected
        if (
            _source_candidate(path)
            and (
                len(channels[path]) >= 2
                or "local_code_url" in channels[path]
                or "exact_issue_path" in channels[path]
                or scores[path] >= best_score * 0.75
            )
        )
    ]
    source_candidates = [path for path in candidate_files if _source_candidate(path)]
    seed_files = _dedupe(
        [path for path in local_anchors if _source_candidate(path)]
        + selected_supported
        + source_candidates,
        limit=seed_limit,
    )
    persistent = [
        path
        for path in candidate_files
        if len(channels[path]) >= 2 or "local_code_url" in channels[path] or "exact_issue_path" in channels[path]
    ]
    return {
        "enabled": True,
        "strategy": "global_10_recall_then_6_responsibility_then_3_active_seeds",
        "candidate_files": candidate_files,
        "responsibility_candidates": selected,
        "seed_files": seed_files,
        "persistent_seed_files": _dedupe(seed_files + persistent, limit=candidate_limit),
        "local_code_anchors": local_anchors,
        "llm_status": llm_status,
        "llm_selected_files": selected,
        "llm_promoted_files": selected_supported,
        "llm_selections": selection_metadata,
        "evidence": {
            path: {
                "channels": sorted(channels[path]),
                "score": round(scores[path], 4),
                "reasons": _dedupe(reasons[path], limit=6),
            }
            for path in candidate_files
        },
    }
