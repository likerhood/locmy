from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, List

from mycode.repo_index.structure_index import tokenize


@dataclass
class NavigationAction:
    action: str
    reason: str
    suggested_queries: List[str] = field(default_factory=list)
    preferred_edge_types: List[str] = field(default_factory=list)
    stop_hint: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dedupe(values: Iterable[str], limit: int = 30) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue
        low = text.lower()
        if low in seen:
            continue
        out.append(text)
        seen.add(low)
        if len(out) >= limit:
            break
    return out


def _evidence_text(evidence_result: dict[str, Any]) -> str:
    pieces: list[str] = []
    packet = evidence_result.get("evidence_packet", {}) or {}
    deterministic = evidence_result.get("deterministic_understanding", {}) or {}
    synthesis = evidence_result.get("evidence_synthesis", {}) or {}
    for key in ("symbol_queries", "concern_queries", "flow_hypotheses"):
        pieces.extend(str(item) for item in packet.get(key, []) or [])
    for item in packet.get("url_inspections", []) or []:
        pieces.append(str(item.get("role") or ""))
        pieces.append(str(item.get("url") or ""))
        pieces.extend(str(term) for term in item.get("semantic_terms", []) or [])
    for item in packet.get("image_inspections", []) or []:
        pieces.append(str(item.get("image_type") or ""))
        pieces.extend(str(term) for term in item.get("visual_queries", []) or [])
    for group in (deterministic.get("search_queries", {}) or {}).values():
        pieces.extend(str(item) for item in group or [])
    for group in (synthesis.get("query_groups", {}) or {}).values():
        pieces.extend(str(item) for item in group or [])
    return "\n".join(pieces).lower()


def _paths_look_like_reproduction(paths: Iterable[str]) -> bool:
    markers = ("example", "examples", "demo", "fixture", "fixtures", "test", "tests", "docs", "playground", "storybook")
    return any(any(marker in path.lower() for marker in markers) for path in paths)


def _flow_missing_roles(current_flow_traces: Iterable[dict[str, Any]] | None) -> list[str]:
    missing: list[str] = []
    for flow in current_flow_traces or []:
        coverage = flow.get("role_coverage", {}) or {}
        for role in coverage.get("missing_roles", []) or []:
            missing.append(str(role))
    return _dedupe(missing, limit=12)


def _failure_mode_actions(text: str) -> list[NavigationAction]:
    actions: list[NavigationAction] = []
    if "code_evidence_seed" in text or ("github.com" in text and "/blob/" in text):
        actions.append(
            NavigationAction(
                action="diagnose_code_url_seed_trap",
                reason="A code URL can be an evidence seed such as a selector/API reference, not the final patch target.",
                suggested_queries=[
                    "downstream caller of referenced symbol",
                    "business flow uses selector api",
                    "implementation layer beyond linked file",
                ],
                preferred_edge_types=["imports", "calls", "selects_state", "dispatches_action", "renders"],
            )
        )
    if any(token in text for token in ("codesandbox", "stackblitz", "playground", "example url", "docs sample")):
        actions.append(
            NavigationAction(
                action="diagnose_reproduction_to_implementation_gap",
                reason="Reproduction URLs expose runnable demo code; localization should cross from demo/config into library implementation.",
                suggested_queries=[
                    "demo import implementation source",
                    "option parser resolver plugin implementation",
                    "runtime reproduction to core module",
                ],
                preferred_edge_types=["imports", "calls", "configures", "renders"],
            )
        )
    if any(token in text for token in ("screenshot", "image", "visual", "layout", "margin", "canvas", "pdf", "style")):
        actions.append(
            NavigationAction(
                action="diagnose_visual_symptom_to_semantic_layer",
                reason="A visual symptom usually maps to style/layout/render semantics instead of the image-bearing issue text.",
                suggested_queries=[
                    "visual symptom style resolver layout pipeline",
                    "expand resolve stylesheet renderer",
                    "canvas chart legend layout plugin",
                ],
                preferred_edge_types=["styles", "renders", "calls", "imports"],
            )
        )
    if any(token in text for token in ("kdf_rounds", "parameter", "option", "config", "flag", "redirect_to", "client_id")):
        actions.append(
            NavigationAction(
                action="diagnose_parameter_propagation_gap",
                reason="The fix likely requires parameter propagation closure rather than a single lexical hit.",
                suggested_queries=[
                    "public api parameter internal object backend implementation",
                    "parameter propagation call chain",
                    "config option parser serializer resolver",
                ],
                preferred_edge_types=["calls", "imports", "configures", "type_flow"],
            )
        )
    if any(token in text for token in ("mypy", "binder", "deleted variable", "typeinfo", "symbol table", "declaration")):
        actions.append(
            NavigationAction(
                action="diagnose_type_binding_gap",
                reason="Type checker failures require symbol binding/type-state navigation.",
                suggested_queries=[
                    "binder declaration deleted variable type state",
                    "symbol table TypeInfo TypeType typevars",
                    "type narrowing frame context",
                ],
                preferred_edge_types=["type_flow", "calls", "imports"],
            )
        )
    return actions


def summarize_agent_observation(
    *,
    top_files: Iterable[str],
    flow_traces: Iterable[dict[str, Any]],
    graph_summary: dict[str, Any],
    verifier: dict[str, Any],
) -> dict[str, Any]:
    top = list(top_files)
    flow_types = Counter(str(flow.get("flow_type") or "unknown") for flow in flow_traces)
    edge_counts = graph_summary.get("edge_type_counts", {}) or {}
    verifier_reasons: Counter[str] = Counter()
    for decision in verifier.get("decisions", []) or []:
        for reason in decision.get("verifier_reasons", []) or []:
            verifier_reasons[str(reason)] += 1
    return {
        "top_files": top[:10],
        "top_file_dirs": [str(Path(path).parent) for path in top[:5]],
        "flow_type_counts": dict(flow_types),
        "visible_graph_edge_types": edge_counts,
        "verifier_reason_counts": dict(verifier_reasons),
        "candidate_count": len(top),
    }


def select_navigation_actions(
    *,
    round_no: int,
    issue_text: str,
    evidence_result: dict[str, Any],
    queries: Iterable[str],
    previous_top_paths: Iterable[str],
    current_seed_files: Iterable[str],
    current_flow_traces: Iterable[dict[str, Any]] | None = None,
) -> list[NavigationAction]:
    """Choose the next repository-navigation tools in an explainable way.

    This is a deterministic controller for the current prototype. It mimics the
    useful part of an autonomous coding agent loop: observe issue/evidence,
    decide which tool family matters, expand the query frontier, and record why.
    """

    text = " ".join([issue_text, _evidence_text(evidence_result), " ".join(queries)]).lower()
    previous = list(previous_top_paths)
    seeds = list(current_seed_files)
    flow_types = {str(flow.get("flow_type") or "") for flow in current_flow_traces or []}
    actions: list[NavigationAction] = [
        NavigationAction(
            action="lexical_search",
            reason="Use issue/evidence terms to get an initial candidate set.",
            suggested_queries=[],
        ),
        NavigationAction(
            action="entity_search",
            reason="Search functions/classes because file-level evidence often hides the real patch entity.",
            suggested_queries=[],
        ),
    ]

    if any(token in text for token in ("github.com", "codesandbox", "stackblitz", "playground", "reproduction", "repro")):
        actions.append(
            NavigationAction(
                action="evidence_role_check",
                reason="URL evidence may point to reproduction/demo/context instead of the final patch target.",
                suggested_queries=["reproduction evidence not patch target", "entry point implementation utility"],
                preferred_edge_types=["imports", "calls", "renders", "configures"],
            )
        )

    if any(token in text for token in ("component", "jsx", "tsx", "react", "vue", "render", "route", "selector", "redux", "state", "dispatch")):
        actions.append(
            NavigationAction(
                action="frontend_concern_graph_expand",
                reason="Frontend issues require route/component/state/style edges, not only plain call edges.",
                suggested_queries=["component renders selector reducer action style route"],
                preferred_edge_types=["renders", "selects_state", "dispatches_action", "styles", "routes_to", "imports"],
            )
        )

    if any(token in text for token in ("css", "scss", "style", "layout", "margin", "color", "font", "canvas", "chart", "legend")):
        actions.append(
            NavigationAction(
                action="visual_style_semantic_expand",
                reason="Visual symptoms often map to style/layout/plugin pipelines rather than the screenshot file.",
                suggested_queries=["style layout plugin renderer resolver"],
                preferred_edge_types=["styles", "renders", "calls", "imports"],
            )
        )

    if any(token in text for token in ("parameter", "option", "config", "flag", "parser", "kdf", "round", "redirect_to", "client_id")):
        actions.append(
            NavigationAction(
                action="parameter_dataflow_trace",
                reason="The likely fix needs following a parameter/config value across wrappers and backend implementation.",
                suggested_queries=["parameter flow public api internal backend serializer config option"],
                preferred_edge_types=["calls", "imports", "configures", "type_flow"],
            )
        )

    if any(token in text for token in ("type", "mypy", "binder", "symbol", "semantic", "narrow", "delete", "deleted variable")):
        actions.append(
            NavigationAction(
                action="python_type_flow_expand",
                reason="Python type-checker bugs need symbol-table/binder/type-flow navigation.",
                suggested_queries=["binder symbol table type flow declaration deleted variable"],
                preferred_edge_types=["type_flow", "calls", "imports"],
            )
        )

    if any(token in text for token in ("java", "interface", "implements", "override", "delegate", "constructor", "exception")):
        actions.append(
            NavigationAction(
                action="java_dispatch_expand",
                reason="Java localization often needs public API to delegate/override/implementation expansion.",
                suggested_queries=["implements overrides delegate constructor public api internal implementation"],
                preferred_edge_types=["inherits_or_implements", "calls", "imports"],
            )
        )

    if round_no > 1 and previous and seeds and previous[:3] == seeds[:3]:
        actions.append(
            NavigationAction(
                action="stagnation_reformulate",
                reason="Top candidates are stable; reformulate using symbols and flow types to avoid looping.",
                suggested_queries=_dedupe([" ".join(flow_types), "alternative implementation layer", "utility backend resolver handler"]),
            )
        )

    if _paths_look_like_reproduction(previous[:5] or seeds[:5]):
        actions.append(
            NavigationAction(
                action="move_from_reproduction_to_implementation",
                reason="Current frontier contains examples/tests/docs; expand toward imported implementation files.",
                suggested_queries=["implementation utility core resolver backend plugin"],
                preferred_edge_types=["imports", "calls", "configures"],
            )
        )

    if round_no >= 2 and previous and not flow_types:
        actions.append(
            NavigationAction(
                action="force_flow_probe",
                reason="No flow trace has emerged yet, so probe explicit call/data/config propagation terms.",
                suggested_queries=["call chain data flow parameter propagation state update"],
                preferred_edge_types=["calls", "selects_state", "dispatches_action", "configures"],
            )
        )

    actions.extend(_failure_mode_actions(text))

    missing_roles = _flow_missing_roles(current_flow_traces)
    if missing_roles:
        actions.append(
            NavigationAction(
                action="flow_obligation_gap_probe",
                reason="Existing flow traces are incomplete; search for the missing implementation roles before stopping.",
                suggested_queries=[f"missing flow role {role}" for role in missing_roles],
                preferred_edge_types=["calls", "imports", "renders", "selects_state", "dispatches_action", "configures", "type_flow"],
            )
        )

    return actions
