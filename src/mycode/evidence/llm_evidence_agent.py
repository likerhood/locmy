from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mycode.evidence.tools.llm_client import (
    chat_completion,
    first_text,
    model_for_stage,
)
from mycode.schemas.evidence import EvidencePacket, ToolObservation


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: list[Any] = [value]
    elif isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = value.values()
    else:
        values = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            text = " ".join(str(v) for v in item.values() if v)
        else:
            text = str(item or "")
        text = " ".join(text.split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if v)
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    return str(value)


def _extract_role_summaries(evidence_roles: Any) -> list[dict[str, Any]]:
    if not isinstance(evidence_roles, dict):
        return []
    summaries: list[dict[str, Any]] = []

    def infer_role(key: str, text: str, explicit: Any = None) -> str:
        if explicit:
            return str(explicit)
        combined = f"{key} {text}".lower()
        if any(token in combined for token in ("codesandbox", "stackblitz", "playground", "runtime reproduction")):
            return "reproduction_entry"
        if any(token in combined for token in ("docs", "documentation", "api contract", "api semantic")):
            return "spec_or_api_semantics"
        if any(token in combined for token in ("image", "screenshot", "visual")):
            return "visual_evidence"
        if any(token in combined for token in ("github", "blob", "code url", "source file")):
            return "code_evidence_seed"
        if any(token in combined for token in ("issue", "discussion", "pr", "commit")):
            return "historical_discussion"
        return key

    def infer_modification_prior(role: str, text: str, explicit: Any = None) -> str:
        if explicit is not None:
            return str(explicit)
        combined = f"{role} {text}".lower()
        if "directly points to gold" in combined or "patch target" in combined:
            return "medium"
        if any(
            token in combined
            for token in (
                "reproduction",
                "reference",
                "docs",
                "documentation",
                "visual",
                "screenshot",
                "wrapper",
                "not likely patch target",
            )
        ):
            return "low"
        return "unknown"

    for key, raw in evidence_roles.items():
        if isinstance(raw, dict):
            url = raw.get("url")
            description = raw.get("description") or raw.get("summary") or raw.get("utility") or ""
            role = infer_role(str(key), _as_text(description), raw.get("role") or raw.get("type"))
            modification_prior = infer_modification_prior(role, _as_text(description), raw.get("modification_prior"))
            summaries.append(
                {
                    "id": key,
                    "url": url,
                    "role": role,
                    "description": _as_text(description),
                    "navigation_value": raw.get("navigation_value") or raw.get("utility") or "unknown",
                    "modification_prior": modification_prior,
                }
            )
        else:
            description = _as_text(raw)
            role = infer_role(str(key), description)
            summaries.append(
                {
                    "id": key,
                    "url": None,
                    "role": role,
                    "description": description,
                    "navigation_value": "unknown",
                    "modification_prior": infer_modification_prior(role, description),
                }
            )
    return summaries


def _search_plan_text(packet: EvidencePacket, key: str) -> str:
    if isinstance(packet.search_plan, dict):
        return _as_text(packet.search_plan.get(key))
    values: list[str] = []
    for item in packet.search_plan:
        if not isinstance(item, dict):
            continue
        if key == "workflow":
            values.extend(_as_list(item.get("stage")))
        elif key == "concern":
            values.extend(_as_list(item.get("action")))
        elif key == "state":
            values.extend(_as_list(item.get("source")))
        elif key == "expected_effect":
            values.extend(_as_list(item.get("likely_layers")))
    return " ".join(values[:8])


def normalize_llm_evidence_analysis(parsed: Dict[str, Any], packet: EvidencePacket) -> Dict[str, Any]:
    """Add a stable issue_sketch layer on top of model-specific JSON.

    Real OpenAI-compatible models vary in how strictly they follow the requested
    schema. Some return a paragraph for issue_understanding, others split fields
    into graph_navigation_plan/search_queries/evidence_roles. The dynamic
    localization stage should not depend on one exact phrasing.
    """

    normalized = dict(parsed)
    issue_text = _as_text(
        normalized.get("issue_understanding")
        or normalized.get("issue_summary")
        or normalized.get("summary")
        or packet.issue_summary
    )
    visual = normalized.get("visual_understanding") if isinstance(normalized.get("visual_understanding"), dict) else {}
    reproduction = (
        normalized.get("reproduction_understanding")
        if isinstance(normalized.get("reproduction_understanding"), dict)
        else {}
    )
    graph_plan = (
        normalized.get("graph_navigation_plan")
        if isinstance(normalized.get("graph_navigation_plan"), dict)
        else {}
    )

    concern_queries = _as_list(normalized.get("search_queries"))
    concern_queries.extend(packet.concern_queries)
    concern_queries.extend(_as_list(graph_plan.get("entry_points")))
    concern_queries.extend(_as_list(visual.get("layers_involved") or visual.get("likely_code_layers")))

    flow_queries = list(packet.flow_hypotheses)
    flow_queries.extend(_as_list(visual.get("implication")))
    flow_queries.extend(_as_list(graph_plan.get("traversal_strategy")))

    missing_tools = _as_list(normalized.get("missing_tools"))
    missing_tools.extend(_as_list(reproduction.get("missing_data")))

    normalized["issue_sketch"] = {
        "workflow": _as_text(
            normalized.get("workflow")
            or reproduction.get("workflow")
            or reproduction.get("next_step")
            or _search_plan_text(packet, "workflow")
        ),
        "concern": _as_text(
            normalized.get("concern")
            or issue_text
            or _search_plan_text(packet, "concern")
        ),
        "state": _as_text(normalized.get("state") or _search_plan_text(packet, "state")),
        "expected_effect": _as_text(
            normalized.get("expected_effect")
            or visual.get("expected_actual_difference")
            or _search_plan_text(packet, "expected_effect")
        ),
        "entities": _as_list(normalized.get("entities")) or packet.symbol_queries,
        "evidence_roles": _extract_role_summaries(normalized.get("evidence_roles")),
        "concern_queries": list(dict.fromkeys(concern_queries))[:30],
        "flow_queries": list(dict.fromkeys(flow_queries))[:30],
        "missing_tools": list(dict.fromkeys(missing_tools))[:30],
        "caution": _as_text(normalized.get("caution") or packet.warnings),
    }
    normalized["schema_version"] = "llm_evidence_understanding.v2"
    return normalized


def _compact_packet(
    packet: EvidencePacket,
    tool_observations: Optional[List[ToolObservation]] = None,
) -> Dict[str, Any]:
    data = {
        "instance_id": packet.instance_id,
        "repo": packet.repo,
        "dataset": packet.dataset,
        "issue_summary": packet.issue_summary,
        "metadata": {
            "language": packet.metadata.get("language"),
            "problem_statement_only": packet.metadata.get("problem_statement_only"),
            "url_count": packet.metadata.get("url_count"),
            "image_count": packet.metadata.get("image_count"),
        },
        "url_inspections": packet.url_inspections,
        "reproduction_cases": packet.reproduction_cases,
        "image_inspections": packet.image_inspections,
        "symbol_queries": packet.symbol_queries,
        "concern_queries": packet.concern_queries,
        "flow_hypotheses": packet.flow_hypotheses,
        "search_plan": packet.search_plan,
        "leakage": packet.leakage,
    }
    if tool_observations is not None:
        data["tool_observations"] = [
            {
                "tool": item.tool,
                "source": item.source,
                "success": item.success,
                "status": item.status,
                "extracted": item.extracted,
                "warnings": item.warnings,
                "errors": item.errors,
            }
            for item in tool_observations
        ]
    return data


def analyze_evidence_with_llm(
    packet: EvidencePacket,
    *,
    tool_observations: Optional[List[ToolObservation]] = None,
    max_tokens: int = 1400,
) -> Dict[str, Any]:
    prompt_packet = _compact_packet(packet, tool_observations=tool_observations)
    messages = [
        {
            "role": "system",
            "content": (
                "You are the Evidence Understanding Agent that runs before issue localization. "
                "Do not guess gold files directly. Determine the roles of URLs, reproduction evidence, "
                "and images, then produce actionable directions for the downstream code-search agent. "
                "Never use gold patches, gold files, or any answer information. If test metadata is "
                "present, use it only to identify the sample. Return one compact JSON object only, with "
                "no Markdown, preamble, prompt restatement, or hidden chain-of-thought. Write generated "
                "descriptions and search queries in English; preserve quoted UI text, paths, URLs, and "
                "program identifiers verbatim."
            ),
        },
        {
            "role": "user",
            "content": (
                "Analyze the structured evidence packet below. Concisely determine: "
                "(1) whether each URL is a reproduction entry point, documentation/API reference, "
                "code-evidence seed, or weak context; (2) what was extracted from playground or "
                "reproduction URLs and which tools are still missing; (3) what completed tool "
                "observations actually extracted and where they failed; (4) which UI or visual symptom "
                "each image demonstrates; and (5) which concern, call/event, and data/state flows should "
                "guide downstream search. Return these JSON fields: issue_understanding, evidence_roles, "
                "reproduction_understanding, visual_understanding, tool_execution_summary, "
                "search_queries, graph_navigation_plan, missing_tools, caution.\n\n"
                + json.dumps(prompt_packet, ensure_ascii=False, indent=2)
            ),
        },
    ]
    response = chat_completion(messages, model=model_for_stage("evidence"), max_tokens=max_tokens)
    text = first_text(response)
    parsed: Dict[str, Any]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw_text": text}
    parsed = normalize_llm_evidence_analysis(parsed, packet)
    parsed["_llm_response_id"] = response.get("id")
    parsed["_model"] = response.get("model")
    parsed["_usage"] = response.get("usage")
    return parsed
