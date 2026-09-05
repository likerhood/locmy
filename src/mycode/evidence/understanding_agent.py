from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from urllib.parse import urlparse

from mycode.evidence.agents.planning_agent import plan_evidence_collection
from mycode.evidence.evidence_agent import build_evidence_packet
from mycode.evidence.llm_evidence_agent import analyze_evidence_with_llm
from mycode.evidence.runtime.tool_executor import execute_collection_plan
from mycode.evidence.synthesis import synthesize_evidence
from mycode.evidence.tools.llm_client import (
    LLMClientError,
    chat_completion,
    first_text,
    model_for_stage,
)
from mycode.schemas.evidence import (
    EvidenceCollectionPlan,
    EvidencePacket,
    NormalizedSample,
    ToolObservation,
    ToolRequest,
)

ALLOWED_FOLLOWUP_TOOLS = {
    "github_url_parser",
    "playground_decoder",
    "browser_reproduction_reader",
    "web_doc_reader",
    "discussion_reader",
    "web_snapshot_fetcher",
    "vlm_image_inspector",
}

REMOTE_URL_TOOLS = {
    "github_url_parser",
    "playground_decoder",
    "browser_reproduction_reader",
    "web_doc_reader",
    "discussion_reader",
    "web_snapshot_fetcher",
}
SOURCE_PATH_RE = re.compile(
    r"(?:^|\s)(?:/?(?:[A-Za-z0-9_.-]+/))*[A-Za-z0-9_$.-]+\.(?:pyi?|jsx?|tsx?|java|go|rs|c|cc|cpp|h|hpp)(?::\d+)?(?:$|\s)",
    re.IGNORECASE,
)
QUALIFIED_SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){2,}$")


def _is_remote_url(source: str) -> bool:
    parsed = urlparse(source.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _local_code_hint(item: Dict[str, Any]) -> str:
    source = str(item.get("source") or item.get("url") or "").strip()
    if not source or _is_remote_url(source):
        return ""
    path_like = "/" in source and not any(char.isspace() for char in source)
    if path_like or SOURCE_PATH_RE.search(source) or QUALIFIED_SYMBOL_RE.fullmatch(source):
        return source
    return ""


def _pending_tool_plan(plan: EvidenceCollectionPlan) -> List[Dict[str, Any]]:
    pending: List[Dict[str, Any]] = []
    for request in plan.tool_requests:
        item = request.to_dict()
        if not request.should_execute:
            item["status"] = "skipped_by_planning_agent"
        else:
            item["status"] = "pending_execution"
        pending.append(item)
    return pending


def _deterministic_understanding(packet: EvidencePacket) -> Dict[str, Any]:
    return {
        "issue_summary": packet.issue_summary,
        "evidence_roles": {
            "urls": [
                {
                    "url": item.get("url"),
                    "role": item.get("role"),
                    "kind": item.get("kind"),
                    "localization_use": item.get("localization_use"),
                }
                for item in packet.url_inspections
            ],
            "images": [
                {
                    "url": item.get("url"),
                    "image_type": item.get("image_type"),
                    "visual_queries": item.get("visual_queries", []),
                    "likely_layers": item.get("likely_layers", []),
                }
                for item in packet.image_inspections
            ],
        },
        "reproduction_understanding": [
            {
                "url": case.get("url"),
                "platform": case.get("platform"),
                "config": case.get("config", {}),
                "semantic_queries": case.get("semantic_queries", []),
                "likely_layers": case.get("likely_layers", []),
                "needs_browser": case.get("needs_browser"),
            }
            for case in packet.reproduction_cases
        ],
        "search_queries": {
            "symbols": packet.symbol_queries,
            "concerns": packet.concern_queries,
            "flows": packet.flow_hypotheses,
        },
        "search_plan": packet.search_plan,
        "caution": packet.warnings,
    }


def _request_key(request: ToolRequest) -> tuple[str, str]:
    return (request.tool, request.source)


def _make_plan_with_requests(
    plan: EvidenceCollectionPlan,
    requests: List[ToolRequest],
    *,
    round_id: int,
) -> EvidenceCollectionPlan:
    return EvidenceCollectionPlan(
        instance_id=plan.instance_id,
        repo=plan.repo,
        dataset=plan.dataset,
        issue_summary=plan.issue_summary,
        suspected_problem_types=list(plan.suspected_problem_types),
        tool_requests=requests,
        initial_search_intent=list(plan.initial_search_intent),
        warnings=list(plan.warnings),
        llm_planning=plan.llm_planning,
        metadata={**plan.metadata, "tool_round": round_id},
    )


def _deterministic_followup_requests(
    plan: EvidenceCollectionPlan,
    observations: List[ToolObservation],
    existing: set[tuple[str, str]],
) -> List[ToolRequest]:
    """Add obvious second-round requests after cheap parsers reveal missing work."""

    followups: List[ToolRequest] = []
    for observation in observations:
        extracted = observation.extracted or {}
        parsed = extracted.get("parsed_reproduction", extracted)
        browser_plan = parsed.get("browser_observation_plan", {}) or {}
        if (
            observation.tool == "playground_decoder"
            and browser_plan.get("requires_browser")
            and ("browser_reproduction_reader", observation.source) not in existing
        ):
            followups.append(
                ToolRequest(
                    tool="browser_reproduction_reader",
                    source=observation.source,
                    evidence_type="reproduction_url",
                    role_hypothesis="runtime_reproduction_evidence",
                    priority="high",
                    reason="second_round_browser_read_after_playground_decoder_identified_live_reproduction",
                    expected_outputs=[
                        "file_tree",
                        "requested_source_file",
                        "package_json_dependencies",
                        "preview_screenshot",
                        "console_log",
                    ],
                    parameters={
                        "requested_file": browser_plan.get("requested_file"),
                        "sandbox_id": browser_plan.get("sandbox_id"),
                    },
                    caution=["reproduction_code_is_evidence_not_patch_target"],
                )
            )

        if (
            observation.tool == "web_snapshot_fetcher"
            and extracted.get("doc_kind") == "api_or_docs"
            and ("web_doc_reader", observation.source) not in existing
        ):
            followups.append(
                ToolRequest(
                    tool="web_doc_reader",
                    source=observation.source,
                    evidence_type="docs_url",
                    role_hypothesis="spec_or_api_semantics",
                    priority="medium",
                    reason="second_round_docs_reader_after_snapshot_identified_api_or_docs_page",
                    expected_outputs=["api_names", "parameters", "behavior_constraints", "code_blocks"],
                    parameters={"source_status": extracted.get("status")},
                )
            )

        if (
            observation.tool == "browser_reproduction_reader"
            and not observation.success
            and ("web_snapshot_fetcher", observation.source) not in existing
        ):
            followups.append(
                ToolRequest(
                    tool="web_snapshot_fetcher",
                    source=observation.source,
                    evidence_type="fallback_web_snapshot",
                    role_hypothesis="fallback_after_browser_reproduction_failed",
                    priority="medium",
                    reason="second_round_fallback_snapshot_after_browser_reader_failed",
                    expected_outputs=["page_title", "visible_text", "links", "code_or_config_hints"],
                    parameters={"failed_tool": observation.tool, "status": observation.status},
                    caution=["snapshot_is_weaker_than_live_browser_reproduction"],
                )
            )

        if (
            observation.tool == "vlm_image_inspector"
            and not observation.success
            and ("vlm_image_inspector", observation.source) not in existing
        ):
            # Keep the failed image in the trace; the agent should explicitly know it
            # could not observe this modality instead of silently dropping it.
            followups.append(
                ToolRequest(
                    tool="vlm_image_inspector",
                    source=observation.source,
                    evidence_type="image_url",
                    role_hypothesis="retry_or_heuristic_image_observation",
                    priority="low",
                    reason="second_round_retry_failed_image_observation",
                    expected_outputs=["image_type", "visible_text", "visual_symptom", "likely_code_layers"],
                    parameters={"previous_status": observation.status},
                    caution=["do_not_block_localization_on_unavailable_image"],
                )
            )
    return followups


def _tool_request_from_dict(item: Dict[str, Any]) -> ToolRequest | None:
    tool = str(item.get("tool") or "").strip()
    source = str(item.get("source") or item.get("url") or "").strip()
    if tool not in ALLOWED_FOLLOWUP_TOOLS or not source:
        return None
    if tool in REMOTE_URL_TOOLS and not _is_remote_url(source):
        return None
    expected = item.get("expected_outputs") or []
    if not isinstance(expected, list):
        expected = [str(expected)]
    parameters = item.get("parameters") or {}
    if not isinstance(parameters, dict):
        parameters = {}
    caution = item.get("caution") or item.get("cautions") or []
    if not isinstance(caution, list):
        caution = [str(caution)]
    return ToolRequest(
        tool=tool,
        source=source,
        evidence_type=str(item.get("evidence_type") or "followup_evidence"),
        role_hypothesis=str(item.get("role_hypothesis") or item.get("role") or "followup"),
        priority=str(item.get("priority") or "medium"),
        reason=str(item.get("reason") or "llm_requested_followup_tool"),
        expected_outputs=[str(value) for value in expected],
        parameters=parameters,
        should_execute=bool(item.get("should_execute", True)),
        caution=[str(value) for value in caution],
    )


def _llm_followup_requests(
    *,
    plan: EvidenceCollectionPlan,
    observations: List[ToolObservation],
    existing: set[tuple[str, str]],
    max_tokens: int = 900,
) -> Dict[str, Any]:
    compact = {
        "instance_id": plan.instance_id,
        "repo": plan.repo,
        "dataset": plan.dataset,
        "problem_statement_only": plan.metadata.get("problem_statement_only") is True,
        "issue_summary": plan.issue_summary,
        "initial_tool_requests": [request.to_dict() for request in plan.tool_requests],
        "tool_observations": [
            {
                "tool": observation.tool,
                "source": observation.source,
                "success": observation.success,
                "status": observation.status,
                "extracted": observation.extracted,
                "warnings": observation.warnings,
                "errors": observation.errors,
            }
            for observation in observations
        ],
        "allowed_followup_tools": sorted(ALLOWED_FOLLOWUP_TOOLS),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a multi-round tool-planning agent for multimodal issue evidence. "
                "Choose follow-up evidence collection using only problem_statement and actual tool "
                "results; never use gold information. Sources passed to browser, web, GitHub, or "
                "playground tools must be valid HTTP(S) URLs. Repository paths and Java, Python, or "
                "JavaScript class names and symbols are not URLs and must not be sent to those tools. "
                "If the existing observations are sufficient, return an empty additional_tool_requests "
                "array. Return one compact JSON object with exactly these top-level fields: "
                "additional_tool_requests, reasoning, stop_reason. Do not restate the prompt or expose "
                "chain-of-thought. Write generated text in English and preserve paths, URLs, and "
                "identifiers verbatim."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(compact, ensure_ascii=False, indent=2),
        },
    ]
    response = chat_completion(messages, model=model_for_stage("evidence"), max_tokens=max_tokens)
    text = first_text(response)
    try:
        parsed: Dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw_text": text, "additional_tool_requests": []}
    additions: List[ToolRequest] = []
    local_code_hints: List[str] = []
    rejected_requests: List[Dict[str, Any]] = []
    for raw in parsed.get("additional_tool_requests", []) or []:
        if not isinstance(raw, dict):
            continue
        request = _tool_request_from_dict(raw)
        if request and _request_key(request) not in existing:
            existing.add(_request_key(request))
            additions.append(request)
            continue
        hint = _local_code_hint(raw)
        if hint:
            local_code_hints.append(hint)
            rejected_requests.append(
                {
                    "tool": str(raw.get("tool") or ""),
                    "source": hint,
                    "reason": "local_code_reference_rerouted_to_repository_search",
                }
            )
    if local_code_hints:
        stored = plan.metadata.setdefault("local_code_hints", [])
        for hint in local_code_hints:
            if hint not in stored:
                stored.append(hint)
    parsed["_llm_response_id"] = response.get("id")
    parsed["_model"] = response.get("model")
    parsed["_usage"] = response.get("usage")
    parsed["_parsed_tool_requests"] = [request.to_dict() for request in additions]
    parsed["_local_code_hints"] = local_code_hints
    parsed["_rejected_tool_requests"] = rejected_requests
    return parsed


def _round_stop_decision(
    *,
    round_id: int,
    max_rounds: int,
    round_observations: List[ToolObservation],
    followups: List[ToolRequest],
    llm_followup: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if round_id >= max_rounds:
        return {
            "should_stop": True,
            "reason": "max_tool_rounds_reached",
            "confidence": 0.6,
        }
    if followups:
        return {
            "should_stop": False,
            "reason": "new_followup_tools_available",
            "confidence": 0.8,
        }
    if llm_followup and str(llm_followup.get("stop_reason") or "").strip():
        return {
            "should_stop": True,
            "reason": str(llm_followup.get("stop_reason")),
            "confidence": 0.75,
        }
    if any(obs.success for obs in round_observations):
        return {
            "should_stop": True,
            "reason": "successful_observations_and_no_new_followups",
            "confidence": 0.7,
        }
    return {
        "should_stop": True,
        "reason": "no_successful_observations_and_no_fallback_available",
        "confidence": 0.45,
    }


def _execute_tool_rounds(
    plan: EvidenceCollectionPlan,
    *,
    cache_dir: str,
    allow_network: bool,
    allow_browser: bool,
    download_images: bool,
    use_vlm: bool,
    use_llm: bool,
    max_rounds: int,
) -> tuple[List[ToolObservation], List[Dict[str, Any]]]:
    observations: List[ToolObservation] = []
    rounds: List[Dict[str, Any]] = []
    existing = {_request_key(request) for request in plan.tool_requests}
    pending = list(plan.tool_requests)

    for round_id in range(1, max(1, max_rounds) + 1):
        if not pending:
            rounds.append({"round": round_id, "status": "no_pending_requests"})
            break
        round_plan = _make_plan_with_requests(plan, pending, round_id=round_id)
        round_observations = execute_collection_plan(
            round_plan,
            cache_dir=cache_dir,
            allow_network=allow_network,
            allow_browser=allow_browser,
            download_images=download_images,
            use_vlm=use_vlm,
        )
        observations.extend(round_observations)
        round_trace: Dict[str, Any] = {
            "round": round_id,
            "request_count": len(pending),
            "requests": [item.to_dict() for item in pending],
            "observations": [item.to_dict() for item in round_observations],
            "reasoning_trace": [
                {
                    "tool": item.tool,
                    "source": item.source,
                    "success": item.success,
                    "status": item.status,
                    "interpretation": (
                        "usable_evidence_observed"
                        if item.success
                        else "tool_failed_or_partial_evidence_observed"
                    ),
                }
                for item in round_observations
            ],
        }
        rounds.append(round_trace)

        if round_id >= max_rounds:
            round_trace["stop_decision"] = _round_stop_decision(
                round_id=round_id,
                max_rounds=max_rounds,
                round_observations=round_observations,
                followups=[],
                llm_followup=None,
            )
            break

        followups = _deterministic_followup_requests(plan, round_observations, existing)
        for request in followups:
            existing.add(_request_key(request))

        llm_followup: Dict[str, Any] | None = None
        if use_llm:
            try:
                llm_followup = _llm_followup_requests(
                    plan=plan,
                    observations=round_observations,
                    existing=existing,
                )
                parsed_followups = [
                    _tool_request_from_dict(item)
                    for item in llm_followup.get("_parsed_tool_requests", []) or []
                    if isinstance(item, dict)
                ]
                followups.extend(item for item in parsed_followups if item is not None)
            except LLMClientError as exc:
                llm_followup = {"error": str(exc), "fallback": "deterministic_followups_only"}
        rounds[-1]["followup_planning"] = {
            "deterministic_requests": [
                request.to_dict()
                for request in followups
                if request.reason.startswith("second_round_")
            ],
            "llm_followup": llm_followup,
            "next_request_count": len(followups),
        }
        rounds[-1]["stop_decision"] = _round_stop_decision(
            round_id=round_id,
            max_rounds=max_rounds,
            round_observations=round_observations,
            followups=followups,
            llm_followup=llm_followup,
        )
        plan.tool_requests.extend(followups)
        if rounds[-1]["stop_decision"]["should_stop"]:
            break
        pending = followups

    return observations, rounds


def run_evidence_understanding(
    sample: NormalizedSample,
    *,
    allow_network: bool = False,
    use_llm: bool = True,
    use_llm_planning: bool | None = None,
    execute_tools: bool = False,
    cache_dir: str | None = None,
    allow_browser: bool = False,
    download_images: bool = True,
    use_vlm: bool = False,
    max_tool_rounds: int = 2,
) -> Dict[str, Any]:
    if use_llm_planning is None:
        use_llm_planning = use_llm
    collection_plan = plan_evidence_collection(sample, use_llm=use_llm_planning)
    packet = build_evidence_packet(sample, allow_network=allow_network)
    tool_observations: List[ToolObservation] = []
    agent_rounds: List[Dict[str, Any]] = []
    if execute_tools:
        tool_observations, agent_rounds = _execute_tool_rounds(
            collection_plan,
            cache_dir=cache_dir or "outputs/tool_cache",
            allow_network=allow_network,
            allow_browser=allow_browser,
            download_images=download_images,
            use_vlm=use_vlm,
            use_llm=use_llm,
            max_rounds=max_tool_rounds,
        )
    synthesized = synthesize_evidence(
        packet=packet,
        plan=collection_plan,
        observations=tool_observations,
    )
    result: Dict[str, Any] = {
        "instance_id": packet.instance_id,
        "repo": packet.repo,
        "dataset": packet.dataset,
        "stage": "evidence_understanding",
        "problem_statement_only": packet.metadata.get("problem_statement_only") is True,
        "collection_plan": collection_plan.to_dict(),
        "evidence_packet": packet.to_dict(),
        "deterministic_understanding": _deterministic_understanding(packet),
        "pending_tool_plan": _pending_tool_plan(collection_plan),
        "tool_observations": [item.to_dict() for item in tool_observations],
        "tools_executed": execute_tools,
        "agent_rounds": agent_rounds,
        "evidence_synthesis": synthesized,
    }
    if use_llm:
        result["llm_understanding"] = analyze_evidence_with_llm(
            packet,
            tool_observations=tool_observations if execute_tools else None,
        )
        result["llm_used"] = True
    else:
        result["llm_understanding"] = None
        result["llm_used"] = False
    return result
