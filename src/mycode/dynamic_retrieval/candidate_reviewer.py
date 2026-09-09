from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
from typing import Any, Callable, Iterable


CandidateReviewLLM = Callable[..., dict[str, Any] | str]


ALLOWED_ROLES = {
    "patch_target",
    "supporting_target",
    "navigation_only",
    "reproduction_only",
    "test_or_docs",
    "unlikely",
}

_REVIEW_CACHE: dict[str, dict[str, Any]] = {}


def _invoke_llm(llm: CandidateReviewLLM, prompt: str, *, max_tokens: int) -> dict[str, Any] | str:
    try:
        parameters = inspect.signature(llm).parameters
    except (TypeError, ValueError):
        return llm(prompt)
    accepts_limit = "max_tokens" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    return llm(prompt, max_tokens=max_tokens) if accepts_limit else llm(prompt)


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
    marker = re.search(r'"(candidates|reviews)"\s*:\s*\[', cleaned)
    if marker is None:
        return {}
    collection_key = marker.group(1)
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
        if (
            isinstance(value, dict)
            and value.get("path")
            and (value.get("role") or value.get("verdict"))
        ):
            candidates.append(value)
    if not candidates:
        return {}
    continue_match = re.search(r'"continue_search"\s*:\s*(true|false)', cleaned, flags=re.IGNORECASE)
    return {
        collection_key: candidates,
        "continue_search": bool(continue_match and continue_match.group(1).lower() == "true"),
        "missing_evidence": [],
        "next_queries": [],
    }


def _parse_review_object(text: str) -> tuple[dict[str, Any], str]:
    parsed = _json_object(text)
    if parsed and (
        isinstance(parsed.get("candidates"), list)
        or isinstance(parsed.get("reviews"), list)
    ):
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
        for snippet_number, (_score, _position, snippet) in enumerate(snippet_rows, start=1):
            line_range = (snippet.get("start_line"), snippet.get("end_line"))
            if line_range in seen_ranges:
                continue
            seen_ranges.add(line_range)
            snippets.append(
                {
                    "id": f"C{rank}S{snippet_number}",
                    "start_line": snippet.get("start_line"),
                    "end_line": snippet.get("end_line"),
                    "text": str(snippet.get("text") or "")[:900],
                }
            )
            if len(snippets) >= 2:
                break
        raw_components = dict(getattr(item, "score_components", {}) or {})
        belief = dict(getattr(item, "belief", {}) or {})
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
                "path_role": str(belief.get("path_role") or "unknown"),
                "score_components": {key: round(float(value or 0.0), 2) for key, value in compact_components.items()},
                "retrieval_reasons": [str(reason)[:180] for reason in list(getattr(item, "reasons", []) or [])[:3]],
                "entities": [
                    {
                        "id": f"C{rank}E{entity_number}",
                        "kind": entity.get("kind"),
                        "name": entity.get("name"),
                        "start_line": entity.get("start_line"),
                        "end_line": entity.get("end_line"),
                    }
                    for entity_number, entity in enumerate(raw_entities, start=1)
                    if isinstance(entity, dict)
                ],
                "snippets": snippets,
                "has_code_context": bool(snippets),
                "direct_flow_evidence": direct_flow_evidence,
            }
        )
    return packet


def _flow_packet(flow_traces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    packet: list[dict[str, Any]] = []
    for flow_number, flow in enumerate(list(flow_traces)[:5], start=1):
        packet.append(
            {
                "id": f"F{flow_number}",
                "flow_type": flow.get("flow_type"),
                "term": flow.get("term"),
                "reason": str(flow.get("reason") or "")[:220],
                "candidate_target_paths": list(flow.get("candidate_target_paths", []) or [])[:8],
            }
        )
    return packet


def _prompt(
    *,
    issue_text: str,
    issue_sketch: Any,
    candidates: list[dict[str, Any]],
    flow_evidence: list[dict[str, Any]],
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
    payload = {
        "round_no": round_no,
        "issue_text": issue_text[:3500],
        "issue_sketch": sketch,
        "flow_evidence": flow_evidence,
        "candidates": candidates,
    }
    return (
        "You are the final edit-responsibility judge for repository issue localization.\n"
        "Evaluate at most the supplied candidate files. Identify the file that most directly owns the required "
        "code change, not a file that is merely related, imported, adjacent, or mentioned by a URL.\n"
        "A candidate is verified only when all five conditions hold: it is editable target-repository source; "
        "a supplied snippet directly supports the responsibility; a supplied entity belongs to that source; "
        "a supplied flow connects the candidate to the observed behavior; and the causal chain explains "
        "state or input -> operation -> incorrect effect.\n"
        "Use snippet_id, entity_id, and flow_id exactly as supplied. Never copy issue text as source evidence. "
        "Words such as likely, probably, or path similarity are not verification. Generated artifacts, tests, "
        "documentation, and external reproduction files cannot be selected unless the issue explicitly targets them.\n"
        "Select at most one new head. Do not rerank the remaining candidates. Set continue_search=true when no "
        "candidate satisfies every verification condition. Keep causal_chain and missing_evidence to one short sentence.\n"
        "Return compact JSON only with this schema:\n"
        "{\"selected_head\":null,\"reviews\":[{\"path\":\"exact candidate path\","
        "\"verdict\":\"verified|plausible|navigation|reject\",\"confidence\":0.0,"
        "\"snippet_id\":\"C1S1|null\",\"entity_id\":\"C1E1|null\",\"flow_id\":\"F1|null\","
        "\"causal_chain\":\"one concise sentence\",\"rejection_code\":\"none|no_source|no_entity|no_flow|artifact|navigation_only\"}],"
        "\"continue_search\":true,\"missing_evidence\":[\"one concise requirement\"],"
        "\"next_queries\":[\"one specific source or symbol query\"]}.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _validate(
    data: dict[str, Any],
    *,
    allowed_paths: set[str],
    grounded_paths: set[str],
    context_text_by_path: dict[str, str],
    direct_flow_paths: set[str],
    snippets_by_path: dict[str, dict[str, dict[str, Any]]],
    entities_by_path: dict[str, dict[str, dict[str, Any]]],
    flow_paths_by_id: dict[str, set[str]],
) -> dict[str, Any]:
    compact_schema = isinstance(data.get("reviews"), list)
    raw_candidates = data.get("reviews") if compact_schema else data.get("candidates")
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
        verdict = str(item.get("verdict") or "").strip().lower()
        compact_roles = {
            "verified": "patch_target",
            "plausible": "supporting_target",
            "navigation": "navigation_only",
            "reject": "unlikely",
        }
        role = compact_roles.get(verdict, str(item.get("role") or "unlikely").strip().lower())
        if role not in ALLOWED_ROLES:
            role = "unlikely"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        snippet_id = str(item.get("snippet_id") or "").strip()
        entity_id = str(item.get("entity_id") or "").strip()
        flow_id = str(item.get("flow_id") or "").strip()
        selected_snippet = snippets_by_path.get(path, {}).get(snippet_id) if compact_schema else None
        selected_entity = entities_by_path.get(path, {}).get(entity_id) if compact_schema else None
        entities = []
        if selected_entity:
            entities.append(
                {
                    "kind": str(selected_entity.get("kind") or "").strip().lower(),
                    "name": str(selected_entity.get("name") or "").strip(),
                }
            )
        else:
            for entity in item.get("entities", []) or []:
                if not isinstance(entity, dict):
                    continue
                kind = str(entity.get("kind") or "").strip().lower()
                name = str(entity.get("name") or "").strip()
                if kind in {"function", "method", "class", "module"} and name:
                    entities.append({"kind": kind, "name": name})
        evidence_quote = (
            " ".join(str((selected_snippet or {}).get("text") or "").split())[:300]
            if compact_schema
            else " ".join(str(item.get("evidence_quote") or "").split())[:300]
        )
        context_text = " ".join(context_text_by_path.get(path, "").split()).lower()
        quote_supported = bool(selected_snippet) if compact_schema else bool(
            len(evidence_quote) >= 8 and evidence_quote.lower() in context_text
        )
        entity_support_text = (
            " ".join(str((selected_snippet or {}).get("text") or "").split()).lower()
            if compact_schema
            else context_text
        )
        supported_entities = [
            entity
            for entity in entities
            if str(entity.get("name") or "").lower() in entity_support_text
        ]
        unsupported_entities = [entity for entity in entities if entity not in supported_entities]
        entity_supported = bool(supported_entities)
        direct_flow_supported = (
            bool(flow_id and path in flow_paths_by_id.get(flow_id, set()))
            if compact_schema
            else path in direct_flow_paths
        )
        patch_mechanism = " ".join(
            str(item.get("causal_chain") if compact_schema else item.get("patch_mechanism") or "").split()
        )[:500]
        mechanism_verified = bool(
            (verdict == "verified" if compact_schema else item.get("mechanism_verified", False))
            and path in grounded_paths
            and quote_supported
            and entity_supported
            and direct_flow_supported
            and patch_mechanism
        )
        candidates.append(
            {
                "path": path,
                "role": role,
                "confidence": round(confidence, 4),
                "rationale": " ".join(
                    str(item.get("causal_chain") if compact_schema else item.get("rationale") or "").split()
                )[:600],
                "evidence_quote": evidence_quote,
                "grounded": path in grounded_paths,
                "quote_supported": quote_supported,
                "entity_supported": entity_supported,
                "supported_entities": supported_entities[:8],
                "unsupported_entities": unsupported_entities[:8],
                "direct_flow_supported": direct_flow_supported,
                "mechanism_verified": mechanism_verified,
                "patch_mechanism": patch_mechanism,
                "counterevidence": _dedupe(
                    item.get("counterevidence", []) or (
                        [str(item.get("rejection_code"))]
                        if compact_schema and str(item.get("rejection_code") or "none") != "none"
                        else []
                    ),
                    limit=6,
                ),
                "matched_issue_axes": (
                    ["concern", "flow"] if compact_schema and direct_flow_supported else
                    _dedupe(item.get("matched_issue_axes", []) or [], limit=8)
                ),
                "entities": entities[:8],
                "snippet_id": snippet_id or None,
                "entity_id": entity_id or None,
                "flow_id": flow_id or None,
                "verdict": verdict or None,
            }
        )
        seen.add(path)
    if not candidates:
        return {}
    requested_head = str(data.get("selected_head") or "").replace("\\", "/").strip().lstrip("./")
    selected = next(
        (item for item in candidates if item["path"] == requested_head and item["mechanism_verified"]),
        None,
    )
    if selected is not None:
        candidates = [selected] + [item for item in candidates if item is not selected]
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
    continue_search = bool(data.get("continue_search", False))
    if not any(item.get("mechanism_verified") for item in candidates) or (compact_schema and selected is None):
        continue_search = True
    return {
        "status": "ok",
        "candidates": candidates,
        "continue_search": continue_search,
        "missing_evidence": missing_evidence,
        "critical_missing_evidence": critical_missing,
        "stop_ready": stop_ready,
        "next_queries": _dedupe(data.get("next_queries", []) or [], limit=12),
        "selected_head": selected["path"] if selected is not None else None,
        "prompt_schema": "evidence_ids_v2" if compact_schema else "legacy_v1",
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
    flow_evidence = _flow_packet(flow_traces)
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
    snippets_by_path = {
        item["path"]: {
            str(snippet.get("id") or ""): snippet
            for snippet in item.get("snippets", []) or []
            if snippet.get("id")
        }
        for item in candidates
    }
    entities_by_path = {
        item["path"]: {
            str(entity.get("id") or ""): entity
            for entity in item.get("entities", []) or []
            if entity.get("id")
        }
        for item in candidates
    }
    flow_paths_by_id = {
        str(flow.get("id") or ""): {
            str(path).replace("\\", "/").strip().lstrip("./")
            for path in flow.get("candidate_target_paths", []) or []
            if str(path).strip()
        }
        for flow in flow_evidence
        if flow.get("id")
    }
    prompt = _prompt(
        issue_text=issue_text,
        issue_sketch=issue_sketch,
        candidates=candidates,
        flow_evidence=flow_evidence,
        round_no=round_no,
    )
    cache_payload = {
        "prompt_schema": "evidence_ids_v2",
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
    review_max_tokens = max(
        600,
        int(os.environ.get("MYCODE_CANDIDATE_REVIEW_MAX_TOKENS", "1200") or 1200),
    )
    for attempt_no in range(1, max(1, repair_attempts + 1) + 1):
        try:
            response = _invoke_llm(llm, active_prompt, max_tokens=review_max_tokens)
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
            snippets_by_path=snippets_by_path,
            entities_by_path=entities_by_path,
            flow_paths_by_id=flow_paths_by_id,
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
            + "\nRequired shape: {\"selected_head\":null,\"reviews\":[{\"path\":\"...\","
            "\"verdict\":\"verified|plausible|navigation|reject\",\"confidence\":0.0,"
            "\"snippet_id\":null,\"entity_id\":null,\"flow_id\":null,"
            "\"causal_chain\":\"short\",\"rejection_code\":\"no_source\"}],"
            "\"continue_search\":true,\"missing_evidence\":[],\"next_queries\":[]}."
            " Use only evidence IDs present in the original packet.\nPrevious partial answer:\n"
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
