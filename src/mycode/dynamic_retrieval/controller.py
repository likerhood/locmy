from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from mycode.repo_index.structure_index import tokenize


LLMController = Callable[[str], str | dict[str, Any]]

ALLOWED_EDGE_TYPES = {
    "calls",
    "reverse_calls",
    "incoming_calls",
    "imports",
    "reverse_imports",
    "incoming_imports",
    "renders",
    "reverse_renders",
    "incoming_renders",
    "selects_state",
    "reverse_selects_state",
    "incoming_selects_state",
    "dispatches_action",
    "reverse_dispatches_action",
    "incoming_dispatches_action",
    "handles_action",
    "reverse_handles_action",
    "incoming_handles_action",
    "uses_hook",
    "reverse_uses_hook",
    "incoming_uses_hook",
    "binds_ui_event",
    "reverse_binds_ui_event",
    "incoming_binds_ui_event",
    "styles",
    "reverse_styles",
    "incoming_styles",
    "configures",
    "reverse_configures",
    "incoming_configures",
    "type_flow",
    "reverse_type_flow",
    "incoming_type_flow",
    "inherits_or_implements",
    "reverse_inherits_or_implements",
    "incoming_inherits_or_implements",
    "overrides",
    "reverse_overrides",
    "incoming_overrides",
    "routes_to",
    "reverse_routes_to",
    "incoming_routes_to",
    "documents",
    "reverse_documents",
    "incoming_documents",
    "same_directory",
}


@dataclass
class ControllerDecision:
    """One explainable next action in the localization loop.

    The controller is intentionally separated from concrete tool execution. It
    decides what kind of move the agent should try next; the search loop maps
    those decisions into lexical search, graph navigation and flow tracing.
    """

    tool: str
    mode: str
    reason: str
    queries: list[str] = field(default_factory=list)
    preferred_edge_types: list[str] = field(default_factory=list)
    seed_strategy: str = ""
    stop: bool = False
    confidence: float = 0.0
    source: str = "heuristic"
    llm_raw: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dedupe(values: Iterable[str], *, limit: int = 40) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
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
        pieces.append(str(item.get("url_type") or ""))
        pieces.append(str(item.get("normalized_url") or item.get("url") or ""))
        pieces.extend(str(term) for term in item.get("semantic_terms", []) or [])
    for item in packet.get("image_inspections", []) or []:
        pieces.append(str(item.get("role") or ""))
        pieces.append(str(item.get("image_type") or ""))
        pieces.extend(str(term) for term in item.get("visual_queries", []) or [])
    for group in (deterministic.get("search_queries", {}) or {}).values():
        pieces.extend(str(item) for item in group or [])
    for group in (synthesis.get("query_groups", {}) or {}).values():
        pieces.extend(str(item) for item in group or [])
    for observation in evidence_result.get("tool_observations", []) or []:
        pieces.append(str(observation.get("tool") or ""))
        extracted = observation.get("extracted", {}) or {}
        pieces.append(json.dumps(extracted, ensure_ascii=False, sort_keys=True)[:3000])
    return "\n".join(pieces)


def _text_has(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(needle.lower() in low for needle in needles)


def _stable_frontier(previous_top_paths: Iterable[str], current_seed_files: Iterable[str]) -> bool:
    previous = [str(path) for path in previous_top_paths]
    current = [str(path) for path in current_seed_files]
    return bool(previous and current and previous[:3] == current[:3])


def heuristic_controller_decisions(
    *,
    round_no: int,
    issue_text: str,
    evidence_result: dict[str, Any],
    queries: Iterable[str],
    previous_top_paths: Iterable[str],
    current_seed_files: Iterable[str],
    current_flow_traces: Iterable[dict[str, Any]] | None = None,
) -> list[ControllerDecision]:
    text = "\n".join([issue_text, _evidence_text(evidence_result), " ".join(queries)])
    low = text.lower()
    decisions: list[ControllerDecision] = [
        ControllerDecision(
            tool="SearchAnchor",
            mode="concern",
            reason="Start from issue concerns, symbols and expected behavior before reading full files.",
            queries=[],
            confidence=0.7,
        )
    ]

    if _text_has(low, "github.com") and _text_has(low, "/blob/"):
        decisions.append(
            ControllerDecision(
                tool="NavigateCode",
                mode="used_by",
                reason=(
                    "The issue contains a code URL. Treat it as an evidence seed first; "
                    "walk to callers/users before ranking the linked file as a patch target."
                ),
                queries=[
                    "downstream users of referenced symbol",
                    "business flow using referenced selector api",
                    "consumer component of linked code evidence",
                ],
                preferred_edge_types=[
                    "reverse_calls",
                    "incoming_calls",
                    "reverse_imports",
                    "incoming_imports",
                    "reverse_selects_state",
                    "incoming_selects_state",
                    "reverse_renders",
                    "incoming_renders",
                ],
                seed_strategy="evidence_seed_then_used_by",
                confidence=0.86,
            )
        )

    if _text_has(low, "codesandbox", "stackblitz", "playground", "repro", "reproduction"):
        decisions.append(
            ControllerDecision(
                tool="NavigateCode",
                mode="call",
                reason=(
                    "Reproduction/playground evidence usually starts in demo code; cross from demo imports "
                    "and options to implementation files."
                ),
                queries=[
                    "playground demo import implementation source",
                    "runtime option plugin resolver implementation",
                    "reproduction code to core module",
                ],
                preferred_edge_types=["imports", "calls", "configures", "renders"],
                seed_strategy="reproduction_to_implementation",
                confidence=0.82,
            )
        )

    if _text_has(low, "react", "jsx", "tsx", "component", "selector", "redux", "route", "dispatch", "hook"):
        decisions.append(
            ControllerDecision(
                tool="NavigateCode",
                mode="concern",
                reason="Frontend failures need horizontal concern expansion over component/state/route/style files.",
                queries=[
                    "component route selector reducer action workflow",
                    "ui state transition expected effect",
                    "frontend business flow implementation",
                ],
                preferred_edge_types=[
                    "renders",
                    "selects_state",
                    "dispatches_action",
                    "uses_hook",
                    "binds_ui_event",
                    "styles",
                    "routes_to",
                    "imports",
                ],
                seed_strategy="frontend_concern",
                confidence=0.78,
            )
        )

    if _text_has(low, "hover", "mouseleave", "onleave", "onclick", "handler", "legend", "tooltip"):
        decisions.append(
            ControllerDecision(
                tool="TraceFlow",
                mode="ui_event_to_handler",
                reason="UI symptoms should be verified by tracing event handler state and render updates.",
                queries=["ui event handler state update render effect", "legend tooltip hover leave handler"],
                preferred_edge_types=["binds_ui_event", "renders", "calls"],
                seed_strategy="event_state_behavior",
                confidence=0.8,
            )
        )

    if _text_has(low, "kdf_rounds", "openssh", "serializer", "serialization", "backend", "private key"):
        decisions.append(
            ControllerDecision(
                tool="TraceFlow",
                mode="serializer_backend_call_chain",
                reason="Serializer/backend issues require parameter propagation closure from public API to final implementation.",
                queries=[
                    "public api parameter internal serialization object backend implementation",
                    "BestAvailableEncryption kdf_rounds backend ssh serializer",
                    "parameter propagation call chain",
                ],
                preferred_edge_types=["calls", "imports", "type_flow", "configures"],
                seed_strategy="parameter_closure",
                confidence=0.9,
            )
        )

    if _text_has(low, "mypy", "binder", "typeinfo", "typetype", "deleted variable", "symbol table", "declaration"):
        decisions.append(
            ControllerDecision(
                tool="TraceFlow",
                mode="python_type_binding_flow",
                reason="Type checker failures require binding/type-state navigation rather than ordinary call search only.",
                queries=[
                    "binder declaration deleted variable TypeInfo TypeType",
                    "symbol table type state frame context",
                    "type narrowing deleted variable read",
                ],
                preferred_edge_types=["type_flow", "calls", "imports"],
                seed_strategy="python_type_state",
                confidence=0.88,
            )
        )

    if _text_has(low, "java", "assertj", "gson", "netty", "override", "implements", "interface", "delegate", "constructor"):
        decisions.append(
            ControllerDecision(
                tool="NavigateCode",
                mode="call",
                reason="Java localization often needs public API to delegate/override/implementation expansion.",
                queries=[
                    "public api delegate internal implementation",
                    "interface implementation override method",
                    "constructor forwards to strategy",
                ],
                preferred_edge_types=[
                    "calls",
                    "imports",
                    "inherits_or_implements",
                    "overrides",
                    "reverse_overrides",
                    "incoming_overrides",
                ],
                seed_strategy="java_dispatch",
                confidence=0.83,
            )
        )

    flow_types = {str(flow.get("flow_type") or "") for flow in current_flow_traces or []}
    if not flow_types:
        decisions.append(
            ControllerDecision(
                tool="TraceFlow",
                mode="state_to_behavior",
                reason="No flow evidence has been observed yet; add a lightweight flow verification pass.",
                queries=["state parameter option effect behavior implementation"],
                preferred_edge_types=["calls", "imports", "configures", "type_flow"],
                seed_strategy="flow_probe",
                confidence=0.62,
            )
        )

    if round_no > 1 and _stable_frontier(previous_top_paths, current_seed_files):
        decisions.append(
            ControllerDecision(
                tool="SearchAnchor",
                mode="reformulate",
                reason="Top candidates are stable; reformulate with missing behavior and role obligations.",
                queries=["missing role implementation behavior effect", "not reproduction target underlying source"],
                seed_strategy="stagnation_reformulation",
                confidence=0.55,
            )
        )

    return decisions[:8]


def _controller_prompt(
    *,
    round_no: int,
    issue_text: str,
    evidence_result: dict[str, Any],
    queries: Iterable[str],
    previous_top_paths: Iterable[str],
    current_seed_files: Iterable[str],
    current_flow_traces: Iterable[dict[str, Any]] | None,
) -> str:
    payload = {
        "round_no": round_no,
        "issue_text": issue_text[:5000],
        "evidence_summary": _evidence_text(evidence_result)[:5000],
        "queries": list(queries)[:40],
        "previous_top_paths": list(previous_top_paths)[:15],
        "current_seed_files": list(current_seed_files)[:15],
        "flow_types": [str(flow.get("flow_type") or "") for flow in current_flow_traces or []][:12],
        "allowed_tools": ["SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"],
        "allowed_modes": [
            "concern",
            "call",
            "used_by",
            "ui_event_to_handler",
            "serializer_backend_call_chain",
            "python_type_binding_flow",
            "java_dispatch",
            "state_to_behavior",
            "reformulate",
        ],
    }
    return (
        "You are the evidence-gap controller for repository issue localization. Choose one or two actions "
        "that resolve the highest-priority missing source or program-flow evidence.\n"
        "Use URL, image, generated, documentation, test, and external-reproduction evidence only for navigation. "
        "Use ReadCode when a leading candidate lacks source verification. Use TraceFlow only when its endpoints "
        "are grounded in observed source symbols. Do not expand the graph merely because two files are adjacent.\n"
        "Do not repeat an earlier query without a specific new evidence gap, and do not run tools merely for "
        "coverage. Keep each action to at most three concrete queries. Return one compact JSON object only.\n"
        "Schema: {\"decisions\":[{\"tool\":\"SearchAnchor|NavigateCode|TraceFlow|ReadCode\","
        "\"mode\":\"allowed mode\",\"reason\":\"one evidence gap\",\"queries\":[\"specific query\"],"
        "\"preferred_edge_types\":[],\"seed_strategy\":\"current_verified_source\","
        "\"confidence\":0.0,\"stop\":false}]}.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _parse_llm_decisions(value: str | dict[str, Any]) -> list[ControllerDecision]:
    if isinstance(value, dict):
        if isinstance(value.get("decisions"), list):
            data: Any = value
        else:
            text = str(value.get("content") or value.get("text") or "").strip()
            if not text and isinstance(value.get("raw_response"), dict):
                choices = value["raw_response"].get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    text = str(message.get("content") or message.get("reasoning_content") or "").strip()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                match = re.search(r"(?:\[.*\]|\{.*\})", text, flags=re.DOTALL)
                if not match:
                    return []
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return []
    else:
        text = str(value or "").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"(?:\[.*\]|\{.*\})", text, flags=re.DOTALL)
            if not match:
                return []
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
    decisions = data.get("decisions") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(decisions, list):
        return []
    parsed: list[ControllerDecision] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        mode = str(item.get("mode") or "")
        reason = str(item.get("reason") or "")
        if tool not in {"SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"} or not reason:
            continue
        queries = [str(q) for q in item.get("queries", []) or []]
        edges = [
            str(edge)
            for edge in item.get("preferred_edge_types", []) or []
            if str(edge) in ALLOWED_EDGE_TYPES
        ]
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        parsed.append(
            ControllerDecision(
                tool=tool,
                mode=mode,
                reason=reason,
                queries=_dedupe(queries, limit=3),
                preferred_edge_types=_dedupe(edges, limit=12),
                seed_strategy=str(item.get("seed_strategy") or ""),
                stop=bool(item.get("stop") or False),
                confidence=max(0.0, min(1.0, confidence)),
                source="llm",
            )
        )
    return parsed[:2]


def decide_next_actions(
    *,
    round_no: int,
    issue_text: str,
    evidence_result: dict[str, Any],
    queries: Iterable[str],
    previous_top_paths: Iterable[str],
    current_seed_files: Iterable[str],
    current_flow_traces: Iterable[dict[str, Any]] | None = None,
    llm_controller: LLMController | None = None,
) -> list[ControllerDecision]:
    heuristic = heuristic_controller_decisions(
        round_no=round_no,
        issue_text=issue_text,
        evidence_result=evidence_result,
        queries=queries,
        previous_top_paths=previous_top_paths,
        current_seed_files=current_seed_files,
        current_flow_traces=current_flow_traces,
    )
    if llm_controller is None:
        return heuristic

    prompt = _controller_prompt(
        round_no=round_no,
        issue_text=issue_text,
        evidence_result=evidence_result,
        queries=queries,
        previous_top_paths=previous_top_paths,
        current_seed_files=current_seed_files,
        current_flow_traces=current_flow_traces,
    )
    try:
        llm_output = llm_controller(prompt)
    except Exception as exc:  # pragma: no cover - defensive for external callers.
        fallback = ControllerDecision(
            tool="SearchAnchor",
            mode="fallback",
            reason=f"LLM controller failed; using heuristic policy: {type(exc).__name__}",
            queries=[],
            source="fallback",
            confidence=0.0,
        )
        return [fallback] + heuristic
    parsed = _parse_llm_decisions(llm_output)
    if not parsed:
        fallback = ControllerDecision(
            tool="SearchAnchor",
            mode="fallback",
            reason="LLM controller returned no valid JSON decisions; using heuristic policy.",
            queries=[],
            source="fallback",
            confidence=0.0,
        )
        return [fallback] + heuristic
    if isinstance(llm_output, dict):
        raw = str(llm_output.get("content") or llm_output.get("text") or "")
        usage = llm_output.get("usage") or {}
        if not usage and isinstance(llm_output.get("raw_response"), dict):
            usage = llm_output["raw_response"].get("usage") or {}
        parsed[0].llm_raw = raw[:6000]
        if isinstance(usage, dict):
            parsed[0].token_usage = {
                key: int(usage.get(key) or 0)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if usage.get(key) is not None
            }

    heuristic_queries = _dedupe(query for decision in heuristic for query in decision.queries)
    parsed_queries = _dedupe(query for decision in parsed for query in decision.queries)
    if len(tokenize(" ".join(parsed_queries))) < 4 and heuristic_queries:
        parsed.append(
            ControllerDecision(
                tool="SearchAnchor",
                mode="heuristic_support",
                reason="Add deterministic query support because the LLM controller produced sparse queries.",
                queries=heuristic_queries[:3],
                preferred_edge_types=_dedupe(edge for decision in heuristic for edge in decision.preferred_edge_types)[:12],
                seed_strategy="hybrid_llm_heuristic",
                source="heuristic_support",
                confidence=0.5,
            )
        )
    return parsed[:2]
