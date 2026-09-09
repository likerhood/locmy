from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable, Iterable


CandidateReviewLLM = Callable[[str], dict[str, Any] | str]


ALLOWED_ROLES = {
    "patch_target",
    "supporting_target",
    "navigation_only",
    "reproduction_only",
    "test_or_docs",
    "unlikely",
}

_REVIEW_CACHE: dict[str, dict[str, Any]] = {}


def _dedupe(values: Iterable[str], *, limit: int = 40) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _response_text(value: dict[str, Any] | str) -> str:
    if isinstance(value, str):
        return value.strip()
    content = value.get("content") or value.get("text")
    if isinstance(content, str) and content.strip():
        return content.strip()
    raw = value.get("raw_response")
    if isinstance(raw, dict):
        choices = raw.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            for key in ("content", "reasoning", "reasoning_content"):
                text = message.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def _json_object(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        return {}
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(fenced)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(fenced):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(fenced[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _partial_review_object(text: str) -> dict[str, Any]:
    """Recover complete candidate objects from a truncated JSON response.

    Candidate review responses commonly hit the controller output limit after
    emitting one or more valid candidate objects but before closing the outer
    array/object.  Throwing those grounded decisions away makes the expensive
    review call pure overhead.  Decode only complete top-level candidate
    objects and leave incomplete fields at conservative defaults.
    """

    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        str(text or "").strip(),
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    marker = re.search(r'"candidates"\s*:\s*\[', cleaned)
    if marker is None:
        return {}
    decoder = json.JSONDecoder()
    tail = cleaned[marker.end() :]
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(tail):
        start = tail.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(tail[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = start + end
        if isinstance(value, dict) and value.get("path") and value.get("role"):
            candidates.append(value)
    if not candidates:
        return {}
    continue_match = re.search(r'"continue_search"\s*:\s*(true|false)', cleaned, flags=re.IGNORECASE)
    return {
        "candidates": candidates,
        "continue_search": bool(continue_match and continue_match.group(1).lower() == "true"),
        "missing_evidence": [],
        "next_queries": [],
    }


def _parse_review_object(text: str) -> tuple[dict[str, Any], str]:
    parsed = _json_object(text)
    if parsed and isinstance(parsed.get("candidates"), list):
        return parsed, "complete_json"
    recovered = _partial_review_object(text)
    if recovered:
        return recovered, "partial_candidate_recovery"
    return {}, "invalid"


def _candidate_packet(
    ranked: Iterable[Any],
    code_contexts: Iterable[dict[str, Any]],
    *,
    issue_text: str,
    limit: int,
) -> list[dict[str, Any]]:
    contexts = {str(item.get("path") or ""): item for item in code_contexts}
    issue_terms = {
        token.lower()
        for token in re.split(r"[^A-Za-z0-9_$]+", issue_text)
        if len(token) >= 4
    }
    packet: list[dict[str, Any]] = []
    for rank, item in enumerate(list(ranked)[:limit], start=1):
        path = str(getattr(item, "path", "") or "")
        if not path:
            continue
        context = contexts.get(path, {})
        raw_entities = list(context.get("entities", []) or getattr(item, "entities", []) or [])[:8]
        entity_terms = {
            str(entity.get("name") or "").lower()
            for entity in raw_entities
            if isinstance(entity, dict) and len(str(entity.get("name") or "")) >= 3
        }
        snippets = []
        seen_ranges: set[tuple[Any, Any]] = set()
        snippet_rows = []
        for original_position, snippet in enumerate(context.get("snippets", []) or []):
            snippet_text = str(snippet.get("text") or "")
            lowered = snippet_text.lower()
            overlap = sum(1 for term in issue_terms if term in lowered)
            entity_overlap = sum(1 for term in entity_terms if term in lowered)
            snippet_rows.append((overlap * 2 + entity_overlap * 3, -original_position, snippet))
        snippet_rows.sort(key=lambda row: (-row[0], -row[1]))
        for _score, _position, snippet in snippet_rows:
            line_range = (snippet.get("start_line"), snippet.get("end_line"))
            if line_range in seen_ranges:
                continue
            seen_ranges.add(line_range)
            snippets.append(
                {
                    "start_line": snippet.get("start_line"),
                    "end_line": snippet.get("end_line"),
                    "text": str(snippet.get("text") or "")[:600],
                }
            )
            if len(snippets) >= 2:
                break
        raw_components = dict(getattr(item, "score_components", {}) or {})
        compact_components = dict(
            sorted(raw_components.items(), key=lambda pair: -abs(float(pair[1] or 0.0)))[:5]
        )
        direct_flow_evidence = any(
            float(raw_components.get(name, 0.0) or 0.0) > 0
            for name in ("call_score", "flow_score", "flow_verifier")
        )
        packet.append(
            {
                "rank": rank,
                "path": path,
                "base_score": round(float(getattr(item, "score", 0.0) or 0.0), 3),
                "score_components": {key: round(float(value or 0.0), 2) for key, value in compact_components.items()},
                "retrieval_reasons": [str(reason)[:180] for reason in list(getattr(item, "reasons", []) or [])[:3]],
                "entities": [
                    {
                        "kind": entity.get("kind"),
                        "name": entity.get("name"),
                        "start_line": entity.get("start_line"),
                        "end_line": entity.get("end_line"),
                    }
                    for entity in raw_entities
                    if isinstance(entity, dict)
                ],
                "snippets": snippets,
                "has_code_context": bool(snippets),
                "direct_flow_evidence": direct_flow_evidence,
            }
        )
    return packet


def _prompt(
    *,
    issue_text: str,
    issue_sketch: Any,
    candidates: list[dict[str, Any]],
    flow_traces: Iterable[dict[str, Any]],
    round_no: int,
) -> str:
    raw_sketch = issue_sketch.to_dict() if hasattr(issue_sketch, "to_dict") else dict(issue_sketch or {})
    sketch = {
        "task_type": str(raw_sketch.get("task_type") or "unknown"),
        "workflow": [str(value)[:180] for value in (raw_sketch.get("workflow") or [])[:6]],
        "concerns": [str(value)[:220] for value in (raw_sketch.get("concerns") or [])[:10]],
        "states": [str(value)[:140] for value in (raw_sketch.get("states") or [])[:10]],
        "expected_effects": [str(value)[:180] for value in (raw_sketch.get("expected_effects") or [])[:8]],
        "entities": [str(value)[:120] for value in (raw_sketch.get("entities") or [])[:12]],
        "architectural_queries": [str(value)[:180] for value in (raw_sketch.get("architectural_queries") or [])[:10]],
        "flow_obligations": [
            {
                "flow_type": item.get("flow_type"),
                "state": str(item.get("state") or "")[:120],
                "behavior": str(item.get("behavior") or "")[:140],
            }
            for item in (raw_sketch.get("flow_obligations") or [])[:6]
            if isinstance(item, dict)
        ],
        "implementation_hypotheses": [
            {
                "role": item.get("role"),
                "value": str(item.get("value") or "")[:180],
                "search_policy": str(item.get("search_policy") or "")[:180],
            }
            for item in (raw_sketch.get("hypotheses") or [])[:6]
            if isinstance(item, dict) and item.get("role")
        ],
    }
    flows = []
    for flow in list(flow_traces)[:5]:
        flows.append(
            {
                "flow_type": flow.get("flow_type"),
                "term": flow.get("term"),
                "reason": str(flow.get("reason") or "")[:260],
                "candidate_target_paths": list(flow.get("candidate_target_paths", []) or [])[:8],
            }
        )
    payload = {
        "round_no": round_no,
        "issue_text": issue_text[:3500],
        "issue_sketch": sketch,
        "flow_evidence": flows[:4],
        "candidates": candidates,
    }
    return (
        "You are the candidate-review stage of a repository issue localization agent.\n"
        "Judge likely EDIT targets, not files that merely repeat issue words. A URL, demo, test, docs page, "
        "selector, or public API can be useful navigation evidence while the actual patch belongs to a caller, "
        "consumer, implementation, serializer, component, reducer, or handler.\n"
        "Use the code snippets/entities and require an explicit connection from issue concern/state to expected effect. "
        "A candidate without a supplied code snippet cannot be a high-confidence patch target.\n"
        "For a feature request, the future call path may not exist. Prefer an existing analogous capability or framework convention over a file that only matches the requested screen path. "
        "Treat VLM-proposed selectors, HTML tags, conditions, and function names as unverified hypotheses unless the supplied source contains them. "
        "Actively report counterevidence when a candidate lacks the required state, action, handler, or behavior.\n"
        "Keep every string concise. Return JSON only with this schema:\n"
        "{\"candidates\":[{\"path\":\"exact candidate path\",\"role\":\"patch_target|supporting_target|"
        "navigation_only|reproduction_only|test_or_docs|unlikely\",\"confidence\":0.0,"
        "\"rationale\":\"short evidence-based reason\",\"evidence_quote\":\"short exact excerpt from the supplied snippet\","
        "\"mechanism_verified\":true,\"patch_mechanism\":\"how this code transforms the faulty state into the observed effect\"," 
        "\"counterevidence\":[\"required behavior absent from supplied source\"],"
        "\"matched_issue_axes\":[\"concern\",\"call\",\"flow\"],"
        "\"entities\":[{\"kind\":\"function|method|class|module\",\"name\":\"exact entity name\"}]}],"
        "\"continue_search\":false,\"missing_evidence\":[\"...\"],\"next_queries\":[\"...\"]}.\n"
        "Review at most the supplied candidates. Put the strongest patch target first. Do not invent files or entities.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _validate(
    data: dict[str, Any],
    *,
    allowed_paths: set[str],
    grounded_paths: set[str],
    context_text_by_path: dict[str, str],
    direct_flow_paths: set[str],
) -> dict[str, Any]:
    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        return {}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip().lstrip("./")
        if path not in allowed_paths or path in seen:
            continue
        role = str(item.get("role") or "unlikely").strip().lower()
        if role not in ALLOWED_ROLES:
            role = "unlikely"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        entities = []
        for entity in item.get("entities", []) or []:
            if not isinstance(entity, dict):
                continue
            kind = str(entity.get("kind") or "").strip().lower()
            name = str(entity.get("name") or "").strip()
            if kind in {"function", "method", "class", "module"} and name:
                entities.append({"kind": kind, "name": name})
        evidence_quote = " ".join(str(item.get("evidence_quote") or "").split())[:300]
        context_text = " ".join(context_text_by_path.get(path, "").split()).lower()
        quote_supported = bool(
            len(evidence_quote) >= 8
            and evidence_quote.lower() in context_text
        )
        supported_entities = [
            entity
            for entity in entities
            if str(entity.get("name") or "").lower() in context_text
        ]
        unsupported_entities = [entity for entity in entities if entity not in supported_entities]
        entity_supported = bool(supported_entities)
        direct_flow_supported = path in direct_flow_paths
        mechanism_verified = bool(
            item.get("mechanism_verified", False)
            and path in grounded_paths
            and quote_supported
            and entity_supported
            and direct_flow_supported
            and str(item.get("patch_mechanism") or "").strip()
        )
        candidates.append(
            {
                "path": path,
                "role": role,
                "confidence": round(confidence, 4),
                "rationale": " ".join(str(item.get("rationale") or "").split())[:600],
                "evidence_quote": evidence_quote,
                "grounded": path in grounded_paths,
                "quote_supported": quote_supported,
                "entity_supported": entity_supported,
                "supported_entities": supported_entities[:8],
                "unsupported_entities": unsupported_entities[:8],
                "direct_flow_supported": direct_flow_supported,
                "mechanism_verified": mechanism_verified,
                "patch_mechanism": " ".join(str(item.get("patch_mechanism") or "").split())[:500],
                "counterevidence": _dedupe(item.get("counterevidence", []) or [], limit=6),
                "matched_issue_axes": _dedupe(item.get("matched_issue_axes", []) or [], limit=8),
                "entities": entities[:8],
            }
        )
        seen.add(path)
    if not candidates:
        return {}
    missing_evidence = _dedupe(data.get("missing_evidence", []) or [], limit=10)
    critical_markers = (
        "actual source",
        "implementation",
        "caller",
        "consumer",
        "call flow",
        "data flow",
        "def-use",
        "runtime behavior",
    )
    critical_missing = [
        item for item in missing_evidence if any(marker in item.lower() for marker in critical_markers)
    ]
    best_patch = next(
        (item for item in candidates if item["role"] in {"patch_target", "supporting_target"}),
        None,
    )
    matched_axes = set((best_patch or {}).get("matched_issue_axes", []) or [])
    stop_ready = bool(
        best_patch
        and bool(best_patch.get("grounded"))
        and bool(best_patch.get("mechanism_verified"))
        and float(best_patch.get("confidence") or 0.0) >= 0.85
        and (best_patch.get("entities") or [])
        and "concern" in matched_axes
        and matched_axes.intersection({"call", "flow", "program", "behavior"})
        and not critical_missing
    )
    return {
        "status": "ok",
        "candidates": candidates,
        "continue_search": bool(data.get("continue_search", False)),
        "missing_evidence": missing_evidence,
        "critical_missing_evidence": critical_missing,
        "stop_ready": stop_ready,
        "next_queries": _dedupe(data.get("next_queries", []) or [], limit=12),
    }


def review_candidates(
    *,
    llm: CandidateReviewLLM | None,
    issue_text: str,
    issue_sketch: Any,
    ranked: Iterable[Any],
    code_contexts: Iterable[dict[str, Any]],
    flow_traces: Iterable[dict[str, Any]],
    round_no: int,
    candidate_limit: int = 12,
    repair_attempts: int = 1,
) -> dict[str, Any]:
    flow_traces = list(flow_traces)
    candidates = _candidate_packet(
        ranked,
        code_contexts,
        issue_text=issue_text,
        limit=max(3, candidate_limit),
    )
    if llm is None:
        return {"status": "disabled", "reason": "no_llm", "candidates": []}
    if not candidates:
        return {"status": "skipped", "reason": "no_candidates", "candidates": []}
    allowed_paths = {item["path"] for item in candidates}
    grounded_paths = {item["path"] for item in candidates if item.get("has_code_context")}
    direct_flow_paths = {item["path"] for item in candidates if item.get("direct_flow_evidence")}
    for flow in flow_traces:
        direct_flow_paths.update(
            str(path).replace("\\", "/").strip().lstrip("./")
            for path in flow.get("candidate_target_paths", []) or []
            if str(path).strip()
        )
    context_text_by_path = {
        item["path"]: "\n".join(str(snippet.get("text") or "") for snippet in item.get("snippets", []) or [])
        for item in candidates
    }
    prompt = _prompt(
        issue_text=issue_text,
        issue_sketch=issue_sketch,
        candidates=candidates,
        flow_traces=flow_traces,
        round_no=round_no,
    )
    cache_payload = {
        "llm_identity": id(llm),
        "issue_text": issue_text,
        "candidates": [
            {
                "path": item.get("path"),
                "entities": item.get("entities", []),
                "snippets": item.get("snippets", []),
                "direct_flow_evidence": item.get("direct_flow_evidence", False),
            }
            for item in candidates
        ],
        "flows": [
            {
                "flow_type": flow.get("flow_type"),
                "term": flow.get("term"),
                "candidate_target_paths": list(flow.get("candidate_target_paths", []) or [])[:8],
            }
            for flow in list(flow_traces)[:5]
        ],
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    ).hexdigest()
    cached = _REVIEW_CACHE.get(cache_key)
    if cached is not None:
        result = copy.deepcopy(cached)
        result["cache_hit"] = True
        return result
    attempts: list[dict[str, Any]] = []
    active_prompt = prompt
    for attempt_no in range(1, max(1, repair_attempts + 1) + 1):
        try:
            response = llm(active_prompt)
        except Exception as exc:
            attempts.append({"attempt": attempt_no, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            break
        raw_text = _response_text(response)
        parsed_object, parse_mode = _parse_review_object(raw_text)
        parsed = _validate(
            parsed_object,
            allowed_paths=allowed_paths,
            grounded_paths=grounded_paths,
            context_text_by_path=context_text_by_path,
            direct_flow_paths=direct_flow_paths,
        )
        usage = response.get("usage") if isinstance(response, dict) else {}
        attempt = {
            "attempt": attempt_no,
            "status": "ok" if parsed else "invalid_json_or_schema",
            "parse_mode": parse_mode,
            "llm_raw": raw_text[:6000],
            "usage": usage if isinstance(usage, dict) else {},
        }
        attempts.append(attempt)
        if parsed:
            parsed["parse_mode"] = parse_mode
            parsed["recovered_from_truncated_json"] = parse_mode == "partial_candidate_recovery"
            parsed["attempts"] = attempts
            parsed["candidate_count"] = len(candidates)
            parsed["cache_hit"] = False
            if len(_REVIEW_CACHE) >= 128:
                _REVIEW_CACHE.pop(next(iter(_REVIEW_CACHE)))
            _REVIEW_CACHE[cache_key] = copy.deepcopy(parsed)
            return parsed
        active_prompt = (
            "Repair the syntax of the previous candidate review. Return compact JSON only. "
            "Use only these exact paths: "
            + json.dumps(sorted(allowed_paths), ensure_ascii=False)
            + "\nRequired shape: {\"candidates\":[{\"path\":\"...\",\"role\":\"patch_target|"
            "supporting_target|navigation_only|reproduction_only|test_or_docs|unlikely\","
            "\"confidence\":0.0,\"rationale\":\"short\",\"evidence_quote\":\"exact supplied excerpt\","
            "\"mechanism_verified\":false,\"patch_mechanism\":\"short\",\"counterevidence\":[],"
            "\"matched_issue_axes\":[],\"entities\":[]}],\"continue_search\":false,"
            "\"missing_evidence\":[],\"next_queries\":[]}.\nPrevious partial answer:\n"
            + raw_text[:2000]
        )
    return {
        "status": "fallback",
        "reason": "llm_review_unavailable_or_invalid",
        "candidates": [],
        "candidate_count": len(candidates),
        "attempts": attempts,
        "continue_search": True,
        "missing_evidence": [
            "Candidate review did not return valid structured output; verify the actual source implementation and flow."
        ],
        "critical_missing_evidence": [
            "Actual source implementation and call or data flow remain unverified."
        ],
        "stop_ready": False,
        "next_queries": [],
    }
