from __future__ import annotations

from typing import Any, Dict, Iterable, List

from mycode.evidence.evidence_builder import build_evidence_sketch
from mycode.evidence.tools.image_inspector import inspect_image
from mycode.evidence.tools.reproduction_extractor import extract_reproduction
from mycode.evidence.tools.url_inspector import inspect_url
from mycode.evidence.tools.web_snapshot import build_web_snapshot
from mycode.schemas.evidence import EvidencePacket, NormalizedSample


def _summarize_issue(text: str, limit: int = 420) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _modality(url_count: int, image_count: int) -> str:
    if url_count and image_count:
        return "image_and_url"
    if image_count:
        return "image_only"
    if url_count:
        return "url_only"
    return "text_only"


def _search_plan_from_evidence(
    url_inspections: List[Dict[str, Any]],
    reproduction_cases: List[Dict[str, Any]],
    image_inspections: List[Dict[str, Any]],
    flow_hypotheses: List[str],
) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []

    for item in url_inspections:
        role = item.get("role")
        if role == "code_evidence_seed":
            plan.append(
                {
                    "stage": "repo_seed_expand",
                    "source": item.get("url"),
                    "action": "inspect_symbol_then_expand_references_callers_import_users",
                    "warning": "do_not_rank_this_path_as_target_without_downstream_evidence",
                }
            )
        elif role == "spec_or_api_semantics":
            plan.append(
                {
                    "stage": "semantic_query_expansion",
                    "source": item.get("url"),
                    "action": "extract_api_names_parameters_behavior_rules",
                }
            )
        elif role == "reproduction_entry":
            plan.append(
                {
                    "stage": "reproduction_understanding",
                    "source": item.get("url"),
                    "action": "parse_input_code_config_version_expected_actual_behavior",
                }
            )
        elif role == "historical_discussion":
            plan.append(
                {
                    "stage": "discussion_snapshot",
                    "source": item.get("url"),
                    "action": "extract_problem_terms_without_pr_commit_diff_leakage",
                }
            )

    for case in reproduction_cases:
        plan.append(
            {
                "stage": "behavior_to_code_layer",
                "source": case.get("url"),
                "action": "search_reproduction_symbols_then_semantic_layers",
                "likely_layers": case.get("likely_layers", []),
            }
        )

    for image in image_inspections:
        plan.append(
            {
                "stage": "visual_to_program_layer",
                "source": image.get("url"),
                "action": "map_visual_symptom_to_route_component_render_or_layout_pipeline",
                "likely_layers": image.get("likely_layers", []),
            }
        )

    if "parameter_or_config_flow" in flow_hypotheses:
        plan.append(
            {
                "stage": "flow_expansion",
                "action": "trace_public_parameter_to_internal_object_backend_serializer_or_renderer",
            }
        )
    if "url_builder_or_route_flow" in flow_hypotheses:
        plan.append(
            {
                "stage": "flow_expansion",
                "action": "trace_route_to_component_action_handler_url_builder_state_selector",
            }
        )
    if "render_style_pipeline_flow" in flow_hypotheses:
        plan.append(
            {
                "stage": "flow_expansion",
                "action": "trace_parser_style_resolve_layout_renderer_pipeline",
            }
        )

    return plan


def build_evidence_packet(
    sample: NormalizedSample,
    allow_network: bool = False,
) -> EvidencePacket:
    sketch = build_evidence_sketch(sample)
    url_inspections = [inspect_url(url.raw_url) for url in sketch.urls]
    reproduction_cases = [
        extract_reproduction(item["url"])
        for item in url_inspections
        if item.get("tool_recommendation") == "reproduction_extractor"
    ]
    web_snapshots = [
        build_web_snapshot(item["url"], allow_network=allow_network)
        for item in url_inspections
        if item.get("tool_recommendation") == "web_snapshot"
    ]
    image_inspections = [inspect_image(image, sketch.issue_text) for image in sketch.images]
    code_references = [
        item
        for item in url_inspections
        if item.get("role") == "code_evidence_seed"
    ]
    leakage = [
        item
        for item in url_inspections
        if item.get("risk") == "high" or item.get("role") == "leakage_skip"
    ]

    warnings = []
    if leakage:
        warnings.append("High-risk leakage URLs were detected and should be excluded from localization prompts.")
    if any(case.get("needs_browser") for case in reproduction_cases):
        warnings.append("Some reproduction URLs need browser observation to extract code/config.")
    if image_inspections:
        warnings.append("Image inspections are heuristic until a VLM is connected.")

    packet = EvidencePacket(
        instance_id=sketch.instance_id,
        repo=sketch.repo,
        dataset=sketch.dataset,
        issue_summary=_summarize_issue(sketch.issue_text),
        modality=_modality(len(sketch.urls), len(sketch.images)),
        url_inspections=url_inspections,
        reproduction_cases=reproduction_cases,
        web_snapshots=web_snapshots,
        image_inspections=image_inspections,
        code_references=code_references,
        symbol_queries=sketch.symbol_queries,
        concern_queries=sketch.concern_queries,
        flow_hypotheses=sketch.flow_hypotheses,
        search_plan=_search_plan_from_evidence(
            url_inspections,
            reproduction_cases,
            image_inspections,
            sketch.flow_hypotheses,
        ),
        leakage=leakage,
        warnings=warnings,
        metadata={
            **sketch.metadata,
            "allow_network": allow_network,
            "problem_statement_only": True,
        },
    )
    return packet


def build_evidence_packets(
    samples: Iterable[NormalizedSample],
    allow_network: bool = False,
) -> List[EvidencePacket]:
    return [build_evidence_packet(sample, allow_network=allow_network) for sample in samples]
