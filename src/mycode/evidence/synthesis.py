from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List

from mycode.schemas.evidence import EvidenceCollectionPlan, EvidencePacket, ToolObservation


def _dedupe(values: Iterable[Any], limit: int = 80) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _short(text: str, limit: int = 180) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _queries_from_observation(observation: ToolObservation) -> Dict[str, List[str]]:
    extracted = observation.extracted or {}
    groups: Dict[str, List[str]] = {
        "symbol": [],
        "concern": [],
        "reproduction": [],
        "visual": [],
        "url_seed": [],
        "docs": [],
        "flow": [],
    }

    if observation.tool == "github_url_parser":
        if extracted.get("path"):
            groups["url_seed"].append(str(extracted["path"]))
        if extracted.get("local_path"):
            groups["local_code"].append(str(extracted["local_path"]))
        if extracted.get("symbol_hint"):
            groups["symbol"].append(str(extracted["symbol_hint"]))
        groups["symbol"].extend(extracted.get("semantic_terms", []) or [])
        if extracted.get("repo"):
            groups["url_seed"].append(str(extracted["repo"]))
        if extracted.get("line"):
            groups["url_seed"].append(f"line {extracted['line']}")

    elif observation.tool in {"playground_decoder", "browser_reproduction_reader"}:
        parsed = extracted.get("parsed_reproduction", extracted)
        groups["reproduction"].extend(parsed.get("semantic_queries", []) or [])
        groups["reproduction"].extend(parsed.get("route_terms", []) or [])
        groups["flow"].extend(parsed.get("likely_layers", []) or [])
        features = parsed.get("code_features", {}) or {}
        groups["symbol"].extend(features.get("symbols", []) or [])
        groups["symbol"].extend(features.get("event_terms", []) or [])
        groups["concern"].extend(features.get("imports", []) or [])
        extracted_features = extracted.get("code_features", {}) or {}
        groups["symbol"].extend(extracted_features.get("symbols", []) or [])
        groups["symbol"].extend(extracted_features.get("event_terms", []) or [])
        groups["concern"].extend(extracted_features.get("imports", []) or [])
        groups["concern"].extend(extracted_features.get("dependency_terms", []) or [])
        groups["reproduction"].extend(extracted.get("semantic_queries", []) or [])
        for dependency in extracted.get("package_dependencies", []) or []:
            if isinstance(dependency, dict):
                groups["concern"].append(str(dependency.get("name") or ""))
        browser_plan = parsed.get("browser_observation_plan", {}) or {}
        if browser_plan.get("requested_file"):
            groups["reproduction"].append(str(browser_plan["requested_file"]))
        for source_file in extracted.get("source_files", []) or []:
            groups["reproduction"].append(str(source_file.get("path") or ""))
            groups["symbol"].append(str(source_file.get("code_preview") or "")[:500])

    elif observation.tool in {"web_doc_reader", "discussion_reader", "web_snapshot_fetcher"}:
        groups["docs"].extend(extracted.get("semantic_queries", []) or [])
        groups["docs"].extend(extracted.get("keywords", []) or [])
        groups["docs"].extend(extracted.get("headings", []) or [])
        groups["symbol"].extend(extracted.get("code_blocks", [])[:4] or [])

    elif observation.tool == "vlm_image_inspector":
        groups["visual"].extend(extracted.get("visual_queries", []) or [])
        groups["flow"].extend(extracted.get("likely_layers", []) or [])
        vlm = extracted.get("vlm_analysis", {}) or {}
        groups["visual"].extend(vlm.get("search_queries", []) or [])
        groups["visual"].extend(vlm.get("visual_entities", []) or [])
        groups["flow"].extend(vlm.get("likely_code_layers", []) or [])
        groups["concern"].append(str(vlm.get("symptom") or ""))

    return {key: _dedupe(value, limit=30) for key, value in groups.items()}


def synthesize_evidence(
    *,
    packet: EvidencePacket,
    plan: EvidenceCollectionPlan,
    observations: List[ToolObservation],
) -> Dict[str, Any]:
    url_roles = Counter(item.get("role", "unknown") for item in packet.url_inspections)
    url_tools = Counter(item.get("tool_recommendation", "unknown") for item in packet.url_inspections)
    image_types = Counter(item.get("image_type", "unknown") for item in packet.image_inspections)
    tool_status = Counter(f"{obs.tool}:{obs.status}" for obs in observations)

    query_groups: Dict[str, List[str]] = {
        "symbol": list(packet.symbol_queries),
        "local_code": [],
        "concern": list(packet.concern_queries),
        "reproduction": [],
        "visual": [],
        "url_seed": [],
        "docs": [],
        "flow": list(packet.flow_hypotheses),
    }
    query_groups["local_code"].extend(plan.metadata.get("local_code_hints", []) or [])
    for case in packet.reproduction_cases:
        query_groups["reproduction"].extend(case.get("semantic_queries", []) or [])
        query_groups["flow"].extend(case.get("likely_layers", []) or [])
        query_groups["reproduction"].extend(case.get("route_terms", []) or [])
    for image in packet.image_inspections:
        query_groups["visual"].extend(image.get("visual_queries", []) or [])
        query_groups["flow"].extend(image.get("likely_layers", []) or [])
    for url_item in packet.url_inspections:
        if url_item.get("role") == "code_evidence_seed":
            query_groups["url_seed"].append(str(url_item.get("path") or ""))
        if url_item.get("role") == "spec_or_api_semantics":
            query_groups["docs"].extend(url_item.get("semantic_terms", []) or [])
        query_groups["concern"].extend(url_item.get("semantic_terms", []) or [])

    observation_summaries: List[Dict[str, Any]] = []
    for obs in observations:
        obs_queries = _queries_from_observation(obs)
        for key, values in obs_queries.items():
            query_groups[key].extend(values)
        observation_summaries.append(
            {
                "tool": obs.tool,
                "source": obs.source,
                "success": obs.success,
                "status": obs.status,
                "role_hypothesis": obs.metadata.get("role_hypothesis"),
                "priority": obs.metadata.get("priority"),
                "queries": {key: value[:8] for key, value in obs_queries.items() if value},
                "warnings": obs.warnings[:5],
                "errors": obs.errors[:3],
                "provenance": (obs.extracted or {}).get("provenance"),
                "local_resolution_status": (obs.extracted or {}).get("local_resolution_status"),
                "browser_used": bool((obs.extracted or {}).get("browser_used")),
                "network_used": bool((obs.extracted or {}).get("network_used")),
            }
        )

    query_groups = {key: _dedupe(values, limit=60) for key, values in query_groups.items()}
    all_queries = _dedupe(
        query
        for values in query_groups.values()
        for query in values
    )

    navigation_hints: List[Dict[str, Any]] = []
    if query_groups["url_seed"]:
        navigation_hints.append(
            {
                "kind": "code_url_seed",
                "action": "inspect seed symbol, then expand references/importers/callers before ranking targets",
                "queries": query_groups["url_seed"][:10],
                "caution": "code URL may be evidence-only, not patch target",
            }
        )
    if query_groups["local_code"]:
        navigation_hints.append(
            {
                "kind": "local_code_navigation",
                "action": "resolve repository path or symbol locally, read it, then follow imports, calls, delegates, and implementations",
                "queries": query_groups["local_code"][:12],
                "caution": "local code references must not be sent to browser or web tools",
            }
        )
    if query_groups["reproduction"]:
        navigation_hints.append(
            {
                "kind": "reproduction_to_program",
                "action": "map reproduction code/config/route to repository API, component, plugin, parser, or behavior layer",
                "queries": query_groups["reproduction"][:12],
            }
        )
    if query_groups["visual"]:
        navigation_hints.append(
            {
                "kind": "visual_symptom_to_layer",
                "action": "map visible symptom to route/component/render/layout/style/chart/plugin layer",
                "queries": query_groups["visual"][:12],
            }
        )
    if query_groups["flow"]:
        navigation_hints.append(
            {
                "kind": "flow_follow_up",
                "action": "trace state, parameter, route, event, render, or type flow from symptom layer to implementation layer",
                "queries": query_groups["flow"][:12],
            }
        )

    missing_tools = []
    for obs in observations:
        extracted_missing = (obs.extracted or {}).get("missing_tools", []) or []
        missing_tools.extend(extracted_missing)
    for warning in packet.warnings + plan.warnings:
        if "VLM" in warning or "Images need" in warning:
            missing_tools.append("vlm_image_semantic_reader")
        if "browser" in warning.lower():
            missing_tools.append("browser_reproduction_reader")

    return {
        "issue_summary": packet.issue_summary,
        "modality": packet.modality,
        "problem_statement_only": packet.metadata.get("problem_statement_only") is True,
        "url_role_counts": dict(sorted(url_roles.items())),
        "url_tool_counts": dict(sorted(url_tools.items())),
        "image_type_counts": dict(sorted(image_types.items())),
        "tool_status_counts": dict(sorted(tool_status.items())),
        "query_groups": query_groups,
        "all_queries": all_queries[:100],
        "navigation_hints": navigation_hints,
        "observation_summaries": observation_summaries,
        "leakage_policy": {
            "leakage_urls_detected": len(packet.leakage),
            "action": "exclude PR/commit/diff/files URLs from localization prompts",
            "leakage_sources": [
                {"url": item.get("url"), "kind": item.get("github_kind"), "risk": item.get("risk")}
                for item in packet.leakage
            ],
        },
        "missing_tools": _dedupe(missing_tools, limit=30),
        "warnings": _dedupe(packet.warnings + plan.warnings, limit=30),
        "agent_brief": _short(
            " | ".join(
                [
                    f"modality={packet.modality}",
                    f"url_roles={dict(url_roles)}",
                    f"image_types={dict(image_types)}",
                    f"top_queries={all_queries[:8]}",
                ]
            ),
            limit=600,
        ),
    }
