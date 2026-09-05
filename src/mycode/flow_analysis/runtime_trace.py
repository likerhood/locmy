from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


PATH_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./@-]+\.(?:py|pyi|js|jsx|ts|tsx|java|c|h|css|scss|json|yaml|yml|md|mdx))"
    r"(?:(?::|#L)(?P<line>\d+))?"
)
FRAME_RE = re.compile(r"\b(?:in|at|function|method)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b")
CONSOLE_HINT_RE = re.compile(r"\b(?:error|warning|trace|stack|console|preview|click|hover|leave|route|url)\b", re.I)


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _dedupe(values: Iterable[str], *, limit: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for key, item in value.items():
            if key in {
                "runtime_trace",
                "trace",
                "trace_summary",
                "console",
                "console_logs",
                "stack",
                "stderr",
                "stdout",
                "browser_snapshot",
                "interaction_trace",
                "parsed_reproduction",
                "source_files",
                "vlm_analysis",
            }:
                texts.extend(_flatten(item))
            elif isinstance(item, (dict, list, tuple)):
                texts.extend(_flatten(item))
        return texts
    if isinstance(value, (list, tuple)):
        texts = []
        for item in value:
            texts.extend(_flatten(item))
        return texts
    return [str(value)]


def _observation_texts(tool_observations: Iterable[dict[str, Any]] | None) -> list[str]:
    texts: list[str] = []
    for observation in tool_observations or []:
        texts.extend(_flatten(observation))
        extracted = observation.get("extracted", {}) if isinstance(observation, dict) else {}
        texts.extend(_flatten(extracted))
    return [text for text in texts if CONSOLE_HINT_RE.search(text) or PATH_RE.search(text)]


def _resolve_path(index: RepositoryIndex, raw_path: str) -> str:
    path = _norm(raw_path)
    if path in index.files:
        return path
    candidates = [candidate for candidate in index.files if candidate.endswith(path) or path.endswith(candidate)]
    if candidates:
        candidates.sort(key=len)
        return candidates[0]
    basename = Path(path).name
    candidates = [candidate for candidate in index.files if Path(candidate).name == basename]
    if candidates:
        candidates.sort(key=len)
        return candidates[0]
    return path


def _entity_for_trace(index: RepositoryIndex, path: str, line: int | None, names: list[str]) -> CodeEntity | None:
    norm_path = _norm(path)
    best: CodeEntity | None = None
    for entity in index.entities:
        if _norm(entity.path) != norm_path:
            continue
        if line and int(entity.start_line or 0) <= line <= int(entity.end_line or 0):
            if best is None or (entity.end_line - entity.start_line) < (best.end_line - best.start_line):
                best = entity
                continue
        if entity.name in names and best is None:
            best = entity
    return best


def _trace_type(text: str, issue_text: str, queries: Iterable[str] | None) -> str:
    low = "\n".join([text, issue_text or "", " ".join(queries or [])]).lower()
    if any(token in low for token in ("hover", "leave", "mousemove", "mouseout", "click", "legend")):
        return "runtime_ui_event_execution_path"
    if any(token in low for token in ("kdf", "serialize", "backend", "private_key", "openssh")):
        return "runtime_serializer_backend_execution_path"
    if any(token in low for token in ("binder", "typeinfo", "deleted", "declaration", "narrow")):
        return "runtime_type_binding_execution_path"
    if any(token in low for token in ("url", "href", "redirect", "route", "post", "site")):
        return "runtime_url_builder_execution_path"
    if any(token in low for token in ("style", "layout", "margin", "canvas", "render")):
        return "runtime_rendering_execution_path"
    return "runtime_trace_execution_path"


def verify_runtime_traces(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Map browser/console/runtime observations back to repository files.

    DAIRA-style runtime evidence is optional in the current prototype. When a
    browser, unit test, or instrumentation tool emits stack frames or console
    traces, this verifier turns them into candidate files and function support.
    """

    flows: list[dict[str, Any]] = []
    for trace_no, text in enumerate(_observation_texts(tool_observations), start=1):
        matches = list(PATH_RE.finditer(text))
        if not matches:
            continue
        function_names = _dedupe([match.group("name") for match in FRAME_RE.finditer(text)], limit=40)
        events: list[dict[str, Any]] = []
        locations: list[dict[str, Any]] = []
        candidate_paths: list[str] = []
        for idx, match in enumerate(matches, start=1):
            raw_path = match.group("path")
            line = int(match.group("line") or 0) or None
            path = _resolve_path(index, raw_path)
            candidate_paths.append(path)
            entity = _entity_for_trace(index, path, line, function_names)
            event = {
                "event_no": idx,
                "path": path,
                "raw_path": raw_path,
                "line": line,
                "functions": function_names[:8],
                "source": "runtime_or_browser_observation",
            }
            if entity:
                event["entity"] = {
                    "path": entity.path,
                    "kind": entity.kind,
                    "name": entity.name,
                    "start_line": entity.start_line,
                    "end_line": entity.end_line,
                }
            events.append(event)
            locations.append(
                {
                    "path": path,
                    "kind": entity.kind if entity else "file",
                    "name": entity.name if entity else "",
                    "role": "runtime_executed_code",
                    "line": line,
                    "trace_event_no": idx,
                    "statements": [],
                }
            )

        unique_paths = _dedupe(candidate_paths, limit=30)
        issue_tokens = set(tokenize(issue_text or ""))
        query_tokens = set(tokenize(" ".join(queries or [])))
        trace_tokens = set(tokenize(text))
        overlap = len((issue_tokens | query_tokens) & trace_tokens)
        confidence = min(0.98, 0.42 + 0.08 * len(unique_paths) + 0.02 * overlap)
        edge_summary = {
            "runtime_events": len(events),
            "unique_paths": len(unique_paths),
            "function_names": len(function_names),
            "issue_trace_token_overlap": overlap,
        }
        flows.append(
            {
                "term": ", ".join(function_names[:3]) or Path(unique_paths[0]).stem,
                "flow_type": _trace_type(text, issue_text, queries),
                "closure_kind": "daira_style_runtime_trace_verification",
                "backend": "runtime_trace_observation_parser",
                "precision_level": "observed_execution_trace_when_available",
                "confidence": round(confidence, 3),
                "candidate_target_paths": unique_paths,
                "locations": locations,
                "trace_events": events,
                "statement_edges": [
                    {
                        "source": events[i]["path"],
                        "target": events[i + 1]["path"],
                        "relation": "runtime_next_frame",
                        "symbol": ",".join(function_names[:3]),
                    }
                    for i in range(max(0, len(events) - 1))
                ][:40],
                "edge_summary": edge_summary,
                "missing_precision_backends": [
                    "instrumented_unit_test_trace",
                    "browser_interaction_trace",
                    "daira_execution_dependency_graph",
                ],
                "reason": f"Runtime/browser trace {trace_no} mentions {len(unique_paths)} repository paths.",
            }
        )
    flows.sort(
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            str(item.get("flow_type") or ""),
            str(item.get("term") or ""),
        )
    )
    return flows[:limit]
