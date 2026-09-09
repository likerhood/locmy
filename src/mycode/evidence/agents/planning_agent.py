from __future__ import annotations

import json
from typing import Any, Dict, List

from mycode.evidence.evidence_builder import build_evidence_sketch
from mycode.evidence.tools.llm_client import (
    LLMClientError,
    chat_completion,
    first_text,
    model_for_stage,
)
from mycode.evidence.tools.reproduction_extractor import extract_reproduction
from mycode.evidence.tools.url_inspector import inspect_url
from mycode.schemas.evidence import (
    EvidenceCollectionPlan,
    EvidenceSketch,
    ImageEvidence,
    NormalizedSample,
    ToolRequest,
    UrlEvidence,
)


def _summarize(text: str, limit: int = 480) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _problem_types(sketch: EvidenceSketch) -> List[str]:
    text = sketch.issue_text.lower()
    types: List[str] = []
    if sketch.images:
        types.append("multimodal_visual_issue")
    if sketch.urls:
        types.append("url_grounded_issue")
    if any(url.role == "reproduction_entry" for url in sketch.urls):
        types.append("reproduction_driven_issue")
    if any(url.leakage_risk == "high" for url in sketch.urls):
        types.append("contains_leakage_url")
    if any(token in text for token in ("click", "hover", "leave", "button", "form", "submit")):
        types.append("ui_or_event_flow")
    if any(token in text for token in ("option", "config", "parameter", "flag")):
        types.append("config_or_parameter_flow")
    if any(token in text for token in ("render", "layout", "style", "canvas", "chart", "pdf")):
        types.append("render_or_layout_flow")
    if any(token in text for token in ("type", "class", "method", "interface", "attribute")):
        types.append("symbol_or_type_flow")
    return list(dict.fromkeys(types))


def _url_tool_request(url: UrlEvidence) -> ToolRequest:
    inspected = inspect_url(url.raw_url)
    tool = "web_snapshot_fetcher"
    expected_outputs = ["page_title", "headings", "visible_text", "semantic_terms"]
    should_execute = True
    caution: List[str] = []
    parameters: Dict[str, Any] = {
        "url_kind": inspected.get("kind"),
        "role": inspected.get("role"),
        "risk": inspected.get("risk"),
    }

    recommendation = inspected.get("tool_recommendation")
    role = inspected.get("role")

    if recommendation == "skip_for_localization":
        tool = "leakage_url_filter"
        expected_outputs = ["leakage_reason", "redacted_url_marker"]
        should_execute = False
        caution.append("skip_pr_commit_diff_url_for_localization")
    elif recommendation == "repo_seed_expand":
        tool = "github_url_parser"
        expected_outputs = ["repo", "path", "line", "symbol_hint", "seed_not_target_warning"]
        caution.append("use_code_url_as_evidence_seed_not_patch_target")
        parameters.update(
            {
                "github_kind": inspected.get("github_kind"),
                "repo": inspected.get("repo"),
                "path": inspected.get("path"),
                "line": inspected.get("line"),
            }
        )
    elif recommendation == "reproduction_extractor":
        reproduction = extract_reproduction(url.raw_url)
        platform = reproduction.get("platform")
        tool = "playground_decoder"
        expected_outputs = ["input_code", "config", "version", "semantic_queries"]
        parameters.update(
            {
                "platform": platform,
                "query_fields": reproduction.get("query_fields", []),
                "needs_browser": reproduction.get("needs_browser"),
                "browser_observation_plan": reproduction.get("browser_observation_plan", {}),
            }
        )
        if reproduction.get("needs_browser") or reproduction.get("browser_observation_plan", {}).get("requires_browser"):
            tool = "browser_reproduction_reader"
            expected_outputs = [
                "file_tree",
                "requested_source_file",
                "package_json_dependencies",
                "preview_screenshot",
                "console_log",
                "interaction_trace",
            ]
            caution.append("reproduction_code_is_not_target_repository_code")
    elif role == "spec_or_api_semantics":
        tool = "web_doc_reader"
        expected_outputs = ["title", "headings", "api_names", "parameters", "behavior_constraints", "code_blocks"]
    elif role == "historical_discussion":
        tool = "discussion_reader"
        expected_outputs = ["problem_terms", "mentioned_symbols", "mentioned_components"]
        caution.append("do_not_extract_patch_or_commit_content")

    return ToolRequest(
        tool=tool,
        source=url.raw_url,
        evidence_type=url.url_type,
        role_hypothesis=url.role,
        priority="high" if role in {"reproduction_entry", "visual_evidence"} else "medium",
        reason=url.reason or inspected.get("localization_use", "extract_structured_url_evidence"),
        expected_outputs=expected_outputs,
        parameters=parameters,
        should_execute=should_execute,
        caution=caution,
    )


def _image_tool_request(image: ImageEvidence) -> ToolRequest:
    return ToolRequest(
        tool="vlm_image_inspector",
        source=image.raw_url,
        evidence_type=image.image_type,
        role_hypothesis=image.role,
        priority="high",
        reason="extract_visible_text_ui_entities_visual_symptom_and_expected_actual_difference",
        expected_outputs=[
            "visible_text",
            "ui_or_chart_entities",
            "visual_symptom",
            "expected_actual_difference",
            "likely_code_layers",
            "visual_search_queries",
        ],
        parameters={
            "source_field": image.source_field,
            "extension": image.extension,
        },
        should_execute=True,
        caution=["image_describes_symptom_not_patch_target"],
    )


def _deterministic_plan(sketch: EvidenceSketch) -> EvidenceCollectionPlan:
    requests = [_url_tool_request(url) for url in sketch.urls]
    requests.extend(_image_tool_request(image) for image in sketch.images)

    warnings: List[str] = []
    if any(not request.should_execute for request in requests):
        warnings.append("Some URLs look like PR/commit/diff leakage and should be redacted before localization.")
    if any(request.tool == "browser_reproduction_reader" for request in requests):
        warnings.append("Some reproduction URLs need a live browser to extract source files, dependencies, and runtime behavior.")
    if any(request.tool == "vlm_image_inspector" for request in requests):
        warnings.append("Images need VLM analysis; heuristic image labels are not enough for final localization.")

    return EvidenceCollectionPlan(
        instance_id=sketch.instance_id,
        repo=sketch.repo,
        dataset=sketch.dataset,
        issue_summary=_summarize(sketch.issue_text),
        suspected_problem_types=_problem_types(sketch),
        tool_requests=requests,
        initial_search_intent=list(dict.fromkeys(sketch.symbol_queries + sketch.concern_queries + sketch.flow_hypotheses))[:24],
        warnings=warnings,
        metadata={
            **sketch.metadata,
            "problem_statement_only": True,
            "url_count": len(sketch.urls),
            "image_count": len(sketch.images),
            "planning_mode": "deterministic_with_optional_llm",
        },
    )


def _compact_for_llm(sketch: EvidenceSketch, plan: EvidenceCollectionPlan) -> Dict[str, Any]:
    return {
        "instance_id": sketch.instance_id,
        "repo": sketch.repo,
        "dataset": sketch.dataset,
        "problem_statement": sketch.issue_text,
        "problem_statement_only": True,
        "detected_urls": [url.to_dict() for url in sketch.urls],
        "detected_images": [image.to_dict() for image in sketch.images],
        "symbol_queries": sketch.symbol_queries,
        "concern_queries": sketch.concern_queries,
        "flow_hypotheses": sketch.flow_hypotheses,
        "deterministic_tool_requests": [request.to_dict() for request in plan.tool_requests],
    }


def _llm_planning(sketch: EvidenceSketch, plan: EvidenceCollectionPlan, max_tokens: int = 1400) -> Dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "You are the Evidence Planning Agent for multimodal issue localization. "
                "Use only information from problem_statement. Never use gold patches, gold files, "
                "test answers, or hints. Your task is not to predict target files directly, but to "
                "decide which tools should process URLs, images, reproduction entry points, and code "
                "snippets, and to state the evidence-handling rationale. Treat PR, commit, and diff "
                "URLs as high-risk leakage and mark them to be skipped. A GitHub code URL is only an "
                "evidence seed, not an automatic patch target. Do not invent repository paths, source symbols, "
                "selectors, handlers, or routes. URL syntax and external reproduction source are evidence "
                "provenance, not target-repository semantics. Return one compact JSON object only, with no "
                "Markdown, preamble, or hidden chain-of-thought. Write generated prose and "
                "search queries in English; preserve source text, paths, URLs, and identifiers verbatim."
            ),
        },
        {
            "role": "user",
            "content": (
                "Review the deterministic tool plan below and add only necessary planning-level "
                "refinements. Do not restate the input. Return these JSON fields: issue_summary, "
                "evidence_roles, tool_plan_adjustments, priority_ranking, missing_tools, "
                "initial_localization_intent, cautions. Keep explanations concise.\n\n"
                + json.dumps(_compact_for_llm(sketch, plan), ensure_ascii=False, indent=2)
            ),
        },
    ]
    response = chat_completion(messages, model=model_for_stage("planning"), max_tokens=max_tokens)
    text = first_text(response)
    try:
        parsed: Dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw_text": text}
    parsed["_llm_response_id"] = response.get("id")
    parsed["_model"] = response.get("model")
    parsed["_usage"] = response.get("usage")
    return parsed


def plan_evidence_collection(
    sample: NormalizedSample,
    *,
    use_llm: bool = True,
    fail_on_llm_error: bool = False,
) -> EvidenceCollectionPlan:
    sketch = build_evidence_sketch(sample)
    plan = _deterministic_plan(sketch)
    if not use_llm:
        return plan

    try:
        plan.llm_planning = _llm_planning(sketch, plan)
    except LLMClientError as exc:
        if fail_on_llm_error:
            raise
        plan.warnings.append(f"LLM planning failed; using deterministic plan only: {exc}")
        plan.llm_planning = {
            "error": str(exc),
            "fallback": "deterministic_plan",
        }
    return plan
