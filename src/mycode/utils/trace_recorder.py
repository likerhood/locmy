from __future__ import annotations

from collections import Counter
from typing import Any


TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def collect_token_usage(payload: Any) -> dict[str, int]:
    """Collect token usage from nested result/trace dictionaries.

    The evidence and localization agents may store usage in different places:
    OpenAI/LiteLLM style `usage`, local `token_usage`, or per-step summaries.
    This function intentionally over-collects only explicit token counters; it
    never estimates from text length.
    """

    totals = {key: 0 for key in TOKEN_KEYS}
    seen_usage_blocks: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            usage_like = value.get("usage")
            token_like = value.get("token_usage")
            aggregate_summaries = (
                value.get("usage_summary"),
                value.get("token_usage_summary"),
            )
            for block in (usage_like, token_like):
                if isinstance(block, dict):
                    if id(block) in seen_usage_blocks:
                        continue
                    seen_usage_blocks.add(id(block))
                    for key in TOKEN_KEYS:
                        totals[key] += _as_int(block.get(key))
            for key in TOKEN_KEYS:
                if key in value and not isinstance(value.get(key), (dict, list)):
                    totals[key] += _as_int(value.get(key))
            for child in value.values():
                if child is usage_like or child is token_like or any(child is summary for summary in aggregate_summaries):
                    continue
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return {key: value for key, value in totals.items() if value}


def _trim_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def collect_llm_events(result: dict[str, Any], *, text_limit: int = 2000) -> list[dict[str, Any]]:
    """Extract auditable LLM events from a full pipeline result.

    The pipeline stores LLM outputs in several places because evidence planning,
    evidence understanding, controller decisions, and the ReAct-style search
    agent are separate modules. This collector keeps one compact JSONL-friendly
    view so long full runs do not require manually digging through
    `localization_results.jsonl`.
    """

    events: list[dict[str, Any]] = []
    seen_event_payloads: set[int] = set()
    instance_id = str(result.get("instance_id") or "")
    repo = str(result.get("repo") or "")
    dataset = str(result.get("dataset") or "")

    def add_event(path: str, event_type: str, payload: dict[str, Any]) -> None:
        if id(payload) in seen_event_payloads:
            return
        seen_event_payloads.add(id(payload))
        raw_response = payload.get("raw_response")
        usage = payload.get("usage") or payload.get("token_usage") or {}
        response_id = payload.get("_llm_response_id")
        if isinstance(raw_response, dict):
            usage = usage or raw_response.get("usage") or {}
            response_id = response_id or raw_response.get("id")
        events.append(
            {
                "instance_id": instance_id,
                "repo": repo,
                "dataset": dataset,
                "event_no": len(events) + 1,
                "path": path,
                "event_type": event_type,
                "response_id": response_id or "",
                "content": _trim_text(
                    payload.get("content")
                    or payload.get("llm_raw")
                    or payload.get("analysis")
                    or payload.get("decision")
                    or payload,
                    text_limit,
                ),
                "usage": usage if isinstance(usage, dict) else {},
            }
        )

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            keys = set(value)
            if "llm_raw" in keys:
                add_event(path, "react_or_controller_raw", value)
            elif "raw_response" in keys:
                add_event(path, "llm_response", value)
            elif "_llm_response_id" in keys:
                add_event(path, "llm_parsed_output", value)
            elif path.endswith("llm_understanding") and value:
                add_event(path, "llm_evidence_understanding", value)
            elif path.endswith("llm_planning") and value:
                add_event(path, "llm_evidence_planning", value)
            elif path.endswith("llm_followup") and value:
                add_event(path, "llm_followup_planning", value)

            for key, child in value.items():
                if key == "raw_response":
                    continue
                visit(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(result, "")
    return events


def _top_paths(items: list[dict[str, Any]], *, limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items[:limit]:
        out.append(
            {
                "path": item.get("path", ""),
                "score": item.get("score", 0),
                "reasons": list(item.get("reasons", []) or [])[:5],
            }
        )
    return out


def _top_entities(items: list[dict[str, Any]], *, limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items[:limit]:
        out.append(
            {
                "id": item.get("id", ""),
                "path": item.get("path", ""),
                "kind": item.get("kind", ""),
                "name": item.get("name", ""),
                "score": item.get("score", 0),
                "reasons": list(item.get("reasons", []) or [])[:5],
            }
        )
    return out


def compact_agent_trace(result: dict[str, Any]) -> dict[str, Any]:
    """Create a compact, auditable trace row for one localization result."""

    localization = result.get("localization") or {}
    evidence = result.get("evidence") or {}
    packet = evidence.get("evidence_packet") or evidence.get("packet") or {}
    react = localization.get("react_agent_trace") or {}
    dynamic_rounds = localization.get("dynamic_rounds") or []
    raw_search_trace = localization.get("search_trace") or []
    setup_search_trace = [
        item for item in raw_search_trace if not str(item.get("step") or "").startswith("round_")
    ][:6]
    dynamic_search_trace = [
        item for item in raw_search_trace if str(item.get("step") or "").startswith("round_")
    ][-12:]
    flows = localization.get("flow_traces") or []
    modification_closure = localization.get("modification_closure") or {}
    flow_backends = Counter(str(flow.get("backend") or "unknown") for flow in flows)
    flow_types = Counter(str(flow.get("flow_type") or "unknown") for flow in flows)

    return {
        "instance_id": result.get("instance_id", ""),
        "repo": result.get("repo", ""),
        "dataset": result.get("dataset", ""),
        "status": result.get("status", ""),
        "elapsed_seconds": result.get("elapsed_seconds", 0),
        "problem_statement_only": result.get("problem_statement_only", False),
        "llm_controller_used": result.get("llm_controller_used", False),
        "modality": packet.get("modality", ""),
        "url_count": len(packet.get("url_inspections") or []),
        "image_count": len(packet.get("image_inspections") or []),
        "issue_sketch": localization.get("issue_sketch", {}),
        "react_agent": {
            "strategy": react.get("strategy", ""),
            "planner_used": react.get("planner_used", False),
            "summary": react.get("summary", {}),
            "steps": [
                {
                    "round_no": step.get("round_no"),
                    "thought": _trim_text(step.get("thought"), 500),
                    "tool": step.get("tool"),
                    "action": step.get("action"),
                    "tool_input": step.get("tool_input", {}),
                    "observation": step.get("observation", {}),
                    "candidate_paths": step.get("candidate_paths", [])[:15],
                    "next_queries": step.get("next_queries", [])[:20],
                    "llm_raw": _trim_text(step.get("llm_raw"), 800),
                    "token_usage": step.get("token_usage", {}),
                    "elapsed_seconds": step.get("elapsed_seconds", 0),
                }
                for step in (react.get("steps") or [])[:10]
            ],
        },
        "search_trace": [
            {
                "step": step.get("step"),
                "strategy": step.get("strategy", ""),
                "top_files": step.get("top_files", [])[:10],
                "next_queries": step.get("next_queries", [])[:10],
                "round_evaluation": step.get("round_evaluation", {}),
                "stop_decision": step.get("stop_decision", {}),
                "round_progress": ((step.get("frontier_state") or {}).get("round_progress") or {}),
            }
            for step in setup_search_trace + dynamic_search_trace
        ],
        "candidate_reviews": [
            {
                "round_no": round_item.get("round_no"),
                "status": ((round_item.get("verifier") or {}).get("llm_candidate_review") or {}).get("status"),
                "continue_search": ((round_item.get("verifier") or {}).get("llm_candidate_review") or {}).get("continue_search"),
                "missing_evidence": list(
                    ((round_item.get("verifier") or {}).get("llm_candidate_review") or {}).get("missing_evidence", []) or []
                )[:8],
                "candidates": list(
                    ((round_item.get("verifier") or {}).get("llm_candidate_review") or {}).get("candidates", []) or []
                )[:12],
                "attempts": [
                    {
                        "attempt": attempt.get("attempt"),
                        "status": attempt.get("status"),
                        "content": _trim_text(attempt.get("llm_raw") or attempt.get("content"), 1000),
                        "usage": attempt.get("usage", {}),
                    }
                    for attempt in list(
                        ((round_item.get("verifier") or {}).get("llm_candidate_review") or {}).get("attempts", []) or []
                    )[:2]
                ],
            }
            for round_item in dynamic_rounds[:5]
        ],
        "flow_summary": {
            "count": len(flows),
            "backend_counts": dict(flow_backends),
            "flow_type_counts": dict(flow_types),
            "preview": [
                {
                    "backend": flow.get("backend", ""),
                    "flow_type": flow.get("flow_type", ""),
                    "term": flow.get("term", ""),
                    "candidate_target_paths": flow.get("candidate_target_paths", [])[:8],
                    "reason": _trim_text(flow.get("reason"), 500),
                    "edge_summary": flow.get("edge_summary", {}),
                }
                for flow in flows[:12]
            ],
        },
        "modification_closure": {
            "strategy": modification_closure.get("strategy", ""),
            "status": modification_closure.get("status", ""),
            "complete": modification_closure.get("complete", False),
            "rounds_run": modification_closure.get("rounds_run", 0),
            "files": modification_closure.get("files", [])[:12],
            "candidates": modification_closure.get("candidates", [])[:12],
            "coverage": modification_closure.get("coverage", {}),
            "obligations": modification_closure.get("obligations", [])[:16],
            "trace": modification_closure.get("trace", [])[:8],
            "ranking_adjustment": modification_closure.get("ranking_adjustment", {}),
        },
        "rank_stage_snapshots": localization.get("rank_stage_snapshots", {}),
        "head_selection": localization.get("head_selection", {}),
        "adaptive_locks": localization.get("adaptive_locks", [])[:4],
        "artifact_mappings": localization.get("artifact_mappings", [])[:12],
        "checkpoint_list_quality": localization.get("checkpoint_list_quality", {}),
        "ranked": {
            "files": _top_paths(localization.get("ranked_locations") or []),
            "modules": _top_entities(localization.get("ranked_modules") or []),
            "functions": _top_entities(localization.get("ranked_functions") or []),
        },
        "evaluation_3level": result.get("evaluation_3level", {}),
        "token_usage_summary": result.get("token_usage_summary") or collect_token_usage(result),
    }
