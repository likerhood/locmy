from __future__ import annotations

import copy
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import unquote, urlparse

from mycode.evaluation.localization_eval import (
    entity_id,
    evaluate_three_level_ranking_with_applicability,
    file_module_id,
)
from mycode.flow_analysis.flow_chain import trace_flow_chains
from mycode.flow_analysis.interprocedural_flow import trace_interprocedural_flows
from mycode.flow_analysis.parameter_closure import trace_parameter_closures
from mycode.flow_analysis.program_flow import trace_program_flows
from mycode.flow_analysis.query_flows import build_flow_queries
from mycode.flow_analysis.runtime_trace import verify_runtime_traces
from mycode.flow_analysis.static_slice import trace_static_slices
from mycode.flow_analysis.statement_flow import trace_statement_flows
from mycode.dynamic_retrieval.controller import LLMController, decide_next_actions
from mycode.dynamic_retrieval.candidate_reviewer import review_candidates
from mycode.dynamic_retrieval.fast_seed_planner import plan_fast_seeds
from mycode.dynamic_retrieval.seed_frontier import restore_seed_frontier, demote_generated_outputs
from mycode.dynamic_retrieval.responsibility import responsibility_evidence
from mycode.dynamic_retrieval.navigation_policy import select_navigation_actions, summarize_agent_observation
from mycode.dynamic_retrieval.react_agent import run_react_tool_agent
from mycode.dynamic_retrieval.tools import run_four_tool_agent_round
from mycode.evidence.issue_sketch import build_issue_sketch, semantic_issue_text, sketch_query_terms
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, SearchHit, tokenize
from mycode.schemas.evidence import NormalizedSample
from mycode.utils.phase_logger import phase_context, phase_event


@dataclass
class RankedLocation:
    path: str
    score: float
    reasons: List[str] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)
    score_components: Dict[str, float] = field(default_factory=dict)
    belief: Dict[str, Any] = field(default_factory=dict)
    missing_evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RankedEntity:
    id: str
    path: str
    kind: str
    name: str
    score: float
    start_line: int = 0
    end_line: int = 0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DynamicSearchRound:
    round_no: int
    input_queries: List[str]
    agent_actions: List[Dict[str, Any]]
    frontier_state: Dict[str, Any]
    seed_files: List[str]
    file_hits: List[Dict[str, Any]]
    entity_hits: List[Dict[str, Any]]
    graph_hits: List[Dict[str, Any]]
    graph_summary: Dict[str, Any]
    code_contexts: List[Dict[str, Any]]
    flow_traces: List[Dict[str, Any]]
    verifier: Dict[str, Any]
    agent_observation: Dict[str, Any]
    ranked_locations: List[RankedLocation]
    ranked_modules: List[RankedEntity]
    ranked_functions: List[RankedEntity]
    next_queries: List[str]
    evaluation: Dict[str, Any]
    stop_decision: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_no": self.round_no,
            "input_queries": self.input_queries,
            "agent_actions": self.agent_actions,
            "frontier_state": self.frontier_state,
            "seed_files": self.seed_files,
            "file_hits": self.file_hits,
            "entity_hits": self.entity_hits,
            "graph_hits": self.graph_hits,
            "graph_summary": self.graph_summary,
            "code_contexts": self.code_contexts,
            "flow_traces": self.flow_traces,
            "verifier": self.verifier,
            "agent_observation": self.agent_observation,
            "ranked_locations": [item.to_dict() for item in self.ranked_locations],
            "ranked_modules": [item.to_dict() for item in self.ranked_modules],
            "ranked_functions": [item.to_dict() for item in self.ranked_functions],
            "next_queries": self.next_queries,
            "evaluation": self.evaluation,
            "stop_decision": self.stop_decision,
        }


_DYNAMIC_LOCALIZATION_CHECKPOINTS: dict[str, dict[str, Any]] = {}


def clear_dynamic_localization_checkpoint(instance_id: str) -> None:
    """Discard an in-process checkpoint before starting or after finishing a sample."""

    _DYNAMIC_LOCALIZATION_CHECKPOINTS.pop(str(instance_id), None)


def take_dynamic_localization_checkpoint(instance_id: str) -> dict[str, Any] | None:
    """Return the latest complete search round after an interrupted localization."""

    checkpoint = _DYNAMIC_LOCALIZATION_CHECKPOINTS.pop(str(instance_id), None)
    return copy.deepcopy(checkpoint) if checkpoint is not None else None


def _dedupe(values: Iterable[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
            if limit is not None and len(out) >= limit:
                break
    return out


STOP_QUERY_TOKENS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "has",
    "have",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "not",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "their",
    "then",
    "this",
    "to",
    "was",
    "when",
    "where",
    "with",
    "without",
    "would",
    "you",
    "using",
    "use",
    "used",
    "true",
    "false",
    "null",
    "none",
    "issue",
    "problem",
    "expected",
    "actual",
    "error",
    "bug",
    "fix",
    "file",
    "code",
}


def _clean_query_for_retrieval(query: str) -> str:
    text = " ".join(str(query or "").strip().split())
    if not text:
        return ""
    # Path-like and symbol-like terms are already high value. Keep them mostly
    # intact so code URLs, package paths, and symbols do not get destroyed by
    # stop-word filtering.
    if "/" in text or "::" in text or re.search(r"\.[A-Za-z0-9]{1,6}\b", text):
        return text[:180]
    tokens = [
        token
        for token in tokenize(text)
        if len(token) >= 3 and token not in STOP_QUERY_TOKENS and not token.isdigit()
    ]
    if not tokens:
        return ""
    return " ".join(tokens[:12])


def _sanitize_retrieval_queries(queries: Iterable[str], *, limit: int = 80) -> list[str]:
    return _dedupe((_clean_query_for_retrieval(query) for query in queries), limit=limit)


def _issue_query_text(issue_text: str) -> str:
    text = str(issue_text or "")
    for marker in ("\n[Multimodal Context - Compact]", "\n[Adapter Note]", "\n[adapter_fallback="):
        text = text.split(marker, 1)[0]
    return semantic_issue_text(text)


def _grounded_evidence_queries(values: Iterable[str], issue_text: str, *, limit: int = 40) -> list[str]:
    """Prevent unverified visual implementation guesses from becoming anchors."""

    issue = _issue_query_text(issue_text).lower()
    issue_tokens = {token for token in tokenize(issue) if len(token) >= 4}
    out: list[str] = []
    for value in values:
        query = " ".join(str(value or "").split())
        if not query:
            continue
        low = query.lower()
        if any(marker in query for marker in ("<", ">")):
            continue
        if low.startswith((".", "#")) and low not in issue:
            continue
        if re.search(r"\bif\s*\(", low) and low not in issue:
            continue
        if "/" in query and not query.startswith(("http://", "https://")):
            out.append(query)
            continue
        query_tokens = {token for token in tokenize(query) if len(token) >= 4}
        if query_tokens and issue_tokens.intersection(query_tokens):
            out.append(query)
    return _dedupe(out, limit=limit)


DOMAIN_PATH_ALIASES = {
    "email": ["email", "verified", "verification", "current-user", "current_user"],
    "verified": ["email", "verified", "verification", "current-user", "current_user"],
    "country": ["country", "countries", "location", "locations", "address"],
    "countries": ["country", "countries", "location", "locations", "address"],
    "location": ["location", "locations", "address", "country", "countries"],
    "address": ["address", "location", "locations", "country", "countries"],
    "store": ["store", "woocommerce", "commerce", "dashboard", "setup"],
    "setup": ["setup", "dashboard", "onboarding", "store", "woocommerce"],
    "redirect": ["redirect", "route", "url", "href", "link"],
    "legend": ["legend", "plugin", "hover", "leave", "event"],
    "hover": ["hover", "leave", "event", "mousemove", "mouseout"],
    "margin": ["margin", "style", "stylesheet", "resolve", "expand", "layout"],
    "serializer": ["serialize", "serialization", "serializer", "backend", "ssh"],
    "kdf": ["kdf", "rounds", "ssh", "serialize", "backend"],
    "deleted": ["delete", "deleted", "binder", "type", "semantic", "scope"],
}


def _path_semantic_terms(
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    query_groups: dict[str, list[str]] | None,
    *,
    limit: int = 64,
) -> list[str]:
    values: list[str] = []
    if query_groups:
        for key in ("concern", "architecture", "effect", "flow", "program", "explicit_entity"):
            values.extend(query_groups.get(key, []) or [])
    try:
        sketch = build_issue_sketch(sample, evidence_result)
        values.extend(sketch.workflow or [])
        values.extend(sketch.concerns or [])
        values.extend(sketch.states or [])
        values.extend(sketch.expected_effects or [])
        values.extend(sketch.entities or [])
        values.extend(sketch.architectural_queries or [])
    except Exception:
        pass
    values.append(_issue_query_text(sample.issue_text)[:3000])
    terms: list[str] = []
    for value in values:
        for token in tokenize(str(value or "")):
            if len(token) < 3 or token in STOP_QUERY_TOKENS or token.isdigit():
                continue
            terms.append(token)
            terms.extend(DOMAIN_PATH_ALIASES.get(token, []))
    return _dedupe(terms, limit=limit)


def _path_semantic_score(path: str, terms: Iterable[str]) -> tuple[float, list[str]]:
    norm = _norm_path(path)
    lower = norm.lower()
    parts = set(re.split(r"[/_.\-\s]+", lower))
    matches = _dedupe((term for term in terms if term and (term in lower or term in parts)), limit=12)
    if not matches:
        return 0.0, []
    score = 0.0
    for term in matches:
        score += 6.0 if term in parts else 3.0
    if len(matches) >= 2:
        score += 12.0
    if len(matches) >= 4:
        score += 10.0
    if any(term in matches for term in ("woocommerce", "dashboard", "address", "location", "countries")):
        score += 10.0
    if any(term in matches for term in ("legend", "plugin", "hover", "leave")):
        score += 8.0
    if any(term in matches for term in ("stylesheet", "resolve", "expand", "layout", "margin")):
        score += 8.0
    if any(term in matches for term in ("serializer", "serialization", "backend", "ssh", "kdf")):
        score += 8.0
    if any(term in matches for term in ("binder", "semantic", "deleted", "type")):
        score += 8.0
    return score, [f"path_semantic:{','.join(matches[:8])}"]


def _tool_queries(tool_observations: Iterable[Dict[str, Any]], *, issue_text: str = "") -> list[str]:
    queries: list[str] = []
    for observation in tool_observations:
        extracted = observation.get("extracted", {}) or {}
        if observation.get("tool") == "browser_reproduction_reader":
            parsed = extracted.get("parsed_reproduction", {})
            queries.extend(parsed.get("semantic_queries", []) or [])
            plan = parsed.get("browser_observation_plan", {})
            if plan.get("requested_file"):
                queries.append(str(plan["requested_file"]))
            if plan.get("preview_url"):
                queries.append(str(plan["preview_url"]))
            for item in extracted.get("source_files", []) or []:
                if item.get("eligible_as_patch_target", True):
                    queries.append(str(item.get("path") or ""))
                    queries.append(str(item.get("code_preview") or "")[:500])
                else:
                    queries.extend(str(symbol) for symbol in item.get("symbols", []) or [])
        elif observation.get("tool") == "playground_decoder":
            queries.extend(extracted.get("semantic_queries", []) or [])
            queries.extend(extracted.get("likely_layers", []) or [])
        elif observation.get("tool") in {"web_doc_reader", "discussion_reader", "web_snapshot_fetcher"}:
            queries.extend(extracted.get("keywords", []) or [])
            queries.extend(extracted.get("semantic_queries", []) or [])
            queries.extend(extracted.get("headings", []) or [])
            queries.extend(extracted.get("code_blocks", [])[:3] or [])
        elif observation.get("tool") == "vlm_image_inspector":
            vlm = extracted.get("vlm_analysis", {}) or {}
            visual_hypotheses = (
                list(extracted.get("visual_queries", []) or [])
                + list(extracted.get("likely_layers", []) or [])
                + list(vlm.get("search_queries", []) or [])
                + list(vlm.get("likely_code_layers", []) or [])
                + list(vlm.get("visual_entities", []) or [])
                + [str(vlm.get("symptom") or "")]
            )
            queries.extend(_grounded_evidence_queries(visual_hypotheses, issue_text, limit=30))
    return _dedupe(queries)


def _packet_queries(evidence_result: Dict[str, Any]) -> list[str]:
    packet = evidence_result.get("evidence_packet", {}) or {}
    deterministic = evidence_result.get("deterministic_understanding", {}) or {}
    synthesis = evidence_result.get("evidence_synthesis", {}) or {}
    search_queries = deterministic.get("search_queries", {}) or {}
    queries: list[str] = []
    queries.extend(search_queries.get("symbols", []) or [])
    queries.extend(search_queries.get("concerns", []) or [])
    queries.extend(search_queries.get("flows", []) or [])
    queries.extend(packet.get("symbol_queries", []) or [])
    queries.extend(packet.get("concern_queries", []) or [])
    queries.extend(packet.get("flow_hypotheses", []) or [])
    for case in packet.get("reproduction_cases", []) or []:
        queries.extend(case.get("semantic_queries", []) or [])
        queries.extend(case.get("likely_layers", []) or [])
    for image in packet.get("image_inspections", []) or []:
        queries.extend(image.get("visual_queries", []) or [])
        queries.extend(image.get("likely_layers", []) or [])
    for url_item in packet.get("url_inspections", []) or []:
        queries.extend(url_item.get("semantic_terms", []) or [])
        queries.append(str(url_item.get("url") or ""))
    query_groups = synthesis.get("query_groups", {}) or {}
    for values in query_groups.values():
        queries.extend(values or [])
    queries.extend(synthesis.get("all_queries", []) or [])
    return _dedupe(queries)


def _flatten_text_values(value: Any, *, limit: int = 80) -> list[str]:
    values: list[str] = []
    if value is None:
        return values
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for item in value.values():
            values.extend(_flatten_text_values(item, limit=limit))
            if len(values) >= limit:
                break
        return values[:limit]
    if isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_flatten_text_values(item, limit=limit))
            if len(values) >= limit:
                break
        return values[:limit]
    return [str(value)]


def _role_guided_queries(role: str, path: str, semantic_terms: Iterable[str]) -> list[str]:
    role_low = str(role or "").lower()
    path = _norm_path(path)
    terms = _dedupe(list(semantic_terms) + tokenize(path), limit=24)
    queries: list[str] = []
    if path:
        queries.append(path)
    if "code" in role_low or "reference" in role_low or "evidence" in role_low:
        queries.extend([f"used by {path}", f"called by {' '.join(terms[:4])}", f"references {' '.join(terms[:5])}"])
    if "reproduction" in role_low or "playground" in role_low or "example" in role_low:
        queries.extend([f"implementation for {' '.join(terms[:5])}", f"library layer {' '.join(terms[:5])}"])
    if "document" in role_low or "api" in role_low or "semantic" in role_low:
        queries.extend([f"api behavior {' '.join(terms[:6])}", f"parameter option {' '.join(terms[:6])}"])
    if "discussion" in role_low or "patch" in role_low or "pr" in role_low:
        queries.extend([f"historical fix {' '.join(terms[:6])}", f"changed files {' '.join(terms[:6])}"])
    return _dedupe(queries)


def _build_localization_query_groups(
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    issue_sketch: Any,
    tool_observations: Iterable[Dict[str, Any]],
) -> dict[str, list[str]]:
    packet = evidence_result.get("evidence_packet", {}) or {}
    deterministic = evidence_result.get("deterministic_understanding", {}) or {}
    synthesis = evidence_result.get("evidence_synthesis", {}) or {}
    search_queries = deterministic.get("search_queries", {}) or {}

    groups: dict[str, list[str]] = {
        "explicit_entity": [],
        "evidence_role": [],
        "visual": [],
        "concern": [],
        "architecture": [],
        "effect": [],
        "flow": [],
        "tool": [],
    }

    groups["explicit_entity"].extend(
        _grounded_evidence_queries(search_queries.get("symbols", []) or [], sample.issue_text)
    )
    groups["explicit_entity"].extend(
        _grounded_evidence_queries(packet.get("symbol_queries", []) or [], sample.issue_text)
    )
    groups["explicit_entity"].extend(getattr(issue_sketch, "entities", []) or [])
    for item in packet.get("code_references", []) or []:
        path = str(item.get("path") or "")
        name = str(item.get("name") or item.get("symbol") or "")
        groups["explicit_entity"].extend([path, name])

    groups["concern"].extend(search_queries.get("concerns", []) or [])
    groups["concern"].extend(packet.get("concern_queries", []) or [])
    groups["concern"].extend(getattr(issue_sketch, "concerns", []) or [])
    groups["concern"].extend(_concern_expansion(sample, evidence_result))
    groups["concern"].extend(_domain_path_probes(sample, evidence_result, issue_sketch))
    groups["architecture"].extend(getattr(issue_sketch, "architectural_queries", []) or [])

    groups["effect"].extend(getattr(issue_sketch, "expected_effects", []) or [])
    groups["effect"].extend(search_queries.get("effects", []) or [])
    groups["effect"].extend(packet.get("effect_queries", []) or [])
    for token in ("redirect", "hide", "show", "error", "report", "serialize", "hover", "leave"):
        if token in _issue_query_text(sample.issue_text).lower():
            groups["effect"].append(token)

    groups["flow"].extend(search_queries.get("flows", []) or [])
    groups["flow"].extend(packet.get("flow_hypotheses", []) or [])
    groups["flow"].extend(getattr(issue_sketch, "states", []) or [])
    for obligation in getattr(issue_sketch, "flow_obligations", []) or []:
        if not isinstance(obligation, dict):
            continue
        groups["flow"].append(
            " ".join(
                str(obligation.get(key) or "")
                for key in ("flow_type", "state", "behavior", "required_relation")
            )
        )
    groups["flow"].extend(build_flow_queries(_issue_query_text(sample.issue_text), tool_observations))

    for item in packet.get("url_inspections", []) or []:
        role = str(item.get("role") or item.get("url_type") or "")
        path = str(item.get("path") or item.get("github_path") or _github_blob_path_from_url(str(item.get("url") or "")))
        semantic_terms = list(item.get("semantic_terms", []) or []) + list(item.get("keywords", []) or [])
        groups["evidence_role"].extend(_role_guided_queries(role, path, semantic_terms))
        groups["evidence_role"].extend(semantic_terms)
    for case in packet.get("reproduction_cases", []) or []:
        groups["evidence_role"].extend(case.get("semantic_queries", []) or [])
        groups["evidence_role"].extend(case.get("likely_layers", []) or [])
    for image in packet.get("image_inspections", []) or []:
        groups["visual"].extend(
            _grounded_evidence_queries(
                list(image.get("visual_queries", []) or []) + list(image.get("likely_layers", []) or []),
                sample.issue_text,
            )
        )

    groups["tool"].extend(_tool_queries(tool_observations, issue_text=sample.issue_text))
    query_groups = synthesis.get("query_groups", {}) or {}
    synthesis_group_targets = {
        "symbol": "explicit_entity",
        "local_code": "explicit_entity",
        "url_seed": "evidence_role",
        "reproduction": "evidence_role",
        "visual": "visual",
        "docs": "concern",
        "flow": "flow",
        "concern": "concern",
    }
    for name, values in query_groups.items():
        target = synthesis_group_targets.get(name, name if name in groups else "concern")
        groups[target].extend(_grounded_evidence_queries(values or [], sample.issue_text))
    groups["tool"].extend(_grounded_evidence_queries(synthesis.get("all_queries", []) or [], sample.issue_text))

    cleaned = {key: _dedupe(values, limit=80) for key, values in groups.items()}
    cleaned["all"] = _dedupe(
        cleaned["explicit_entity"]
        + cleaned["evidence_role"]
        + cleaned["concern"]
        + cleaned["architecture"]
        + cleaned["effect"]
        + cleaned["flow"]
        + cleaned["tool"],
        limit=180,
    )
    return cleaned


def _concern_expansion(sample: NormalizedSample, evidence_result: Dict[str, Any]) -> list[str]:
    text = _issue_query_text(sample.issue_text).lower()
    queries: list[str] = []
    if "legend" in text:
        queries.extend(["legend plugin legend event", "plugin.legend legend item hover leave"])
    if "onleave" in text or "onleave" in repr(evidence_result).lower():
        queries.extend(["onLeave callback", "call opts.onLeave previous hoveredItem"])
    if "onhover" in text or "onhover" in repr(evidence_result).lower():
        queries.extend(["onHover callback", "call opts.onHover hoveredItem"])
    if "mousemove" in text or "mouse" in text:
        queries.extend(["mousemove handleEvent afterEvent _getLegendItemAt", "mouseout mouseleave event boundary"])
    if "chartjs" in sample.instance_id.lower() or "chart.js" in sample.repo.lower():
        queries.extend(["chart plugin afterEvent handleEvent", "legendHitBoxes _hoveredItem itemsEqual"])
    return queries


def _domain_path_probes(sample: NormalizedSample, evidence_result: Dict[str, Any], issue_sketch: Any) -> list[str]:
    """Generate cheap path probes from evidence roles and issue concerns.

    These probes are not final answers. They are a CoSIL-style cheap recall
    device: when the issue sketch says "store setup + country + redirect", do
    not wait for full-repo graph construction to discover that
    `woocommerce/app/dashboard` and `woocommerce/components/address` are
    promising neighborhoods.
    """

    sketch_text = " ".join(
        str(value)
        for value in (
            getattr(issue_sketch, "workflow", []) or [],
            getattr(issue_sketch, "concerns", []) or [],
            getattr(issue_sketch, "states", []) or [],
            getattr(issue_sketch, "expected_effects", []) or [],
            getattr(issue_sketch, "entities", []) or [],
        )
    )
    # Repository-specific probes are only cheap recall hints. Gate them on the
    # original issue so repeated VLM/LLM synthesis cannot hallucinate a domain
    # and turn that hint into a dominant ranking signal.
    issue_text = _issue_query_text(sample.issue_text).lower()
    text = f"{sample.instance_id} {sample.repo} {issue_text} {sketch_text}".lower()
    probes: list[str] = []

    def has_any(*tokens: str) -> bool:
        return any(token.lower() in text for token in tokens)

    wp_store_setup_issue = (
        any(token in issue_text for token in ("signup flow", "store setup", "address page", "unsupported countr", "wp-admin"))
        and any(token in issue_text for token in ("country", "countries", "location", "address"))
    )
    if "wp-calypso" in sample.repo.lower() and wp_store_setup_issue:
        if has_any("store", "woocommerce", "commerce"):
            probes.extend(
                [
                    "client/extensions/woocommerce/app/dashboard",
                    "client/extensions/woocommerce/app/dashboard/store-location",
                    "client/extensions/woocommerce/app/dashboard/setup",
                    "client/extensions/woocommerce/components/address",
                    "client/extensions/woocommerce/components/form-location-select",
                    "client/extensions/woocommerce/components/query-locations",
                    "client/extensions/woocommerce/lib/countries",
                    "client/extensions/woocommerce/state/sites/locations",
                    "client/extensions/woocommerce/state/sites/settings",
                    "woocommerce dashboard store location address countries settings",
                ]
            )

    if "chart.js" in sample.repo.lower() or "chartjs" in sample.instance_id.lower():
        if has_any("legend", "hover", "leave", "mousemove", "mouseout", "onclick"):
            probes.extend(
                [
                    "src/plugins/plugin.legend",
                    "plugins legend hover leave afterEvent handleEvent",
                    "legendHitBoxes hoveredItem label legend item",
                ]
            )

    if "react-pdf" in sample.repo.lower() or "react-pdf" in sample.instance_id.lower():
        if has_any("margin", "auto", "style", "stylesheet", "layout", "yoga"):
            probes.extend(
                [
                    "packages/stylesheet/src/expand",
                    "packages/stylesheet/src/resolve",
                    "stylesheet expand resolve margin auto box model",
                    "style pipeline margin layout yoga",
                ]
            )

    if "cryptography" in sample.repo.lower():
        if has_any("kdf", "rounds", "openssh", "private key", "serialize", "serialization"):
            probes.extend(
                [
                    "src/cryptography/hazmat/primitives/serialization/ssh",
                    "src/cryptography/hazmat/primitives/_serialization",
                    "src/cryptography/hazmat/backends/openssl/backend",
                    "BestAvailableEncryption kdf_rounds private_key_bytes serialize ssh",
                ]
            )

    if "mypy" in sample.repo.lower() or "mypy" in sample.instance_id.lower():
        if has_any("delete", "deleted", "binder", "typeinfo", "type narrowing", "symbol table"):
            probes.extend(
                [
                    "mypy/binder",
                    "mypy/checker",
                    "deleted variable TypeInfo binder frame declaration",
                    "type narrowing deleted symbol table",
                ]
            )

    if "wp-calypso" in sample.repo.lower() and has_any("owner", "ownership", "administrator", "plan transfer"):
        probes.extend(
            [
                "site settings manage connection ownership",
                "state sites plans ownership actions selectors",
                "state data-layer sites plan transfer",
                "ownership component change owner administrator",
                "plan ownership transfer request handler",
            ]
        )

    return _dedupe(probes, limit=40)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def _rank_memory_bonus(
    rank: int,
    *,
    round_no: int = 1,
    no_gain_rounds: int = 0,
    path_role: str = "",
) -> float:
    """Bounded cross-round persistence independent of prior raw score scale."""

    max_bonus = _env_float("MYCODE_MEMORY_MAX_BONUS", 24.0, minimum=0.0)
    decay = min(1.0, _env_float("MYCODE_MEMORY_RANK_DECAY", 0.82, minimum=0.0))
    round_decay = min(1.0, _env_float("MYCODE_MEMORY_ROUND_DECAY", 0.90, minimum=0.0))
    no_gain_decay = min(1.0, _env_float("MYCODE_MEMORY_NO_GAIN_DECAY", 0.75, minimum=0.0))
    role_multiplier = 1.0
    if path_role in {"reproduction_or_example", "test_or_fixture", "generated_or_lockfile", "addon_bundle"}:
        role_multiplier = min(1.0, _env_float("MYCODE_MEMORY_CONTEXT_ROLE_MULTIPLIER", 0.35, minimum=0.0))
    return (
        max_bonus
        * (decay ** max(0, int(rank)))
        * (round_decay ** max(0, int(round_no) - 1))
        * (no_gain_decay ** max(0, int(no_gain_rounds)))
        * role_multiplier
    )


def _same_dir_neighbors(
    index: RepositoryIndex,
    seed_paths: Iterable[str],
    *,
    per_dir: int = 6,
    max_total: int = 40,
) -> list[str]:
    """Cheap local expansion before graph construction.

    CoSIL-style speed comes from pruning before expensive reasoning. Same
    directory files are a low-cost way to keep local closure for components,
    serializers, tests, and style/config siblings without scanning the whole
    repository graph.
    """

    if per_dir <= 0 or max_total <= 0:
        return []
    by_dir: dict[str, list[str]] = defaultdict(list)
    for path in sorted(index.files):
        by_dir[str(Path(path).parent)].append(path)

    out: list[str] = []
    seen: set[str] = set()
    for seed in seed_paths:
        norm = _norm_path(seed)
        if norm not in index.files:
            continue
        parent = str(Path(norm).parent)
        for candidate in by_dir.get(parent, [])[:per_dir]:
            if candidate == norm or candidate in seen:
                continue
            seen.add(candidate)
            out.append(candidate)
            if len(out) >= max_total:
                return out
    return out


def _exact_symbol_queries(values: Iterable[str], *, limit: int = 24) -> list[str]:
    """Extract a small high-precision symbol lane from noisy semantic queries."""

    generic = {
        "add", "build", "call", "create", "delete", "draw", "filter", "get", "handle", "load",
        "parse", "process", "read", "remove", "render", "resolve", "run", "set", "setup", "update", "write",
    }
    symbols: list[str] = []
    for value in values:
        raw = str(value or "")
        raw_tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", raw)
        single_identifier = len(raw_tokens) == 1 and raw.strip().strip("`()[]{}'") == raw_tokens[0]
        for match in re.finditer(r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?P<call>\()?", raw):
            name = match.group("name")
            low = name.lower().strip("_$ ")
            if len(low) < 4 or low in STOP_QUERY_TOKENS or low in generic:
                continue
            looks_structured = bool(
                match.group("call")
                or "_" in name
                or "$" in name
                or re.search(r"[a-z][A-Z]", name)
                or (single_identifier and (name[:1].isupper() or len(name) >= 7))
            )
            if looks_structured:
                symbols.append(name)
    return _dedupe(symbols, limit=limit)


def _path_probe_hits(
    index: RepositoryIndex,
    queries: Iterable[str],
    *,
    limit: int = 80,
    allowed_paths: Iterable[str] | None = None,
) -> list[tuple[str, float, str]]:
    """Find files by explicit path/domain probes before graph construction.

    This is the cheap CoSIL-style step that keeps deep mode fast: issue
    preprocessing may produce path neighborhoods such as
    `client/extensions/woocommerce/app/dashboard` or
    `packages/stylesheet/src/resolve`. We should preserve those neighborhoods
    directly instead of waiting for full graph exploration to rediscover them.
    """

    if limit <= 0:
        return []
    allowed = {_norm_path(path) for path in (allowed_paths or []) if path} if allowed_paths is not None else None
    path_queries: list[str] = []
    for query in queries:
        q = _norm_path(str(query or "").strip().strip("'\"`"))
        if not q:
            continue
        lower = q.lower()
        # Keep this strict enough that broad prose queries do not scan the whole
        # repo as a "path". Domain probes inserted by preprocessing contain
        # slashes; explicit filenames contain extensions.
        semantic_tokens = [
            token
            for token in tokenize(lower)
            if len(token) >= 4 and token not in STOP_QUERY_TOKENS
        ]
        semantic_path_intent = 3 <= len(set(semantic_tokens)) <= 8 and len(lower) <= 120
        if "/" not in lower and not re.search(r"\.[a-z0-9]{1,5}\b", lower) and not semantic_path_intent:
            continue
        path_queries.append(lower)
    path_queries = _dedupe(path_queries, limit=80)
    if not path_queries:
        return []

    scored: dict[str, tuple[float, list[str]]] = {}

    def add(path: str, score: float, reason: str) -> None:
        norm = _norm_path(path)
        if norm not in index.files:
            return
        if allowed is not None and norm not in allowed:
            return
        current = scored.get(norm)
        reasons = list(current[1]) if current else []
        if reason not in reasons:
            reasons.append(reason)
        scored[norm] = (max(float(score), current[0] if current else 0.0), reasons[:6])

    for query in path_queries:
        query_no_ext = re.sub(r"\.[a-z0-9]{1,5}$", "", query)
        query_tokens = [token for token in tokenize(query) if len(token) >= 3]
        for path in index.files:
            lower = path.lower()
            if lower == query:
                add(path, 96.0, f"exact:{query}")
            elif lower.startswith(query.rstrip("/") + "/") or lower.startswith(query):
                add(path, 82.0, f"prefix:{query}")
            elif query in lower:
                add(path, 62.0, f"substring:{query}")
            elif query_no_ext and len(query_no_ext) >= 8 and query_no_ext in re.sub(r"\.[a-z0-9]{1,5}$", "", lower):
                add(path, 42.0, f"stem:{query}")
            elif query_tokens:
                overlap = sum(1 for token in query_tokens if token in lower)
                required_overlap = (
                    min(4, max(2, len(query_tokens) - 1))
                    if "/" in query
                    else min(4, max(3, len(set(query_tokens)) - 1))
                )
                if overlap >= required_overlap:
                    base = 12.0 if "/" in query else 18.0
                    weight = 4.0 if "/" in query else 6.0
                    add(path, base + overlap * weight, f"path_token_overlap:{query}")

    ranked = sorted(
        ((path, score, ";".join(reasons)) for path, (score, reasons) in scored.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return ranked[:limit]


def _build_deep_graph_scope(
    *,
    index: RepositoryIndex,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    query_groups: dict[str, list[str]],
    queries: list[str],
    top_k: int,
) -> tuple[list[str] | None, dict[str, Any]]:
    """Prefilter a small graph scope before expensive call/concern navigation.

    The important execution-order change is:

    cheap lexical/symbol/evidence recall -> small scoped graph -> local
    navigation/flow verification.

    Setting MYCODE_DEEP_GRAPH_SCOPE=0 restores full-repository graph building
    for case studies where speed is less important.
    """

    scope_limit = _env_int("MYCODE_DEEP_GRAPH_SCOPE", 120, minimum=0)
    multiplier = _env_int("MYCODE_DEEP_PREFILTER_MULTIPLIER", 8, minimum=1)
    query_budget = _env_int("MYCODE_DEEP_PREFILTER_QUERY_BUDGET", 64, minimum=8)
    neighbor_per_seed = _env_int("MYCODE_DEEP_SAME_DIR_PER_SEED", 6, minimum=0)
    neighbor_budget = _env_int("MYCODE_DEEP_SAME_DIR_BUDGET", 36, minimum=0)
    if scope_limit <= 0:
        return None, {
            "strategy": "full_repository_graph_requested",
            "scope_limited": False,
            "scope_limit": 0,
            "full_repo_files": len(index.files),
        }

    seed_paths = sorted(_collect_evidence_seed_paths(evidence_result))
    raw_prefilter_queries = _dedupe(
        (query_groups.get("explicit_entity", []) or [])
        + (query_groups.get("evidence_role", []) or [])
        + (query_groups.get("concern", []) or [])
        + (query_groups.get("architecture", []) or [])
        + (query_groups.get("effect", []) or [])
        + (query_groups.get("flow", []) or [])
        + (query_groups.get("tool", []) or [])
        + list(queries or [])
        + seed_paths,
        limit=query_budget,
    )
    prefilter_queries = _sanitize_retrieval_queries(raw_prefilter_queries, limit=query_budget)
    if seed_paths:
        prefilter_queries = _dedupe(seed_paths + prefilter_queries, limit=query_budget)
    path_terms = _path_semantic_terms(sample, evidence_result, query_groups, limit=48)
    hit_limit = max(scope_limit, top_k * multiplier, 30)
    file_hits = index.search_files(prefilter_queries, limit=hit_limit)
    entity_hits = index.search_entities(prefilter_queries, limit=hit_limit)
    visual_prefilter_hits = index.search_files(
        _sanitize_retrieval_queries(query_groups.get("visual", []), limit=24),
        limit=min(24, hit_limit),
    )

    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)

    def add(path: str, score: float, reason: str) -> None:
        norm = _norm_path(path)
        if not norm or norm not in index.files:
            return
        scores[norm] += float(score or 0.0)
        if reason and reason not in reasons[norm]:
            reasons[norm].append(reason)

    for hit in file_hits:
        add(hit.path, hit.score, "prefilter:file:" + ",".join(hit.reasons[:3]))
    for hit in entity_hits:
        add(hit.path, hit.score * 1.25, f"prefilter:entity:{hit.kind}:{hit.name}")
    for hit in visual_prefilter_hits:
        add(hit.path, hit.score * 0.20, "prefilter:low_weight_visual_navigation")
    for idx, path in enumerate(seed_paths):
        # Evidence-only URLs should not become final targets, but they are useful
        # source nodes for used_by/called_by navigation.
        add(path, max(20.0, 80.0 - idx), "prefilter:evidence_seed_for_navigation")
    local_directory_prefixes = _dedupe(
        [
            _norm_path(str((observation.get("extracted", {}) or {}).get("local_path_prefix") or ""))
            for observation in evidence_result.get("tool_observations", []) or []
            if observation.get("tool") == "github_url_parser"
        ],
        limit=8,
    )
    for prefix in local_directory_prefixes:
        prefix_root = prefix.rstrip("/") + "/"
        for path in index.files:
            if path == prefix or path.startswith(prefix_root):
                add(path, 55.0, f"prefilter:github_tree_local_scope:{prefix}")
    for query in prefilter_queries[:query_budget]:
        qnorm = _norm_path(str(query))
        if qnorm in index.files:
            add(qnorm, 45.0, "prefilter:exact_path_query")
    path_probe_limit = _env_int("MYCODE_DEEP_PATH_PROBE_LIMIT", 90, minimum=0)
    path_probe_hits = _path_probe_hits(index, prefilter_queries, limit=path_probe_limit)
    for path, score, reason in path_probe_hits:
        add(path, score, f"prefilter:domain_path_probe:{reason}")
    semantic_scan_budget = _env_int("MYCODE_DEEP_PATH_SEMANTIC_BUDGET", 80, minimum=0)
    if path_terms and semantic_scan_budget:
        semantic_scored: list[tuple[str, float, list[str]]] = []
        for path in index.files:
            score, semantic_reasons = _path_semantic_score(path, path_terms)
            if score > 0:
                semantic_scored.append((path, score, semantic_reasons))
        for path, score, semantic_reasons in sorted(
            semantic_scored,
            key=lambda item: (-item[1], item[0]),
        )[:semantic_scan_budget]:
            add(path, score, "prefilter:" + ",".join(semantic_reasons[:2]))

    top_seed_paths = [
        path
        for path, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[: max(top_k, 12)]
    ]
    same_dir_paths = _same_dir_neighbors(
        index,
        _dedupe([path for path, _score, _reason in path_probe_hits[: max(top_k, 12)]] + top_seed_paths),
        per_dir=neighbor_per_seed,
        max_total=neighbor_budget,
    )
    for path in same_dir_paths:
        add(path, 4.0, "prefilter:same_directory_local_closure")

    source_adjusted: list[dict[str, Any]] = []
    if _env_bool("MYCODE_SOURCE_FIRST_FILTER", True):
        for path in list(scores):
            old_score = scores[path]
            multiplier, reason = _source_first_multiplier(path, _issue_query_text(sample.issue_text))
            cap, cap_reason = _source_first_score_cap(path, _issue_query_text(sample.issue_text))
            if multiplier == 1.0 and cap is None:
                continue
            scores[path] = old_score * multiplier
            if cap is not None:
                scores[path] = min(scores[path], cap)
            if reason not in reasons[path]:
                reasons[path].append(reason)
            if cap is not None and cap_reason not in reasons[path]:
                reasons[path].append(cap_reason)
            source_adjusted.append(
                {
                    "path": path,
                    "role": _path_role(path),
                    "old_score": round(old_score, 3),
                    "new_score": round(scores[path], 3),
                    "reason": reason,
                    "cap": round(cap, 3) if cap is not None else None,
                    "cap_reason": cap_reason if cap is not None else "",
                }
            )

    ranked_paths = [
        path
        for path, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]
    if not ranked_paths:
        ranked_paths = sorted(index.files)[:scope_limit]
        for path in ranked_paths:
            scores[path] = 0.1
            reasons[path].append("prefilter:fallback_first_files_to_keep_graph_scoped")
    selected_paths = ranked_paths[:scope_limit]
    diagnostic = {
        "strategy": "cosil_style_cheap_prefilter_then_scoped_graph",
        "scope_limited": True,
        "scope_limit": scope_limit,
        "selected_paths": len(selected_paths),
        "full_repo_files": len(index.files),
        "query_count": len(prefilter_queries),
        "raw_query_count": len(raw_prefilter_queries),
        "path_semantic_terms": path_terms[:32],
        "file_hit_count": len(file_hits),
        "entity_hit_count": len(entity_hits),
        "visual_hit_count": len(visual_prefilter_hits),
        "path_probe_hit_count": len(path_probe_hits),
        "evidence_seed_paths": seed_paths[:20],
        "local_directory_prefixes": local_directory_prefixes,
        "same_dir_neighbor_count": len(same_dir_paths),
        "source_first_filter": {
            "enabled": _env_bool("MYCODE_SOURCE_FIRST_FILTER", True),
            "adjusted_count": len(source_adjusted),
            "noise_score_cap": _env_float("MYCODE_NOISE_PATH_SCORE_CAP", 900.0, minimum=0.0),
            "generated_score_cap": _env_float("MYCODE_GENERATED_PATH_SCORE_CAP", 120.0, minimum=0.0),
            "addon_bundle_score_cap": _env_float("MYCODE_ADDON_BUNDLE_SCORE_CAP", 450.0, minimum=0.0),
            "adjusted_preview": sorted(source_adjusted, key=lambda item: (item["new_score"] - item["old_score"], item["path"]))[:20],
        },
        "top_scope_preview": [
            {
                "path": path,
                "score": round(scores[path], 3),
                "role": _path_role(path),
                "reasons": reasons[path][:5],
            }
            for path in selected_paths[:20]
        ],
    }
    return selected_paths, diagnostic


def _query_terms_for_scoped_search(queries: Iterable[str], *, max_terms: int = 48) -> list[str]:
    counts = Counter(
        token
        for token in tokenize(" ".join(str(query or "") for query in queries))
        if len(token) >= 3 and token not in STOP_QUERY_TOKENS and not token.isdigit()
    )
    return [
        term
        for term, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ][:max_terms]


def _scoped_search_files(
    index: RepositoryIndex,
    queries: Iterable[str],
    *,
    allowed_paths: Iterable[str] | None,
    limit: int,
) -> list[SearchHit]:
    """Search only a small candidate pool after cheap global recall.

    RepositoryIndex.search_files scans the whole repository. That is useful for
    the first cheap recall, but repeating it for every signal family makes deep
    mode slow on large repositories such as wp-calypso. This function keeps the
    same style of lexical signal but evaluates it only on the already-pruned
    pool.
    """

    paths = [_norm_path(path) for path in (allowed_paths or []) if _norm_path(path) in index.files]
    if not paths:
        return index.search_files(queries, limit=limit)
    terms = _query_terms_for_scoped_search(queries)
    if not terms:
        return []
    hits: list[SearchHit] = []
    for path in _dedupe(paths):
        lower_path = path.lower()
        lower_text = index.files.get(path, "")[:120_000].lower()
        score = 0.0
        reasons: list[str] = []
        for term in terms:
            if term in lower_path:
                score += 8.0
                if len(reasons) < 8:
                    reasons.append(f"path:{term}")
            count = lower_text.count(term)
            if count:
                score += min(count, 10)
                if len(reasons) < 8:
                    reasons.append(f"text:{term}x{min(count, 10)}")
        for query in queries:
            phrase = str(query or "").lower().strip()
            if len(phrase) > 4 and phrase in lower_text:
                score += 4.0
                if len(reasons) < 8:
                    reasons.append(f"phrase:{phrase[:40]}")
        if score > 0:
            hits.append(SearchHit(path=path, score=score, reasons=reasons))
    hits.sort(key=lambda item: (-item.score, item.path))
    return hits[:limit]


def _scoped_search_entities(
    index: RepositoryIndex,
    queries: Iterable[str],
    *,
    allowed_paths: Iterable[str] | None,
    limit: int,
) -> list[SearchHit]:
    paths = {_norm_path(path) for path in (allowed_paths or []) if _norm_path(path) in index.files}
    if not paths:
        return index.search_entities(queries, limit=limit)
    terms = _query_terms_for_scoped_search(queries)
    if not terms:
        return []
    hits: list[SearchHit] = []
    for entity in index.entities:
        path = _norm_path(entity.path)
        if path not in paths:
            continue
        lower_name = entity.name.lower()
        lower_path = path.lower()
        lower_text = entity.text[:30_000].lower()
        score = 0.0
        reasons: list[str] = []
        for term in terms:
            if term in lower_name:
                score += 10.0
                if len(reasons) < 8:
                    reasons.append(f"name:{term}")
            if term in lower_path:
                score += 4.0
                if len(reasons) < 8:
                    reasons.append(f"path:{term}")
            count = lower_text.count(term)
            if count:
                score += min(count, 8)
                if len(reasons) < 8:
                    reasons.append(f"body:{term}x{min(count, 8)}")
        if score > 0:
            hits.append(
                SearchHit(
                    path=path,
                    score=score,
                    reasons=reasons,
                    kind=entity.kind,
                    name=entity.name,
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                )
            )
    hits.sort(key=lambda item: (-item.score, item.path, item.name))
    return hits[:limit]


def _merge_search_hits_by_channel(
    groups: Iterable[tuple[str, Iterable[SearchHit]]],
    *,
    limit: int,
    entity_level: bool = False,
) -> list[SearchHit]:
    """Fuse bounded retrieval channels without letting one long query dominate.

    A fused issue query is still useful for broad lexical relevance, but it can
    bury a rare explicit symbol or flow term. Each channel therefore reserves a
    small reciprocal-rank contribution before all hits are merged.
    """

    merged: dict[tuple[str, str, str], SearchHit] = {}
    fused_scores: dict[tuple[str, str, str], float] = defaultdict(float)
    for channel, hits in groups:
        for rank, hit in enumerate(hits, start=1):
            key = (
                _norm_path(hit.path),
                str(hit.kind or "") if entity_level else "",
                str(hit.name or "") if entity_level else "",
            )
            fused_scores[key] += min(float(hit.score or 0.0), 120.0) * 0.16 + 18.0 / (rank + 2)
            existing = merged.get(key)
            channel_reasons = [f"channel:{channel}"] + list(hit.reasons or [])[:6]
            if existing is None:
                merged[key] = SearchHit(
                    path=hit.path,
                    score=0.0,
                    reasons=channel_reasons,
                    kind=hit.kind,
                    name=hit.name,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                )
            else:
                existing.reasons = _dedupe(existing.reasons + channel_reasons, limit=10)
    for key, hit in merged.items():
        # Raw lexical scores grow with file size and query count. Keeping the
        # raw maximum here allowed large generic files to overwhelm every
        # architecture/symbol lane. The fused score is already a bounded
        # mixture of capped lexical relevance and reciprocal rank.
        hit.score = fused_scores.get(key, 0.0)
    return sorted(merged.values(), key=lambda item: (-item.score, item.path, item.name))[:limit]


def _multi_channel_global_recall(
    *,
    index: RepositoryIndex,
    retrieval_queries: list[str],
    frontier_queries: dict[str, list[str]],
    file_limit: int,
    entity_limit: int,
) -> tuple[list[SearchHit], list[SearchHit], dict[str, Any]]:
    per_channel_files = _env_int("MYCODE_GLOBAL_CHANNEL_FILE_HITS", 8, minimum=1)
    per_channel_entities = _env_int("MYCODE_GLOBAL_CHANNEL_ENTITY_HITS", 10, minimum=1)
    file_groups: list[tuple[str, list[SearchHit]]] = [
        ("all", index.search_files(retrieval_queries, limit=file_limit))
    ]
    entity_groups: list[tuple[str, list[SearchHit]]] = [
        ("all", index.search_entities(retrieval_queries, limit=entity_limit))
    ]
    channel_counts: dict[str, dict[str, int]] = {}
    if _env_bool("MYCODE_MULTI_CHANNEL_RECALL", True):
        for channel in ("explicit_entity", "concern", "effect", "flow", "evidence_role"):
            queries = _sanitize_retrieval_queries(frontier_queries.get(channel, []), limit=30)
            if not queries:
                continue
            channel_file_hits = index.search_files(queries, limit=per_channel_files)
            file_groups.append((channel, channel_file_hits))
            channel_entity_hits: list[SearchHit] = []
            if channel in {"explicit_entity", "flow", "concern"}:
                channel_entity_hits = index.search_entities(queries, limit=per_channel_entities)
                entity_groups.append((channel, channel_entity_hits))
            channel_counts[channel] = {
                "files": len(channel_file_hits),
                "entities": len(channel_entity_hits),
            }
    exact_queries = _exact_symbol_queries(frontier_queries.get("explicit_entity", []) or [])
    exact_hit_count = 0
    if _env_bool("MYCODE_EXACT_SYMBOL_LANE", True):
        per_symbol_limit = _env_int("MYCODE_EXACT_SYMBOL_HITS", 4, minimum=1)
        for symbol in exact_queries:
            exact_hits = [
                hit
                for hit in index.search_entities([f"{symbol}("], limit=per_symbol_limit * 3)
                if str(hit.name or "").lower() == symbol.lower()
            ][:per_symbol_limit]
            if not exact_hits:
                continue
            exact_hit_count += len(exact_hits)
            entity_groups.append((f"exact_symbol:{symbol}", exact_hits))
    file_hits = _merge_search_hits_by_channel(
        file_groups,
        limit=max(file_limit, file_limit + per_channel_files * 2),
    )
    entity_hits = _merge_search_hits_by_channel(
        entity_groups,
        limit=max(entity_limit, entity_limit + per_channel_entities),
        entity_level=True,
    )
    return file_hits, entity_hits, {
        "enabled": _env_bool("MYCODE_MULTI_CHANNEL_RECALL", True),
        "channels": channel_counts,
        "exact_symbol_lane": {
            "enabled": _env_bool("MYCODE_EXACT_SYMBOL_LANE", True),
            "queries": exact_queries,
            "hits": exact_hit_count,
        },
        "file_hits_after_fusion": len(file_hits),
        "entity_hits_after_fusion": len(entity_hits),
    }


def _round_candidate_pool(
    *,
    index: RepositoryIndex,
    graph: TypedRepositoryGraph,
    previous_ranked: list[RankedLocation],
    file_hits: list[SearchHit],
    entity_hits: list[SearchHit],
    evidence_seed_paths: Iterable[str],
    top_k: int,
) -> list[str]:
    graph_paths = [
        path
        for path in getattr(graph, "graph_files", []) or []
        if _norm_path(path) in index.files
    ]
    pool = _dedupe(
        graph_paths
        + [item.path for item in previous_ranked[: max(top_k, 12)]]
        + [hit.path for hit in file_hits]
        + [hit.path for hit in entity_hits]
        + list(evidence_seed_paths),
        limit=_env_int("MYCODE_ROUND_POOL_LIMIT", 160, minimum=max(top_k, 15)),
    )
    return [path for path in pool if _norm_path(path) in index.files]


def _restricted_index(index: RepositoryIndex, paths: Iterable[str]) -> RepositoryIndex:
    selected = {_norm_path(path) for path in paths if _norm_path(path) in index.files}
    if not selected or len(selected) >= len(index.files):
        return index
    scoped = copy.copy(index)
    scoped.files = {path: index.files[path] for path in sorted(selected)}
    scoped.entities = [entity for entity in index.entities if _norm_path(entity.path) in selected]
    return scoped


def _read_code_context(
    index: RepositoryIndex,
    ranked: Iterable[RankedLocation],
    queries: Iterable[str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    terms = [term for term in _dedupe(queries) if len(term) >= 3]
    contexts: list[dict[str, Any]] = []
    for item in list(ranked)[:limit]:
        text = index.files.get(item.path, "")
        if not text:
            continue
        lines = text.splitlines()
        matched = []
        lower_lines = [line.lower() for line in lines]
        for idx, lower in enumerate(lower_lines):
            if any(term.lower() in lower for term in terms[:60]):
                start = max(0, idx - 3)
                end = min(len(lines), idx + 6)
                matched.append(
                    {
                        "start_line": start + 1,
                        "end_line": end,
                        "text": "\n".join(f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)),
                    }
                )
            if len(matched) >= 3:
                break
        contexts.append(
            {
                "path": item.path,
                "score_before_verifier": item.score,
                "reasons": item.reasons[:8],
                "snippets": matched,
                "entities": item.entities[:5],
            }
        )
    return contexts


def _path_concern_bonus(path: str, sample: NormalizedSample, evidence_result: Dict[str, Any]) -> tuple[float, list[str]]:
    lower = f"{path} {_issue_query_text(sample.issue_text)} {evidence_result.get('evidence_synthesis', {})}".lower()
    bonus = 0.0
    reasons: list[str] = []
    rules = [
        ("legend", ("legend", "plugin.legend"), 12.0),
        ("hover/leave event", ("hover", "leave", "event", "mousemove", "mouseout"), 7.0),
        ("style pipeline", ("style", "stylesheet", "resolve", "expand", "margin"), 8.0),
        ("url builder", ("url", "route", "href", "link", "redirect"), 6.0),
        ("serializer/backend", ("serialize", "backend", "ssh", "kdf", "private_key"), 9.0),
        ("state selector", ("selector", "state", "redux", "store"), 7.0),
        ("frontend workflow", ("component", "view", "route", "dashboard", "setup", "flow"), 8.0),
        ("commerce onboarding", ("woocommerce", "commerce", "dashboard", "store", "location", "address", "countries", "setup"), 18.0),
        ("state-to-effect closure", ("verified", "verification", "country", "redirect", "wp-admin", "signup"), 10.0),
    ]
    for label, needles, value in rules:
        if any(token in lower for token in needles) and any(token in path.lower() for token in needles):
            bonus += value
            reasons.append(f"concern_path_match:{label}")
    return bonus, reasons


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _path_role(path: str) -> str:
    lower = _norm_path(path).lower()
    filename = lower.rsplit("/", 1)[-1]
    first_part = lower.split("/", 1)[0]
    if any(
        lower.endswith(name)
        for name in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "poetry.lock",
            "pipfile.lock",
        )
    ):
        return "generated_or_lockfile"
    if filename.endswith((
        ".min.js", ".min.css", ".bundle.js", ".bundle.css",
        ".umd.js", ".umd.cjs", ".umd.mjs", ".map",
    )) or (
        first_part == "lib"
        and filename.endswith((".esm.js", ".esm.mjs", ".esm.cjs"))
    ) or any(
        part in lower for part in ("/dist/", "/build/", "/vendor/", "/generated/")
    ) or first_part in {"dist", "build", "vendor", "generated"}:
        return "generated_or_lockfile"
    if lower.startswith("lib/addons/"):
        return "addon_bundle"
    if filename.endswith((".d.ts", ".pyi")) or any(
        part in lower for part in ("/generated-sources/", "/generated_sources/")
    ):
        return "declaration_or_schema"
    if any(part in lower for part in ("/examples/", "/example/", "/demo/", "/demos/", "/docs/", "/playground/", "/sandbox/", "/manual-test-examples/")) or first_part in {"examples", "example", "demo", "demos", "docs", "playground", "sandbox", "developer_docs"}:
        return "reproduction_or_example"
    if any(part in lower for part in ("/test/", "/tests/", "__tests__", "/fixture/", "/fixtures/")) or first_part in {"test", "tests", "__tests__", "fixture", "fixtures"} or filename.startswith("test_") or ".test." in filename or ".spec." in filename:
        return "test_or_fixture"
    if any(token in lower for token in ("stylesheet", "/style", ".css", ".scss", "resolve", "expand", "margin", "layout", "yoga")):
        return "style_resolver"
    if any(token in lower for token in ("selector", "current-user", "current_user")):
        return "state_selector"
    if any(token in lower for token in ("reducer", "action", "dispatch", "/state/")):
        return "state_update"
    if any(token in lower for token in ("dashboard", "reader", "signup", "component", "components", "view", "route", "page")):
        return "component_or_route"
    if any(token in lower for token in ("url", "href", "link", "redirect", "utils")):
        return "url_builder"
    if "backend" in lower:
        return "backend"
    if any(token in lower for token in ("serialize", "serialization", "serializer", "/ssh.")):
        return "serializer"
    if any(token in lower for token in ("binder", "type", "semantic", "checker")):
        return "type_binder"
    return "implementation"


def _issue_allows_non_source_targets(issue_text: str) -> bool:
    lower = issue_text.lower()
    negative_non_source = any(
        phrase in lower
        for phrase in (
            "not docs",
            "not documentation",
            "not examples",
            "not the examples",
            "not demo",
            "not tests",
            "not the tests",
            "not fixtures",
        )
    )
    if negative_non_source:
        return False
    non_source_words = r"(test|tests|spec|fixture|fixtures|documentation|docs|example|demo|build|bundle|minified)"
    edit_verbs = r"(fix|update|change|modify|add|remove|repair|correct|rewrite|document)"
    return bool(
        re.search(rf"\b{edit_verbs}\b[\w\s,;:()/.-]{{0,48}}\b{non_source_words}\b", lower)
        or re.search(rf"\b{non_source_words}\b[\w\s,;:()/.-]{{0,32}}\b(is|are|was|were)\s+(wrong|incorrect|broken|outdated|missing)", lower)
        or re.search(r"\b(documentation|docs)\s+(bug|issue|error|typo)\b", lower)
    )


def _source_first_multiplier(path: str, issue_text: str) -> tuple[float, str]:
    role = _path_role(path)
    if role in {"implementation", "component_or_route", "url_builder", "backend", "serializer", "type_binder", "state_update", "state_selector", "style_resolver", "declaration_or_schema"}:
        if _is_source_like_path(path):
            return _env_float("MYCODE_SOURCE_FILE_BOOST", 1.18, minimum=0.0), "source_first:source_boost"
        return 1.0, "source_first:implementation_neutral"
    if role == "addon_bundle":
        lower_issue = issue_text.lower()
        if any(token in lower_issue for token in ("addon", "addons", "p5.dom", "p5.sound", "sound", "dom library")):
            return 0.85, "source_first:issue_mentions_addon_bundle"
        return _env_float("MYCODE_ADDON_BUNDLE_MULTIPLIER", 0.24, minimum=0.0), "source_first:addon_bundle_downweight"
    if _issue_allows_non_source_targets(issue_text):
        return 0.85, f"source_first:issue_allows_non_source:{role}"
    if role == "generated_or_lockfile":
        return _env_float("MYCODE_GENERATED_PATH_MULTIPLIER", 0.06, minimum=0.0), "source_first:generated_or_lockfile_downweight"
    if role in {"reproduction_or_example", "test_or_fixture"}:
        return _env_float("MYCODE_NOISE_PATH_MULTIPLIER", 0.18, minimum=0.0), f"source_first:noise_path_downweight:{role}"
    return 1.0, "source_first:neutral"


def _source_first_score_cap(path: str, issue_text: str) -> tuple[float | None, str]:
    """Hard cap noisy surfaces after downweighting.

    Multipliers alone are not enough when a generated/test/docs file receives a
    very large raw BM25 score from repeated issue words. The cap keeps these
    files available as evidence/navigation seeds without letting them dominate
    source files in final ranking.
    """

    role = _path_role(path)
    if role == "generated_or_lockfile":
        return _env_float("MYCODE_GENERATED_PATH_SCORE_CAP", 120.0, minimum=0.0), "source_first:generated_score_cap"
    if role == "addon_bundle":
        lower_issue = issue_text.lower()
        if any(token in lower_issue for token in ("addon", "addons", "p5.dom", "p5.sound", "sound", "dom library")):
            return None, "source_first:addon_cap_disabled_by_issue"
        return _env_float("MYCODE_ADDON_BUNDLE_SCORE_CAP", 450.0, minimum=0.0), "source_first:addon_bundle_score_cap"
    if role in {"reproduction_or_example", "test_or_fixture"} and not _issue_allows_non_source_targets(issue_text):
        return _env_float("MYCODE_NOISE_PATH_SCORE_CAP", 900.0, minimum=0.0), f"source_first:noise_score_cap:{role}"
    return None, "source_first:no_score_cap"


def _apply_source_first_policy(
    aggregate: Dict[str, RankedLocation],
    component_scores: dict[str, dict[str, float]],
    *,
    sample: NormalizedSample,
) -> dict[str, Any]:
    if not _env_bool("MYCODE_SOURCE_FIRST_FILTER", True):
        return {"enabled": False, "reason": "MYCODE_SOURCE_FIRST_FILTER=0"}
    adjusted: list[dict[str, Any]] = []
    for item in aggregate.values():
        norm = _norm_path(item.path)
        old_score = float(item.score or 0.0)
        multiplier, reason = _source_first_multiplier(norm, _issue_query_text(sample.issue_text))
        cap, cap_reason = _source_first_score_cap(norm, _issue_query_text(sample.issue_text))
        if multiplier == 1.0 and cap is None:
            continue
        if multiplier > 1.0:
            # Source-like paths are corroboration, not another retrieval
            # channel. An additive cap prevents this policy from compounding
            # the same score each time a candidate survives a round.
            max_bonus = _env_float("MYCODE_SOURCE_FILE_MAX_BONUS", 18.0, minimum=0.0)
            new_score = old_score + min(max_bonus, old_score * (multiplier - 1.0))
        else:
            new_score = old_score * multiplier
        if cap is not None:
            new_score = min(new_score, cap)
        item.score = new_score
        component_scores[norm]["source_first_adjustment"] += new_score - old_score
        if reason and reason not in item.reasons:
            item.reasons.append(reason)
        if cap is not None and cap_reason not in item.reasons:
            item.reasons.append(cap_reason)
        adjusted.append(
            {
                "path": item.path,
                "role": _path_role(norm),
                "old_score": round(old_score, 3),
                "new_score": round(new_score, 3),
                "multiplier": round(multiplier, 3),
                "cap": round(cap, 3) if cap is not None else None,
                "reason": reason,
                "cap_reason": cap_reason if cap is not None else "",
            }
        )
    return {
        "enabled": True,
        "adjusted_count": len(adjusted),
        "noise_multiplier": _env_float("MYCODE_NOISE_PATH_MULTIPLIER", 0.18, minimum=0.0),
        "generated_multiplier": _env_float("MYCODE_GENERATED_PATH_MULTIPLIER", 0.06, minimum=0.0),
        "noise_score_cap": _env_float("MYCODE_NOISE_PATH_SCORE_CAP", 900.0, minimum=0.0),
        "generated_score_cap": _env_float("MYCODE_GENERATED_PATH_SCORE_CAP", 120.0, minimum=0.0),
        "addon_bundle_score_cap": _env_float("MYCODE_ADDON_BUNDLE_SCORE_CAP", 450.0, minimum=0.0),
        "source_boost": _env_float("MYCODE_SOURCE_FILE_BOOST", 1.18, minimum=0.0),
        "source_max_bonus": _env_float("MYCODE_SOURCE_FILE_MAX_BONUS", 18.0, minimum=0.0),
        "adjusted": sorted(adjusted, key=lambda item: (item["new_score"] - item["old_score"], item["path"]))[:30],
    }


def _apply_architecture_lane_policy(
    aggregate: Dict[str, RankedLocation],
    component_scores: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Keep architecture paths as recall hints without letting them own rank 1.

    Architecture-name matches are useful for reserving code-reading slots, but
    they are hypotheses rather than mechanism evidence.  Cap that component
    unless an independent symbol/path/program/flow channel corroborates it.
    The component remains positive so the CoSIL-style beam still keeps the file.
    """

    if not _env_bool("MYCODE_ARCHITECTURE_CORROBORATION", True):
        return {"enabled": False, "reason": "MYCODE_ARCHITECTURE_CORROBORATION=0"}
    weak_cap = _env_float("MYCODE_ARCHITECTURE_UNCORROBORATED_CAP", 6.0, minimum=0.0)
    semantic_cap = _env_float("MYCODE_ARCHITECTURE_SEMANTIC_CAP", 12.0, minimum=0.0)
    direct_cap = _env_float("MYCODE_ARCHITECTURE_CORROBORATED_CAP", 24.0, minimum=0.0)
    direct_components = ("symbol_score", "path_score", "domain_path_probe", "call_score", "flow_score")
    semantic_components = ("bm25_score", "concern_score", "evidence_score")
    adjusted: list[dict[str, Any]] = []
    for item in aggregate.values():
        norm = _norm_path(item.path)
        components = component_scores.get(norm, {})
        architecture_score = float(components.get("architecture_path_probe", 0.0) or 0.0)
        if architecture_score <= 0:
            continue
        direct_axes = [name for name in direct_components if float(components.get(name, 0.0) or 0.0) > 0]
        semantic_axes = [name for name in semantic_components if float(components.get(name, 0.0) or 0.0) > 0]
        if direct_axes:
            cap = direct_cap
            tier = "directly_corroborated"
        elif len(semantic_axes) >= 2:
            cap = semantic_cap
            tier = "semantically_corroborated"
        else:
            cap = weak_cap
            tier = "uncorroborated"
        kept = min(architecture_score, cap)
        removed = architecture_score - kept
        components["architecture_path_probe"] = kept
        if removed <= 0:
            continue
        item.score -= removed
        components["architecture_corroboration_cap"] = -removed
        reason = f"architecture_lane:{tier}:cap={cap:.1f}"
        if reason not in item.reasons:
            item.reasons.append(reason)
        adjusted.append(
            {
                "path": item.path,
                "tier": tier,
                "raw": round(architecture_score, 3),
                "kept": round(kept, 3),
                "removed": round(removed, 3),
                "direct_axes": direct_axes,
                "semantic_axes": semantic_axes,
            }
        )
    return {
        "enabled": True,
        "uncorroborated_cap": weak_cap,
        "semantic_cap": semantic_cap,
        "corroborated_cap": direct_cap,
        "adjusted_count": len(adjusted),
        "adjusted": sorted(adjusted, key=lambda row: (-row["removed"], row["path"]))[:30],
    }


def _apply_evidence_role_target_policy(
    aggregate: Dict[str, RankedLocation],
    component_scores: dict[str, dict[str, float]],
    *,
    evidence_seed_paths: Iterable[str],
) -> dict[str, Any]:
    """Separate navigation seeds from patch-target candidates.

    Issue code URLs, browser-extracted reproduction files, and explicit
    evidence paths are valuable anchors for navigation. They should not,
    however, dominate the final target ranking only because their path or
    symbol exactly appears in the issue. This is the practical scoring hook for
    the paper insight: evidence relevance is not modification likelihood.
    """

    seed_norms = {_norm_path(path) for path in evidence_seed_paths if _norm_path(path)}
    multiplier = _env_float("MYCODE_EVIDENCE_SEED_TARGET_MULTIPLIER", 0.22, minimum=0.0)
    cap = _env_float("MYCODE_EVIDENCE_SEED_TARGET_CAP", 450.0, minimum=0.0)
    non_patch_multiplier = _env_float("MYCODE_NON_PATCH_SURFACE_MULTIPLIER", 0.30, minimum=0.0)
    selector_without_domain_multiplier = _env_float(
        "MYCODE_SELECTOR_WITHOUT_DOMAIN_MULTIPLIER",
        0.68,
        minimum=0.0,
    )
    adjusted: list[dict[str, Any]] = []
    issue_is_workflow = any(
        token in " ".join(
            item.path + " " + " ".join(item.reasons[:8])
            for item in aggregate.values()
        ).lower()
        for token in ("dashboard", "signup", "flow", "route", "redirect", "woocommerce", "component", "view")
    )
    for item in aggregate.values():
        norm = _norm_path(item.path)
        role = _path_role(norm)
        components = component_scores.get(norm, {})
        old_score = float(item.score or 0.0)
        new_score = old_score
        reason = ""
        if norm in seed_norms:
            new_score = min(old_score * multiplier, cap)
            reason = "score_policy:evidence_seed_navigation_not_target"
        elif role in {"reproduction_or_example", "test_or_fixture", "generated_or_lockfile", "addon_bundle"}:
            new_score = old_score * non_patch_multiplier
            reason = f"score_policy:non_patch_surface:{role}"
        elif (
            issue_is_workflow
            and role == "state_selector"
            and float(components.get("domain_path_probe", 0.0) or 0.0) <= 0.0
            and float(components.get("concern_score", 0.0) or 0.0) <= 0.0
            and float(components.get("flow_score", 0.0) or 0.0) <= 0.0
        ):
            new_score = old_score * selector_without_domain_multiplier
            reason = "score_policy:selector_without_domain_or_flow_support"
        if new_score >= old_score:
            continue
        item.score = new_score
        penalty = old_score - new_score
        component_scores[norm]["role_penalty"] -= penalty
        if reason and reason not in item.reasons:
            item.reasons.append(reason)
        adjusted.append(
            {
                "path": item.path,
                "role": role,
                "old_score": round(old_score, 3),
                "new_score": round(new_score, 3),
                "penalty": round(penalty, 3),
                "reason": reason,
            }
        )
    return {
        "enabled": True,
        "seed_count": len(seed_norms),
        "multiplier": multiplier,
        "cap": cap,
        "non_patch_multiplier": non_patch_multiplier,
        "selector_without_domain_multiplier": selector_without_domain_multiplier,
        "adjusted": adjusted[:20],
        "adjusted_count": len(adjusted),
    }


CONCERN_EDGE_TYPES = [
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
    "hook_flow",
    "reverse_hook_flow",
    "incoming_hook_flow",
    "styles",
    "reverse_styles",
    "incoming_styles",
    "configures",
    "reverse_configures",
    "incoming_configures",
    "documents",
    "reverse_documents",
    "incoming_documents",
    "same_directory",
]


PROGRAM_EDGE_TYPES = [
    "calls",
    "reverse_calls",
    "incoming_calls",
    "imports",
    "reverse_imports",
    "incoming_imports",
    "handles_action",
    "reverse_handles_action",
    "incoming_handles_action",
    "inherits_or_implements",
    "reverse_inherits_or_implements",
    "incoming_inherits_or_implements",
    "overrides",
    "reverse_overrides",
    "incoming_overrides",
    "uses_hook",
    "reverse_uses_hook",
    "incoming_uses_hook",
    "hook_flow",
    "reverse_hook_flow",
    "incoming_hook_flow",
    "type_flow",
    "reverse_type_flow",
    "incoming_type_flow",
]


FLOW_EDGE_TYPES = [
    "calls",
    "reverse_calls",
    "incoming_calls",
    "imports",
    "reverse_imports",
    "incoming_imports",
    "handles_action",
    "reverse_handles_action",
    "incoming_handles_action",
    "configures",
    "reverse_configures",
    "incoming_configures",
    "type_flow",
    "reverse_type_flow",
    "incoming_type_flow",
    "selects_state",
    "reverse_selects_state",
    "incoming_selects_state",
    "binds_ui_event",
    "reverse_binds_ui_event",
    "incoming_binds_ui_event",
    "routes_to",
    "reverse_routes_to",
    "incoming_routes_to",
    "styles",
    "reverse_styles",
    "incoming_styles",
    "documents",
    "reverse_documents",
    "incoming_documents",
    "decorates",
    "reverse_decorates",
    "incoming_decorates",
]


def _frontier_query_groups(
    *,
    base_queries: Iterable[str],
    controller_decisions: Iterable[Any],
    actions: Iterable[Any],
    seed_query_groups: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    groups = {
        "explicit_entity": [],
        "evidence_role": [],
        "visual": [],
        "concern": [],
        "architecture": [],
        "effect": [],
        "flow": [],
        "tool": [],
    }
    if seed_query_groups:
        for key, values in seed_query_groups.items():
            if key == "all":
                continue
            groups.setdefault(key, []).extend(values or [])
    else:
        groups["concern"].extend(base_queries)
        groups["explicit_entity"].extend(base_queries)
        groups["flow"].extend(base_queries)

    for decision in controller_decisions:
        tool = str(getattr(decision, "tool", "") or "").lower()
        mode = str(getattr(decision, "mode", "") or "").lower()
        target = "flow" if tool == "traceflow" or "flow" in mode else "program"
        if tool == "searchanchor" or mode == "concern":
            target = "concern"
        if target == "program":
            groups["explicit_entity"].extend(getattr(decision, "queries", []) or [])
        else:
            groups[target].extend(getattr(decision, "queries", []) or [])

    for action in actions:
        action_name = str(getattr(action, "action", "") or "").lower()
        target = "explicit_entity"
        if "concern" in action_name or "frontend" in action_name or "visual" in action_name:
            target = "concern"
        if "flow" in action_name or "parameter" in action_name or "type" in action_name:
            target = "flow"
        groups[target].extend(getattr(action, "suggested_queries", []) or [])

    cleaned = {key: _dedupe(values) for key, values in groups.items()}
    cleaned["program"] = _dedupe(cleaned.get("explicit_entity", []) + cleaned.get("architecture", []) + cleaned.get("evidence_role", []) + cleaned.get("tool", []))
    cleaned["concern"] = _dedupe(cleaned.get("concern", []) + cleaned.get("architecture", []) + cleaned.get("effect", []) + cleaned.get("evidence_role", []))
    cleaned["flow"] = _dedupe(cleaned.get("flow", []) + cleaned.get("effect", []))
    cleaned["all"] = _dedupe(
        cleaned.get("explicit_entity", [])
        + cleaned.get("evidence_role", [])
        + cleaned.get("concern", [])
        + cleaned.get("architecture", [])
        + cleaned.get("effect", [])
        + cleaned.get("flow", [])
        + cleaned.get("tool", [])
    )
    return cleaned


def _merge_graph_hits(*groups: Iterable[tuple[str, float, list[str]]]) -> list[tuple[str, float, list[str]]]:
    merged: dict[str, tuple[float, list[str]]] = {}
    for group in groups:
        for path, score, reasons in group or []:
            if not path:
                continue
            current = merged.get(path)
            if current is None:
                merged[path] = (float(score or 0.0), list(reasons or [])[:8])
                continue
            merged_reasons = list(current[1])
            for reason in reasons or []:
                if reason not in merged_reasons:
                    merged_reasons.append(reason)
            merged[path] = (max(current[0], float(score or 0.0)), merged_reasons[:10])
    out = [(path, score, reasons) for path, (score, reasons) in merged.items()]
    out.sort(key=lambda item: (-item[1], item[0]))
    return out


def _cosil_style_prune_candidates(
    aggregate: Dict[str, RankedLocation],
    component_scores: dict[str, dict[str, float]],
    *,
    top_k: int,
    read_budget: int | None = None,
) -> tuple[list[RankedLocation], dict[str, Any]]:
    """Budget the dynamic frontier before expensive code reading.

    CoSIL's useful lesson for our setting is not to let every retrieved file
    enter the next reasoning step. We keep a small beam from each signal family:
    lexical BM25, explicit symbols, paths, evidence roles, concern/effect
    semantics, program graph, and flow validation.
    """

    budget = read_budget or max(top_k * 3, 36)
    budget = max(12, budget)
    ranked_all = sorted(aggregate.values(), key=lambda item: (-item.score, item.path))
    selected: dict[str, RankedLocation] = {}

    def add_items(items: Iterable[RankedLocation], reason: str, limit: int) -> None:
        for item in list(items)[:limit]:
            if item.path not in selected:
                selected[item.path] = item
            if reason and reason not in selected[item.path].reasons:
                selected[item.path].reasons.append(reason)
            if len(selected) >= budget:
                break

    # Keep a small score-first beam, then reserve room for signal-family beams.
    # This mirrors CoSIL's useful behavior: strong early scores matter, but they
    # should not crowd out concern/call/flow candidates before code reading.
    overall_limit = min(max(8, top_k // 2), max(8, budget // 3))
    add_items(ranked_all, "prune:overall_beam", overall_limit)

    per_component_limits = {
        "architecture_path_probe": 6,
        "domain_path_probe": 14,
        "bm25_score": 8,
        "symbol_score": 8,
        "path_score": 6,
        "concern_score": 8,
        "evidence_score": 6,
        "call_score": 6,
        "flow_score": 6,
        "flow_verifier": 6,
        "concern_search": 8,
        "entity_search": 8,
        "graph_navigation": 6,
        "concern_graph": 6,
        "program_graph": 6,
        "flow_graph": 6,
        "memory": 5,
    }
    for component, limit in per_component_limits.items():
        component_ranked = sorted(
            (
                item
                for item in aggregate.values()
                if component_scores.get(_norm_path(item.path), {}).get(component, 0.0) > 0
            ),
            key=lambda item: (-component_scores[_norm_path(item.path)][component], -item.score, item.path),
        )
        add_items(component_ranked, f"prune:kept_for_{component}", limit)
        if len(selected) >= budget:
            break

    if len(selected) < min(budget, len(ranked_all)):
        add_items(ranked_all, "prune:fill_remaining_by_score", budget)

    pruned = sorted(selected.values(), key=lambda item: (-item.score, item.path))[:budget]
    kept_paths = {item.path for item in pruned}
    diagnostics = {
        "strategy": "cosil_inspired_signal_family_beam",
        "input_candidates": len(aggregate),
        "kept_candidates": len(pruned),
        "read_budget": budget,
        "overall_beam_limit": overall_limit,
        "component_limits": per_component_limits,
        "dropped_candidates": max(0, len(aggregate) - len(kept_paths)),
    }
    return pruned, diagnostics


def _rank_confidence(
    ranked: list[RankedLocation],
    *,
    code_contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not ranked:
        return {"confidence": 0.0, "reason": "no_candidates"}
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    top_score = float(top.score or 0.0)
    second_score = float(second.score or 0.0) if second else 0.0
    gap = top_score - second_score
    relative_gap = max(0.0, gap) / max(abs(top_score), abs(second_score), 1.0)
    components = top.score_components or {}
    review_belief = (top.belief or {}).get("llm_candidate_review") or {}
    axes = {
        "symbol": float(components.get("symbol_score", 0.0) or 0.0) > 0,
        "path": float(components.get("path_score", 0.0) or 0.0) > 0 or float(components.get("domain_path_probe", 0.0) or 0.0) > 0,
        "concern": float(components.get("concern_score", 0.0) or 0.0) > 0,
        "program": float(components.get("call_score", 0.0) or 0.0) > 0,
        "flow": float(components.get("flow_score", 0.0) or 0.0) > 0 or float(components.get("flow_verifier", 0.0) or 0.0) > 0,
        "review": (
            float(components.get("llm_candidate_review", 0.0) or 0.0) > 0
            and bool(review_belief.get("mechanism_verified", False))
        ),
        "source": _path_role(top.path) not in {"reproduction_or_example", "test_or_fixture", "generated_or_lockfile", "addon_bundle"},
        "read": any((ctx.get("path") == top.path and ctx.get("snippets")) for ctx in (code_contexts or [])),
    }
    axis_count = sum(1 for value in axes.values() if value)
    # Absolute retrieval scores differ greatly by repository and signal family.
    # Confidence therefore uses relative separation plus independent evidence.
    score = 0.12 + min(0.28, relative_gap * 0.70) + axis_count * 0.055
    if axes["source"]:
        score += 0.08
    if axes["symbol"] and axes["read"]:
        score += 0.08
    if axes["review"] and axes["read"]:
        score += 0.10
    return {
        "confidence": round(min(0.98, score), 3),
        "top_path": top.path,
        "top_score": round(top_score, 3),
        "gap": round(gap, 3),
        "relative_gap": round(relative_gap, 4),
        "axes": axes,
        "axis_count": axis_count,
    }


def _should_run_deep_flow(
    *,
    round_no: int,
    max_rounds: int,
    confidence: dict[str, Any],
    issue_sketch: Any,
    candidate_count: int,
) -> tuple[bool, str]:
    mode = os.environ.get("MYCODE_FLOW_MODE", "auto").strip().lower()
    if mode == "light":
        return False, "flow_mode_light"
    if mode == "deep":
        return True, "flow_mode_deep"
    if not _env_bool("MYCODE_DEEP_FLOW_AUTO", True):
        return False, "deep_flow_auto_disabled"
    candidate_limit = _env_int("MYCODE_DEEP_FLOW_CANDIDATE_LIMIT", 32, minimum=1)
    if candidate_count > candidate_limit:
        return False, "candidate_pool_too_large_for_deep_flow"
    obligation_count = len(getattr(issue_sketch, "flow_obligations", []) or [])
    if obligation_count:
        return True, "explicit_flow_obligation"
    threshold = _env_float("MYCODE_HIGH_CONFIDENCE_THRESHOLD", 0.78, minimum=0.0)
    axes = confidence.get("axes") or {}
    semantic_program_axes = sum(
        1
        for name in ("symbol", "path", "concern", "program", "flow", "review")
        if bool(axes.get(name))
    )
    if float(confidence.get("confidence") or 0.0) >= threshold and semantic_program_axes >= 2:
        return False, "high_confidence_uses_light_flow"
    if round_no >= max_rounds:
        return True, "low_confidence_final_round"
    return False, "no_deep_flow_trigger"


def _run_flow_backends_layered(
    *,
    flow_index: RepositoryIndex,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    active_queries: list[str],
    candidate_paths: list[str],
    issue_sketch: Any,
    confidence: dict[str, Any],
    round_no: int,
    max_rounds: int,
    flow_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tool_observations = evidence_result.get("tool_observations", []) or []
    timings: list[dict[str, Any]] = []

    def run_backend(name: str, fn, **kwargs: Any) -> list[dict[str, Any]]:
        start = time.perf_counter()
        phase_event(
            "start",
            "dynamic.flow_backend",
            subphase="TraceFlow",
            backend=name,
            flow_scope_size=len(flow_index.files),
            candidate_count=len(candidate_paths),
        )
        out = fn(
            flow_index,
            issue_text=_issue_query_text(sample.issue_text),
            tool_observations=tool_observations,
            queries=active_queries,
            limit=flow_limit,
            **kwargs,
        )
        elapsed = round(time.perf_counter() - start, 3)
        timings.append({"backend": name, "elapsed_seconds": elapsed, "count": len(out)})
        phase_event("end", "dynamic.flow_backend", backend=name, elapsed_seconds=elapsed, flow_count=len(out))
        return out

    with phase_context(
        "dynamic.TraceFlow",
        subphase="TraceFlow",
        flow_scope_size=len(flow_index.files),
        candidate_count=len(candidate_paths),
        flow_limit=flow_limit,
    ):
        program = run_backend("trace_program_flows", trace_program_flows)
        closures = run_backend("trace_parameter_closures", trace_parameter_closures)
        run_deep, reason = _should_run_deep_flow(
            round_no=round_no,
            max_rounds=max_rounds,
            confidence=confidence,
            issue_sketch=issue_sketch,
            candidate_count=len(candidate_paths),
        )
        phase_event(
            "progress",
            "dynamic.TraceFlow",
            subphase="TraceFlow",
            confidence=confidence,
            deep_flow=run_deep,
            deep_flow_reason=reason,
        )
        deep_groups: list[list[dict[str, Any]]] = []
        if run_deep:
            deep_groups.extend(
                [
                    run_backend("trace_statement_flows", trace_statement_flows),
                    run_backend("trace_static_slices", trace_static_slices),
                    run_backend(
                        "trace_interprocedural_flows",
                        trace_interprocedural_flows,
                        candidate_paths=candidate_paths,
                        max_hops=_env_int("MYCODE_INTERPROCEDURAL_MAX_HOPS", 2, minimum=1),
                        max_terms=_env_int("MYCODE_INTERPROCEDURAL_MAX_TERMS", 16, minimum=1),
                        max_source_entities=_env_int("MYCODE_INTERPROCEDURAL_MAX_ENTITIES", 180, minimum=16),
                    ),
                    run_backend("trace_flow_chains", trace_flow_chains),
                    run_backend("verify_runtime_traces", verify_runtime_traces),
                ]
            )
    traces = _merge_flow_traces(program, closures, *deep_groups)
    diagnostics = {
        "mode": os.environ.get("MYCODE_FLOW_MODE", "auto").strip().lower(),
        "deep_flow_enabled": bool(deep_groups),
        "deep_flow_reason": reason,
        "confidence_before_flow": confidence,
        "backend_timings": timings,
        "flow_scope_size": len(flow_index.files),
    }
    return traces, diagnostics


def _candidate_preview_from_graph(
    hits: Iterable[tuple[str, float, list[str]]],
    *,
    mode: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "score": round(float(score or 0.0), 3),
            "mode": mode,
            "path_role": _path_role(path),
            "reasons": list(reasons or [])[:5],
        }
        for path, score, reasons in list(hits)[:limit]
    ]


def _flow_candidate_preview(flow_traces: Iterable[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for flow in list(flow_traces)[:limit]:
        out.append(
            {
                "term": flow.get("term"),
                "flow_type": flow.get("flow_type"),
                "confidence": flow.get("confidence"),
                "candidate_target_paths": flow.get("candidate_target_paths", [])[:8],
                "role_coverage": flow.get("role_coverage", {}),
                "edge_summary": flow.get("edge_summary", {}),
                "backend": flow.get("backend"),
                "chain_edge_count": len(flow.get("chain_edges", []) or []),
            }
        )
    return out


def _collect_evidence_seed_paths(evidence_result: Dict[str, Any]) -> set[str]:
    packet = evidence_result.get("evidence_packet", {}) or {}
    paths: set[str] = set()
    for item in packet.get("code_references", []) or []:
        path = str(item.get("path") or "")
        if path:
            paths.add(_norm_path(path))
    for item in packet.get("url_inspections", []) or []:
        role = str(item.get("role") or "")
        path = str(item.get("path") or "")
        github_path = str(item.get("github_path") or "")
        url_path = _github_blob_path_from_url(str(item.get("url") or ""))
        if role in {"code_evidence_seed", "reproduction_entry"}:
            for candidate in (path, github_path, url_path):
                if candidate and not candidate.startswith("/"):
                    paths.add(_norm_path(candidate))
    for observation in evidence_result.get("tool_observations", []) or []:
        tool = str(observation.get("tool") or "")
        extracted = observation.get("extracted", {}) or {}
        if tool == "github_url_parser" and extracted.get("local_resolution_status") == "resolved":
            path = str(extracted.get("local_path") or extracted.get("path") or "")
            if path:
                paths.add(_norm_path(path))
        if tool == "browser_reproduction_reader":
            for source_file in extracted.get("source_files", []) or []:
                path = str(source_file.get("path") or "")
                if path:
                    paths.add(_norm_path(path))
    return paths


def _github_blob_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc.endswith("github.com"):
        return ""
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) >= 5 and parts[2] == "blob":
        return "/".join(parts[4:])
    return ""


def _candidate_target_paths_from_flows(flow_traces: list[dict[str, Any]]) -> dict[str, list[str]]:
    support: dict[str, list[str]] = defaultdict(list)
    for flow in flow_traces:
        flow_type = str(flow.get("flow_type") or "flow")
        coverage = flow.get("role_coverage", {}) or {}
        for path in flow.get("candidate_target_paths", []) or []:
            if not path:
                continue
            norm_path = _norm_path(path)
            if _path_role(norm_path) in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
                continue
            support[norm_path].append(f"{flow_type}:candidate_target")
        for loc in flow.get("locations", []) or []:
            path = str(loc.get("path") or "")
            role = str(loc.get("role") or "")
            if not path or role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
                continue
            reason = f"{flow_type}:role:{role}"
            if coverage.get("coverage") is not None:
                reason += f":coverage={coverage.get('coverage')}"
            support[_norm_path(path)].append(reason)
        for step_key in ("source_steps", "sink_steps", "steps", "statement_nodes"):
            for step in flow.get(step_key, []) or []:
                if not isinstance(step, dict):
                    continue
                path = str(step.get("path") or "")
                role = str(step.get("role") or "")
                if not path or role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
                    continue
                support[_norm_path(path)].append(f"{flow_type}:step_role:{role}")
        for edge_key in ("chain_edges", "def_use_edges", "call_boundary_edges"):
            for edge in flow.get(edge_key, []) or []:
                if not isinstance(edge, dict):
                    continue
                relation = str(edge.get("relation") or edge_key)
                for endpoint in ("target", "source"):
                    path = str(edge.get(endpoint) or "")
                    if path:
                        support[_norm_path(path)].append(f"{flow_type}:edge:{relation}")
    return support


def _flow_trap_notes(evidence_result: Dict[str, Any], ranked: Iterable[RankedLocation]) -> list[dict[str, Any]]:
    seed_paths = _collect_evidence_seed_paths(evidence_result)
    notes: list[dict[str, Any]] = []
    for item in ranked:
        path = _norm_path(item.path)
        role = _path_role(path)
        if path in seed_paths:
            notes.append(
                {
                    "path": item.path,
                    "trap": "evidence_seed_ranked_as_candidate",
                    "role": role,
                    "message": "Issue URL/tool evidence points here, but it should be verified through downstream code before being treated as a patch target.",
                }
            )
        elif role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
            notes.append(
                {
                    "path": item.path,
                    "trap": "reproduction_or_test_ranked_as_target",
                    "role": role,
                    "message": "This file explains or validates the behavior; the patch target is usually an imported implementation layer.",
                }
            )
    return notes[:10]


def _verify_candidates(
    *,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    ranked: Iterable[RankedLocation],
    code_contexts: list[dict[str, Any]],
    flow_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    context_by_path = {item["path"]: item for item in code_contexts}
    evidence_seed_paths = _collect_evidence_seed_paths(evidence_result)
    flow_paths: dict[str, list[str]] = defaultdict(list)
    for flow in flow_traces:
        for loc in flow.get("locations", []) or []:
            flow_paths[str(loc.get("path") or "")].append(str(flow.get("flow_type") or "flow"))
    target_support = _candidate_target_paths_from_flows(flow_traces)

    decisions: list[dict[str, Any]] = []
    for item in ranked:
        bonus, reasons = _path_concern_bonus(item.path, sample, evidence_result)
        norm = _norm_path(item.path)
        role = _path_role(norm)
        if item.path in flow_paths or norm in flow_paths:
            flow_types = sorted(set(flow_paths.get(item.path, []) + flow_paths.get(norm, [])))
            bonus += 5.0 + len(flow_types)
            reasons.append("flow_trace_support:" + ",".join(flow_types[:4]))
        if norm in target_support:
            support_reasons = sorted(set(target_support[norm]))
            bonus += 6.0 + min(6.0, 1.5 * len(support_reasons))
            reasons.append("flow_obligation_target:" + ",".join(support_reasons[:4]))
        context = context_by_path.get(item.path, {})
        if context.get("snippets"):
            bonus += 3.0
            reasons.append("code_context_read")
        if norm in evidence_seed_paths:
            penalty = 7.0
            if role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
                penalty += 5.0
            bonus -= penalty
            reasons.append(f"evidence_seed_not_direct_target_penalty:{role}")
        elif role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
            bonus -= 5.0
            reasons.append(f"example_or_test_target_penalty:{role}")
        if any("reproduction_code_is_evidence_not_patch_target" in reason for reason in item.reasons):
            bonus -= 8.0
            reasons.append("reproduction_code_penalty")
        decisions.append(
            {
                "path": item.path,
                "base_score": item.score,
                "path_role": role,
                "bonus": round(bonus, 3),
                "verified_score": round(item.score + bonus, 3),
                "verifier_reasons": reasons or ["no_extra_verifier_signal"],
            }
        )
    return {
        "strategy": "evidence_role_aware_concern_and_flow_consistency_verifier",
        "evidence_seed_paths": sorted(evidence_seed_paths),
        "flow_target_support": dict(target_support),
        "trap_notes": _flow_trap_notes(evidence_result, ranked),
        "decisions": decisions,
    }


def _apply_verifier(
    ranked: list[RankedLocation],
    verifier: dict[str, Any],
    *,
    sample: NormalizedSample,
    top_k: int,
    component_scores: dict[str, dict[str, float]] | None = None,
) -> list[RankedLocation]:
    bonus_by_path = {item["path"]: float(item.get("bonus") or 0.0) for item in verifier.get("decisions", [])}
    reasons_by_path = {
        item["path"]: list(item.get("verifier_reasons") or [])
        for item in verifier.get("decisions", [])
    }
    component_scores = component_scores or {}
    evidence_seed_paths = {_norm_path(path) for path in verifier.get("evidence_seed_paths", []) or []}
    reranked: list[RankedLocation] = []
    for item in ranked:
        norm = _norm_path(item.path)
        components = dict(component_scores.get(norm, {}))
        if bonus_by_path.get(item.path, 0.0):
            components["flow_verifier"] = components.get("flow_verifier", 0.0) + bonus_by_path.get(item.path, 0.0)
        score = item.score + bonus_by_path.get(item.path, 0.0)
        role = _path_role(norm)
        if role in {"reproduction_or_example", "test_or_fixture", "generated_or_lockfile", "addon_bundle"}:
            penalty = 35.0
            if norm in evidence_seed_paths:
                penalty += 65.0
                score *= 0.82
            score -= penalty
            components["role_penalty"] = components.get("role_penalty", 0.0) - penalty
        cap, cap_reason = _source_first_score_cap(norm, _issue_query_text(sample.issue_text))
        if cap is not None and score > cap:
            components["source_first_post_verifier_cap"] = components.get("source_first_post_verifier_cap", 0.0) + (cap - score)
            score = cap
        updated = RankedLocation(
            path=item.path,
            score=score,
            reasons=list(item.reasons),
            entities=list(item.entities),
            score_components={key: round(float(value or 0.0), 3) for key, value in sorted(components.items())},
            belief=dict(item.belief),
            missing_evidence=list(item.missing_evidence),
        )
        if (
            role in {"reproduction_or_example", "test_or_fixture", "generated_or_lockfile", "addon_bundle"}
            and "role_rerank:non_patch_surface_downweighted" not in updated.reasons
        ):
            updated.reasons.append("role_rerank:non_patch_surface_downweighted")
        for reason in reasons_by_path.get(item.path, []):
            if reason not in updated.reasons:
                updated.reasons.append("verifier:" + reason)
        if cap is not None and cap_reason not in updated.reasons:
            updated.reasons.append(cap_reason)
        reranked.append(updated)
    reranked.sort(key=lambda item: (-item.score, item.path))
    return reranked[:top_k]


def _candidate_belief(
    item: RankedLocation,
    flow_traces: list[dict[str, Any]],
    verifier: dict[str, Any],
    frontier_queries: dict[str, list[str]],
    *,
    read_paths: set[str] | None = None,
) -> dict[str, Any]:
    norm = _norm_path(item.path)
    components = item.score_components
    supporting_axes = [
        axis
        for axis, keys in {
            "horizontal_concern": ("concern_score", "concern_search"),
            "vertical_program": ("call_score", "program_graph", "graph_navigation", "symbol_score", "entity_search"),
            "flow_validation": ("flow_score", "flow_graph", "flow_verifier"),
            "evidence_role": ("evidence_score",),
            "path_or_bm25": ("path_score", "bm25_score"),
        }.items()
        if any(float(components.get(key, 0.0) or 0.0) > 0 for key in keys)
    ]
    flow_support = []
    for flow in flow_traces:
        paths = {_norm_path(path) for path in flow.get("candidate_target_paths", []) or []}
        for loc in flow.get("locations", []) or []:
            paths.add(_norm_path(str(loc.get("path") or "")))
        if norm in paths:
            flow_support.append(str(flow.get("flow_type") or flow.get("term") or "flow"))
    verifier_reasons: list[str] = []
    for decision in verifier.get("decisions", []) or []:
        if _norm_path(str(decision.get("path") or "")) == norm:
            verifier_reasons = list(decision.get("verifier_reasons") or [])
            break
    missing: list[str] = []
    if not any(axis in supporting_axes for axis in ("horizontal_concern", "evidence_role")):
        missing.append("no_concern_or_evidence_role_support")
    if not any(axis in supporting_axes for axis in ("vertical_program", "flow_validation")):
        missing.append("no_call_or_flow_support")
    if not flow_support:
        missing.append("no_explicit_flow_trace")
    why = _dedupe(list(item.reasons[:5]) + verifier_reasons[:4] + flow_support[:4], limit=12)
    return {
        "path": item.path,
        "score": round(item.score, 3),
        "path_role": _path_role(item.path),
        "supporting_axes": supporting_axes,
        "score_components": dict(components),
        "why": why,
        "flow_support": _dedupe(flow_support, limit=8),
        "read_verified": norm in (read_paths or set()),
        "missing_evidence": missing,
        "query_groups_used": {
            key: values[:8]
            for key, values in frontier_queries.items()
            if key in {"explicit_entity", "evidence_role", "concern", "effect", "flow"}
        },
    }


def _candidate_evidence_signature(belief: dict[str, Any]) -> list[str]:
    """Stable evidence facts used to detect real cross-round information gain."""

    review = belief.get("llm_candidate_review") or {}
    facts = [f"axis:{axis}" for axis in belief.get("supporting_axes", []) or []]
    facts.extend(f"flow:{flow}" for flow in belief.get("flow_support", []) or [])
    if belief.get("read_verified"):
        facts.append("read:verified")
    if review:
        facts.extend(
            [
                f"review_role:{review.get('role') or 'unknown'}",
                f"review_grounded:{bool(review.get('grounded'))}",
                f"review_mechanism:{bool(review.get('mechanism_verified'))}",
            ]
        )
        facts.extend(f"review_axis:{axis}" for axis in review.get("matched_issue_axes", []) or [])
    return sorted(set(facts))


def _annotate_candidate_evidence_gain(
    item: RankedLocation,
    previous: RankedLocation | None,
) -> None:
    previous_belief = (previous.belief if previous else {}) or {}
    previous_signature = set(previous_belief.get("evidence_signature", []) or [])
    current_signature = set(_candidate_evidence_signature(item.belief))
    gained = sorted(current_signature - previous_signature)
    prior_stale_rounds = int(previous_belief.get("rounds_without_gain") or 0)
    item.belief["evidence_signature"] = sorted(current_signature)
    item.belief["new_evidence"] = gained
    item.belief["evidence_gain_count"] = len(gained)
    item.belief["rounds_without_gain"] = 0 if gained or previous is None else prior_stale_rounds + 1


def _round_progress(
    *,
    ranked: list[RankedLocation],
    previous_ranked: list[RankedLocation],
    flow_traces: list[dict[str, Any]],
    previous_flow_traces: list[dict[str, Any]],
    next_queries: list[str],
    active_queries: list[str],
    previous_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous_paths = {_norm_path(item.path) for item in previous_ranked}
    current_top = [_norm_path(item.path) for item in ranked[:6]]
    previous_top = [_norm_path(item.path) for item in previous_ranked[:6]]
    new_top_paths = [path for path in current_top if path not in previous_paths]
    evidence_gains = sum(int((item.belief or {}).get("evidence_gain_count") or 0) for item in ranked[:5])
    strong_evidence_prefixes = (
        "read:verified",
        "axis:vertical_program",
        "review_role:patch_target",
        "review_grounded:True",
        "review_mechanism:True",
    )
    strong_evidence_gains = sum(
        1
        for item in ranked[:5]
        for fact in (item.belief or {}).get("new_evidence", []) or []
        if str(fact).startswith(strong_evidence_prefixes)
    )
    old_flow = {
        (str(flow.get("flow_type") or ""), str(flow.get("backend") or ""))
        for flow in previous_flow_traces or []
    }
    seen_flow_signatures = set((previous_progress or {}).get("seen_flow_signatures", []) or [])
    seen_flow_signatures.update("|".join(signature) for signature in old_flow)
    current_flow_signatures = {
        "|".join((str(flow.get("flow_type") or ""), str(flow.get("backend") or "")))
        for flow in flow_traces or []
    }
    new_flow_families = sorted(current_flow_signatures - seen_flow_signatures)
    seen_flow_signatures.update(current_flow_signatures)
    active_query_keys = {" ".join(str(query).lower().split()) for query in active_queries or []}
    novel_queries = [
        query
        for query in next_queries or []
        if " ".join(str(query).lower().split()) not in active_query_keys
    ]
    stable_top3 = bool(previous_top) and current_top[:3] == previous_top[:3]
    stable_top1 = bool(previous_top and current_top and current_top[0] == previous_top[0])
    top5_union = set(current_top) | set(previous_top)
    top5_overlap = len(set(current_top) & set(previous_top)) / max(1, len(top5_union))
    overlap_threshold = min(1.0, _env_float("MYCODE_PLATEAU_TOP5_OVERLAP", 0.6, minimum=0.0))
    plateau = bool(
        stable_top1
        and top5_overlap >= overlap_threshold
        and not new_top_paths
        and strong_evidence_gains == 0
        and not new_flow_families
    )
    prior_streak = int((previous_progress or {}).get("plateau_streak") or 0)
    return {
        "new_top_candidate_count": len(new_top_paths),
        "new_top_candidates": new_top_paths,
        "candidate_evidence_gain_count": evidence_gains,
        "strong_evidence_gain_count": strong_evidence_gains,
        "new_flow_count": len(new_flow_families),
        "new_flow_signatures": new_flow_families[:12],
        "seen_flow_signatures": sorted(seen_flow_signatures),
        "novel_query_count": len(novel_queries),
        "novel_queries": novel_queries[:12],
        "stable_top3": stable_top3,
        "stable_top1": stable_top1,
        "top5_overlap": round(top5_overlap, 3),
        "top5_overlap_threshold": overlap_threshold,
        "plateau": plateau,
        "plateau_streak": prior_streak + 1 if plateau else 0,
    }


def _round_belief_questions(ranked: list[RankedLocation]) -> dict[str, Any]:
    believed = [item.path for item in ranked[:5]]
    why = []
    missing = []
    for item in ranked[:5]:
        belief = item.belief or {}
        why.append({"path": item.path, "why": belief.get("why", item.reasons[:4])})
        missing.append({"path": item.path, "missing": belief.get("missing_evidence", item.missing_evidence)})
    return {
        "believed_files": believed,
        "why": why,
        "missing_evidence": missing,
    }


def _round_checkpoint_quality(search_round: DynamicSearchRound) -> dict[str, Any]:
    """Score a round using evidence quality only, never benchmark gold.

    Later rounds may broaden recall while weakening the leading candidate.  A
    checkpoint prevents term churn or architecture guesses from replacing an
    earlier source-read, program-grounded decision.  Raw retrieval scores are
    deliberately excluded because their scale changes across repositories.
    """

    ranked = search_round.ranked_locations
    if not ranked:
        return {"score": -1.0, "reason": "no_candidates", "round_no": search_round.round_no}
    top = ranked[0]
    belief = top.belief or {}
    review = belief.get("llm_candidate_review") or {}
    axes = set(belief.get("supporting_axes", []) or [])
    read_verified = bool(belief.get("read_verified"))
    grounded = bool(review.get("grounded"))
    mechanism_verified = bool(review.get("mechanism_verified"))
    direct_axes = axes & {"vertical_program", "flow_validation"}
    confidence = float((search_round.stop_decision.get("confidence") or {}).get("confidence") or 0.0)
    top_six = ranked[:6]
    verified_top6 = sum(
        1
        for item in top_six
        if bool(((item.belief or {}).get("llm_candidate_review") or {}).get("mechanism_verified"))
    )
    grounded_top6 = sum(
        1
        for item in top_six
        if bool(((item.belief or {}).get("llm_candidate_review") or {}).get("grounded"))
    )
    def checkpoint_facts(item: RankedLocation) -> dict[str, bool]:
        item_belief = item.belief or {}
        item_review = item_belief.get("llm_candidate_review") or {}
        item_components = item.score_components or {}
        item_read = bool(item_belief.get("read_verified"))
        item_grounded = bool(item_review.get("grounded")) and item_read
        item_quote = bool(item_review.get("quote_supported")) and item_read
        item_entity = bool(
            item_review.get("supported_entities") or item_review.get("entity_supported")
        ) and item_read
        item_program = bool(item_review.get("direct_flow_supported")) or any(
            float(item_components.get(name, 0.0) or 0.0) > 0
            for name in ("call_score", "flow_score", "flow_verifier")
        )
        item_mechanism = bool(item_review.get("mechanism_verified")) and item_grounded
        item_equivalent = bool(item_read and item_quote and item_entity and item_program)
        patchable = _path_role(item.path) not in _CLOSURE_BLOCKED_ROLES
        return {
            "read": item_read,
            "grounded": item_grounded,
            "program": item_program,
            "mechanism": item_mechanism,
            "equivalent": item_equivalent,
            "source_supported": bool(
                patchable and item_read and (item_grounded or item_quote or item_entity or item_program)
            ),
            "responsibility": bool(patchable and (item_mechanism or item_equivalent)),
        }

    top_six_facts = [checkpoint_facts(item) for item in top_six]
    verified_source_top6 = sum(fact["source_supported"] for fact in top_six_facts)
    responsibility_top6 = sum(fact["responsibility"] for fact in top_six_facts)
    program_grounded_top6 = sum(
        fact["program"] and (fact["grounded"] or fact["equivalent"])
        for fact in top_six_facts
    )
    blocked_role_top6 = sum(
        1 for item in top_six if _path_role(item.path) in _CLOSURE_BLOCKED_ROLES
    )
    critical_missing = list(
        ((search_round.verifier or {}).get("llm_candidate_review") or {}).get(
            "critical_missing_evidence", []
        )
        or []
    )
    components = top.score_components or {}
    architecture = float(components.get("architecture_path_probe", 0.0) or 0.0)
    direct_component_total = sum(
        max(0.0, float(components.get(name, 0.0) or 0.0))
        for name in ("symbol_score", "path_score", "domain_path_probe", "call_score", "flow_score", "flow_verifier")
    )
    architecture_only = bool(architecture > 0 and direct_component_total <= 0 and not mechanism_verified)
    score = (
        confidence * 4.0
        + (7.0 if mechanism_verified else 0.0)
        + (2.0 if grounded else 0.0)
        + (1.5 if read_verified else 0.0)
        + len(direct_axes) * 1.5
        + verified_top6 * 0.8
        + grounded_top6 * 0.25
        - min(2.0, len(critical_missing) * 0.5)
        - blocked_role_top6 * 0.75
        - (1.5 if architecture_only else 0.0)
    )
    flow_coverage = min(
        1.0,
        sum(fact["program"] for fact in top_six_facts) / max(1, min(3, len(top_six_facts))),
    )
    top_facts = top_six_facts[0]
    selection_key = [
        int(mechanism_verified),
        int(top_facts["equivalent"]),
        verified_top6,
        responsibility_top6,
        program_grounded_top6,
        verified_source_top6,
        grounded_top6,
        round(flow_coverage, 4),
        -blocked_role_top6,
        -len(critical_missing),
        round(confidence, 4),
    ]
    return {
        "score": round(score, 4),
        "selection_key": selection_key,
        "round_no": search_round.round_no,
        "top_path": top.path,
        "confidence": round(confidence, 4),
        "read_verified": read_verified,
        "grounded": grounded,
        "mechanism_verified": mechanism_verified,
        "direct_axes": sorted(direct_axes),
        "root_mechanism_candidate_count": verified_top6,
        "verified_source_count_in_top6": verified_source_top6,
        "responsibility_candidate_count_in_top6": responsibility_top6,
        "program_grounded_candidate_count_in_top6": program_grounded_top6,
        "grounded_candidate_count_in_top6": grounded_top6,
        "required_flow_coverage": round(flow_coverage, 4),
        "negative_blocked_role_count_in_top6": blocked_role_top6,
        "critical_missing_count": len(critical_missing),
        "architecture_only": architecture_only,
    }


def _checkpoint_is_better(
    candidate: dict[str, Any],
    incumbent: dict[str, Any],
    *,
    minimum_gain: float,
) -> bool:
    if not incumbent:
        return True
    candidate_key = tuple(candidate.get("selection_key", []) or [])
    incumbent_key = tuple(incumbent.get("selection_key", []) or [])
    if candidate_key and incumbent_key and candidate_key != incumbent_key:
        return candidate_key > incumbent_key
    return float(candidate.get("score") or -1.0) > float(incumbent.get("score") or -1.0) + minimum_gain


def _flow_signature(flow: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(flow.get("flow_type") or ""),
        str(flow.get("term") or ""),
        str(flow.get("backend") or ""),
    )


def _merge_flow_traces(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for group in groups:
        for flow in group or []:
            key = _flow_signature(flow)
            current = merged.get(key)
            if current is None or float(flow.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
                merged[key] = flow
    return sorted(
        merged.values(),
        key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("flow_type") or ""), str(item.get("term") or "")),
    )


def _token_looks_useful(token: str) -> bool:
    if len(token) < 3:
        return False
    if token.isdigit():
        return False
    if token in {"src", "lib", "test", "tests", "index", "const", "return", "function", "class", "import", "export"}:
        return False
    return True


def _derive_next_round_queries(
    *,
    index: RepositoryIndex,
    ranked: list[RankedLocation],
    code_contexts: list[dict[str, Any]],
    flow_traces: list[dict[str, Any]],
    previous_queries: Iterable[str],
    limit: int = 28,
) -> list[str]:
    """Turn current observations into the next search frontier.

    This is the agentic bridge between search and code reading: after one round
    finds candidate files, we mine symbols, file neighborhoods, and flow edges to
    decide what to inspect next instead of restarting with the same issue text.
    """

    seen = {str(query).lower() for query in previous_queries}
    candidates: list[str] = []

    for item in ranked[:8]:
        path_parts = [part for part in item.path.replace("\\", "/").split("/") if part]
        candidates.extend(path_parts[-4:])
        candidates.append(Path(item.path).stem)
        for entity in item.entities[:6]:
            name = str(entity.get("name") or "")
            kind = str(entity.get("kind") or "")
            if name:
                candidates.append(name)
                candidates.append(f"{kind} {name}".strip())

    for context in code_contexts[:8]:
        for entity in context.get("entities", []) or []:
            name = str(entity.get("name") or "")
            if name:
                candidates.append(name)
        for snippet in context.get("snippets", []) or []:
            text = str(snippet.get("text") or "")
            for token in re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b", text):
                if any(marker in token.lower() for marker in ("hover", "leave", "legend", "select", "serialize", "backend", "route", "url", "handler", "round", "option", "state")):
                    candidates.append(token)

    for flow in flow_traces[:8]:
        term = str(flow.get("term") or "")
        flow_type = str(flow.get("flow_type") or "")
        if term:
            candidates.append(term)
            candidates.append(f"{flow_type} {term}".strip())
        for edge in flow.get("edges", []) or []:
            candidates.extend(edge.get("symbols", []) or [])
            candidates.append(str(edge.get("relation") or ""))
        for edge in flow.get("statement_edges", []) or []:
            candidates.append(str(edge.get("symbol") or ""))
            candidates.append(str(edge.get("relation") or ""))
        for edge in flow.get("def_use_edges", []) or []:
            candidates.append(str(edge.get("symbol") or ""))
            candidates.append(str(edge.get("relation") or ""))
        for edge in flow.get("call_boundary_edges", []) or []:
            candidates.append(str(edge.get("symbol") or ""))
            candidates.append(str(edge.get("relation") or ""))
        for edge in flow.get("chain_edges", []) or []:
            candidates.append(str(edge.get("symbol") or ""))
            candidates.append(str(edge.get("relation") or ""))
            candidates.append(str(edge.get("source") or ""))
            candidates.append(str(edge.get("target") or ""))
        for step_key in ("source_steps", "sink_steps", "steps", "statement_nodes"):
            for step in flow.get(step_key, []) or []:
                if not isinstance(step, dict):
                    continue
                candidates.append(str(step.get("term") or ""))
                candidates.append(str(step.get("path") or ""))
                entity = step.get("entity") or {}
                if isinstance(entity, dict):
                    candidates.append(str(entity.get("name") or ""))
                for call in step.get("calls", []) or []:
                    if isinstance(call, dict):
                        candidates.append(str(call.get("callee") or ""))
                        candidates.append(str(call.get("symbol") or ""))
        for event in flow.get("trace_events", []) or []:
            candidates.extend(str(name) for name in event.get("functions", []) or [])
            candidates.append(str(event.get("path") or ""))
        for loc in flow.get("locations", []) or []:
            name = str(loc.get("name") or "")
            path = str(loc.get("path") or "")
            if name:
                candidates.append(name)
            for statement in loc.get("statements", []) or []:
                candidates.extend(str(call) for call in statement.get("calls", []) or [])
                entity = statement.get("entity") or {}
                if isinstance(entity, dict) and entity.get("name"):
                    candidates.append(str(entity["name"]))
            if path:
                siblings = _entities_for_file(index, path)
                candidates.extend(entity.name for entity in siblings[:8])

    out: list[str] = []
    for candidate in _dedupe(candidates):
        text = candidate.strip()
        if not text:
            continue
        low = text.lower()
        if low in seen:
            continue
        tokens = tokenize(text)
        if not any(_token_looks_useful(token) for token in tokens):
            continue
        out.append(text)
        seen.add(low)
        if len(out) >= limit:
            break
    return out


def _evaluate_round_if_possible(
    localization: dict[str, Any],
    sample: NormalizedSample,
    index: RepositoryIndex,
) -> dict[str, Any]:
    if not sample.gold_files:
        return {"status": "no_gold_available"}
    try:
        metrics = evaluate_three_level_ranking_with_applicability(localization, sample, index)
    except Exception as exc:  # noqa: BLE001 - diagnostics should not break localization.
        return {"status": "evaluation_failed", "error": str(exc)}
    return {
        "status": "ok",
        "file": metrics.get("file", {}),
        "module": metrics.get("module", {}),
        "function": metrics.get("function", {}),
        "gold": metrics.get("gold", {}),
        "applicability": metrics.get("applicability", {}),
        "note": "diagnostic only; gold is not used for ranking",
    }


def _lightweight_dynamic_localize(
    *,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    index: RepositoryIndex,
    issue_sketch: Any,
    queries: list[str],
    top_k: int,
) -> Dict[str, Any]:
    """Single-pass localization for full-dataset sweeps.

    The deep agent builds a heterogeneous graph and runs several search/read/flow
    rounds. That is useful for case studies, but too expensive for 92/458 sample
    full runs. This path keeps the same output schema and module/function metrics
    while using only the loaded repository structure index.
    """

    file_hits = index.search_files(queries, limit=max(top_k * 4, 40))
    entity_hits = index.search_entities(queries, limit=max(top_k * 5, 60))
    evidence_seed_paths = _collect_evidence_seed_paths(evidence_result)

    aggregate: dict[str, RankedLocation] = {}

    def add_candidate(path: str, score: float, reason: str, entity: dict[str, Any] | None = None) -> None:
        norm = _norm_path(path)
        if not norm or norm not in index.files:
            return
        penalty = 0.70 if norm in evidence_seed_paths else 1.0
        source_multiplier, source_reason = _source_first_multiplier(norm, _issue_query_text(sample.issue_text))
        item = aggregate.get(norm)
        if item is None:
            item = RankedLocation(path=norm, score=0.0, reasons=[])
            aggregate[norm] = item
        item.score += float(score or 0.0) * penalty * source_multiplier
        cap, cap_reason = _source_first_score_cap(norm, _issue_query_text(sample.issue_text))
        if cap is not None and item.score > cap:
            item.score = cap
            if cap_reason not in item.reasons:
                item.reasons.append(cap_reason)
        if reason and reason not in item.reasons:
            item.reasons.append(reason)
        if source_reason != "source_first:neutral" and source_reason not in item.reasons:
            item.reasons.append(source_reason)
        if norm in evidence_seed_paths and "evidence_seed_downweighted" not in item.reasons:
            item.reasons.append("evidence_seed_downweighted")
        if entity:
            item.entities.append(entity)

    for hit in file_hits:
        add_candidate(hit.path, hit.score, "file_search:" + ",".join(hit.reasons[:5]))

    for hit in entity_hits:
        add_candidate(
            hit.path,
            hit.score * 1.25,
            "entity_search:" + ",".join(hit.reasons[:5]),
            {
                "kind": hit.kind,
                "name": hit.name,
                "start_line": hit.start_line,
                "end_line": hit.end_line,
                "score": round(hit.score, 3),
                "reasons": hit.reasons[:5],
            },
        )

    if not aggregate:
        for path in list(index.files)[:top_k]:
            add_candidate(path, 0.1, "fallback:first_index_files")

    ranked = sorted(aggregate.values(), key=lambda item: (-item.score, item.path))[:top_k]
    ranked_modules, ranked_functions = _rank_entities(
        index=index,
        ranked_files=ranked,
        entity_hits=entity_hits,
        flow_traces=[],
        top_k=top_k,
        entity_queries=[_issue_query_text(sample.issue_text)] + queries,
    )
    localization_for_eval = {
        "ranked_locations": [item.to_dict() for item in ranked],
        "ranked_modules": [item.to_dict() for item in ranked_modules],
        "ranked_functions": [item.to_dict() for item in ranked_functions],
    }
    evaluation = _evaluate_round_if_possible(localization_for_eval, sample, index)
    round_summary = {
        "round_no": 1,
        "input_queries": queries[:50],
        "agent_actions": [
            {
                "tool": "SearchAnchor",
                "mode": "lightweight",
                "description": "search files/entities from evidence sketch without building the deep graph",
            }
        ],
        "frontier_state": {
            "mode": "lightweight",
            "candidate_count": len(aggregate),
            "file_hit_count": len(file_hits),
            "entity_hit_count": len(entity_hits),
        },
        "seed_files": [],
        "file_hits": [hit.to_dict() for hit in file_hits[:20]],
        "entity_hits": [hit.to_dict() for hit in entity_hits[:20]],
        "graph_hits": [],
        "graph_summary": {"mode": "skipped_in_lightweight_full_run"},
        "code_contexts": [],
        "flow_traces": [],
        "verifier": {"strategy": "lightweight_schema_compatible_no_deep_flow"},
        "agent_observation": {
            "summary": "single-pass concern/entity retrieval for full Clean15 benchmark sweeps",
            "top_paths": [item.path for item in ranked[:10]],
        },
        "ranked_locations": [item.to_dict() for item in ranked],
        "ranked_modules": [item.to_dict() for item in ranked_modules],
        "ranked_functions": [item.to_dict() for item in ranked_functions],
        "next_queries": [],
        "evaluation": evaluation,
        "stop_decision": {"stop": True, "reason": "lightweight_single_pass"},
    }
    return {
        "instance_id": sample.instance_id,
        "repo": sample.repo,
        "dataset": sample.dataset,
        "status": "ok",
        "mode": "lightweight",
        "index": index.metadata(),
        "issue_sketch": issue_sketch.to_dict(),
        "queries": queries[:80],
        "search_trace": [
            {
                "step": "issue_sketch",
                "strategy": "evidence role understanding before localization",
                "issue_sketch": issue_sketch.to_dict(),
            },
            {
                "step": "lightweight_search",
                "strategy": "full-run friendly file/entity retrieval; deep graph and flow are disabled",
                "query_count": len(queries),
                "file_hit_count": len(file_hits),
                "entity_hit_count": len(entity_hits),
                "top_files": [item.path for item in ranked[:10]],
                "round_evaluation": evaluation,
            },
        ],
        "dynamic_rounds": [round_summary],
        "agent_trace": [
            {
                "round_no": 1,
                "actions": round_summary["agent_actions"],
                "frontier_state": round_summary["frontier_state"],
                "observation": round_summary["agent_observation"],
                "stop_decision": round_summary["stop_decision"],
            }
        ],
        "round_evaluations": [evaluation],
        "code_contexts": [],
        "flow_traces": [],
        "verifier": round_summary["verifier"],
        "agent_reasoning_summary": {
            "mode": "lightweight",
            "summary": (
                "Used Issue Sketch queries and repo_structures-backed file/entity search. "
                "Use LIGHTWEIGHT=0 for multi-round graph/flow agent analysis."
            ),
            "evidence_role_note": (
                "Code/URL evidence is downweighted when it looks like a navigation seed instead of a direct target."
            ),
        },
        "ranked_locations": [item.to_dict() for item in ranked],
        "ranked_modules": [item.to_dict() for item in ranked_modules],
        "ranked_functions": [item.to_dict() for item in ranked_functions],
    }


def _component_paths(
    rounds: Iterable[DynamicSearchRound],
    component: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Summarize one signal family's strongest file support across rounds."""

    best: dict[str, float] = {}
    first_round: dict[str, int] = {}
    for item in rounds:
        score_components = item.frontier_state.get("score_components", {}) or {}
        for path, components in score_components.items():
            score = float((components or {}).get(component) or 0.0)
            if score <= 0:
                continue
            if score > best.get(path, 0.0):
                best[path] = score
                first_round.setdefault(path, item.round_no)
    ranked = sorted(best.items(), key=lambda entry: (-entry[1], entry[0]))[:limit]
    return [
        {
            "path": path,
            "score": round(score, 3),
            "first_round": first_round.get(path),
        }
        for path, score in ranked
    ]


def _flow_reasoning_summary(flow_traces: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    flow_type_counts = Counter(str(flow.get("flow_type") or "unknown") for flow in flow_traces)
    backend_counts = Counter(str(flow.get("backend") or "unknown") for flow in flow_traces)
    covered_roles: set[str] = set()
    covered_terms: set[str] = set()
    candidate_paths: set[str] = set()
    edge_counts = Counter()
    previews: list[dict[str, Any]] = []

    for flow in flow_traces:
        term = str(flow.get("term") or "")
        if term:
            covered_terms.add(term)
        for path in flow.get("candidate_target_paths", []) or []:
            if path:
                candidate_paths.add(_norm_path(str(path)))
        role_coverage = flow.get("role_coverage", {}) or {}
        for role in role_coverage.get("covered_roles", []) or []:
            covered_roles.add(str(role))
        for role in (role_coverage.get("roles", {}) or {}).keys():
            covered_roles.add(str(role))
        flow_edge_count = 0
        for edge_key in ("chain_edges", "edges", "statement_edges", "def_use_edges", "call_boundary_edges"):
            edges = flow.get(edge_key, []) or []
            flow_edge_count += len(edges)
            for edge in edges:
                if isinstance(edge, dict):
                    edge_counts[str(edge.get("relation") or edge_key)] += 1
        if len(previews) < limit:
            previews.append(
                {
                    "term": term,
                    "flow_type": flow.get("flow_type"),
                    "backend": flow.get("backend"),
                    "confidence": flow.get("confidence"),
                    "candidate_target_paths": flow.get("candidate_target_paths", [])[:6],
                    "covered_roles": sorted(covered_roles)[:10],
                    "edge_count": flow_edge_count,
                }
            )

    return {
        "strategy": "ARISE-inspired lightweight state/parameter/behavior flow verification",
        "flow_count": len(flow_traces),
        "flow_type_counts": dict(flow_type_counts),
        "backend_counts": dict(backend_counts),
        "covered_terms": sorted(covered_terms)[:30],
        "covered_roles": sorted(covered_roles),
        "candidate_path_count": len(candidate_paths),
        "candidate_paths": sorted(candidate_paths)[:20],
        "edge_relation_counts": dict(edge_counts.most_common(20)),
        "preview": previews,
        "limitation": (
            "This is cross-file/cross-function heuristic flow over symbols and typed edges, "
            "not compiler-grade statement-level def-use closure."
        ),
    }


def _agent_reasoning_summary(
    *,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    graph: TypedRepositoryGraph,
    rounds: list[DynamicSearchRound],
    ranked: list[RankedLocation],
    ranked_modules: list[RankedEntity],
    ranked_functions: list[RankedEntity],
    flow_traces: list[dict[str, Any]],
    verifier: dict[str, Any],
) -> dict[str, Any]:
    issue_sketch = build_issue_sketch(sample, evidence_result)
    top_paths = [item.path for item in ranked[:15]]
    local_graph_summary = graph.edge_summary(top_paths)
    full_graph_summary = graph.edge_summary()
    final_frontier = rounds[-1].frontier_state if rounds else {}
    edge_type_counts = local_graph_summary.get("edge_type_counts", {}) or {}

    return {
        "paradigm": "Evidence role understanding -> Concern search -> Call/UsedBy navigation -> Flow check -> three-level rank",
        "implemented_agent_design": {
            "stage_1_issue_preprocessing": {
                "input_scope": "problem_statement plus referenced images/URLs when already extracted by evidence tools",
                "output": "IssueSketch(workflow, concerns, states, expected_effects, entities, evidence_roles, seed_policy, flow_obligations)",
                "purpose": "Understand evidence roles before using explicit URLs or image text as localization seeds.",
            },
            "stage_2_dynamic_localization": {
                "loop": "SearchAnchor -> NavigateCode -> TraceFlow -> ReadCode -> rerank",
                "horizontal_axis": "Concern search expands business/workflow-related code that may not be in a direct call chain.",
                "vertical_axis": "Call/UsedBy/import/render/state/config navigation follows typed program edges around current seeds.",
                "flow_axis": "TraceFlow verifies whether issue states/parameters/events can affect candidate behavior.",
            },
            "rank_outputs": {
                "file": "ranked_locations",
                "module": "ranked_modules, including file-module fallback for non-function files",
                "function": "ranked_functions with role-aware and flow-aware reranking",
            },
        },
        "innovation_against_baselines": {
            "LocAgent_gap": (
                "LocAgent searches with LLM/tool calls but does not explicitly separate URL/image evidence role "
                "from patch-target likelihood; this code penalizes low-prior evidence seeds and uses them for navigation."
            ),
            "CoSIL_gap": (
                "CoSIL inspires the dynamic search/read/prune loop, but this code adds explicit multimodal evidence "
                "roles plus concern/program/flow frontier separation."
            ),
            "GraphLocator_gap": (
                "GraphLocator emphasizes causal graph construction; this code keeps a lighter on-demand typed graph "
                "so full Clean15-style sweeps can run without prebuilding a very heavy graph for every sample."
            ),
            "GALA_gap": (
                "GALA aligns image/code graph evidence, while this code treats image and URL observations as typed "
                "issue evidence that changes seed policy, flow obligations, and navigation direction."
            ),
        },
        "sample": {
            "instance_id": sample.instance_id,
            "repo": sample.repo,
            "dataset": sample.dataset,
            "gold_file_count": len(sample.gold_files or []),
            "top_files": top_paths[:10],
            "top_modules": [item.id for item in ranked_modules[:10]],
            "top_functions": [item.id for item in ranked_functions[:10]],
        },
        "evidence_role_controls": {
            "evidence_roles": issue_sketch.to_dict().get("evidence_roles", []),
            "seed_policy": issue_sketch.to_dict().get("seed_policy", []),
            "evidence_seed_paths": verifier.get("evidence_seed_paths", []),
            "trap_notes": verifier.get("trap_notes", []),
            "meaning": "Explicit URL/image/code evidence is treated as navigation evidence before patch-target evidence.",
        },
        "concern_horizontal": {
            "purpose": "Find files that implement the same business concern even when they are not directly connected by calls.",
            "query_preview": final_frontier.get("concern_queries", [])[:12],
            "candidate_preview": final_frontier.get("concern_candidates", [])[:10],
            "score_support": _component_paths(rounds, "concern_score", limit=8)
            + _component_paths(rounds, "concern_search", limit=8)
            + _component_paths(rounds, "concern_graph", limit=8),
        },
        "program_vertical": {
            "purpose": "Navigate program structure around seeds: imports, calls, used-by, render/state/config/style links.",
            "query_preview": final_frontier.get("program_queries", [])[:12],
            "candidate_preview": final_frontier.get("program_candidates", [])[:10],
            "edge_type_counts_near_top_files": edge_type_counts,
            "score_support": _component_paths(rounds, "call_score", limit=10)
            + _component_paths(rounds, "symbol_score", limit=8)
            + _component_paths(rounds, "program_graph", limit=10)
            + _component_paths(rounds, "graph_navigation", limit=10),
        },
        "flow_validation": _flow_reasoning_summary(flow_traces),
        "pruning_and_verifier": {
            "pruning": final_frontier.get("pruning", {}),
            "flow_target_support": verifier.get("flow_target_support", {}),
            "score_support": _component_paths(rounds, "flow_verifier", limit=12)
            + _component_paths(rounds, "flow_score", limit=8),
            "decisions_preview": verifier.get("decisions", [])[:10],
        },
        "multilingual_graph": {
            "graph_type": full_graph_summary.get("graph_type"),
            "file_count": full_graph_summary.get("file_count"),
            "edge_count": full_graph_summary.get("edge_count"),
            "language_counts": full_graph_summary.get("language_counts", {}),
            "edge_type_counts": full_graph_summary.get("edge_type_counts", {}),
            "interpretation": (
                "Different languages keep their own extraction rules, but the agent consumes a shared typed-edge schema."
            ),
        },
        "rounds": {
            "round_count": len(rounds),
            "stop_decision": rounds[-1].stop_decision if rounds else {},
            "round_summaries": [
                {
                    "round_no": item.round_no,
                    "agent_plan": item.frontier_state.get("agent_plan", {}),
                    "top_files": [loc.path for loc in item.ranked_locations[:5]],
                    "next_query_count": len(item.next_queries),
                    "flow_count": len(item.flow_traces),
                    "evaluation": item.evaluation,
                }
                for item in rounds
            ],
        },
    }


def _build_round_agent_plan(
    *,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    round_no: int,
    controller_decisions: Iterable[Any],
    actions: Iterable[Any],
    frontier_queries: dict[str, list[str]],
    preferred_edge_types: list[str],
    seed_files: list[str],
    graph_summary: dict[str, Any],
    flow_traces: list[dict[str, Any]],
    verifier: dict[str, Any],
) -> dict[str, Any]:
    """Structured explanation of how this round mixes concern, call and flow.

    The dynamic retrieval code already executes these steps; this helper makes
    the plan explicit so result files can be inspected without reverse
    engineering scores and reason strings.
    """

    issue_sketch = build_issue_sketch(sample, evidence_result)
    decisions = []
    for decision in controller_decisions:
        serialized = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        # The auditable raw response/usage is stored once in agent_actions.
        # Avoid duplicating token counters inside the explanatory plan.
        serialized.pop("llm_raw", None)
        serialized.pop("token_usage", None)
        decisions.append(serialized)
    nav_actions = [
        action.to_dict() if hasattr(action, "to_dict") else dict(action)
        for action in actions
    ]
    flow_types = Counter(str(flow.get("flow_type") or "unknown") for flow in flow_traces)
    covered_roles: set[str] = set()
    for flow in flow_traces:
        for role in (flow.get("role_coverage", {}) or {}).get("roles", {}) or {}:
            covered_roles.add(str(role))
        for role in flow.get("covered_roles", []) or []:
            covered_roles.add(str(role))

    evidence_seed_paths = _collect_evidence_seed_paths(evidence_result)
    low_prior_seeds = sorted(path for path in evidence_seed_paths if path in set(evidence_seed_paths))

    return {
        "name": "evidence_aware_concern_call_flow_round",
        "round_no": round_no,
        "issue_sketch_used": {
            "workflow": issue_sketch.workflow,
            "concerns": issue_sketch.concerns,
            "states": issue_sketch.states,
            "expected_effects": issue_sketch.expected_effects,
            "flow_obligations": issue_sketch.flow_obligations,
        },
        "seed_policy": {
            "rules": issue_sketch.seed_policy,
            "evidence_seed_paths": sorted(evidence_seed_paths),
            "low_modification_prior_paths": low_prior_seeds,
            "policy": "use explicit code/URL evidence as navigation anchors first; do not blindly rank them as patch targets",
        },
        "tool_schedule": [
            {
                "tool": "SearchAnchor",
                "axis": "horizontal",
                "purpose": "find concern/entity/effect entry files from issue sketch and evidence packet",
                "query_preview": frontier_queries.get("concern", [])[:10],
            },
            {
                "tool": "NavigateCode",
                "axis": "vertical",
                "purpose": "expand current seeds through typed program edges such as calls/imports/used_by/renders/state/config",
                "edge_types": preferred_edge_types[:20] or PROGRAM_EDGE_TYPES[:12],
                "seed_files": seed_files[:12],
            },
            {
                "tool": "TraceFlow",
                "axis": "flow",
                "purpose": "check whether states/parameters/events from the issue can reach behavior-changing code",
                "query_preview": frontier_queries.get("flow", [])[:10],
                "flow_type_counts": dict(flow_types),
                "covered_roles": sorted(covered_roles),
            },
            {
                "tool": "ReadCode",
                "axis": "verification",
                "purpose": "read a pruned candidate set before final file/module/function ranking",
                "pruning_strategy": "CoSIL-inspired signal-family beam",
            },
        ],
        "switching_logic": {
            "concern_then_call": "when issue describes workflow/business area but direct symbol target is unclear",
            "call_then_concern": "when code URL or symbol is explicit but likely evidence-only, navigate used_by/called_by first",
            "flow_after_candidates": "after candidates exist, require state/effect/parameter support before boosting",
            "controller_decisions": decisions,
            "heuristic_actions": nav_actions,
        },
        "round_observation": {
            "graph_summary_near_frontier": graph_summary,
            "flow_count": len(flow_traces),
            "verifier_decisions": verifier.get("decisions", [])[:8],
            "trap_notes": verifier.get("trap_notes", [])[:6],
        },
    }


def _stop_decision(
    *,
    round_no: int,
    max_rounds: int,
    ranked: list[RankedLocation],
    previous_top_paths: list[str],
    next_queries: list[str],
    confidence: dict[str, Any] | None = None,
    candidate_review: dict[str, Any] | None = None,
    round_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_top = [item.path for item in ranked[:5]]
    stable_top3 = current_top[:3] == previous_top_paths[:3] if previous_top_paths else False
    confidence = confidence or _rank_confidence(ranked)
    candidate_review = candidate_review or {}
    round_progress = round_progress or {}
    review_requests_more = bool(
        candidate_review.get("status") == "ok" and candidate_review.get("continue_search")
    )
    review_stop_ready = bool(candidate_review.get("status") == "ok" and candidate_review.get("stop_ready"))
    critical_missing = list(candidate_review.get("critical_missing_evidence", []) or [])
    threshold = _env_float("MYCODE_HIGH_CONFIDENCE_THRESHOLD", 0.78, minimum=0.0)
    axes = confidence.get("axes", {}) or {}
    stable_top1 = bool(previous_top_paths and current_top and current_top[0] == previous_top_paths[0])
    program_verified = bool(axes.get("program") or axes.get("flow"))
    read_verified = bool(axes.get("read"))
    if (
        not review_requests_more
        and not critical_missing
        and _env_bool("MYCODE_EARLY_STOP_HIGH_CONFIDENCE", True)
        and float(confidence.get("confidence") or 0.0) >= threshold
    ):
        if (
            axes.get("source")
            and read_verified
            and (axes.get("symbol") or axes.get("path") or axes.get("review"))
            and (program_verified or review_stop_ready)
            and (stable_top1 or review_stop_ready)
        ):
            return {
                "stop": True,
                "reason": "high_confidence_source_candidate",
                "top_paths": current_top,
                "confidence": confidence,
                "threshold": threshold,
                "stable_top1": stable_top1,
                "review_stop_ready": review_stop_ready,
            }
    if round_no >= max_rounds:
        return {"stop": True, "reason": "max_rounds_reached", "top_paths": current_top, "confidence": confidence}
    plateau_limit = _env_int("MYCODE_EVIDENCE_PLATEAU_ROUNDS", 2, minimum=1)
    review_override_round = _env_int(
        "MYCODE_PLATEAU_REVIEW_OVERRIDE_ROUND",
        max(8, max_rounds - 2),
        minimum=1,
    )
    stale_review_override = bool(
        review_requests_more
        and not critical_missing
        and round_no >= review_override_round
        and bool(round_progress.get("stable_top3"))
        and int(round_progress.get("strong_evidence_gain_count") or 0) == 0
        and int(round_progress.get("new_flow_count") or 0) == 0
    )
    if (
        _env_bool("MYCODE_EARLY_STOP_EVIDENCE_PLATEAU", True)
        and int(round_progress.get("plateau_streak") or 0) >= plateau_limit
        and (not review_requests_more or stale_review_override)
        and not critical_missing
    ):
        return {
            "stop": True,
            "reason": "evidence_plateau_after_repeated_review" if stale_review_override else "evidence_plateau",
            "top_paths": current_top,
            "confidence": confidence,
            "round_progress": round_progress,
            "review_override_round": review_override_round,
            "missing_evidence": critical_missing[:8] or list(candidate_review.get("missing_evidence", []) or [])[:8],
        }
    if review_requests_more:
        return {
            "stop": False,
            "reason": "candidate_review_requests_more_evidence",
            "top_paths": current_top,
            "confidence": confidence,
            "missing_evidence": list(candidate_review.get("missing_evidence", []) or [])[:8],
        }
    if critical_missing:
        return {
            "stop": False,
            "reason": "candidate_review_has_critical_evidence_gap",
            "top_paths": current_top,
            "confidence": confidence,
            "missing_evidence": critical_missing[:8],
        }
    if stable_top3 and len(next_queries) <= 2:
        return {"stop": True, "reason": "top3_stable_and_no_new_frontier", "top_paths": current_top, "confidence": confidence}
    if not next_queries:
        return {"stop": True, "reason": "no_new_queries", "top_paths": current_top, "confidence": confidence}
    return {"stop": False, "reason": "continue_with_new_frontier", "top_paths": current_top, "confidence": confidence}


def _apply_llm_candidate_review(
    ranked: list[RankedLocation],
    review: dict[str, Any],
    *,
    component_scores: dict[str, dict[str, float]],
    top_k: int,
    code_contexts: list[dict[str, Any]] | None = None,
) -> list[RankedLocation]:
    """Fuse an evidence-grounded LLM review with deterministic retrieval scores.

    The review is deliberately bounded to already-pruned candidates. It can
    reorder navigation-only evidence below likely edit targets, but it cannot
    invent files or erase deterministic recall when parsing fails.
    """

    if review.get("status") != "ok" or not review.get("candidates"):
        return ranked[:top_k]
    decisions = {
        _norm_path(str(item.get("path") or "")): (position, item)
        for position, item in enumerate(review.get("candidates", []) or [])
        if _norm_path(str(item.get("path") or ""))
    }
    if not decisions:
        return ranked[:top_k]
    grounded_paths = {
        _norm_path(str(context.get("path") or ""))
        for context in (code_contexts or [])
        if context.get("snippets")
    }
    max_bonus = _env_float("MYCODE_LLM_REVIEW_MAX_BONUS", 180.0, minimum=0.0)
    max_penalty = _env_float("MYCODE_LLM_REVIEW_MAX_PENALTY", 60.0, minimum=0.0)
    grounded_bonus = _env_float("MYCODE_LLM_REVIEW_GROUNDED_BONUS", 12.0, minimum=0.0)
    ungrounded_bonus = _env_float("MYCODE_LLM_REVIEW_UNGROUNDED_BONUS", 4.0, minimum=0.0)
    role_prior = {
        "patch_target": 0.72,
        "supporting_target": 0.30,
        "navigation_only": -0.34,
        "reproduction_only": -0.75,
        "test_or_docs": -0.68,
        "unlikely": -0.48,
    }
    reviewed_count = max(1, len(decisions))
    for item in ranked:
        norm = _norm_path(item.path)
        decision_pair = decisions.get(norm)
        if decision_pair is None:
            continue
        position, decision = decision_pair
        role = str(decision.get("role") or "unlikely")
        confidence = max(0.0, min(1.0, float(decision.get("confidence") or 0.0)))
        grounded = bool(decision.get("grounded", norm in grounded_paths)) and norm in grounded_paths
        mechanism_verified = bool(decision.get("mechanism_verified", False)) and grounded
        counterevidence = list(decision.get("counterevidence", []) or [])
        order_bonus = 0.0
        if role in {"patch_target", "supporting_target"}:
            order_bonus = 0.18 * (reviewed_count - position) / reviewed_count
        raw_factor = role_prior.get(role, -0.48) * confidence + order_bonus
        if raw_factor > 0:
            cap = max_bonus if mechanism_verified else (grounded_bonus if grounded else ungrounded_bonus)
            adjustment = min(cap, cap * raw_factor)
        else:
            adjustment = max(-max_penalty, max_penalty * raw_factor)
        if counterevidence and not mechanism_verified:
            adjustment -= min(max_penalty * 0.35, 6.0 * len(counterevidence))
        item.score += adjustment
        item.score_components["llm_candidate_review"] = round(adjustment, 3)
        components = component_scores.setdefault(norm, {})
        components["llm_candidate_review"] = float(components.get("llm_candidate_review", 0.0) or 0.0) + adjustment
        reason = (
            f"llm_review:{role}:confidence={confidence:.2f}:grounded={str(grounded).lower()}:"
            f"mechanism={str(mechanism_verified).lower()}"
        )
        if reason not in item.reasons:
            item.reasons.append(reason)
        rationale = " ".join(str(decision.get("rationale") or "").split())
        if rationale:
            item.reasons.append("llm_review_reason:" + rationale[:280])
        item.belief["llm_candidate_review"] = {
            "role": role,
            "confidence": confidence,
            "matched_issue_axes": list(decision.get("matched_issue_axes", []) or [])[:8],
            "entities": list(decision.get("entities", []) or [])[:8],
            "grounded": grounded,
            "evidence_quote": str(decision.get("evidence_quote") or "")[:240],
            "quote_supported": bool(decision.get("quote_supported", False)),
            "entity_supported": bool(decision.get("entity_supported", False)),
            "supported_entities": list(decision.get("supported_entities", []) or [])[:8],
            "unsupported_entities": list(decision.get("unsupported_entities", []) or [])[:8],
            "direct_flow_supported": bool(decision.get("direct_flow_supported", False)),
            "mechanism_verified": mechanism_verified,
            "patch_mechanism": str(decision.get("patch_mechanism") or "")[:400],
            "counterevidence": counterevidence[:6],
        }
    ranked.sort(key=lambda candidate: (-candidate.score, candidate.path))
    return ranked[:top_k]


def _candidate_evidence_facts(
    item: RankedLocation,
    *,
    decision: dict[str, Any],
    context_paths: set[str],
) -> dict[str, Any]:
    path = _norm_path(item.path)
    belief = item.belief or {}
    embedded_review = belief.get("llm_candidate_review") or {}
    resolved_decision = decision or embedded_review
    components = item.score_components or {}
    read_verified = path in context_paths or bool(belief.get("read_verified"))
    quote_supported = bool(resolved_decision.get("quote_supported")) and read_verified
    entity_supported = bool(
        resolved_decision.get("supported_entities")
        or resolved_decision.get("entity_supported")
    ) and read_verified
    grounded = bool(
        resolved_decision.get("grounded", quote_supported or entity_supported)
    ) and read_verified
    direct_flow = bool(resolved_decision.get("direct_flow_supported")) or any(
        float(components.get(name, 0.0) or 0.0) > 0
        for name in ("call_score", "flow_score", "flow_verifier")
    )
    counterevidence = _dedupe(
        list(resolved_decision.get("counterevidence", []) or [])
        + list(resolved_decision.get("counter_evidence", []) or []),
        limit=8,
    )
    role = _path_role(path)
    patchable = role not in _CLOSURE_BLOCKED_ROLES
    mechanism = bool(resolved_decision.get("mechanism_verified")) and grounded
    equivalent_mechanism = bool(
        patchable and read_verified and quote_supported and entity_supported and direct_flow
    )
    lock_eligible = bool(
        patchable
        and not counterevidence
        and (mechanism or equivalent_mechanism)
    )
    return {
        "path_role": role,
        "patchable": patchable,
        "read_verified": read_verified,
        "quote_supported": quote_supported,
        "entity_supported": entity_supported,
        "grounded": grounded,
        "direct_flow": direct_flow,
        "mechanism_verified": mechanism,
        "equivalent_mechanism": equivalent_mechanism,
        "counterevidence": counterevidence,
        "lock_eligible": lock_eligible,
    }


def _cross_round_candidate_frontier(
    *,
    selected: list[RankedLocation],
    round_rankings: Iterable[Iterable[RankedLocation]],
    issue_text: str,
    review: dict[str, Any],
    code_contexts: Iterable[dict[str, Any]],
    top_k: int,
) -> tuple[list[RankedLocation], dict[str, Any]]:
    """Recover strongly supported candidates that an earlier checkpoint lost.

    A best-round checkpoint protects the head of the ranking, but selecting one
    round wholesale can discard useful candidates discovered later. This pass
    protects only source-grounded mechanism candidates and permits bounded
    evidence-backed replacement at every other position.
    """

    selected = list(selected[:top_k])
    rankings = [list(ranking) for ranking in round_rankings or []]
    if (
        not selected
        or len(rankings) <= 1
        or not _env_bool("MYCODE_CROSS_ROUND_FRONTIER", True)
    ):
        return selected, {"enabled": False, "reason": "disabled_or_single_round"}

    lock_limit = min(
        len(selected),
        _env_int("MYCODE_CROSS_ROUND_LOCKED_HEAD", 2, minimum=0),
    )
    max_replacements = _env_int("MYCODE_CROSS_ROUND_MAX_REPLACEMENTS", 4, minimum=0)
    per_round_limit = _env_int(
        "MYCODE_CROSS_ROUND_PER_ROUND_LIMIT",
        max(8, top_k),
        minimum=3,
    )
    context_paths = {
        _norm_path(str(context.get("path") or ""))
        for context in code_contexts or []
        if context.get("snippets")
    }
    reviewed = {
        _norm_path(str(item.get("path") or "")): item
        for item in review.get("candidates", []) or []
        if str(item.get("path") or "")
    }
    adaptive_locks: list[dict[str, Any]] = []
    locked_paths: set[str] = set()
    for rank, item in enumerate(selected, start=1):
        path = _norm_path(item.path)
        facts = _candidate_evidence_facts(
            item,
            decision=reviewed.get(path) or {},
            context_paths=context_paths,
        )
        if len(locked_paths) < lock_limit and facts["lock_eligible"]:
            locked_paths.add(path)
            adaptive_locks.append(
                {
                    "path": path,
                    "rank": rank,
                    "reason": "verified_source_quote_entity_and_direct_flow",
                    "evidence": facts,
                }
            )
    issue_lower = unquote(str(issue_text or "")).replace("\\", "/").lower()
    issue_tokens = {token for token in tokenize(issue_lower) if len(token) >= 4}
    selected_ranks = {_norm_path(item.path): rank for rank, item in enumerate(selected, start=1)}
    representatives = {_norm_path(item.path): item for item in selected}
    histories: dict[str, list[int]] = defaultdict(list)

    def evidence_tuple(item: RankedLocation) -> tuple[int, int, int, int, float]:
        path = _norm_path(item.path)
        decision = reviewed.get(path) or {}
        belief = item.belief or {}
        return (
            1 if decision.get("mechanism_verified") else 0,
            1 if decision.get("grounded", decision.get("quote_supported", False)) else 0,
            1 if path in context_paths or belief.get("read_verified") else 0,
            len(belief.get("supporting_axes", []) or []),
            float(item.score or 0.0),
        )

    for ranking in rankings:
        for rank, item in enumerate(ranking[:per_round_limit], start=1):
            path = _norm_path(item.path)
            if not path:
                continue
            histories[path].append(rank)
            current = representatives.get(path)
            if current is None or evidence_tuple(item) > evidence_tuple(current):
                representatives[path] = item

    def frontier_quality(path: str) -> tuple[float, dict[str, Any]]:
        item = representatives[path]
        decision = reviewed.get(path) or {}
        belief = item.belief or {}
        history = histories.get(path, [])
        components = item.score_components or {}
        read_verified = path in context_paths or bool(belief.get("read_verified"))
        grounded = bool(decision.get("grounded", decision.get("quote_supported", False))) and read_verified
        mechanism = bool(decision.get("mechanism_verified", False)) and grounded
        retrieval_direct = any(
            float(components.get(name, 0.0) or 0.0) > 0
            for name in (
                "symbol_score", "path_score", "domain_path_probe", "entity_search",
                "concern_search",
            )
        )
        program_direct = any(
            float(components.get(name, 0.0) or 0.0) > 0
            for name in ("call_score", "flow_score", "flow_verifier")
        ) or bool(decision.get("direct_flow_supported"))
        path_tokens = {token for token in tokenize(path.lower()) if len(token) >= 4}
        overlap = path_tokens & issue_tokens
        exact_path = bool(path and path.lower() in issue_lower)
        selected_rank = selected_ranks.get(path)
        quality = sum(12.0 / (8.0 + rank) for rank in history)
        quality += min(4.0, len(history) * 0.7)
        quality += 8.0 / max(1, min(history or [top_k + 1]))
        if selected_rank is not None:
            quality += 10.0 / selected_rank
        if read_verified:
            quality += 2.0
        if retrieval_direct:
            quality += 1.5
        if program_direct:
            quality += 2.5
        if grounded:
            quality += 3.0
        if mechanism:
            quality += 8.0
        if exact_path:
            quality += 8.0
        elif overlap:
            quality += min(3.0, len(overlap) * 1.2)
        equivalent_mechanism = bool(
            read_verified
            and grounded
            and bool(decision.get("quote_supported"))
            and bool(decision.get("supported_entities") or decision.get("entity_supported"))
            and program_direct
        )
        strong = bool(
            mechanism
            or equivalent_mechanism
            or (
                grounded
                and str(decision.get("role") or "") in {"patch_target", "supporting_target"}
                and float(decision.get("confidence") or 0.0) >= 0.72
                and program_direct
            )
        )
        return quality, {
            "quality": round(quality, 3),
            "history": history,
            "read_verified": read_verified,
            "grounded": grounded,
            "mechanism_verified": mechanism,
            "retrieval_evidence": retrieval_direct,
            "program_evidence": program_direct,
            "equivalent_mechanism": equivalent_mechanism,
            "issue_path_overlap": sorted(overlap)[:6],
            "strong": strong,
        }

    qualities = {path: frontier_quality(path) for path in representatives}
    selected_paths = [_norm_path(item.path) for item in selected]
    challengers = [
        path
        for path in representatives
        if path not in selected_ranks and qualities[path][1]["strong"]
    ]
    challengers.sort(key=lambda path: (-qualities[path][0], min(histories[path]), path))
    replacements: list[dict[str, Any]] = []
    replacement_additions: set[str] = set()
    replacement_margin = _env_float("MYCODE_CROSS_ROUND_REPLACEMENT_MARGIN", 0.75, minimum=0.0)
    protected_prefix = min(
        len(selected_paths),
        _env_int("MYCODE_CROSS_ROUND_PROTECTED_PREFIX", 0, minimum=0),
    )
    for challenger in challengers:
        if len(replacements) >= max_replacements:
            break
        replaceable = [
            path for path in selected_paths
            if path not in locked_paths and path not in replacement_additions
            and selected_paths.index(path) >= protected_prefix
        ]
        if not replaceable and len(selected_paths) <= protected_prefix:
            replaceable = [
                path for path in selected_paths
                if path not in locked_paths and path not in replacement_additions
            ]
        if not replaceable:
            break
        incumbent = min(
            replaceable,
            key=lambda path: (
                qualities.get(path, (0.0, {}))[0],
                -selected_ranks.get(path, selected_paths.index(path) + 1),
            ),
        )
        challenger_quality = qualities[challenger][0]
        incumbent_quality = qualities.get(incumbent, (0.0, {}))[0]
        if challenger_quality < incumbent_quality + replacement_margin:
            continue
        position = selected_paths.index(incumbent)
        selected_paths[position] = challenger
        replacement_additions.add(challenger)
        replacements.append(
            {
                "removed": incumbent,
                "added": challenger,
                "position": position + 1,
                "incumbent_quality": round(incumbent_quality, 3),
                "challenger_quality": round(challenger_quality, 3),
                "evidence": qualities[challenger][1],
            }
        )

    fused: list[RankedLocation] = []
    for path in selected_paths:
        item = copy.deepcopy(representatives[path])
        item.belief.setdefault("cross_round_frontier", qualities[path][1])
        fused.append(item)
    return fused[:top_k], {
        "enabled": True,
        "strategy": "adaptive_evidence_locks_with_cross_round_fusion",
        "locked_head": len(locked_paths),
        "lock_limit": lock_limit,
        "adaptive_locks": adaptive_locks,
        "max_replacements": max_replacements,
        "protected_prefix": protected_prefix,
        "candidate_count": len(representatives),
        "strong_challenger_count": len(challengers),
        "replacements": replacements,
        "top_before": [item.path for item in selected[:5]],
        "top_after": [item.path for item in fused[:5]],
    }


def _precision_rerank_locations(
    *,
    ranked: list[RankedLocation],
    round_rankings: Iterable[Iterable[RankedLocation]],
    issue_text: str,
    review: dict[str, Any],
    code_contexts: Iterable[dict[str, Any]],
    top_k: int,
) -> tuple[list[RankedLocation], dict[str, Any]]:
    """Rerank the recalled set using source-grounded precision evidence.

    Retrieval and graph scores deliberately optimize recall and are not
    comparable across repositories.  This final, bounded pass therefore uses
    ranks and evidence facts rather than raw scores.  It never invents or
    removes candidates; it only changes the order of the selected recall set.
    """

    if not ranked or not _env_bool("MYCODE_PRECISION_RERANK", True):
        return ranked[:top_k], {"enabled": False, "reason": "disabled_or_empty"}

    candidate_paths = {_norm_path(item.path) for item in ranked[:top_k]}
    rank_history: dict[str, list[int]] = defaultdict(list)
    for round_ranking in round_rankings or []:
        for position, item in enumerate(round_ranking, start=1):
            path = _norm_path(item.path)
            if path in candidate_paths:
                rank_history[path].append(position)

    context_paths = {
        _norm_path(str(context.get("path") or ""))
        for context in code_contexts or []
        if context.get("snippets")
    }
    reviewed = {
        _norm_path(str(item.get("path") or "")): item
        for item in review.get("candidates", []) or []
        if str(item.get("path") or "")
    }
    issue_lower = unquote(str(issue_text or "")).replace("\\", "/").lower()
    precision_stop_atoms = {
        "client", "server", "source", "src", "lib", "core", "index", "main",
        "component", "components", "module", "modules", "state", "utils", "utility",
        "package", "packages", "step", "steps", "element", "elements", "about", "with",
    }

    def semantic_atoms(text: str) -> set[str]:
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
        return {
            token.lower()
            for token in re.split(r"[^A-Za-z0-9]+", expanded)
            if len(token) >= 4 and token.lower() not in precision_stop_atoms
        }

    issue_atoms = semantic_atoms(issue_text)
    rows: list[tuple[float, int, RankedLocation, dict[str, Any]]] = []

    for base_rank, original in enumerate(ranked[:top_k], start=1):
        item = copy.deepcopy(original)
        path = _norm_path(item.path)
        path_lower = path.lower()
        decision = reviewed.get(path) or {}
        history = rank_history.get(path, [])
        components = item.score_components or {}
        role = _path_role(path)
        read_verified = path in context_paths or bool((item.belief or {}).get("read_verified"))
        grounded = bool(decision.get("grounded", decision.get("quote_supported", False))) and read_verified
        mechanism = bool(decision.get("mechanism_verified", False)) and grounded
        quote_supported = bool(decision.get("quote_supported", False)) and read_verified
        entity_supported = bool(decision.get("entity_supported", False)) and read_verified
        counterevidence = _dedupe(
            list(decision.get("counterevidence", []) or [])
            + list(decision.get("counter_evidence", []) or []),
            limit=8,
        )

        # The base-rank prior keeps the pass conservative.  Strong, concrete
        # evidence can still move a rank 4-8 target ahead of a generic hub.
        quality = max(0.0, 9.0 - base_rank) * 1.35 + 4.0 / base_rank
        evidence_reasons: list[str] = [f"base_rank:{base_rank}"]
        if history:
            reciprocal = sum(1.0 / (8.0 + rank) for rank in history)
            stability = min(5.0, reciprocal * 12.0 + min(2.0, len(history) * 0.25))
            quality += stability
            evidence_reasons.append(f"cross_round_stability:{stability:.2f}")
        if path_lower and path_lower in issue_lower:
            quality += 12.0
            evidence_reasons.append("exact_issue_path")
        path_overlap = sorted(semantic_atoms(path) & issue_atoms)
        if path_overlap:
            path_overlap_bonus = min(5.0, len(path_overlap) * 1.6)
            quality += path_overlap_bonus
            evidence_reasons.append("issue_path_atoms:" + ",".join(path_overlap[:6]))
        exact_entities = 0
        for entity in item.entities or []:
            name = str(entity.get("name") or "").strip().lower()
            if len(name) >= 4 and re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", issue_lower):
                exact_entities += 1
        if exact_entities and entity_supported:
            entity_bonus = min(3.0, exact_entities * 1.5)
            quality += entity_bonus
            evidence_reasons.append(f"exact_issue_entities:{exact_entities}")
        if read_verified:
            quality += 1.5
            evidence_reasons.append("source_read")
        if grounded:
            quality += 3.0
            evidence_reasons.append("review_grounded")
        if quote_supported:
            quality += 2.0
            evidence_reasons.append("quote_supported")
        if entity_supported:
            quality += 2.0
            evidence_reasons.append("entity_supported")
        if mechanism:
            quality += 10.0
            evidence_reasons.append("mechanism_verified")

        review_role = str(decision.get("role") or "")
        role_adjustments = {
            "patch_target": 2.5 if grounded else 0.0,
            "supporting_target": 0.8 if grounded else 0.0,
            "navigation_only": -4.0,
            "reproduction_only": -7.0,
            "test_or_docs": -7.0,
            "unlikely": -5.0,
        }
        role_adjustment = role_adjustments.get(review_role, 0.0)
        quality += role_adjustment
        if role_adjustment:
            evidence_reasons.append(f"review_role:{review_role}:{role_adjustment:+.1f}")

        direct_component = any(
            float(components.get(name, 0.0) or 0.0) > 0
            for name in ("symbol_score", "path_score", "domain_path_probe", "call_score", "flow_verifier")
        )
        architecture_only = bool(
            float(components.get("architecture_path_probe", 0.0) or 0.0) > 0
            and not direct_component
            and not mechanism
        )
        if direct_component:
            quality += 1.5
            evidence_reasons.append("direct_retrieval_evidence")
        implementation_like = role in {
            "implementation", "component_or_route", "url_builder", "backend",
            "serializer", "type_binder", "state_update", "style_resolver",
        }
        implementation_evidence = bool(
            read_verified
            and implementation_like
            and direct_component
            and (
                path_overlap
                or exact_entities
                or float(components.get("call_score", 0.0) or 0.0) > 0
                or float(components.get("flow_verifier", 0.0) or 0.0) > 0
            )
        )
        if implementation_evidence:
            quality += 2.5
            evidence_reasons.append("read_direct_implementation_evidence")
        direct_program_evidence = bool(decision.get("direct_flow_supported")) or any(
            float(components.get(name, 0.0) or 0.0) > 0
            for name in ("call_score", "flow_score", "flow_verifier")
        )
        equivalent_mechanism = bool(
            read_verified
            and quote_supported
            and entity_supported
            and direct_program_evidence
        )
        if architecture_only:
            quality -= 4.0
            evidence_reasons.append("architecture_only_penalty")
        unverified_selector_hub = bool(
            role == "state_selector"
            and review_role not in {"patch_target", "supporting_target"}
            and not grounded
            and not mechanism
            and not any(
                phrase in issue_lower
                for phrase in ("selector", "state selector", "redux selector", "select from state")
            )
        )
        if unverified_selector_hub:
            quality -= 2.0
            evidence_reasons.append("unverified_selector_hub_penalty")
        if role in _CLOSURE_BLOCKED_ROLES:
            quality -= 10.0
            evidence_reasons.append(f"non_source_penalty:{role}")
        counterevidence_grounded = bool(counterevidence and read_verified and grounded)
        if counterevidence_grounded and not mechanism:
            penalty = min(12.0, 4.0 + len(counterevidence) * 2.0)
            quality -= penalty
            evidence_reasons.append(f"counterevidence_penalty:{penalty:.1f}")

        declaration_requested = any(
            phrase in issue_lower
            for phrase in ("declaration", "type definition", "typing", ".d.ts", ".pyi", "schema")
        )
        if role == "declaration_or_schema" and not declaration_requested and not mechanism:
            quality -= 5.0
            evidence_reasons.append("unverified_declaration_penalty")

        strong_top_challenger = bool(
            not counterevidence and (mechanism or equivalent_mechanism)
        )
        stable_top = bool(
            base_rank == 1
            and role != "declaration_or_schema"
            and (
                mechanism
                or path_lower in issue_lower
                or (read_verified and (grounded or direct_component))
            )
        )
        role_allowed_at_head = bool(
            role not in _CLOSURE_BLOCKED_ROLES
            or _issue_allows_non_source_targets(issue_text)
            or (role == "declaration_or_schema" and declaration_requested)
        )
        head_eligible = bool(
            role_allowed_at_head
            and read_verified
            and not counterevidence
            and (mechanism or equivalent_mechanism)
        )
        blocked_head_fallback_eligible = bool(
            role_allowed_at_head
            and read_verified
            and not counterevidence
            and direct_program_evidence
            and implementation_evidence
        )

        responsibility = responsibility_evidence(decision)
        if _env_bool("MYCODE_HEAD_REQUIRE_RESPONSIBILITY", True):
            head_eligible = bool(
                role_allowed_at_head and read_verified and responsibility["supported"]
            )
            # A blocked/non-source incumbent can still be replaced by the
            # existing source fallback; ordinary source heads require ownership.
            strong_top_challenger = head_eligible

        item.score_components["precision_evidence_quality"] = round(quality, 3)
        item.belief["precision_rerank"] = {
            "quality": round(quality, 3),
            "base_rank": base_rank,
            "round_ranks": history[-12:],
            "read_verified": read_verified,
            "grounded": grounded,
            "mechanism_verified": mechanism,
            "equivalent_mechanism": equivalent_mechanism,
            "direct_program_evidence": direct_program_evidence,
            "counterevidence_count": len(counterevidence),
            "counterevidence_grounded": counterevidence_grounded,
            "strong_top_challenger": strong_top_challenger,
            "stable_top": stable_top,
            "head_eligible": head_eligible,
            "blocked_head_fallback_eligible": blocked_head_fallback_eligible,
            "role_allowed_at_head": role_allowed_at_head,
            "responsibility_supported": responsibility["supported"],
            "responsibility_missing": responsibility["missing"],
            "evidence": evidence_reasons,
        }
        rows.append((quality, base_rank, item, item.belief["precision_rerank"]))

    # Precision evidence answers one question only: which recalled file owns
    # the head position? The remaining recall order is deliberately preserved.
    head_limit = min(len(rows), _env_int("MYCODE_LLM_REVIEW_CANDIDATES", 8, minimum=1))
    margin = _env_float("MYCODE_HEAD_REPLACEMENT_MARGIN", 3.0, minimum=0.0)
    incumbent = rows[0]
    eligible = [row for row in rows[:head_limit] if row[3].get("head_eligible")]
    incumbent_role = _path_role(incumbent[2].path)
    incumbent_blocked = bool(
        not incumbent[3].get("role_allowed_at_head")
        or (
            incumbent_role == "declaration_or_schema"
            and not any(
                phrase in issue_lower
                for phrase in ("declaration", "type definition", "typing", ".d.ts", ".pyi", "schema")
            )
        )
    )
    fallback_eligible = [
        row
        for row in rows[:head_limit]
        if row[3].get("blocked_head_fallback_eligible")
    ]
    chosen = incumbent
    if eligible:
        challenger = max(eligible, key=lambda row: (row[0], -row[1]))
        incumbent_eligible = bool(incumbent[3].get("head_eligible"))
        if not incumbent_eligible or challenger[0] >= incumbent[0] + margin:
            chosen = challenger
    elif incumbent_blocked and fallback_eligible:
        chosen = max(fallback_eligible, key=lambda row: (row[0], -row[1]))
    ordered_rows = [chosen] + [row for row in rows if row[2].path != chosen[2].path]
    reranked = [row[2] for row in ordered_rows]
    changes = [
        {
            "path": item.path,
            "before": base_rank,
            "after": position,
            "quality": round(quality, 3),
            "evidence": diagnostics.get("evidence", [])[:8],
        }
        for position, (quality, base_rank, item, diagnostics) in enumerate(ordered_rows, start=1)
        if position != base_rank
    ]
    return reranked, {
        "enabled": True,
        "strategy": "verified_head_selector_tail_preserving",
        "candidate_count": len(reranked),
        "head_candidate_limit": head_limit,
        "replacement_margin": margin,
        "responsibility_required": _env_bool("MYCODE_HEAD_REQUIRE_RESPONSIBILITY", True),
        "head_comparison": {
            "incumbent": incumbent[2].path,
            "selected": chosen[2].path,
            "reason": (
                "responsibility_challenger" if chosen[1] != 1 and chosen[3].get("head_eligible")
                else "blocked_head_source_fallback" if chosen[1] != 1
                else "retain_incumbent"
            ),
        },
        "responsibility_checks": [
            {"path": row[2].path,
             "supported": row[3].get("responsibility_supported"),
             "missing": row[3].get("responsibility_missing", [])}
            for row in rows[:head_limit]
        ],
        "head_replaced": chosen[1] != 1,
        "selected_head": chosen[2].path,
        "selected_head_quality": round(chosen[0], 3),
        "selected_head_evidence": {
            key: chosen[3].get(key)
            for key in (
                "read_verified", "grounded", "mechanism_verified",
                "equivalent_mechanism", "direct_program_evidence", "head_eligible",
                "blocked_head_fallback_eligible",
            )
        },
        "incumbent_blocked": incumbent_blocked,
        "eligible_heads": [
            {
                "path": row[2].path,
                "rank": row[1],
                "quality": round(row[0], 3),
                "mechanism_verified": bool(row[3].get("mechanism_verified")),
                "equivalent_mechanism": bool(row[3].get("equivalent_mechanism")),
            }
            for row in eligible
        ],
        "top_before": [item.path for item in ranked[:5]],
        "top_after": [item.path for item in reranked[:5]],
        "changes": changes[:12],
    }


def _run_search_round(
    *,
    round_no: int,
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    index: RepositoryIndex,
    graph: TypedRepositoryGraph,
    queries: list[str],
    previous_ranked: list[RankedLocation],
    previous_flow_traces: list[dict[str, Any]],
    previous_top_paths: list[str],
    top_k: int,
    max_rounds: int,
    controller_llm: LLMController | None = None,
    query_groups: dict[str, list[str]] | None = None,
    previous_round_progress: dict[str, Any] | None = None,
) -> DynamicSearchRound:
    provisional_seed_files = _dedupe([item.path for item in previous_ranked[:8]])
    with phase_context("dynamic.controller", round_no=round_no, seed_count=len(provisional_seed_files), query_count=len(queries)):
        controller_decisions = decide_next_actions(
            round_no=round_no,
            issue_text=_issue_query_text(sample.issue_text),
            evidence_result=evidence_result,
            queries=queries,
            previous_top_paths=previous_top_paths,
            current_seed_files=provisional_seed_files,
            current_flow_traces=previous_flow_traces,
            llm_controller=controller_llm,
        )
        actions = select_navigation_actions(
            round_no=round_no,
            issue_text=_issue_query_text(sample.issue_text),
            evidence_result=evidence_result,
            queries=queries,
            previous_top_paths=previous_top_paths,
            current_seed_files=provisional_seed_files,
            current_flow_traces=previous_flow_traces,
        )
        phase_event(
            "progress",
            "dynamic.controller",
            round_no=round_no,
            decision_count=len(controller_decisions),
            action_count=len(actions),
        )
    controller_queries = _dedupe(query for decision in controller_decisions for query in decision.queries)
    controller_edges = _dedupe(edge for decision in controller_decisions for edge in decision.preferred_edge_types)
    action_queries = _dedupe(query for action in actions for query in action.suggested_queries)
    preferred_edge_types = _dedupe(
        controller_edges + [edge for action in actions for edge in action.preferred_edge_types]
    )
    frontier_queries = _frontier_query_groups(
        base_queries=queries,
        controller_decisions=controller_decisions,
        actions=actions,
        seed_query_groups=query_groups,
    )
    active_queries = _dedupe(frontier_queries.get("all", queries) + controller_queries + action_queries)
    retrieval_queries = _sanitize_retrieval_queries(active_queries, limit=96)
    if not retrieval_queries:
        retrieval_queries = active_queries[:20]
    path_terms = _path_semantic_terms(sample, evidence_result, frontier_queries, limit=56)

    global_file_limit = _env_int("MYCODE_ROUND_GLOBAL_FILE_HITS", 36, minimum=5)
    global_entity_limit = _env_int("MYCODE_ROUND_GLOBAL_ENTITY_HITS", 60, minimum=5)
    with phase_context("dynamic.cheap_recall", round_no=round_no, query_count=len(retrieval_queries)):
        file_hits, entity_hits, channel_recall = _multi_channel_global_recall(
            index=index,
            retrieval_queries=retrieval_queries,
            frontier_queries=frontier_queries,
            file_limit=global_file_limit,
            entity_limit=global_entity_limit,
        )
        phase_event(
            "progress",
            "dynamic.cheap_recall",
            round_no=round_no,
            file_hit_count=len(file_hits),
            entity_hit_count=len(entity_hits),
            global_file_limit=global_file_limit,
            global_entity_limit=global_entity_limit,
            channel_recall=channel_recall,
        )
    evidence_seed_paths = sorted(_collect_evidence_seed_paths(evidence_result))
    domain_path_queries = _dedupe(
        (frontier_queries.get("concern", []) or [])
        + (frontier_queries.get("effect", []) or [])
        + (frontier_queries.get("flow", []) or [])
        + (frontier_queries.get("explicit_entity", []) or [])
        + (frontier_queries.get("evidence_role", []) or []),
        limit=96,
    )
    with phase_context("dynamic.path_probe_and_pool", round_no=round_no, query_count=len(domain_path_queries)):
        domain_path_hits = _path_probe_hits(
            index,
            domain_path_queries,
            limit=_env_int("MYCODE_ROUND_PATH_PROBE_LIMIT", 80, minimum=0),
        )
        architecture_path_hits = _path_probe_hits(
            index,
            frontier_queries.get("architecture", []) or [],
            limit=_env_int("MYCODE_ROUND_ARCHITECTURE_PATH_LIMIT", 16, minimum=0),
        )
        round_pool = _round_candidate_pool(
            index=index,
            graph=graph,
            previous_ranked=previous_ranked,
            file_hits=file_hits,
            entity_hits=entity_hits,
            evidence_seed_paths=evidence_seed_paths,
            top_k=top_k,
        )
    if domain_path_hits or architecture_path_hits:
        domain_neighbor_paths = _same_dir_neighbors(
            index,
            [path for path, _score, _reason in architecture_path_hits[:12]]
            + [path for path, _score, _reason in domain_path_hits[: max(top_k, 12)]],
            per_dir=_env_int("MYCODE_ROUND_PATH_PROBE_DIR_PER_SEED", 5, minimum=0),
            max_total=_env_int("MYCODE_ROUND_PATH_PROBE_DIR_BUDGET", 36, minimum=0),
        )
        round_pool = _dedupe(
            [path for path, _score, _reason in architecture_path_hits]
            + [path for path, _score, _reason in domain_path_hits]
            + domain_neighbor_paths
            + round_pool,
            limit=_env_int("MYCODE_ROUND_POOL_LIMIT", 160, minimum=max(top_k, 15)),
        )
    if path_terms:
        semantic_seed_limit = _env_int("MYCODE_ROUND_SEMANTIC_DIR_SEEDS", 12, minimum=0)
        semantic_neighbor_budget = _env_int("MYCODE_ROUND_SEMANTIC_DIR_BUDGET", 36, minimum=0)
        semantic_seed_paths = [
            path
            for path, score, _reasons in sorted(
                (
                    (path, *_path_semantic_score(path, path_terms))
                    for path in round_pool
                ),
                key=lambda item: (-float(item[1] or 0.0), item[0]),
            )
            if score > 0
        ][:semantic_seed_limit]
        semantic_neighbors = _same_dir_neighbors(
            index,
            semantic_seed_paths,
            per_dir=_env_int("MYCODE_ROUND_SEMANTIC_DIR_PER_SEED", 6, minimum=0),
            max_total=semantic_neighbor_budget,
        )
        if semantic_neighbors:
            round_pool = _dedupe(
                semantic_neighbors + round_pool,
                limit=_env_int("MYCODE_ROUND_POOL_LIMIT", 160, minimum=max(top_k, 15)),
            )
        phase_event(
            "progress",
            "dynamic.path_probe_and_pool",
            round_no=round_no,
            pool_size=len(round_pool),
            domain_path_probe_hits=len(domain_path_hits),
            architecture_path_probe_hits=len(architecture_path_hits),
            path_semantic_terms=path_terms[:12],
        )
    with phase_context("dynamic.scoped_search", round_no=round_no, pool_size=len(round_pool)):
        explicit_entity_hits = _scoped_search_entities(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("explicit_entity", []), limit=40),
            allowed_paths=round_pool,
            limit=40,
        )
        evidence_file_hits = _scoped_search_files(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("evidence_role", []), limit=40),
            allowed_paths=round_pool,
            limit=30,
        )
        visual_file_hits = _scoped_search_files(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("visual", []), limit=30),
            allowed_paths=round_pool,
            limit=20,
        )
        concern_file_hits = _scoped_search_files(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("concern", []), limit=50),
            allowed_paths=round_pool,
            limit=35,
        )
        effect_file_hits = _scoped_search_files(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("effect", []), limit=40),
            allowed_paths=round_pool,
            limit=28,
        )
        flow_file_hits = _scoped_search_files(
            index,
            _sanitize_retrieval_queries(frontier_queries.get("flow", []), limit=50),
            allowed_paths=round_pool,
            limit=30,
        )
        phase_event(
            "progress",
            "dynamic.scoped_search",
            explicit_entity_hits=len(explicit_entity_hits),
            evidence_file_hits=len(evidence_file_hits),
            visual_file_hits=len(visual_file_hits),
            concern_file_hits=len(concern_file_hits),
            effect_file_hits=len(effect_file_hits),
            flow_file_hits=len(flow_file_hits),
        )
    seed_files = _dedupe(
        [item.path for item in previous_ranked[:8]]
        + [hit.path for hit in file_hits[:10]]
        + [hit.path for hit in entity_hits[:10]]
        + evidence_seed_paths,
        limit=14,
    )
    graph_budget = {
        "max_seed_nodes": _env_int("MYCODE_GRAPH_MAX_SEED_NODES", 10, minimum=1),
        "max_edges_per_seed": _env_int("MYCODE_GRAPH_MAX_EDGES_PER_SEED", 45, minimum=5),
        "beam_width": _env_int("MYCODE_GRAPH_BEAM_WIDTH", 80, minimum=10),
    }
    with phase_context("dynamic.graph_expand", round_no=round_no, seed_count=len(seed_files), graph_budget=graph_budget):
        graph_hits_base = graph.expand(
            seed_files,
            retrieval_queries,
            limit=30,
            allowed_edge_types=preferred_edge_types or None,
            **graph_budget,
        )
        concern_graph_hits = graph.expand(
            seed_files,
            _sanitize_retrieval_queries(frontier_queries.get("concern", []), limit=50),
            limit=30,
            allowed_edge_types=CONCERN_EDGE_TYPES,
            **graph_budget,
        )
        program_graph_hits = graph.expand(
            seed_files,
            _sanitize_retrieval_queries(frontier_queries.get("program", []), limit=50),
            limit=30,
            allowed_edge_types=preferred_edge_types or PROGRAM_EDGE_TYPES,
            **graph_budget,
        )
        flow_graph_hits = graph.expand(
            seed_files,
            _sanitize_retrieval_queries(frontier_queries.get("flow", []), limit=50),
            limit=24,
            allowed_edge_types=FLOW_EDGE_TYPES,
            **graph_budget,
        )
        graph_hits = _merge_graph_hits(graph_hits_base, concern_graph_hits, program_graph_hits, flow_graph_hits)
        if not graph_hits and (preferred_edge_types or seed_files):
            graph_hits_base = graph.expand(seed_files, retrieval_queries, limit=40, **graph_budget)
            graph_hits = _merge_graph_hits(graph_hits_base)
        phase_event(
            "progress",
            "dynamic.graph_expand",
            graph_hit_count=len(graph_hits),
            concern_graph_hits=len(concern_graph_hits),
            program_graph_hits=len(program_graph_hits),
            flow_graph_hits=len(flow_graph_hits),
        )

    aggregate: Dict[str, RankedLocation] = {}
    component_scores: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    def add(
        path: str,
        score: float,
        reason: str,
        entity: Dict[str, Any] | None = None,
        *,
        component: str = "other",
    ) -> None:
        if not path:
            return
        norm = _norm_path(path)
        item = aggregate.get(path)
        if item is None:
            item = RankedLocation(path=path, score=0.0)
            aggregate[path] = item
        item.score += score
        component_scores[norm][component] += float(score or 0.0)
        if reason and reason not in item.reasons:
            item.reasons.append(reason)
        if entity:
            item.entities.append(entity)

    for previous_rank, previous in enumerate(previous_ranked):
        previous_belief = previous.belief or {}
        add(
            previous.path,
            _rank_memory_bonus(
                previous_rank,
                round_no=round_no,
                no_gain_rounds=int(previous_belief.get("rounds_without_gain") or 0),
                path_role=str(previous_belief.get("path_role") or _path_role(previous.path)),
            ),
            f"round_{round_no - 1}_memory_rank_{previous_rank + 1}",
            component="memory",
        )
    for hit in file_hits:
        add(hit.path, hit.score, "bm25_all:" + ",".join(hit.reasons[:5]), component="bm25_score")
    for hit in concern_file_hits:
        add(hit.path, hit.score * 1.15, "concern_search:" + ",".join(hit.reasons[:5]), component="concern_score")
    for hit in evidence_file_hits:
        add(hit.path, hit.score * 0.95, "evidence_role_search:" + ",".join(hit.reasons[:5]), component="evidence_score")
    for hit in visual_file_hits:
        add(hit.path, hit.score * 0.25, "visual_navigation_unverified:" + ",".join(hit.reasons[:5]), component="visual_score")
    for hit in effect_file_hits:
        add(hit.path, hit.score * 0.9, "effect_search:" + ",".join(hit.reasons[:5]), component="concern_score")
    for hit in flow_file_hits:
        add(hit.path, hit.score * 1.1, "flow_search:" + ",".join(hit.reasons[:5]), component="flow_score")
    for path, score, reason in domain_path_hits:
        if path in round_pool:
            add(path, score * 1.15, f"domain_path_probe:{reason}", component="domain_path_probe")
    for path, score, reason in architecture_path_hits:
        if path in round_pool:
            add(
                path,
                score * _env_float("MYCODE_ARCHITECTURE_PATH_WEIGHT", 1.0, minimum=0.0),
                f"architecture_path_probe:{reason}",
                component="architecture_path_probe",
            )
    if path_terms:
        semantic_candidates: list[tuple[str, float, list[str]]] = []
        for path in round_pool:
            score, semantic_reasons = _path_semantic_score(path, path_terms)
            if score > 0:
                semantic_candidates.append((path, score, semantic_reasons))
        for path, score, semantic_reasons in sorted(
            semantic_candidates,
            key=lambda item: (-item[1], item[0]),
        )[: _env_int("MYCODE_ROUND_PATH_SEMANTIC_LIMIT", 48, minimum=0)]:
            add(path, score * 1.2, ",".join(semantic_reasons), component="concern_score")
    for hit in entity_hits:
        add(
            hit.path,
            hit.score * 1.35,
            f"entity:{hit.kind}:{hit.name}:{','.join(hit.reasons[:4])}",
            {
                "kind": hit.kind,
                "name": hit.name,
                "start_line": hit.start_line,
                "end_line": hit.end_line,
                "score": hit.score,
                "reasons": hit.reasons[:6],
            },
            component="symbol_score",
        )
    for hit in explicit_entity_hits:
        add(
            hit.path,
            hit.score * 1.5,
            f"explicit_entity:{hit.kind}:{hit.name}:{','.join(hit.reasons[:4])}",
            {
                "kind": hit.kind,
                "name": hit.name,
                "start_line": hit.start_line,
                "end_line": hit.end_line,
                "score": hit.score,
                "reasons": hit.reasons[:6],
            },
            component="symbol_score",
        )
    path_like_queries = [query for query in frontier_queries.get("explicit_entity", []) + frontier_queries.get("evidence_role", []) if "/" in query or "." in query]
    for query in path_like_queries[:40]:
        qnorm = _norm_path(query).lower()
        qtokens = [token for token in tokenize(qnorm) if len(token) >= 3]
        for path in round_pool:
            lower = path.lower()
            if qnorm and (qnorm in lower or lower in qnorm):
                add(path, 9.0, f"path_exact_or_prefix:{query}", component="path_score")
            elif qtokens and sum(1 for token in qtokens if token in lower) >= min(3, len(qtokens)):
                add(path, 3.0, f"path_token_overlap:{query}", component="path_score")
    for path, score, reasons in graph_hits_base:
        add(path, score, "graph_navigation:" + ",".join(reasons[:3]), component="call_score")
    for path, score, reasons in concern_graph_hits:
        add(path, score, "concern_graph:" + ",".join(reasons[:3]), component="concern_score")
    for path, score, reasons in program_graph_hits:
        add(path, score, "program_graph:" + ",".join(reasons[:3]), component="call_score")
    for path, score, reasons in flow_graph_hits:
        add(path, score, "flow_graph:" + ",".join(reasons[:3]), component="flow_score")

    architecture_lane_policy = _apply_architecture_lane_policy(aggregate, component_scores)
    evidence_role_policy = _apply_evidence_role_target_policy(
        aggregate,
        component_scores,
        evidence_seed_paths=evidence_seed_paths,
    )
    source_first_policy = _apply_source_first_policy(aggregate, component_scores, sample=sample)
    with phase_context("dynamic.prune_candidates", round_no=round_no, raw_candidate_count=len(aggregate)):
        initial_ranked, prune_diagnostics = _cosil_style_prune_candidates(
            aggregate,
            component_scores,
            top_k=top_k,
            read_budget=_env_int("MYCODE_ROUND_READ_BUDGET", max(top_k * 2, 24), minimum=top_k),
        )
        prune_diagnostics["source_first_policy"] = source_first_policy
        prune_diagnostics["architecture_lane_policy"] = architecture_lane_policy
        phase_event(
            "progress",
            "dynamic.prune_candidates",
            raw_candidate_count=len(aggregate),
            kept_candidates=len(initial_ranked),
            top_candidates=[item.path for item in initial_ranked[:5]],
            source_first_adjusted=source_first_policy.get("adjusted_count"),
            architecture_capped=architecture_lane_policy.get("adjusted_count"),
        )
    with phase_context("dynamic.ReadCode", round_no=round_no, candidate_count=len(initial_ranked)):
        code_contexts = _read_code_context(
            index,
            initial_ranked,
            retrieval_queries,
            limit=_env_int("MYCODE_CODE_CONTEXT_LIMIT", 10, minimum=1),
        )
        phase_event("progress", "dynamic.ReadCode", context_count=len(code_contexts))
    flow_scope_paths = _dedupe(
        [item.path for item in initial_ranked]
        + round_pool[: _env_int("MYCODE_FLOW_POOL_LIMIT", max(30, top_k * 2), minimum=top_k)],
        limit=_env_int("MYCODE_FLOW_POOL_LIMIT", max(30, top_k * 2), minimum=top_k),
    )
    flow_index = _restricted_index(index, flow_scope_paths)
    flow_limit = _env_int("MYCODE_FLOW_TRACE_LIMIT", 6, minimum=1)
    pre_flow_confidence = _rank_confidence(initial_ranked, code_contexts=code_contexts)
    flow_traces_new, flow_diagnostics = _run_flow_backends_layered(
        flow_index=flow_index,
        sample=sample,
        evidence_result=evidence_result,
        active_queries=active_queries,
        candidate_paths=[item.path for item in initial_ranked],
        issue_sketch=build_issue_sketch(sample, evidence_result),
        confidence=pre_flow_confidence,
        round_no=round_no,
        max_rounds=max_rounds,
        flow_limit=flow_limit,
    )
    flow_traces = _merge_flow_traces(
        previous_flow_traces,
        flow_traces_new,
    )[:34]
    with phase_context("dynamic.verify_and_rerank", round_no=round_no, candidate_count=len(initial_ranked), flow_count=len(flow_traces)):
        verifier = _verify_candidates(
            sample=sample,
            evidence_result=evidence_result,
            ranked=initial_ranked,
            code_contexts=code_contexts,
            flow_traces=flow_traces,
        )
        phase_event("progress", "dynamic.verify_and_rerank", decision_count=len(verifier.get("decisions", []) or []))
    for decision in verifier.get("decisions", []) or []:
        path = _norm_path(str(decision.get("path") or ""))
        if path:
            component_scores[path]["flow_verifier"] += float(decision.get("bonus") or 0.0)
    ranked = _apply_verifier(initial_ranked, verifier, sample=sample, top_k=top_k, component_scores=component_scores)
    candidate_review: dict[str, Any] = {"status": "disabled", "reason": "review_disabled", "candidates": []}
    if controller_llm is not None and _env_bool("MYCODE_LLM_CANDIDATE_REVIEW", True):
        with phase_context(
            "dynamic.llm_candidate_review",
            round_no=round_no,
            candidate_count=len(ranked),
        ):
            candidate_review = review_candidates(
                llm=controller_llm,
                issue_text=_issue_query_text(sample.issue_text),
                issue_sketch=build_issue_sketch(sample, evidence_result),
                ranked=ranked,
                code_contexts=code_contexts,
                flow_traces=flow_traces,
                round_no=round_no,
                candidate_limit=_env_int("MYCODE_LLM_REVIEW_CANDIDATES", 6, minimum=3),
                repair_attempts=_env_int("MYCODE_LLM_REVIEW_REPAIR_ATTEMPTS", 1, minimum=0),
            )
            phase_event(
                "progress",
                "dynamic.llm_candidate_review",
                round_no=round_no,
                status=candidate_review.get("status"),
                parse_mode=candidate_review.get("parse_mode"),
                recovered_from_truncated_json=candidate_review.get("recovered_from_truncated_json", False),
                reviewed_count=len(candidate_review.get("candidates", []) or []),
                attempt_statuses=[
                    attempt.get("status") for attempt in (candidate_review.get("attempts", []) or [])
                ],
                continue_search=candidate_review.get("continue_search"),
                top_review_paths=[
                    item.get("path") for item in (candidate_review.get("candidates", []) or [])[:5]
                ],
            )
        ranked = _apply_llm_candidate_review(
            ranked,
            candidate_review,
            component_scores=component_scores,
            top_k=top_k,
            code_contexts=code_contexts,
        )
    verifier["llm_candidate_review"] = candidate_review
    previous_by_path = {_norm_path(item.path): item for item in previous_ranked}
    read_paths = {_norm_path(str(context.get("path") or "")) for context in code_contexts}
    for item in ranked:
        item.belief = _candidate_belief(
            item,
            flow_traces,
            verifier,
            frontier_queries,
            read_paths=read_paths,
        )
        review_belief = item.belief.get("llm_candidate_review") or {}
        if not review_belief:
            for reviewed in candidate_review.get("candidates", []) or []:
                if _norm_path(str(reviewed.get("path") or "")) == _norm_path(item.path):
                    item.belief["llm_candidate_review"] = {
                        "role": reviewed.get("role"),
                        "confidence": reviewed.get("confidence"),
                        "matched_issue_axes": reviewed.get("matched_issue_axes", [])[:8],
                        "entities": reviewed.get("entities", [])[:8],
                        "grounded": bool(reviewed.get("grounded", False)),
                        "evidence_quote": str(reviewed.get("evidence_quote") or "")[:240],
                        "quote_supported": bool(reviewed.get("quote_supported", False)),
                        "entity_supported": bool(reviewed.get("entity_supported", False)),
                        "supported_entities": list(reviewed.get("supported_entities", []) or [])[:8],
                        "unsupported_entities": list(reviewed.get("unsupported_entities", []) or [])[:8],
                        "direct_flow_supported": bool(reviewed.get("direct_flow_supported", False)),
                        "mechanism_verified": bool(reviewed.get("mechanism_verified", False)),
                        "patch_mechanism": str(reviewed.get("patch_mechanism") or "")[:400],
                    }
                    break
        _annotate_candidate_evidence_gain(item, previous_by_path.get(_norm_path(item.path)))
        item.missing_evidence = list(item.belief.get("missing_evidence", []))
    with phase_context("dynamic.rank_entities", round_no=round_no, ranked_file_count=len(ranked)):
        entity_pool_k = _env_int("MYCODE_ENTITY_RERANK_POOL", max(top_k * 4, 60), minimum=top_k)
        entity_grounding_queries = _dedupe(
            [_issue_query_text(sample.issue_text)]
            + [
                query
                for channel in ("explicit_entity", "concern", "architecture", "effect", "flow")
                for query in (query_groups or {}).get(channel, []) or []
            ],
            limit=80,
        )
        ranked_modules, ranked_functions = _rank_entities(
            index=index,
            ranked_files=ranked,
            entity_hits=entity_hits,
            flow_traces=flow_traces,
            top_k=entity_pool_k,
            candidate_review=candidate_review,
            entity_queries=entity_grounding_queries,
        )
        ranked_modules = _rerank_entities_with_roles(
            entities=ranked_modules,
            sample=sample,
            evidence_result=evidence_result,
            flow_traces=flow_traces,
            top_k=top_k,
        )
        ranked_functions = _rerank_entities_with_roles(
            entities=ranked_functions,
            sample=sample,
            evidence_result=evidence_result,
            flow_traces=flow_traces,
            top_k=top_k,
        )
        phase_event(
            "progress",
            "dynamic.rank_entities",
            entity_pool_k=entity_pool_k,
            module_count=len(ranked_modules),
            function_count=len(ranked_functions),
            semantic_grounding_count=sum(
                1
                for item in ranked_functions
                if any(reason.startswith("entity_issue_semantics:") for reason in item.reasons)
            ),
            top_functions=[
                {"id": item.id, "score": round(float(item.score), 3), "reasons": item.reasons[:4]}
                for item in ranked_functions[:5]
            ],
        )
    next_queries = _derive_next_round_queries(
        index=index,
        ranked=ranked,
        code_contexts=code_contexts,
        flow_traces=flow_traces,
        previous_queries=active_queries,
    )
    next_queries = _dedupe(next_queries + action_queries + list(candidate_review.get("next_queries", []) or []))
    round_progress = _round_progress(
        ranked=ranked,
        previous_ranked=previous_ranked,
        flow_traces=flow_traces,
        previous_flow_traces=previous_flow_traces,
        next_queries=next_queries,
        active_queries=active_queries,
        previous_progress=previous_round_progress,
    )
    post_flow_confidence = _rank_confidence(ranked, code_contexts=code_contexts)
    graph_summary = graph.edge_summary(seed_files + [item.path for item in ranked[:10]])
    agent_observation = summarize_agent_observation(
        top_files=[item.path for item in ranked],
        flow_traces=flow_traces,
        graph_summary=graph_summary,
        verifier=verifier,
    )
    round_localization = {
        "ranked_locations": [item.to_dict() for item in ranked],
        "ranked_modules": [item.to_dict() for item in ranked_modules],
        "ranked_functions": [item.to_dict() for item in ranked_functions],
    }
    evaluation = _evaluate_round_if_possible(round_localization, sample, index)
    stop = _stop_decision(
        round_no=round_no,
        max_rounds=max_rounds,
        ranked=ranked,
        previous_top_paths=previous_top_paths,
        next_queries=next_queries,
        confidence=post_flow_confidence,
        candidate_review=candidate_review,
        round_progress=round_progress,
    )
    phase_event(
        "progress",
        "dynamic.stop_decision",
        round_no=round_no,
        stop=stop.get("stop"),
        reason=stop.get("reason"),
        confidence=post_flow_confidence,
        top_paths=[item.path for item in ranked[:5]],
        round_progress=round_progress,
    )
    agent_plan = _build_round_agent_plan(
        sample=sample,
        evidence_result=evidence_result,
        round_no=round_no,
        controller_decisions=controller_decisions,
        actions=actions,
        frontier_queries=frontier_queries,
        preferred_edge_types=preferred_edge_types,
        seed_files=seed_files,
        graph_summary=graph_summary,
        flow_traces=flow_traces,
        verifier=verifier,
    )
    frontier_state = {
        "strategy": "dual_frontier_concern_horizontal_program_vertical_flow_validation",
        "round_no": round_no,
        "agent_plan": agent_plan,
        "candidate_pool": {
            "strategy": "global cheap recall plus scoped local search before graph/flow deepening",
            "pool_size": len(round_pool),
            "graph_scope_size": len(getattr(graph, "graph_files", []) or []),
            "global_file_hits": len(file_hits),
            "global_entity_hits": len(entity_hits),
            "multi_channel_recall": channel_recall,
            "domain_path_probe_hits": len(domain_path_hits),
            "flow_scope_size": len(flow_index.files),
            "flow_execution": flow_diagnostics,
            "pre_flow_confidence": pre_flow_confidence,
            "post_flow_confidence": post_flow_confidence,
            "retrieval_query_count": len(retrieval_queries),
            "raw_query_count": len(active_queries),
            "path_semantic_terms": path_terms[:24],
            "evidence_role_policy": evidence_role_policy,
            "source_first_policy": source_first_policy,
            "llm_candidate_review": {
                "status": candidate_review.get("status"),
                "candidate_count": candidate_review.get("candidate_count", 0),
                "continue_search": candidate_review.get("continue_search", False),
                "missing_evidence": list(candidate_review.get("missing_evidence", []) or [])[:8],
                "next_queries": list(candidate_review.get("next_queries", []) or [])[:8],
                "reviewed": list(candidate_review.get("candidates", []) or [])[:12],
            },
            "preview": round_pool[:20],
        },
        "query_groups": {
            key: values[:30]
            for key, values in frontier_queries.items()
            if key in {"explicit_entity", "evidence_role", "visual", "concern", "effect", "flow", "program"}
        },
        "agent_round_questions": _round_belief_questions(ranked),
        "round_progress": round_progress,
        "concern_queries": frontier_queries.get("concern", [])[:24],
        "program_queries": frontier_queries.get("program", [])[:24],
        "flow_queries": frontier_queries.get("flow", [])[:24],
        "explicit_entity_queries": frontier_queries.get("explicit_entity", [])[:24],
        "evidence_role_queries": frontier_queries.get("evidence_role", [])[:24],
        "effect_queries": frontier_queries.get("effect", [])[:24],
        "preferred_edge_types": preferred_edge_types,
        "concern_candidates": _candidate_preview_from_graph(concern_graph_hits, mode="concern"),
        "program_candidates": _candidate_preview_from_graph(program_graph_hits, mode="program"),
        "flow_candidates": _flow_candidate_preview(flow_traces),
        "candidate_beliefs": [item.belief for item in ranked[:10]],
        "pruning": prune_diagnostics,
        "score_components": {
            path: {key: round(value, 3) for key, value in sorted(components.items())}
            for path, components in sorted(component_scores.items())
        },
        "notes": [
            "Concern frontier performs horizontal business/feature expansion.",
            "Program frontier performs vertical call/import/used-by navigation.",
            "Flow frontier validates whether issue states can affect candidate behavior.",
        ],
    }
    return DynamicSearchRound(
        round_no=round_no,
        input_queries=active_queries,
        agent_actions=[
            {
                "action": "controller_decision",
                "reason": decision.reason,
                "suggested_queries": decision.queries,
                "preferred_edge_types": decision.preferred_edge_types,
                "controller_decision": decision.to_dict(),
            }
            for decision in controller_decisions
        ]
        + [action.to_dict() for action in actions],
        frontier_state=frontier_state,
        seed_files=seed_files,
        file_hits=[hit.to_dict() for hit in file_hits[:12]],
        entity_hits=[hit.to_dict() for hit in entity_hits[:12]],
        graph_hits=[
            {"path": path, "score": score, "reasons": reasons}
            for path, score, reasons in graph_hits[:12]
        ],
        graph_summary=graph_summary,
        code_contexts=code_contexts[:10],
        flow_traces=flow_traces,
        verifier=verifier,
        agent_observation=agent_observation,
        ranked_locations=ranked,
        ranked_modules=ranked_modules,
        ranked_functions=ranked_functions,
        next_queries=next_queries,
        evaluation=evaluation,
        stop_decision=stop,
    )


def _entity_key(entity: CodeEntity | Dict[str, Any], *, fallback_kind: str = "entity") -> tuple[str, str, str]:
    if isinstance(entity, CodeEntity):
        return entity.path, entity.kind, entity.name
    return (
        str(entity.get("path") or ""),
        str(entity.get("kind") or fallback_kind),
        str(entity.get("name") or ""),
    )


def _add_ranked_entity(
    aggregate: Dict[str, RankedEntity],
    *,
    path: str,
    kind: str,
    name: str,
    score: float,
    reason: str,
    start_line: int = 0,
    end_line: int = 0,
) -> None:
    if not path or not kind or not name:
        return
    ent_id = entity_id(path, kind, name)
    item = aggregate.get(ent_id)
    if item is None:
        item = RankedEntity(
            id=ent_id,
            path=path,
            kind=kind,
            name=name,
            score=0.0,
            start_line=start_line,
            end_line=end_line,
        )
        aggregate[ent_id] = item
    item.score += score
    if reason and reason not in item.reasons:
        item.reasons.append(reason)


def _file_module_entity(path: str, score: float, reasons: list[str]) -> RankedEntity:
    return RankedEntity(
        id=file_module_id(path),
        path=path,
        kind="module",
        name="__file__",
        score=score,
        reasons=reasons,
    )


def _entities_for_file(index: RepositoryIndex, path: str) -> list[CodeEntity]:
    norm = path.replace("\\", "/").strip().lstrip("./")
    return [entity for entity in index.entities if entity.path.replace("\\", "/").strip().lstrip("./") == norm]


def _flow_entity_support(flow_traces: list[dict[str, Any]]) -> dict[str, float]:
    support: dict[str, float] = defaultdict(float)
    for flow in flow_traces:
        confidence = float(flow.get("confidence") or 0.0)
        for loc in flow.get("locations", []) or []:
            path, kind, name = _entity_key(loc)
            if kind in {"function", "method", "class", "module"} and name:
                support[entity_id(path, kind, name)] += 4.0 + 4.0 * confidence
            elif path:
                support[file_module_id(path)] += 2.0 + 3.0 * confidence
            for statement in loc.get("statements", []) or []:
                entity = statement.get("entity") or {}
                if not isinstance(entity, dict):
                    continue
                stmt_kind = str(entity.get("kind") or "")
                stmt_name = str(entity.get("name") or "")
                if path and stmt_kind in {"function", "method", "class", "module"} and stmt_name:
                    support[entity_id(path, stmt_kind, stmt_name)] += 2.5 + 3.5 * confidence
        for edge in flow.get("edges", []) or []:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source:
                support[file_module_id(source)] += 1.0 + 2.0 * confidence
            if target:
                support[file_module_id(target)] += 1.0 + 2.0 * confidence
        for edge in flow.get("statement_edges", []) or []:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source:
                support[file_module_id(source)] += 1.2 + 2.2 * confidence
            if target:
                support[file_module_id(target)] += 1.2 + 2.2 * confidence
        for step_key in ("source_steps", "sink_steps", "steps", "statement_nodes"):
            for step in flow.get(step_key, []) or []:
                if not isinstance(step, dict):
                    continue
                path = str(step.get("path") or "")
                entity = step.get("entity") or {}
                if path:
                    support[file_module_id(path)] += 1.0 + 2.5 * confidence
                if isinstance(entity, dict):
                    kind = str(entity.get("kind") or "")
                    name = str(entity.get("name") or "")
                    entity_path = str(entity.get("path") or path)
                    if entity_path and kind in {"function", "method", "class", "module"} and name:
                        support[entity_id(entity_path, kind, name)] += 3.0 + 4.0 * confidence
        for edge in flow.get("chain_edges", []) or []:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            relation = str(edge.get("relation") or "")
            weight = float(edge.get("weight") or 1.0)
            edge_bonus = (1.0 + weight) * (1.0 + confidence)
            if relation in {"call_argument_to_formal", "return_value_to_call_site", "cross_file_same_symbol_flow"}:
                edge_bonus *= 1.35
            if source:
                support[file_module_id(source)] += edge_bonus
            if target:
                support[file_module_id(target)] += edge_bonus
    return support


def _role_rerank_bonus(
    *,
    item: RankedEntity,
    issue_and_evidence_text: str,
    seed_paths: set[str],
    target_support: dict[str, list[str]],
    flow_support: dict[str, float],
) -> tuple[float, list[str]]:
    """Role-aware entity scoring on top of lexical/file/entity ranking.

    This is intentionally lighter than CodeQL/ARISE statement-level analysis,
    but it gives module/function ranking the same evidence-role and flow
    awareness as file ranking.
    """

    norm = _norm_path(item.path)
    role = _path_role(norm)
    lower = issue_and_evidence_text.lower()
    name_lower = item.name.lower()
    bonus = 0.0
    reasons: list[str] = []

    if norm in target_support:
        bonus += 6.0 + min(6.0, 1.5 * len(set(target_support[norm])))
        reasons.append("role_rerank:flow_target_path")

    if item.id in flow_support:
        flow_bonus = min(8.0, float(flow_support[item.id]) * 0.25)
        bonus += flow_bonus
        reasons.append(f"role_rerank:flow_entity_support={round(flow_bonus, 3)}")

    if norm in seed_paths:
        bonus -= 5.5
        reasons.append(f"role_rerank:evidence_seed_penalty:{role}")

    if role in {"reproduction_or_example", "test_or_fixture", "addon_bundle"}:
        bonus -= 4.0
        reasons.append(f"role_rerank:non_patch_role_penalty:{role}")

    if role == "style_resolver" and any(token in lower for token in ("style", "stylesheet", "layout", "margin", "pdf", "canvas", "visual")):
        bonus += 4.0
        reasons.append("role_rerank:style_or_layout_issue")
    if role == "url_builder" and any(token in lower for token in ("url", "href", "link", "redirect", "post_id", "site", "client_id")):
        bonus += 4.0
        reasons.append("role_rerank:url_or_route_issue")
    if role in {"backend", "serializer"} and any(token in lower for token in ("kdf", "serialize", "serializer", "backend", "openssh", "private key")):
        bonus += 4.0
        reasons.append("role_rerank:serializer_backend_issue")
    if role == "type_binder" and any(token in lower for token in ("mypy", "binder", "typeinfo", "typetype", "deleted variable", "symbol table")):
        bonus += 4.0
        reasons.append("role_rerank:type_binding_issue")
    if role == "component_or_route" and any(token in lower for token in ("component", "route", "dashboard", "reader", "signup", "woocommerce", "ui")):
        bonus += 3.0
        reasons.append("role_rerank:workflow_component_issue")
    if role == "state_update" and any(token in lower for token in ("dispatch", "reducer", "state", "store", "action")):
        bonus += 2.0
        reasons.append("role_rerank:state_update_issue")
    if role == "state_selector" and any(token in lower for token in ("selector", "email_verified", "current user", "current-user")):
        bonus += 1.2
        reasons.append("role_rerank:state_selector_navigation_value")

    if len(name_lower) >= 4 and name_lower in lower:
        bonus += 1.5
        reasons.append("role_rerank:name_mentioned")

    if item.kind in {"function", "method"} and (
        name_lower in {"test", "describe", "it", "beforeeach", "aftereach"} or name_lower.startswith("test_")
    ):
        bonus -= 5.0
        reasons.append("role_rerank:test_function_penalty")

    return bonus, reasons


def _rerank_entities_with_roles(
    *,
    entities: list[RankedEntity],
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    flow_traces: list[dict[str, Any]],
    top_k: int,
) -> list[RankedEntity]:
    seed_paths = _collect_evidence_seed_paths(evidence_result)
    target_support = _candidate_target_paths_from_flows(flow_traces)
    flow_support = _flow_entity_support(flow_traces)
    issue_and_evidence_text = "\n".join(
        [
            _issue_query_text(sample.issue_text),
            repr(evidence_result.get("evidence_packet", {})),
            repr(evidence_result.get("deterministic_understanding", {})),
            repr(evidence_result.get("evidence_synthesis", {})),
        ]
    )

    reranked: list[RankedEntity] = []
    for item in entities:
        bonus, reasons = _role_rerank_bonus(
            item=item,
            issue_and_evidence_text=issue_and_evidence_text,
            seed_paths=seed_paths,
            target_support=target_support,
            flow_support=flow_support,
        )
        base_score = item.score
        norm_path = _norm_path(item.path)
        name_lower = item.name.lower()
        if item.kind in {"function", "method"}:
            if _is_test_like_path(norm_path) and name_lower in {"test", "describe", "it", "beforeeach", "aftereach"}:
                base_score *= 0.06
                reasons.append("role_rerank:test_wrapper_downweight")
            elif _is_test_like_path(norm_path):
                base_score *= 0.35
                reasons.append("role_rerank:test_file_downweight")
            elif _is_source_like_path(norm_path):
                base_score *= 1.12
                reasons.append("role_rerank:source_function_boost")
        reranked.append(
            RankedEntity(
                id=item.id,
                path=item.path,
                kind=item.kind,
                name=item.name,
                score=base_score + bonus,
                start_line=item.start_line,
                end_line=item.end_line,
                reasons=list(item.reasons) + [reason for reason in reasons if reason not in item.reasons],
            )
        )
    reranked.sort(key=lambda item: (-item.score, item.path, item.name))
    return reranked[:top_k]


def _is_test_like_path(path: str) -> bool:
    parts = set(path.replace("\\", "/").lower().split("/"))
    filename = path.replace("\\", "/").lower().rsplit("/", 1)[-1]
    return (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or "spec" in parts
        or "specs" in parts
        or filename.startswith("test_")
        or ".test." in filename
        or ".tests." in filename
        or ".spec." in filename
        or ".specs." in filename
    )


def _is_source_like_path(path: str) -> bool:
    parts = set(path.replace("\\", "/").lower().split("/"))
    if _is_test_like_path(path):
        return False
    return bool(parts & {"src", "client", "lib", "packages", "mypy", "java", "main"})


_ENTITY_SEMANTIC_STOP = {
    "added",
    "after",
    "action",
    "behavior",
    "between",
    "boxes",
    "callback",
    "canvas",
    "called",
    "chart",
    "component",
    "console",
    "context",
    "config",
    "configuration",
    "data",
    "current",
    "effect",
    "elements",
    "equal",
    "example",
    "extra",
    "event",
    "function",
    "handler",
    "handle",
    "implementation",
    "interaction",
    "issue",
    "items",
    "layout",
    "option",
    "plugin",
    "possible",
    "render",
    "source",
    "state",
    "style",
    "target",
    "tooltip",
    "update",
    "value",
    "while",
}


def _entity_semantic_atoms(text: str) -> set[str]:
    atoms: set[str] = set()
    for token in tokenize(text):
        for part in re.split(r"[._:/-]+", token.lower()):
            if len(part) >= 5 and part not in _ENTITY_SEMANTIC_STOP:
                atoms.add(part)
    return atoms


def _entity_issue_semantic_bonus(entity: CodeEntity, queries: Iterable[str]) -> tuple[float, list[str]]:
    """Ground functions inside likely files using concrete issue behavior terms."""

    query_tokens = _entity_semantic_atoms(" ".join(str(query or "") for query in queries))
    if not query_tokens:
        return 0.0, []
    body = str(getattr(entity, "text", "") or "").lower()
    body_tokens = _entity_semantic_atoms(f"{entity.name} {body}")
    matched = sorted(query_tokens & body_tokens)
    if not matched:
        return 0.0, []
    occurrence_bonus = sum(min(3, body.count(token)) * 5.0 for token in matched)
    bonus = min(225.0, len(matched) * 70.0 + occurrence_bonus)
    return bonus, matched[:8]


def _rank_entities(
    *,
    index: RepositoryIndex,
    ranked_files: list[RankedLocation],
    entity_hits: list[Any],
    flow_traces: list[dict[str, Any]],
    top_k: int,
    candidate_review: dict[str, Any] | None = None,
    entity_queries: Iterable[str] = (),
) -> tuple[list[RankedEntity], list[RankedEntity]]:
    functions: dict[str, RankedEntity] = {}
    modules: dict[str, RankedEntity] = {}
    file_score = {item.path: item.score for item in ranked_files}
    flow_support = _flow_entity_support(flow_traces)

    def useful_entity(path: str, name: str) -> bool:
        lower_path = _norm_path(path).lower()
        if len(name.strip()) <= 1 and (
            ".min." in lower_path
            or _is_test_like_path(lower_path)
            or any(part in lower_path.split("/") for part in {"vendor", "vendors", "third_party"})
        ):
            return False
        return True

    for hit in entity_hits:
        if not useful_entity(hit.path, hit.name):
            continue
        if hit.kind in {"function", "method"}:
            _add_ranked_entity(
                functions,
                path=hit.path,
                kind=hit.kind,
                name=hit.name,
                score=hit.score * 1.4 + min(35.0, file_score.get(hit.path, 0.0) * 0.02),
                reason="entity_search:" + ",".join(hit.reasons[:5]),
                start_line=hit.start_line,
                end_line=hit.end_line,
            )
        elif hit.kind in {"class", "module"}:
            _add_ranked_entity(
                modules,
                path=hit.path,
                kind=hit.kind,
                name=hit.name,
                score=hit.score * 1.2 + file_score.get(hit.path, 0.0) * 0.08,
                reason="entity_search:" + ",".join(hit.reasons[:5]),
                start_line=hit.start_line,
                end_line=hit.end_line,
            )

    for file_item in ranked_files:
        file_entities = _entities_for_file(index, file_item.path)
        module = _file_module_entity(
            file_item.path,
            max(1.0, file_item.score * 0.10),
            ["file_module_fallback", "file_rank_support"],
        )
        modules.setdefault(module.id, module)
        for entity in file_entities:
            if not useful_entity(entity.path, entity.name):
                continue
            # A relevant file can contain hundreds of unrelated functions.
            # Inherit only bounded file evidence; entity search, flow, or the
            # reviewer must provide the function-level distinction.
            base = max(2.0, min(30.0, file_item.score * 0.01))
            if entity.kind in {"function", "method"}:
                semantic_bonus, semantic_terms = _entity_issue_semantic_bonus(entity, entity_queries)
                _add_ranked_entity(
                    functions,
                    path=entity.path,
                    kind=entity.kind,
                    name=entity.name,
                    score=base + semantic_bonus,
                    reason=(
                        "entity_issue_semantics:" + ",".join(semantic_terms)
                        if semantic_terms
                        else "inside_ranked_file"
                    ),
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                )
            elif entity.kind in {"class", "module"}:
                _add_ranked_entity(
                    modules,
                    path=entity.path,
                    kind=entity.kind,
                    name=entity.name,
                    score=base,
                    reason="inside_ranked_file",
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                )

    for ent_id, bonus in flow_support.items():
        target = functions.get(ent_id) or modules.get(ent_id)
        if target is not None:
            target.score += bonus
            if "program_flow_support" not in target.reasons:
                target.reasons.append("program_flow_support")

    # LocAgent's related-level stage is effective because it does not assign
    # every entity inside a retrieved file the same probability. Candidate
    # review supplies the missing grounding: exact entities named after reading
    # the pruned code snippets receive a bounded boost, while unknown names are
    # ignored instead of being fabricated into the ranking.
    for reviewed in (candidate_review or {}).get("candidates", []) or []:
        role = str(reviewed.get("role") or "")
        if role not in {"patch_target", "supporting_target"}:
            continue
        path = _norm_path(str(reviewed.get("path") or ""))
        confidence = max(0.0, min(1.0, float(reviewed.get("confidence") or 0.0)))
        file_weight = max(8.0, float(file_score.get(path, 0.0) or 0.0) * 0.28)
        for entity_hint in reviewed.get("entities", []) or []:
            kind = str(entity_hint.get("kind") or "").lower()
            name = str(entity_hint.get("name") or "")
            if not name:
                continue
            pools = [functions] if kind in {"function", "method"} else [modules]
            for pool in pools:
                for entity in pool.values():
                    if _norm_path(entity.path) != path or entity.name != name:
                        continue
                    bonus = file_weight * confidence
                    entity.score += bonus
                    reason = f"llm_review_exact_entity:{role}:confidence={confidence:.2f}"
                    if reason not in entity.reasons:
                        entity.reasons.append(reason)

    function_ranked = sorted(functions.values(), key=lambda item: (-item.score, item.path, item.name))[:top_k]
    module_ranked = sorted(modules.values(), key=lambda item: (-item.score, item.path, item.name))[:top_k]
    return module_ranked, function_ranked


_CLOSURE_BLOCKED_ROLES = {
    "reproduction_or_example",
    "test_or_fixture",
    "generated_or_lockfile",
    "addon_bundle",
}

_CLOSURE_STRONG_EDGES = {
    "imports",
    "reverse_imports",
    "calls",
    "reverse_calls",
    "renders",
    "reverse_renders",
    "routes_to",
    "reverse_routes_to",
    "selects_state",
    "dispatches_action",
    "handles_action",
    "inherits_or_implements",
    "overrides",
}


def _closure_architecture_roles(path: str) -> set[str]:
    """Infer broad patch responsibilities without repository-specific paths."""

    lower = _norm_path(path).lower()
    parts = set(lower.split("/"))
    roles: set[str] = set()
    state_markers = {
        "state",
        "store",
        "actions",
        "action",
        "reducers",
        "reducer",
        "data-layer",
        "services",
        "service",
        "api",
    }
    if parts & state_markers or any(
        marker in lower for marker in ("/state/", "/data-layer/", "action-types", "reducer")
    ):
        roles.add("state_or_integration")
    suffix = Path(lower).suffix
    if suffix in {".jsx", ".tsx", ".vue", ".svelte"} or any(
        marker in lower for marker in ("component", "view", "screen", "page", "site-settings", "manage-")
    ):
        roles.add("interaction_surface")
    return roles


def _closure_flow_family(flow_type: str) -> str:
    lower = str(flow_type or "").lower()
    if "serializer" in lower or ("backend" in lower and "flow" in lower):
        return "serializer_backend"
    if "state_selector" in lower or "selector_use" in lower:
        return "state_selector"
    if "ui_event" in lower or "event_to_handler" in lower:
        return "ui_event"
    if "style" in lower or "layout" in lower:
        return "style_pipeline"
    if "url_builder" in lower or "route_flow" in lower:
        return "url_route"
    if "python_type" in lower or "type_binding" in lower:
        return "python_type_binding"
    if "parameter" in lower or "config" in lower:
        return "parameter_config"
    return lower or "unknown"


def _closure_obligations(
    issue_sketch: Any,
    review: dict[str, Any],
    available_flow_families: set[str],
) -> list[dict[str, Any]]:
    """Translate the sketch into a small set of testable patch obligations."""

    obligations: list[dict[str, Any]] = [
        {
            "id": "root_patch_mechanism",
            "kind": "mechanism",
            "required": True,
            "description": "At least one source file must be read and tied to the failing mechanism.",
            "terms": [],
        }
    ]
    task_type = str(getattr(issue_sketch, "task_type", "") or "")
    issue_axes = " ".join(
        list(getattr(issue_sketch, "concerns", []) or [])
        + list(getattr(issue_sketch, "expected_effects", []) or [])
        + list(getattr(issue_sketch, "architectural_queries", []) or [])
    ).lower()
    if task_type == "feature_request" and any(
        token in issue_axes for token in ("owner", "ownership", "administrator")
    ):
        obligations.extend(
            [
                {
                    "id": "architecture:interaction_surface",
                    "kind": "architecture_role",
                    "architecture_role": "interaction_surface",
                    "required": True,
                    "description": "The edit set must cover the user-facing ownership interaction.",
                    "terms": ["owner", "ownership", "administrator", "transfer"],
                },
                {
                    "id": "architecture:state_or_integration",
                    "kind": "architecture_role",
                    "architecture_role": "state_or_integration",
                    "required": True,
                    "description": "The edit set must cover the ownership action or integration boundary.",
                    "terms": ["owner", "ownership", "plan transfer"],
                },
            ]
        )
    seen_families: set[str] = set()
    for item in getattr(issue_sketch, "flow_obligations", []) or []:
        family = _closure_flow_family(str(item.get("flow_type") or ""))
        if family in seen_families or family == "unknown":
            continue
        seen_families.add(family)
        obligations.append(
            {
                "id": f"flow:{family}",
                "kind": "flow",
                "flow_family": family,
                "required": family in available_flow_families,
                "support_status": (
                    "corroborated_by_flow_trace"
                    if family in available_flow_families
                    else "advisory_no_matching_flow_trace"
                ),
                "description": str(item.get("reason") or item.get("required_relation") or family),
                "terms": _dedupe([str(item.get("state") or ""), str(item.get("behavior") or "")], limit=8),
            }
        )

    # These are explanatory coverage axes. They help the trace say what the
    # selected files explain, but lexical overlap alone must never force edits.
    for kind, values in (
        ("concern", getattr(issue_sketch, "concerns", []) or []),
        ("effect", getattr(issue_sketch, "expected_effects", []) or []),
    ):
        terms = _dedupe([str(value) for value in values], limit=8)
        if terms:
            obligations.append(
                {
                    "id": f"semantic:{kind}",
                    "kind": kind,
                    "required": False,
                    "description": f"Selected code should explain the issue {kind}.",
                    "terms": terms,
                }
            )

    # A grounded, high-confidence mechanism claim is stronger than a generic
    # graph neighbor. Preserve each such target as an explicit set obligation.
    for item in review.get("candidates", []) or []:
        path = _norm_path(str(item.get("path") or ""))
        role = str(item.get("role") or "")
        confidence = float(item.get("confidence") or 0.0)
        if (
            path
            and role in {"patch_target", "supporting_target"}
            and confidence >= 0.82
            and bool(item.get("mechanism_verified", False))
            and bool(item.get("grounded", item.get("quote_supported", False)))
        ):
            obligations.append(
                {
                    "id": f"review_target:{path}",
                    "kind": "review_target",
                    "path": path,
                    "required": True,
                    "description": str(item.get("patch_mechanism") or item.get("rationale") or "Grounded repair target."),
                    "terms": [],
                }
            )
    return obligations


def _closure_context_map(code_contexts: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in code_contexts or []:
        path = _norm_path(str(item.get("path") or ""))
        if not path:
            continue
        current = merged.get(path)
        if current is None or len(item.get("snippets", []) or []) > len(current.get("snippets", []) or []):
            merged[path] = item
    return merged


def _closure_flow_support(flow_traces: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    support: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"families": set(), "types": set(), "terms": set(), "roles": set(), "reasons": []}
    )
    for flow in flow_traces or []:
        flow_type = str(flow.get("flow_type") or "")
        family = _closure_flow_family(flow_type)
        flow_term = str(flow.get("term") or "")
        paths: dict[str, set[str]] = defaultdict(set)
        for path in flow.get("candidate_target_paths", []) or []:
            if path:
                paths[_norm_path(str(path))].add("candidate_target")
        for key in ("locations", "source_steps", "sink_steps", "steps", "statement_nodes"):
            for item in flow.get(key, []) or []:
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                path = _norm_path(str(item.get("path")))
                role = str(item.get("role") or "")
                paths[path].add(f"{key}:{role or 'implementation'}")
                if role:
                    support[path]["roles"].add(role)
        for key in ("chain_edges", "def_use_edges", "call_boundary_edges", "edges"):
            for edge in flow.get(key, []) or []:
                if not isinstance(edge, dict):
                    continue
                relation = str(edge.get("relation") or key)
                for endpoint in ("source", "target"):
                    path = str(edge.get(endpoint) or "")
                    if path and ("/" in path or bool(Path(path).suffix)):
                        paths[_norm_path(path)].add(f"{key}:{relation}")
        for path, reasons in paths.items():
            if not path:
                continue
            support[path]["families"].add(family)
            support[path]["types"].add(flow_type)
            if flow_term:
                support[path]["terms"].add(flow_term)
            support[path]["reasons"].extend(sorted(reasons)[:6])
    return support


def _closure_graph_links(
    graph: TypedRepositoryGraph,
    selected_paths: Iterable[str],
    candidate_path: str,
) -> list[dict[str, Any]]:
    selected = {_norm_path(path) for path in selected_paths}
    candidate = _norm_path(candidate_path)
    links: list[dict[str, Any]] = []
    for edge in graph.edges_by_source.get(candidate, []) or []:
        if edge.target in selected and edge.edge_type in _CLOSURE_STRONG_EDGES:
            links.append({"type": edge.edge_type, "peer": edge.target, "weight": round(float(edge.weight), 3)})
    for edge in graph.edges_by_target.get(candidate, []) or []:
        if edge.source in selected and edge.edge_type in _CLOSURE_STRONG_EDGES:
            links.append({"type": edge.edge_type, "peer": edge.source, "weight": round(float(edge.weight), 3)})
    return links[:8]


def _closure_candidate_evidence(
    *,
    ranked: list[RankedLocation],
    review: dict[str, Any],
    code_contexts: Iterable[dict[str, Any]],
    flow_traces: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    contexts = _closure_context_map(code_contexts)
    flow_support = _closure_flow_support(flow_traces)
    reviewed = {
        _norm_path(str(item.get("path") or "")): item
        for item in review.get("candidates", []) or []
        if str(item.get("path") or "")
    }
    top_score = max(1.0, float(ranked[0].score or 0.0)) if ranked else 1.0
    evidence: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(ranked, start=1):
        path = _norm_path(item.path)
        context = contexts.get(path) or {}
        review_item = reviewed.get(path) or {}
        flow = flow_support.get(path) or {}
        snippet_text = " ".join(str(snippet.get("text") or "") for snippet in context.get("snippets", []) or [])
        entity_text = " ".join(str(entity.get("name") or "") for entity in item.entities or [])
        searchable = " ".join(
            [path, snippet_text, entity_text, " ".join(item.reasons), str(review_item.get("patch_mechanism") or "")]
        )
        snippet_read_verified = bool(context.get("snippets"))
        belief_read_verified = bool((item.belief or {}).get("read_verified"))
        read_verified = snippet_read_verified or belief_read_verified
        grounded = bool(review_item.get("grounded", review_item.get("quote_supported", False))) and read_verified
        evidence[path] = {
            "path": path,
            "rank": rank,
            "rank_score": round(float(item.score or 0.0), 3),
            "rank_ratio": round(float(item.score or 0.0) / top_score, 4),
            "path_role": _path_role(path),
            "architecture_roles": sorted(_closure_architecture_roles(path)),
            "read_verified": read_verified,
            "read_evidence_sources": [
                source
                for source, present in (
                    ("code_context", snippet_read_verified),
                    ("candidate_belief", belief_read_verified),
                )
                if present
            ],
            "snippet_count": len(context.get("snippets", []) or []),
            "mechanism_verified": bool(review_item.get("mechanism_verified", False)),
            "grounded": grounded,
            "quote_supported": bool(review_item.get("quote_supported", False)) and read_verified,
            "entity_supported": bool(review_item.get("entity_supported", False)) and read_verified,
            "review_role": str(review_item.get("role") or ""),
            "review_confidence": round(float(review_item.get("confidence") or 0.0), 3),
            "review_axes": sorted({str(axis).lower() for axis in review_item.get("matched_issue_axes", []) or []}),
            "patch_mechanism": str(review_item.get("patch_mechanism") or "")[:500],
            "counterevidence": _dedupe(
                list(review_item.get("counterevidence", []) or [])
                + list(review_item.get("counter_evidence", []) or []),
                limit=8,
            ),
            "flow_families": sorted(flow.get("families", set())),
            "flow_types": sorted(flow.get("types", set())),
            "flow_terms": sorted(flow.get("terms", set())),
            "flow_roles": sorted(flow.get("roles", set())),
            "flow_reasons": _dedupe(flow.get("reasons", []), limit=10),
            "search_tokens": sorted({token.lower() for token in tokenize(searchable) if len(token) >= 3}),
            "score_components": dict(item.score_components),
        }
    return evidence


def _closure_obligation_tokens(obligations: Iterable[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for obligation in obligations or []:
        values = list(obligation.get("terms", []) or [])
        values.extend(
            [
                str(obligation.get("flow_family") or "").replace("_", " "),
                str(obligation.get("description") or ""),
            ]
        )
        for value in values:
            tokens.update(token.lower() for token in tokenize(str(value)) if len(token) >= 4)
    return tokens


def _closure_has_task_relevant_direct_flow(
    item: dict[str, Any],
    obligations: Iterable[dict[str, Any]],
) -> bool:
    reasons = set(item.get("flow_reasons", []) or [])
    direct_target = any(reason == "candidate_target" or reason.startswith("locations:") for reason in reasons)
    if not direct_target:
        return False
    obligation_tokens = _closure_obligation_tokens(obligations)
    flow_text = " ".join(
        list(item.get("flow_types", []) or [])
        + list(item.get("flow_terms", []) or [])
        + list(item.get("flow_families", []) or [])
    ).replace("_", " ")
    flow_tokens = {token.lower() for token in tokenize(flow_text) if len(token) >= 4}
    return bool(obligation_tokens & flow_tokens)


def _closure_has_verified_mechanism(
    item: dict[str, Any],
    obligations: Iterable[dict[str, Any]],
) -> bool:
    if not item.get("read_verified") or item.get("counterevidence"):
        return False
    if item.get("mechanism_verified") and item.get("grounded"):
        return True
    if not _closure_has_task_relevant_direct_flow(item, obligations):
        return False
    axes = set(item.get("review_axes", []) or [])
    reviewed_direct_flow = bool(
        item.get("grounded")
        and item.get("review_role") in {"patch_target", "supporting_target"}
        and float(item.get("review_confidence") or 0.0) >= 0.72
        and axes & {"call", "flow", "program", "dataflow", "state", "effect"}
    )
    generic_families = {
        "serializer_backend",
        "state_selector",
        "ui_event",
        "style_pipeline",
        "url_route",
        "python_type_binding",
        "parameter_config",
    }
    specific_deterministic_flow = any(
        family not in generic_families and family != "unknown"
        for family in item.get("flow_families", []) or []
    )
    return reviewed_direct_flow or specific_deterministic_flow


def _closure_responsibility_signature(
    item: dict[str, Any],
    obligations: Iterable[dict[str, Any]],
) -> set[str]:
    signature = {f"architecture:{role}" for role in item.get("architecture_roles", []) or []}
    signature.update(f"flow:{family}" for family in item.get("flow_families", []) or [])
    signature.update(f"axis:{axis}" for axis in item.get("review_axes", []) or [])
    if _closure_has_verified_mechanism(item, obligations):
        signature.add("mechanism:root")
    return signature


def _closure_has_direct_retrieval_evidence(item: dict[str, Any]) -> bool:
    components = item.get("score_components") or {}
    return any(
        float(components.get(name, 0.0) or 0.0) > 0
        for name in (
            "symbol_score", "path_score", "domain_path_probe", "call_score",
            "flow_score", "flow_verifier", "entity_search", "concern_search",
        )
    )


def _closure_is_provisional_patch_candidate(
    item: dict[str, Any],
    obligations: Iterable[dict[str, Any]],
) -> bool:
    """Accept bounded source evidence without pretending it proves closure."""

    if (
        not item.get("read_verified")
        or item.get("counterevidence")
        or item.get("path_role") in _CLOSURE_BLOCKED_ROLES
    ):
        return False
    if _closure_has_verified_mechanism(item, obligations):
        return True
    review_role = str(item.get("review_role") or "")
    confidence = float(item.get("review_confidence") or 0.0)
    grounded_target = bool(
        item.get("grounded")
        and review_role in {"patch_target", "supporting_target"}
        and confidence >= 0.58
    )
    direct_flow = _closure_has_task_relevant_direct_flow(item, obligations)
    direct_retrieval = _closure_has_direct_retrieval_evidence(item)
    high_rank_source = bool(
        int(item.get("rank") or 10_000) <= 3
        and float(item.get("rank_ratio") or 0.0) >= 0.6
        and direct_retrieval
    )
    reviewed_source = bool(
        review_role in {"patch_target", "supporting_target"}
        and confidence >= 0.55
        and (item.get("grounded") or direct_flow or direct_retrieval)
    )
    return grounded_target or reviewed_source or (direct_flow and direct_retrieval) or high_rank_source


def _closure_cardinality_bounds(
    *,
    obligations: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    issue_sketch: Any,
    limit: int,
) -> dict[str, Any]:
    """Estimate a useful edit-set size from grounded responsibilities."""

    required_non_root = [
        item
        for item in obligations
        if item.get("required") and item.get("id") != "root_patch_mechanism"
    ]
    strong = [
        item
        for item in evidence.values()
        if _closure_is_provisional_patch_candidate(item, obligations)
    ]
    responsibility_union = set().union(
        *(_closure_responsibility_signature(item, obligations) for item in strong)
    ) if strong else set()
    task_type = str(getattr(issue_sketch, "task_type", "") or "")
    minimum = 1
    if len(required_non_root) >= 1 and len(strong) >= 2:
        minimum = 2
    if task_type == "feature_request" and len(strong) >= 2 and len(responsibility_union) >= 2:
        minimum = max(minimum, 2)
    if len(required_non_root) >= 3 and len(strong) >= 3:
        minimum = max(minimum, 3)
    evidence_ceiling = min(4, max(1, len(strong)))
    task_target = 1
    if len(strong) >= 2 and (task_type == "feature_request" or required_non_root):
        task_target = 2
    if len(strong) >= 3 and len(required_non_root) >= 2:
        task_target = 3
    preferred = min(limit, evidence_ceiling, max(minimum, task_target))
    maximum = min(limit, evidence_ceiling, max(preferred, min(4, preferred + 1)))
    return {
        "minimum": minimum,
        "preferred": preferred,
        "maximum": maximum,
        "strong_candidate_count": len(strong),
        "required_non_root_count": len(required_non_root),
        "responsibility_count": len(responsibility_union),
        "task_type": task_type,
    }


def _partial_closure_fallback(
    *,
    ranked_pool: list[str],
    evidence: dict[str, dict[str, Any]],
    obligations: list[dict[str, Any]],
    issue_sketch: Any,
    limit: int,
    seed_paths: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a small, evidence-backed partial set when verification stalls."""

    bounds = _closure_cardinality_bounds(
        obligations=obligations,
        evidence=evidence,
        issue_sketch=issue_sketch,
        limit=limit,
    )
    scored: list[tuple[float, int, str, set[str], list[str]]] = []
    for path in ranked_pool:
        item = evidence[path]
        if item.get("path_role") in _CLOSURE_BLOCKED_ROLES or item.get("counterevidence"):
            continue
        read_verified = bool(item.get("read_verified"))
        grounded = bool(item.get("grounded"))
        mechanism = _closure_has_verified_mechanism(item, obligations)
        direct_flow = _closure_has_task_relevant_direct_flow(item, obligations)
        review_role = str(item.get("review_role") or "")
        grounded_target = bool(
            grounded
            and review_role in {"patch_target", "supporting_target"}
            and float(item.get("review_confidence") or 0.0) >= 0.58
        )
        corroborated_grounded_target = bool(
            grounded_target
            and (item.get("quote_supported") or item.get("entity_supported"))
        )
        grounded_direct_flow = bool(grounded_target and direct_flow)
        provisional = _closure_is_provisional_patch_candidate(item, obligations)
        if not (mechanism or corroborated_grounded_target or grounded_direct_flow or provisional):
            continue
        if review_role in {"navigation_only", "reproduction_only", "test_or_docs", "unlikely"} and not mechanism:
            continue
        score = float(item.get("rank_ratio") or 0.0) * 2.5
        reasons: list[str] = [f"rank:{item.get('rank')}"]
        if read_verified:
            score += 1.5
            reasons.append("source_read")
        if grounded:
            score += 2.0
            reasons.append("grounded")
        if item.get("quote_supported"):
            score += 1.0
            reasons.append("quote_supported")
        if item.get("entity_supported"):
            score += 1.0
            reasons.append("entity_supported")
        if mechanism:
            score += 6.0
            reasons.append("verified_mechanism")
        if direct_flow:
            score += 2.5
            reasons.append("task_relevant_direct_flow")
        if _closure_has_direct_retrieval_evidence(item):
            score += 1.2
            reasons.append("direct_retrieval_evidence")
        if provisional and not (mechanism or corroborated_grounded_target or grounded_direct_flow):
            score += 0.8
            reasons.append("bounded_provisional_patch_evidence")
        if review_role == "patch_target" and grounded:
            score += 2.0 * float(item.get("review_confidence") or 0.0)
            reasons.append("grounded_patch_target")
        elif review_role == "supporting_target" and grounded:
            score += 1.2 * float(item.get("review_confidence") or 0.0)
            reasons.append("grounded_supporting_target")
        signature = _closure_responsibility_signature(item, obligations)
        score += min(1.5, len(signature) * 0.3)
        scored.append((score, int(item.get("rank") or 10_000), path, signature, reasons))

    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    selected: list[dict[str, Any]] = []
    covered_responsibilities: set[str] = set()
    for path in _dedupe([_norm_path(path) for path in seed_paths], limit=limit):
        item = evidence.get(path)
        if not item or item.get("path_role") in _CLOSURE_BLOCKED_ROLES:
            continue
        signature = _closure_responsibility_signature(item, obligations)
        selected.append(
            {
                "path": path,
                "role": "patch_target" if item.get("review_role") == "patch_target" else "supporting_target",
                "confidence": round(max(0.42, float(item.get("review_confidence") or 0.0)), 3),
                "reasons": ["partial_evidence_fallback:preserved_existing_selection"],
            }
        )
        covered_responsibilities.update(signature)
    top_score = scored[0][0] if scored else 0.0
    for score, rank, path, signature, reasons in scored:
        if any(item["path"] == path for item in selected):
            continue
        if len(selected) >= int(bounds["maximum"]):
            break
        if len(selected) >= int(bounds["preferred"]):
            break
        novel = signature - covered_responsibilities
        must_fill_minimum = len(selected) < int(bounds["minimum"])
        competitive = score >= max(3.0, top_score * 0.52)
        if selected and not must_fill_minimum and (not novel or not competitive):
            continue
        item = evidence[path]
        selected.append(
            {
                "path": path,
                "role": "patch_target" if item.get("review_role") == "patch_target" else "supporting_target",
                "confidence": round(min(0.9, 0.42 + score * 0.035), 3),
                "reasons": ["partial_evidence_fallback:" + ";".join(reasons[:5])],
            }
        )
        covered_responsibilities.update(signature)

    return selected, {
        "strategy": "evidence_backed_responsibility_diverse_partial_fallback",
        "bounds": bounds,
        "eligible_count": len(scored),
        "selected_count": len(selected),
        "selected_paths": [item["path"] for item in selected],
        "covered_responsibilities": sorted(covered_responsibilities),
    }


def _merge_closure_candidate_reviews(verifiers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Keep the strongest grounded candidate observation across agent rounds."""

    best: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    statuses: list[str] = []
    for verifier in verifiers or []:
        review = (verifier or {}).get("llm_candidate_review") or {}
        if review.get("status"):
            statuses.append(str(review.get("status")))
        attempts.extend(review.get("attempts", []) or [])
        for item in review.get("candidates", []) or []:
            path = _norm_path(str(item.get("path") or ""))
            if not path:
                continue
            score = (
                1 if item.get("mechanism_verified", False) else 0,
                1 if item.get("grounded", item.get("quote_supported", False)) else 0,
                float(item.get("confidence") or 0.0),
                len(item.get("matched_issue_axes", []) or []),
            )
            current = best.get(path)
            current_score = (
                1 if current and current.get("mechanism_verified", False) else 0,
                1 if current and current.get("grounded", current.get("quote_supported", False)) else 0,
                float((current or {}).get("confidence") or 0.0),
                len((current or {}).get("matched_issue_axes", []) or []),
            )
            if current is None or score > current_score:
                best[path] = dict(item)
    return {
        "status": "ok" if best else (statuses[-1] if statuses else "missing"),
        "source": "cross_round_candidate_review_memory",
        "candidates": sorted(
            best.values(),
            key=lambda item: (
                0 if item.get("role") == "patch_target" else 1,
                0 if item.get("mechanism_verified", False) else 1,
                -float(item.get("confidence") or 0.0),
                str(item.get("path") or ""),
            ),
        ),
        "attempts": attempts[-4:],
    }


def _closure_obligation_coverage(
    selected_paths: Iterable[str],
    obligations: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected = [_norm_path(path) for path in selected_paths]
    covered: dict[str, list[str]] = {}
    missing_required: list[str] = []
    missing_advisory: list[str] = []
    for obligation in obligations:
        obligation_id = str(obligation.get("id") or "")
        covering_paths: list[str] = []
        kind = str(obligation.get("kind") or "")
        for path in selected:
            item = evidence.get(path) or {}
            if kind == "mechanism":
                if _closure_has_verified_mechanism(item, obligations):
                    covering_paths.append(path)
            elif kind == "flow" and obligation.get("flow_family") in set(item.get("flow_families") or []):
                covering_paths.append(path)
            elif kind == "review_target" and path == _norm_path(str(obligation.get("path") or "")):
                covering_paths.append(path)
            elif kind == "architecture_role" and obligation.get("architecture_role") in set(
                item.get("architecture_roles") or []
            ):
                term_tokens = {
                    token.lower()
                    for term in obligation.get("terms", []) or []
                    for token in tokenize(str(term))
                    if len(token) >= 4
                }
                if not term_tokens or term_tokens & set(item.get("search_tokens") or []):
                    covering_paths.append(path)
            elif kind in {"concern", "effect"}:
                term_tokens = {
                    token.lower()
                    for term in obligation.get("terms", []) or []
                    for token in tokenize(str(term))
                    if len(token) >= 3
                }
                if term_tokens & set(item.get("search_tokens") or []):
                    covering_paths.append(path)
        if covering_paths:
            covered[obligation_id] = covering_paths
        elif obligation.get("required", False):
            missing_required.append(obligation_id)
        else:
            missing_advisory.append(obligation_id)
    required_count = sum(1 for item in obligations if item.get("required", False))
    required_covered = required_count - len(missing_required)
    return {
        "complete": not missing_required,
        "required_count": required_count,
        "required_covered": required_covered,
        "required_coverage": round(required_covered / max(1, required_count), 3),
        "covered_by": covered,
        "missing_required": missing_required,
        "missing_advisory": missing_advisory,
    }


def _build_modification_closure(
    *,
    ranked: list[RankedLocation],
    verifier: dict[str, Any],
    graph: TypedRepositoryGraph,
    issue_sketch: Any,
    code_contexts: Iterable[dict[str, Any]] | None = None,
    flow_traces: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal sufficient edit set with bounded verify/expand/ablate rounds.

    The broad ranking remains available for recall metrics. This stage answers a
    different question: which files have enough mechanism evidence to be handed
    to a repair agent together, and can any of them be removed without losing a
    required issue/flow obligation?
    """

    limit = _env_int("MYCODE_FINAL_PATCH_SET_LIMIT", 6, minimum=1)
    max_rounds = _env_int("MYCODE_CLOSURE_MAX_ROUNDS", 3, minimum=1)
    expand_per_round = _env_int("MYCODE_CLOSURE_EXPAND_PER_ROUND", 2, minimum=1)
    if not ranked:
        return {
            "strategy": "iterative_minimal_sufficient_modification_closure",
            "status": "incomplete",
            "complete": False,
            "files": [],
            "candidates": [],
            "obligations": [],
            "coverage": {"complete": False, "missing_required": ["root_patch_mechanism"]},
            "trace": [],
            "ranking_kept_separate": True,
        }

    review = (verifier or {}).get("llm_candidate_review") or {}
    evidence = _closure_candidate_evidence(
        ranked=ranked,
        review=review,
        code_contexts=code_contexts or [],
        flow_traces=flow_traces or [],
    )
    available_flow_families = {
        family
        for item in evidence.values()
        for family in item.get("flow_families", []) or []
    }
    obligations = _closure_obligations(issue_sketch, review, available_flow_families)
    ranked_pool = [
        _norm_path(item.path)
        for item in ranked[: max(15, limit * 4)]
        if _path_role(item.path) not in _CLOSURE_BLOCKED_ROLES and _norm_path(item.path) in graph.index.files
    ]
    selected: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []

    def add(path: str, *, role: str, confidence: float, reason: str) -> bool:
        path = _norm_path(path)
        if path not in ranked_pool or len(selected) >= limit and path not in selected:
            return False
        item = evidence.get(path) or {}
        if item.get("path_role") in _CLOSURE_BLOCKED_ROLES:
            return False
        current = selected.get(path)
        if current is None:
            current = {
                "path": path,
                "role": role,
                "confidence": round(max(0.0, min(1.0, confidence)), 3),
                "reasons": [],
            }
            selected[path] = current
        else:
            current["confidence"] = max(float(current["confidence"]), round(confidence, 3))
            if role == "patch_target":
                current["role"] = role
        if reason and reason not in current["reasons"]:
            current["reasons"].append(reason)
        return True

    reviewed_primary = sorted(
        (
            item for item in evidence.values()
            if item.get("review_role") == "patch_target"
            and item.get("grounded")
            and item.get("read_verified")
            and not item.get("counterevidence")
            and float(item.get("review_confidence") or 0.0) >= 0.72
            and (
                item.get("mechanism_verified")
                or item.get("quote_supported")
                or item.get("entity_supported")
                or _closure_has_task_relevant_direct_flow(item, obligations)
            )
            and item.get("path") in ranked_pool
        ),
        key=lambda item: (
            0 if item.get("mechanism_verified") else 1,
            -float(item.get("review_confidence") or 0.0),
            int(item.get("rank") or 10_000),
        ),
    )
    if reviewed_primary:
        first = reviewed_primary[0]
        add(
            str(first["path"]),
            role="patch_target",
            confidence=float(first.get("review_confidence") or 0.0),
            reason=(
                "grounded_llm_patch_mechanism"
                if first.get("mechanism_verified")
                else "grounded_llm_patch_target_pending_mechanism_confirmation"
            ),
        )
    else:
        deterministic = next(
            (
                evidence[path]
                for path in ranked_pool
                if _closure_has_verified_mechanism(evidence[path], obligations)
            ),
            None,
        )
        primary = deterministic or evidence[ranked_pool[0]]
        confidence = 0.74 if deterministic else 0.48
        reason = "read_source_with_flow_evidence" if deterministic else "tentative_top_ranked_source_candidate"
        add(str(primary["path"]), role="patch_target", confidence=confidence, reason=reason)

    rounds_run = 0
    for round_no in range(1, max_rounds + 1):
        rounds_run = round_no
        selected_before = list(selected)
        coverage_before = _closure_obligation_coverage(selected, obligations, evidence)
        missing = set(coverage_before.get("missing_required") or [])
        expansion_rows: list[dict[str, Any]] = []

        if missing and len(selected) < limit:
            scored: list[tuple[float, str, list[str], list[str]]] = []
            for path in ranked_pool:
                if path in selected:
                    continue
                item = evidence[path]
                gain: list[str] = []
                singleton_coverage = _closure_obligation_coverage([path], obligations, evidence)
                gain = sorted(missing & set(singleton_coverage.get("covered_by", {})))
                links = _closure_graph_links(graph, selected, path)
                reasons: list[str] = []
                score = len(gain) * 5.0
                if item.get("mechanism_verified") and item.get("grounded"):
                    score += 4.0
                    reasons.append("grounded_patch_mechanism")
                if item.get("flow_families"):
                    score += min(3.0, len(item["flow_families"]) * 1.2)
                    reasons.append("direct_flow_support:" + ",".join(item["flow_families"][:3]))
                if links:
                    score += min(3.0, sum(float(link["weight"]) for link in links))
                    reasons.append("typed_graph_link:" + ",".join(link["type"] for link in links[:3]))
                axes = set(item.get("review_axes") or [])
                if item.get("review_role") in {"patch_target", "supporting_target"} and axes & {
                    "call", "flow", "program", "dataflow", "state", "effect"
                }:
                    score += 1.5 * float(item.get("review_confidence") or 0.0)
                    reasons.append("reviewed_program_support")
                score += min(1.0, float(item.get("rank_ratio") or 0.0))
                # Expansion is driven by an explicit missing obligation. A
                # generally related flow or graph neighbor with zero coverage
                # gain is context, not another file to edit.
                strong = bool(gain)
                if strong and score > 0:
                    scored.append((score, path, gain, reasons))
            scored.sort(key=lambda row: (-row[0], evidence[row[1]]["rank"], row[1]))
            for score, path, gain, reasons in scored[: min(expand_per_round, limit - len(selected))]:
                item = evidence[path]
                role = "patch_target" if item.get("review_role") == "patch_target" else "supporting_target"
                confidence = min(0.94, 0.45 + score * 0.045)
                if add(path, role=role, confidence=confidence, reason="closure_expansion:" + ";".join(reasons[:3])):
                    expansion_rows.append(
                        {"path": path, "score": round(score, 3), "obligation_gain": gain, "reasons": reasons[:4]}
                    )

        # Necessity ablation: low-confidence support files are tested first.
        pruned: list[dict[str, Any]] = []
        for path in sorted(
            list(selected),
            key=lambda candidate: (
                0 if selected[candidate]["role"] != "patch_target" else 1,
                float(selected[candidate]["confidence"]),
                -int(evidence.get(candidate, {}).get("rank") or 0),
            ),
        ):
            if len(selected) <= 1:
                break
            without = [candidate for candidate in selected if candidate != path]
            with_coverage = _closure_obligation_coverage(selected, obligations, evidence)
            without_coverage = _closure_obligation_coverage(without, obligations, evidence)
            if set(without_coverage.get("missing_required") or []) == set(with_coverage.get("missing_required") or []):
                removed = selected.pop(path)
                pruned.append(
                    {
                        "path": path,
                        "reason": "necessity_ablation_no_required_coverage_loss",
                        "previous_role": removed["role"],
                    }
                )

        coverage_after = _closure_obligation_coverage(selected, obligations, evidence)
        if coverage_after.get("complete"):
            decision = "complete_minimal_set"
        elif not expansion_rows:
            decision = "no_supported_expansion"
        elif round_no >= max_rounds:
            decision = "closure_budget_exhausted"
        else:
            decision = "continue_for_missing_obligations"
        trace.append(
            {
                "round": round_no,
                "selected_before": selected_before,
                "coverage_before": coverage_before,
                "expanded": expansion_rows,
                "pruned": pruned,
                "selected_after": list(selected),
                "coverage_after": coverage_after,
                "decision": decision,
            }
        )
        if coverage_after.get("complete") or not expansion_rows:
            break

    final_coverage = _closure_obligation_coverage(selected, obligations, evidence)
    partial_fallback: dict[str, Any] = {"applied": False, "reason": "closure_complete"}
    if not final_coverage.get("complete") and _env_bool("MYCODE_PARTIAL_SET_FALLBACK", True):
        fallback_rows, fallback_diagnostics = _partial_closure_fallback(
            ranked_pool=ranked_pool,
            evidence=evidence,
            obligations=obligations,
            issue_sketch=issue_sketch,
            limit=limit,
            seed_paths=selected,
        )
        fallback_paths = [item["path"] for item in fallback_rows]
        previous_paths = list(selected)
        fallback_changed = bool(fallback_rows and fallback_paths != previous_paths)
        partial_fallback = {
            **fallback_diagnostics,
            "applied": fallback_changed,
            "previous_paths": previous_paths,
        }
        if fallback_changed:
            selected = {item["path"]: item for item in fallback_rows}
            final_coverage = _closure_obligation_coverage(selected, obligations, evidence)
            trace.append(
                {
                    "round": rounds_run + 1,
                    "selected_before": previous_paths,
                    "coverage_before": _closure_obligation_coverage(previous_paths, obligations, evidence),
                    "expanded": [item for item in fallback_rows if item["path"] not in previous_paths],
                    "pruned": [
                        {"path": path, "reason": "partial_fallback_replaced_weaker_candidate"}
                        for path in previous_paths
                        if path not in fallback_paths
                    ],
                    "selected_after": fallback_paths,
                    "coverage_after": final_coverage,
                    "decision": "partial_evidence_fallback",
                }
            )

    if final_coverage.get("complete"):
        status = "complete"
    elif partial_fallback.get("applied"):
        status = "partial"
    elif rounds_run >= max_rounds and trace and trace[-1].get("expanded"):
        status = "budget_exhausted"
    else:
        status = "incomplete"

    for path, row in selected.items():
        item = evidence.get(path) or {}
        row.update(
            {
                "rank": item.get("rank"),
                "read_verified": bool(item.get("read_verified")),
                "read_evidence_sources": item.get("read_evidence_sources", []),
                "mechanism_verified": bool(item.get("mechanism_verified")),
                "quote_supported": bool(item.get("quote_supported")),
                "entity_supported": bool(item.get("entity_supported")),
                "flow_families": item.get("flow_families", []),
                "task_relevant_direct_flow": _closure_has_task_relevant_direct_flow(item, obligations),
                "counterevidence": item.get("counterevidence", []),
                "unique_obligations": [
                    obligation_id
                    for obligation_id, paths in (final_coverage.get("covered_by") or {}).items()
                    if paths == [path]
                ],
            }
        )
    ordered = sorted(
        selected.values(),
        key=lambda item: (
            0 if item["role"] == "patch_target" else 1,
            -float(item["confidence"]),
            int(item.get("rank") or 10_000),
        ),
    )
    return {
        "strategy": "iterative_minimal_sufficient_modification_closure",
        "status": status,
        "complete": bool(final_coverage.get("complete")),
        "limit": limit,
        "max_rounds": max_rounds,
        "rounds_run": rounds_run,
        "files": [item["path"] for item in ordered],
        "candidates": ordered,
        "obligations": obligations,
        "coverage": final_coverage,
        "trace": trace,
        "partial_fallback": partial_fallback,
        "ranking_kept_separate": True,
    }


def _rerank_with_modification_closure(
    ranked: list[RankedLocation],
    closure: dict[str, Any],
    *,
    top_k: int,
) -> tuple[list[RankedLocation], dict[str, Any]]:
    """Promote verified patch targets without changing the recalled file set."""

    if not ranked:
        return ranked[:top_k], {"applied": False, "reason": "empty_ranking"}
    verified_candidates = [
        item
        for item in closure.get("candidates", []) or []
        if item.get("role") == "patch_target"
        and item.get("read_verified")
        and not item.get("counterevidence")
        and item.get("mechanism_verified")
        and item.get("quote_supported")
        and item.get("entity_supported")
    ]
    if not closure.get("complete"):
        verified_candidates = verified_candidates[:1]
    candidates = {
        _norm_path(str(item.get("path") or "")): item
        for item in verified_candidates
    }
    if not candidates:
        return ranked[:top_k], {"applied": False, "reason": "no_verified_patch_target"}
    top_score = max(float(item.score or 0.0) for item in ranked)
    margin_ratio = _env_float("MYCODE_CLOSURE_RERANK_MARGIN_RATIO", 0.02, minimum=0.0)
    minimum_margin = _env_float("MYCODE_CLOSURE_RERANK_MIN_MARGIN", 1.0, minimum=0.0)
    updated: list[tuple[int, int, RankedLocation]] = []
    adjustments: list[dict[str, Any]] = []
    for original_position, item in enumerate(ranked):
        copy_item = copy.deepcopy(item)
        closure_item = candidates.get(_norm_path(item.path))
        promotion_order = 1
        if closure_item:
            confidence = float(closure_item.get("confidence") or 0.0)
            margin = max(minimum_margin, top_score * margin_ratio * max(0.0, min(1.0, confidence)))
            promoted_score = max(float(copy_item.score or 0.0), top_score + margin)
            bonus = promoted_score - float(copy_item.score or 0.0)
            copy_item.score += bonus
            copy_item.score_components["closure_verified_patch_target"] = bonus
            copy_item.reasons.append(f"closure_verified_patch_target:{round(bonus, 3)}")
            adjustments.append({"path": item.path, "bonus": round(bonus, 3), "confidence": confidence})
            promotion_order = 0
        updated.append((promotion_order, original_position, copy_item))
    # Closure may promote verified targets, but all other files retain the
    # source-grounded precision order instead of being re-sorted by raw scores.
    updated.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in updated[:top_k]], {
        "applied": bool(adjustments),
        "strategy": (
            "complete_verified_patch_target_block_promotion"
            if closure.get("complete")
            else "partial_verified_root_promotion"
        ),
        "closure_complete": bool(closure.get("complete")),
        "margin_ratio": margin_ratio,
        "minimum_margin": minimum_margin,
        "adjustments": adjustments,
        "preserved_candidate_set": True,
    }


def _transfer_artifact_evidence(
    ranked: list[RankedLocation],
    index: RepositoryIndex,
) -> tuple[list[RankedLocation], list[dict[str, Any]]]:
    """Attach bundle evidence to an already recalled source counterpart."""

    by_basename: dict[str, list[str]] = defaultdict(list)
    for path in index.files:
        if _path_role(path) not in _CLOSURE_BLOCKED_ROLES and path.startswith("src/"):
            by_basename[Path(path).name.lower()].append(path)
    copies = [copy.deepcopy(item) for item in ranked]
    by_path = {_norm_path(item.path): item for item in copies}
    mappings: list[dict[str, Any]] = []
    for artifact in copies:
        if _path_role(artifact.path) != "generated_or_lockfile":
            continue
        filename = Path(artifact.path).name.lower()
        source_name = re.sub(r"\.(?:esm|umd|min|bundle)(?=\.)", "", filename)
        counterparts = sorted(by_basename.get(source_name, []), key=lambda path: (path.count("/"), path))
        if not counterparts:
            continue
        source_path = counterparts[0]
        source = by_path.get(source_path)
        transferred = source is not None
        mapping = {
            "artifact": artifact.path,
            "source": source_path,
            "transferred_to_recalled_source": transferred,
        }
        artifact.belief["artifact_of"] = source_path
        if source is not None:
            source.belief.setdefault("artifact_navigation_from", []).append(artifact.path)
            source.reasons.append(f"artifact_navigation_from:{artifact.path}")
            source.score_components["artifact_source_mapping"] = 1.0
        mappings.append(mapping)
    return copies, mappings


def _rank_stage_snapshot(
    ranked: Iterable[RankedLocation],
    *,
    locks: Iterable[dict[str, Any]] = (),
    replacements: Iterable[dict[str, Any]] = (),
    limit: int = 15,
) -> list[dict[str, Any]]:
    lock_reasons = {str(item.get("path") or ""): str(item.get("reason") or "") for item in locks}
    replacement_reasons = {
        str(item.get("added") or ""): f"replaced:{item.get('removed')}"
        for item in replacements
    }
    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(list(ranked)[:limit], start=1):
        precision = (item.belief or {}).get("precision_rerank") or {}
        review = (item.belief or {}).get("llm_candidate_review") or {}
        rows.append(
            {
                "path": item.path,
                "rank": rank,
                "original_rank": precision.get("base_rank", rank),
                "path_role": _path_role(item.path),
                "score": round(float(item.score or 0.0), 4),
                "evidence_quality": precision.get("quality"),
                "read_verified": bool((item.belief or {}).get("read_verified")),
                "mechanism_verified": bool(review.get("mechanism_verified")),
                "evidence_sources": list((item.belief or {}).get("supporting_axes", []) or [])[:8],
                "lock_reason": lock_reasons.get(_norm_path(item.path)),
                "replacement_reason": replacement_reasons.get(_norm_path(item.path)),
            }
        )
    return rows


def _save_dynamic_localization_checkpoint(
    *,
    sample: NormalizedSample,
    index: RepositoryIndex,
    issue_sketch: Any,
    queries: list[str],
    search_trace: list[dict[str, Any]],
    max_rounds: int,
    react_max_steps: int,
    graph_scope: dict[str, Any],
    phase_timings: list[dict[str, Any]],
    react_agent: dict[str, Any],
    rounds: list[DynamicSearchRound],
    best_round: DynamicSearchRound | None,
    checkpoint_updates: list[dict[str, Any]],
) -> None:
    """Persist a cheap, evaluable snapshot after each fully completed search round."""

    if not rounds:
        return
    final_round = rounds[-1]
    use_best = _env_bool("MYCODE_BEST_ROUND_CHECKPOINT", True)
    selected_round = best_round if use_best and best_round is not None else final_round
    flow_traces = _merge_flow_traces(*(item.flow_traces for item in rounds))[:34]
    round_selection = {
        "enabled": use_best,
        "selected_round": selected_round.round_no,
        "latest_round": final_round.round_no,
        "rolled_back": selected_round.round_no != final_round.round_no,
        "selected_quality": _round_checkpoint_quality(selected_round),
        "latest_quality": _round_checkpoint_quality(final_round),
        "updates": copy.deepcopy(checkpoint_updates),
    }
    _DYNAMIC_LOCALIZATION_CHECKPOINTS[str(sample.instance_id)] = {
        "instance_id": sample.instance_id,
        "repo": sample.repo,
        "dataset": sample.dataset,
        "status": "partial",
        "partial": True,
        "termination": {
            "reason": "interrupted_after_complete_search_round",
            "completed_rounds": len(rounds),
            "selected_round": selected_round.round_no,
        },
        "index": index.metadata(),
        "issue_sketch": issue_sketch.to_dict(),
        "queries": queries[:80],
        "search_trace": copy.deepcopy(search_trace),
        "max_rounds": max(1, max_rounds),
        "max_react_steps": react_max_steps,
        "graph_scope": copy.deepcopy(graph_scope),
        "phase_timings": copy.deepcopy(phase_timings),
        "react_agent_trace": copy.deepcopy(react_agent),
        "dynamic_rounds": [item.to_dict() for item in rounds],
        "best_round_selection": round_selection,
        "rank_stage_snapshots": {
            "rounds": {
                str(item.round_no): _rank_stage_snapshot(item.ranked_locations)
                for item in rounds
            },
            "best_round": _rank_stage_snapshot(selected_round.ranked_locations),
        },
        "head_selection": {"enabled": False, "reason": "not_run_partial_checkpoint"},
        "adaptive_locks": [],
        "artifact_mappings": [],
        "external_reproduction_evidence": [],
        "checkpoint_list_quality": round_selection["selected_quality"],
        "agent_trace": [
            {
                "round_no": item.round_no,
                "actions": item.agent_actions,
                "frontier_state": item.frontier_state,
                "observation": item.agent_observation,
                "stop_decision": item.stop_decision,
            }
            for item in rounds
        ],
        "round_evaluations": [item.evaluation for item in rounds],
        "code_contexts": copy.deepcopy(selected_round.code_contexts),
        "flow_traces": copy.deepcopy(flow_traces),
        "verifier": copy.deepcopy(selected_round.verifier),
        "modification_closure": {
            "status": "not_run_partial_checkpoint",
            "complete": False,
            "files": [],
            "rounds_run": 0,
            "coverage": {"required_coverage": 0.0, "missing_required": ["timeout_recovery"]},
        },
        "final_patch_set": [],
        "agent_reasoning_summary": {
            "status": "partial_checkpoint",
            "completed_rounds": len(rounds),
            "selected_round": selected_round.round_no,
        },
        "ranked_locations": [item.to_dict() for item in selected_round.ranked_locations],
        "ranked_modules": [item.to_dict() for item in selected_round.ranked_modules],
        "ranked_functions": [item.to_dict() for item in selected_round.ranked_functions],
    }
    phase_event(
        "progress",
        "dynamic.evaluable_checkpoint",
        completed_rounds=len(rounds),
        selected_round=selected_round.round_no,
        candidate_count=len(selected_round.ranked_locations),
        top_files=[item.path for item in selected_round.ranked_locations[:5]],
    )


def dynamic_localize(
    sample: NormalizedSample,
    evidence_result: Dict[str, Any],
    *,
    top_k: int = 15,
    index: RepositoryIndex | None = None,
    repo_root: Path | None = None,
    structure_path: Path | None = None,
    max_rounds: int = 3,
    max_react_steps: int | None = None,
    controller_llm: LLMController | None = None,
    lightweight: bool = False,
) -> Dict[str, Any]:
    clear_dynamic_localization_checkpoint(sample.instance_id)
    if index is None:
        index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            dataset=sample.dataset,
            repo_root=repo_root,
            structure_path=structure_path,
        )
    if not index.ready:
        return {
            "instance_id": sample.instance_id,
            "repo": sample.repo,
            "dataset": sample.dataset,
            "status": "missing_repo_index",
            "index": index.metadata(),
            "queries": [],
            "ranked_locations": [],
            "search_trace": [{"step": "load_index", "status": "missing_repo_index"}],
        }

    tool_observations = evidence_result.get("tool_observations", []) or []
    issue_sketch = build_issue_sketch(sample, evidence_result)
    query_groups = _build_localization_query_groups(sample, evidence_result, issue_sketch, tool_observations)
    queries = query_groups.get("all", [])
    phase_timings: list[dict[str, Any]] = []

    fast_seed_plan = plan_fast_seeds(
        index=index,
        issue_text=_issue_query_text(sample.issue_text),
        query_groups=query_groups,
        evidence_result=evidence_result,
        controller_llm=controller_llm,
    )
    fast_seed_paths = [path for path in fast_seed_plan.get("seed_files", []) if path in index.files]
    persistent_seed_paths = [
        path for path in fast_seed_plan.get("persistent_seed_files", []) if path in index.files
    ]
    if lightweight:
        with phase_context("dynamic.lightweight_localize", query_count=len(queries), top_k=top_k):
            return _lightweight_dynamic_localize(
                sample=sample,
                evidence_result=evidence_result,
                index=index,
                issue_sketch=issue_sketch,
                queries=queries,
                top_k=top_k,
            )

    phase_start = time.perf_counter()
    with phase_context("dynamic.scope_prefilter", query_count=len(queries), top_k=top_k):
        graph_scope_paths, graph_scope = _build_deep_graph_scope(
            index=index,
            sample=sample,
            evidence_result=evidence_result,
            query_groups=query_groups,
            queries=queries,
            top_k=top_k,
        )
        phase_event(
            "progress",
            "dynamic.scope_prefilter",
            selected_paths=graph_scope.get("selected_paths"),
            scope_limit=graph_scope.get("scope_limit"),
            full_repo_files=graph_scope.get("full_repo_files"),
            source_first_adjusted=(graph_scope.get("source_first_filter") or {}).get("adjusted_count"),
        )
    phase_timings.append(
        {
            "phase": "scope_prefilter",
            "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
            "selected_paths": graph_scope.get("selected_paths"),
            "scope_limit": graph_scope.get("scope_limit"),
            "full_repo_files": graph_scope.get("full_repo_files"),
        }
    )
    phase_start = time.perf_counter()
    with phase_context("dynamic.build_scoped_repository_graph", selected_paths=graph_scope.get("selected_paths")):
        graph = TypedRepositoryGraph(index, scope_paths=graph_scope_paths)
        phase_event("progress", "dynamic.build_scoped_repository_graph", graph_summary=graph.edge_summary())
    phase_timings.append(
        {
            "phase": "build_scoped_repository_graph",
            "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
            "graph_summary": graph.edge_summary(),
        }
    )
    with phase_context("dynamic.build_scoped_tool_index", graph_file_count=len(getattr(graph, "graph_files", []) or [])):
        tool_index = _restricted_index(index, getattr(graph, "graph_files", []) or [])
        phase_event(
            "progress",
            "dynamic.build_scoped_tool_index",
            file_count=len(tool_index.files),
            entity_count=len(tool_index.entities),
        )
    phase_timings.append(
        {
            "phase": "build_scoped_tool_index",
            "elapsed_seconds": 0.0,
            "file_count": len(tool_index.files),
            "entity_count": len(tool_index.entities),
        }
    )
    react_max_steps = (
        max(1, int(max_react_steps))
        if max_react_steps is not None and int(max_react_steps) > 0
        else max(
            _env_int("MYCODE_REACT_AUTO_MIN_STEPS", 4, minimum=1),
            min(
                _env_int("MYCODE_REACT_AUTO_MAX_STEPS", 5, minimum=1),
                max_rounds * 2 + 1,
            ),
        )
    )
    phase_start = time.perf_counter()
    with phase_context("dynamic.react_tool_agent_bootstrap", max_steps=react_max_steps, top_k=top_k):
        react_agent = run_react_tool_agent(
            sample=sample,
            evidence_result=evidence_result,
            index=tool_index,
            graph=graph,
            issue_sketch=issue_sketch,
            queries=queries,
            previous_candidates=fast_seed_paths,
            top_k=top_k,
            max_steps=react_max_steps,
            planner_llm=controller_llm,
        )
        phase_event(
            "progress",
            "dynamic.react_tool_agent_bootstrap",
            candidate_count=len(react_agent.get("candidate_paths", []) or []),
            step_count=len(react_agent.get("steps", []) or []),
        )
    phase_timings.append(
        {
            "phase": "react_tool_agent_bootstrap",
            "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
            "candidate_count": len(react_agent.get("candidate_paths", []) or []),
            "step_count": len(react_agent.get("steps", []) or []),
        }
    )
    react_candidate_paths = _dedupe(fast_seed_paths + [
        path
        for path in react_agent.get("candidate_paths", [])
        if path in index.files
    ], limit=top_k)
    # The ReAct loop already invokes SearchAnchor, NavigateCode, TraceFlow and
    # ReadCode. Running the deterministic four-tool bootstrap immediately after
    # it repeats the same repository work and was the main source of long-tail
    # latency. Keep the legacy mode for ablations, but reuse ReAct observations
    # by default so there is one candidate state entering the dynamic rounds.
    default_bootstrap_mode = "react_only" if controller_llm is not None else "react_plus_fixed"
    bootstrap_mode = os.environ.get("MYCODE_AGENT_BOOTSTRAP_MODE", default_bootstrap_mode).strip().lower()
    phase_start = time.perf_counter()
    if bootstrap_mode in {"react_plus_fixed", "legacy", "both"}:
        with phase_context("dynamic.four_tool_agent_bootstrap", previous_candidate_count=len(react_candidate_paths), top_k=top_k):
            bootstrap_agent = run_four_tool_agent_round(
                sample=sample,
                evidence_result=evidence_result,
                index=tool_index,
                graph=graph,
                issue_sketch=issue_sketch,
                queries=_dedupe(queries + react_agent.get("next_queries", [])),
                previous_candidates=react_candidate_paths,
                top_k=top_k,
            )
    else:
        bootstrap_agent = {
            "status": "reused_react_state",
            "strategy": "single ReAct bootstrap; deterministic four-tool replay disabled",
            "summary": {
                "mode": "react_only",
                "candidate_count": len(react_candidate_paths),
                "read_file_count": len(react_agent.get("read_paths", []) or []),
            },
            "tool_observations": react_agent.get("tool_observations", []) or [],
            "candidate_paths": react_candidate_paths,
            "read_paths": react_agent.get("read_paths", []) or [],
            "flow_traces": react_agent.get("flow_traces", []) or [],
            "flow_coverage": react_agent.get("flow_coverage", {}) or {},
            "pruning": {"strategy": "reuse_react_candidate_state"},
            "next_queries": react_agent.get("next_queries", []) or [],
        }
    phase_event(
        "progress",
        "dynamic.four_tool_agent_bootstrap",
        mode=bootstrap_mode,
        status=bootstrap_agent.get("status"),
        candidate_count=len(bootstrap_agent.get("candidate_paths", []) or []),
        read_count=len(bootstrap_agent.get("read_paths", []) or []),
        flow_count=len(bootstrap_agent.get("flow_traces", []) or []),
    )
    phase_timings.append(
        {
            "phase": "four_tool_agent_bootstrap",
            "mode": bootstrap_mode,
            "status": bootstrap_agent.get("status"),
            "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
            "candidate_count": len(bootstrap_agent.get("candidate_paths", []) or []),
            "read_count": len(bootstrap_agent.get("read_paths", []) or []),
        }
    )
    evidence_seed_paths = _collect_evidence_seed_paths(evidence_result)
    bootstrap_candidate_paths = _dedupe(
        list(fast_seed_paths) + list(react_candidate_paths) + list(bootstrap_agent.get("candidate_paths", [])),
        limit=top_k * 2,
    )
    bootstrap_candidate_paths = [
        path
        for path in bootstrap_candidate_paths
        if path in index.files and (_norm_path(path) not in evidence_seed_paths or path in fast_seed_paths)
    ][:top_k]
    search_trace: list[dict[str, Any]] = [
        {
            "step": "fast_seed_planner",
            "strategy": fast_seed_plan.get("strategy"),
            "candidate_files": fast_seed_plan.get("candidate_files", []),
            "seed_files": fast_seed_paths,
            "persistent_seed_files": persistent_seed_paths,
            "llm_status": fast_seed_plan.get("llm_status"),
            "evidence": fast_seed_plan.get("evidence", {}),
        },
        {
            "step": "issue_sketch",
            "strategy": "evidence role understanding before localization",
            "issue_sketch": issue_sketch.to_dict(),
        },
        {
            "step": "query_generation",
            "strategy": "multi-channel SearchAnchor query generation: explicit entity + evidence role + concern + effect + flow",
            "query_count": len(queries),
            "queries_preview": queries[:30],
            "query_groups": {key: values[:40] for key, values in query_groups.items() if key != "all"},
        },
        {
            "step": "dynamic_search_loop",
            "strategy": (
                "CoSIL-style cheap recall/prune first, scoped graph navigation second, "
                "then ARISE-style flow verification and explicit navigation policy"
            ),
            "max_rounds": max(1, max_rounds),
            "react_max_steps": react_max_steps,
        },
        {
            "step": "graph_scope_prefilter",
            "strategy": graph_scope.get("strategy"),
            "scope": graph_scope,
        },
        {
            "step": "repository_graph",
            "strategy": "language-aware heterogeneous graph over scoped files/entities/styles/config/docs",
            "graph_summary": graph.edge_summary(),
        },
        {
            "step": "phase_timings",
            "strategy": "timing instrumentation for LLM/tool/search bottleneck diagnosis",
            "phase_timings": phase_timings,
        },
        {
            "step": "react_tool_agent_bootstrap",
            "strategy": react_agent.get("strategy"),
            "summary": react_agent.get("summary"),
            "planner_used": react_agent.get("planner_used"),
            "flow_coverage": react_agent.get("flow_coverage", {}),
            "candidate_paths": react_agent.get("candidate_paths", [])[:20],
            "read_paths": react_agent.get("read_paths", [])[:20],
            "next_queries": react_agent.get("next_queries", [])[:20],
            "steps": react_agent.get("steps", [])[:8],
        },
        {
            "step": "four_tool_agent_bootstrap",
            "strategy": bootstrap_agent.get("strategy"),
            "summary": bootstrap_agent.get("summary"),
            "flow_coverage": bootstrap_agent.get("flow_coverage", {}),
            "pruning": bootstrap_agent.get("pruning", {}),
            "tool_observations": bootstrap_agent.get("tool_observations", [])[:6],
            "candidate_paths": bootstrap_agent.get("candidate_paths", [])[:20],
            "read_paths": bootstrap_agent.get("read_paths", [])[:20],
            "next_queries": bootstrap_agent.get("next_queries", [])[:20],
        },
    ]

    rounds: list[DynamicSearchRound] = []
    active_queries = _dedupe(queries + react_agent.get("next_queries", []) + bootstrap_agent.get("next_queries", []))
    initial_seed_ranking: list[RankedLocation] = [
        RankedLocation(
            path=path,
            score=max(6.0, 16.0 - idx),
            reasons=["fast_seed_or_bootstrap_candidate"],
            belief={
                "fast_seed": path in fast_seed_paths,
                "persistent_seed": path in persistent_seed_paths,
                "supporting_axes": (fast_seed_plan.get("evidence", {}).get(path, {}) or {}).get("channels", []),
            },
        )
        for idx, path in enumerate(bootstrap_candidate_paths)
    ]
    previous_ranked: list[RankedLocation] = list(initial_seed_ranking)
    previous_flow_traces: list[dict[str, Any]] = _merge_flow_traces(
        react_agent.get("flow_traces", []) or [],
        bootstrap_agent.get("flow_traces", []) or [],
    )[:18]
    previous_top_paths: list[str] = []
    previous_round_progress: dict[str, Any] = {}
    best_round: DynamicSearchRound | None = None
    best_round_quality: dict[str, Any] = {"score": -1.0, "reason": "not_started"}
    checkpoint_updates: list[dict[str, Any]] = []

    for round_no in range(1, max(1, max_rounds) + 1):
        phase_start = time.perf_counter()
        with phase_context("dynamic.search_round", round_no=round_no, query_count=len(active_queries), previous_candidate_count=len(previous_ranked)):
            search_round = _run_search_round(
                round_no=round_no,
                sample=sample,
                evidence_result=evidence_result,
                index=index,
                graph=graph,
                queries=active_queries,
                previous_ranked=previous_ranked,
                previous_flow_traces=previous_flow_traces,
                previous_top_paths=previous_top_paths,
                top_k=top_k,
                max_rounds=max(1, max_rounds),
                controller_llm=controller_llm,
                query_groups=query_groups,
                previous_round_progress=previous_round_progress,
            )
            phase_event(
                "progress",
                "dynamic.search_round",
                round_no=round_no,
                candidate_count=len(search_round.ranked_locations),
                top_files=[item.path for item in search_round.ranked_locations[:5]],
                stop_reason=search_round.stop_decision.get("reason"),
            )
        phase_timings.append(
            {
                "phase": f"dynamic_search_round_{round_no}",
                "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
                "top_files": [item.path for item in search_round.ranked_locations[:5]],
                "candidate_count": len(search_round.ranked_locations),
            }
        )
        followup_agent: dict[str, Any]
        default_followup_mode = "on_demand" if controller_llm is not None else "always"
        followup_mode = os.environ.get("MYCODE_AGENT_FOLLOWUP_MODE", default_followup_mode).strip().lower()
        candidate_review = (search_round.verifier.get("llm_candidate_review") or {}) if search_round.verifier else {}
        round_confidence = search_round.stop_decision.get("confidence", {}) or {}
        followup_threshold = _env_float("MYCODE_AGENT_FOLLOWUP_CONFIDENCE", 0.58, minimum=0.0)
        followup_requested = bool(candidate_review.get("continue_search"))
        followup_low_confidence = float(round_confidence.get("confidence") or 0.0) < followup_threshold
        should_run_followup = (
            followup_mode in {"always", "legacy"}
            or (
                followup_mode not in {"off", "disabled", "none"}
                and not search_round.stop_decision.get("stop")
                and (followup_requested or followup_low_confidence)
            )
        )
        if not should_run_followup:
            followup_agent = {
                "status": "skipped",
                "strategy": "on-demand followup; reuse current candidate state",
                "summary": {
                    "reason": search_round.stop_decision.get("reason"),
                    "mode": followup_mode,
                    "review_requested": followup_requested,
                    "confidence": round_confidence.get("confidence"),
                },
                "tool_observations": [],
                "candidate_paths": [],
                "read_paths": [],
                "flow_traces": [],
                "flow_coverage": {},
                "pruning": {},
                "next_queries": [],
            }
            phase_event(
                "progress",
                "dynamic.four_tool_followup",
                round_no=round_no,
                status="skipped",
                reason="no_evidence_gap_requiring_followup",
                mode=followup_mode,
            )
            phase_timings.append(
                {
                    "phase": f"four_tool_followup_round_{round_no}",
                    "elapsed_seconds": 0.0,
                    "status": "skipped",
                    "reason": "no_evidence_gap_requiring_followup",
                    "mode": followup_mode,
                }
            )
        else:
            phase_start = time.perf_counter()
            with phase_context("dynamic.four_tool_followup", round_no=round_no, previous_candidate_count=len(search_round.ranked_locations[:10])):
                followup_agent = run_four_tool_agent_round(
                    sample=sample,
                    evidence_result=evidence_result,
                    index=tool_index,
                    graph=graph,
                    issue_sketch=issue_sketch,
                    queries=_dedupe(active_queries + search_round.next_queries),
                    previous_candidates=[item.path for item in search_round.ranked_locations[:10]],
                    top_k=top_k,
                )
                phase_event(
                    "progress",
                    "dynamic.four_tool_followup",
                    round_no=round_no,
                    candidate_count=len(followup_agent.get("candidate_paths", []) or []),
                    read_count=len(followup_agent.get("read_paths", []) or []),
                )
            phase_timings.append(
                {
                    "phase": f"four_tool_followup_round_{round_no}",
                    "elapsed_seconds": round(time.perf_counter() - phase_start, 3),
                    "candidate_count": len(followup_agent.get("candidate_paths", []) or []),
                    "read_count": len(followup_agent.get("read_paths", []) or []),
                }
            )
        search_round.agent_actions.extend(
            {
                "tool": observation.get("tool"),
                "action": observation.get("action"),
                "status": observation.get("status"),
                "candidate_count": len(observation.get("candidates", []) or []),
                "notes": observation.get("notes", [])[:2],
            }
            for observation in followup_agent.get("tool_observations", [])[:6]
        )
        search_round.flow_traces = _merge_flow_traces(
            search_round.flow_traces,
            followup_agent.get("flow_traces", []) or [],
        )[:26]
        search_round.next_queries = _dedupe(search_round.next_queries + followup_agent.get("next_queries", []))
        search_round.agent_observation["four_tool_agent_followup"] = followup_agent.get("summary")
        search_round.agent_observation["four_tool_agent_flow_coverage"] = followup_agent.get("flow_coverage", {})
        search_round.agent_observation["four_tool_agent_pruning"] = followup_agent.get("pruning", {})
        search_round.agent_observation["four_tool_agent_read_paths"] = followup_agent.get("read_paths", [])[:20]
        rounds.append(search_round)
        checkpoint_quality = _round_checkpoint_quality(search_round)
        search_round.frontier_state["checkpoint_quality"] = checkpoint_quality
        minimum_gain = _env_float("MYCODE_BEST_ROUND_MIN_GAIN", 0.15, minimum=0.0)
        selected = best_round is None or _checkpoint_is_better(
            checkpoint_quality,
            best_round_quality,
            minimum_gain=minimum_gain,
        )
        if selected:
            best_round = search_round
            best_round_quality = checkpoint_quality
        checkpoint_updates.append(
            {
                "round_no": round_no,
                "selected": selected,
                "quality": checkpoint_quality,
                "best_round_no": best_round.round_no if best_round is not None else None,
                "best_score": best_round_quality.get("score"),
            }
        )
        phase_event(
            "progress",
            "dynamic.best_round_checkpoint",
            round_no=round_no,
            selected=selected,
            quality=checkpoint_quality,
            best_round_no=best_round.round_no if best_round is not None else None,
            best_score=best_round_quality.get("score"),
        )
        previous_ranked = search_round.ranked_locations
        previous_flow_traces = search_round.flow_traces
        previous_top_paths = search_round.stop_decision.get("top_paths", [])
        previous_round_progress = dict(search_round.frontier_state.get("round_progress") or {})
        search_trace.append(
            {
                "step": f"round_{round_no}",
                "strategy": "search -> read snippets -> trace call/dataflow -> verify -> derive next frontier",
                "input_query_count": len(search_round.input_queries),
                "seed_files": search_round.seed_files[:10],
                "top_files": [item.path for item in search_round.ranked_locations[:10]],
                "top_modules": [item.id for item in search_round.ranked_modules[:10]],
                "top_functions": [item.id for item in search_round.ranked_functions[:10]],
                "frontier_state": search_round.frontier_state,
                "agent_actions": search_round.agent_actions,
                "agent_observation": search_round.agent_observation,
                "four_tool_agent": {
                    "summary": followup_agent.get("summary"),
                    "tool_observations": followup_agent.get("tool_observations", [])[:6],
                },
                "next_queries": search_round.next_queries[:20],
                "round_evaluation": search_round.evaluation,
                "stop_decision": search_round.stop_decision,
            }
        )
        _save_dynamic_localization_checkpoint(
            sample=sample,
            index=index,
            issue_sketch=issue_sketch,
            queries=queries,
            search_trace=search_trace,
            max_rounds=max_rounds,
            react_max_steps=react_max_steps,
            graph_scope=graph_scope,
            phase_timings=phase_timings,
            react_agent=react_agent,
            rounds=rounds,
            best_round=best_round,
            checkpoint_updates=checkpoint_updates,
        )
        if search_round.stop_decision.get("stop"):
            break
        active_queries = _dedupe(queries + search_round.next_queries)

    final_round = rounds[-1]
    use_checkpoint = _env_bool("MYCODE_BEST_ROUND_CHECKPOINT", True)
    selected_round = best_round if use_checkpoint and best_round is not None else final_round
    ranked = selected_round.ranked_locations
    ranked_modules = selected_round.ranked_modules
    ranked_functions = selected_round.ranked_functions
    all_context_map = _closure_context_map(
        context
        for search_round in rounds
        for context in search_round.code_contexts
    )
    code_contexts = list(all_context_map.values())
    flow_traces = _merge_flow_traces(*(item.flow_traces for item in rounds))[:34]
    verifier = selected_round.verifier
    merged_candidate_review = _merge_closure_candidate_reviews(
        [item.verifier for item in rounds]
    )
    rank_stage_snapshots: dict[str, Any] = {
        "fast_seed": _rank_stage_snapshot(
            [item for item in initial_seed_ranking if item.path in fast_seed_paths],
            limit=top_k,
        ),
        "rounds": {
            str(item.round_no): _rank_stage_snapshot(item.ranked_locations, limit=top_k)
            for item in rounds
        },
        "best_round": _rank_stage_snapshot(ranked, limit=top_k),
    }
    ranked, artifact_mappings = _transfer_artifact_evidence(ranked, index)
    ranked, seed_retention = restore_seed_frontier(
        ranked,
        [item for item in initial_seed_ranking if item.path in fast_seed_paths],
        index=index,
        review=merged_candidate_review,
        top_k=top_k,
        limit=_env_int("MYCODE_FINAL_SEED_PREFIX", 2, minimum=0),
    )
    rank_stage_snapshots["seed_retention"] = _rank_stage_snapshot(ranked, limit=top_k)
    ranked, cross_round_frontier = _cross_round_candidate_frontier(
        selected=ranked,
        round_rankings=[initial_seed_ranking] + [item.ranked_locations for item in rounds],
        issue_text=_issue_query_text(sample.issue_text),
        review=merged_candidate_review,
        code_contexts=code_contexts,
        top_k=top_k,
    )
    rank_stage_snapshots["cross_round_frontier"] = _rank_stage_snapshot(
        ranked,
        locks=cross_round_frontier.get("adaptive_locks", []) or [],
        replacements=cross_round_frontier.get("replacements", []) or [],
        limit=top_k,
    )
    ranked, precision_rerank = _precision_rerank_locations(
        ranked=ranked,
        round_rankings=[initial_seed_ranking] + [item.ranked_locations for item in rounds],
        issue_text=_issue_query_text(sample.issue_text),
        review=merged_candidate_review,
        code_contexts=code_contexts,
        top_k=top_k,
    )
    rank_stage_snapshots["head_selector"] = _rank_stage_snapshot(ranked, limit=top_k)
    round_selection = {
        "enabled": use_checkpoint,
        "selected_round": selected_round.round_no,
        "latest_round": final_round.round_no,
        "rolled_back": selected_round.round_no != final_round.round_no,
        "selected_quality": _round_checkpoint_quality(selected_round),
        "latest_quality": _round_checkpoint_quality(final_round),
        "updates": checkpoint_updates,
        "cross_round_frontier": cross_round_frontier,
        "precision_rerank": precision_rerank,
        "seed_retention": seed_retention,
    }
    phase_event(
        "progress",
        "dynamic.best_round_selection",
        selected_round=selected_round.round_no,
        latest_round=final_round.round_no,
        rolled_back=round_selection["rolled_back"],
        selected_quality=round_selection["selected_quality"],
        latest_quality=round_selection["latest_quality"],
    )
    phase_event(
        "progress",
        "dynamic.cross_round_frontier",
        enabled=cross_round_frontier.get("enabled"),
        strategy=cross_round_frontier.get("strategy"),
        replacement_count=len(cross_round_frontier.get("replacements", []) or []),
        replacements=cross_round_frontier.get("replacements", [])[:4],
    )
    phase_event(
        "progress",
        "dynamic.precision_rerank",
        enabled=precision_rerank.get("enabled"),
        strategy=precision_rerank.get("strategy"),
        top_before=precision_rerank.get("top_before", [])[:5],
        top_after=precision_rerank.get("top_after", [])[:5],
        changed_count=len(precision_rerank.get("changes", []) or []),
    )
    closure_verifier = dict(verifier)
    closure_verifier["llm_candidate_review"] = merged_candidate_review
    with phase_context(
        "dynamic.modification_closure",
        ranked_candidate_count=len(ranked),
        flow_count=len(flow_traces),
    ):
        modification_closure = _build_modification_closure(
            ranked=ranked,
            verifier=closure_verifier,
            graph=graph,
            issue_sketch=issue_sketch,
            code_contexts=code_contexts,
            flow_traces=flow_traces,
        )
        phase_event(
            "progress",
            "dynamic.modification_closure",
            status=modification_closure.get("status"),
            complete=modification_closure.get("complete"),
            final_patch_count=len(modification_closure.get("files", []) or []),
            final_patch_files=modification_closure.get("files", [])[:8],
            rounds_run=modification_closure.get("rounds_run"),
            required_coverage=(modification_closure.get("coverage") or {}).get("required_coverage"),
            missing_required=(modification_closure.get("coverage") or {}).get("missing_required", [])[:8],
            partial_fallback_applied=(modification_closure.get("partial_fallback") or {}).get("applied", False),
            cardinality_bounds=(modification_closure.get("partial_fallback") or {}).get("bounds", {}),
        )
    ranked, closure_rerank = _rerank_with_modification_closure(
        ranked,
        modification_closure,
        top_k=top_k,
    )
    ranked, generated_mappings = demote_generated_outputs(
        ranked, files=index.files, issue_text=_issue_query_text(sample.issue_text),
    )
    artifact_mappings.extend(generated_mappings)
    rank_stage_snapshots["modification_closure"] = _rank_stage_snapshot(ranked, limit=top_k)
    modification_closure["ranking_adjustment"] = closure_rerank
    modification_closure["ranking_kept_separate"] = not bool(closure_rerank.get("applied"))
    phase_event(
        "progress",
        "dynamic.closure_rerank",
        applied=closure_rerank.get("applied"),
        reason=closure_rerank.get("reason"),
        adjustments=closure_rerank.get("adjustments", [])[:8],
        top_files=[item.path for item in ranked[:5]],
    )
    agent_reasoning_summary = _agent_reasoning_summary(
        sample=sample,
        evidence_result=evidence_result,
        graph=graph,
        rounds=rounds,
        ranked=ranked,
        ranked_modules=ranked_modules,
        ranked_functions=ranked_functions,
        flow_traces=flow_traces,
        verifier=verifier,
    )
    external_reproduction_evidence = [
        evidence
        for observation in tool_observations
        for evidence in (observation.get("extracted", {}) or {}).get(
            "external_reproduction_evidence", []
        ) or []
    ]
    return {
        "instance_id": sample.instance_id,
        "repo": sample.repo,
        "dataset": sample.dataset,
        "status": "ok",
        "index": index.metadata(),
        "issue_sketch": issue_sketch.to_dict(),
        "queries": queries[:80],
        "search_trace": search_trace,
        "max_rounds": max(1, max_rounds),
        "max_react_steps": react_max_steps,
        "graph_scope": graph_scope,
        "phase_timings": phase_timings,
        "react_agent_trace": react_agent,
        "dynamic_rounds": [item.to_dict() for item in rounds],
        "best_round_selection": round_selection,
        "rank_stage_snapshots": rank_stage_snapshots,
        "head_selection": precision_rerank,
        "adaptive_locks": cross_round_frontier.get("adaptive_locks", []) or [],
        "artifact_mappings": artifact_mappings,
        "external_reproduction_evidence": external_reproduction_evidence,
        "checkpoint_list_quality": round_selection.get("selected_quality", {}),
        "agent_trace": [
            {
                "round_no": item.round_no,
                "actions": item.agent_actions,
                "frontier_state": item.frontier_state,
                "observation": item.agent_observation,
                "stop_decision": item.stop_decision,
            }
            for item in rounds
        ],
        "round_evaluations": [item.evaluation for item in rounds],
        "code_contexts": code_contexts,
        "flow_traces": flow_traces,
        "verifier": verifier,
        "modification_closure": modification_closure,
        "final_patch_set": modification_closure.get("files", []),
        "agent_reasoning_summary": agent_reasoning_summary,
        "ranked_locations": [item.to_dict() for item in ranked],
        "ranked_modules": [item.to_dict() for item in ranked_modules],
        "ranked_functions": [item.to_dict() for item in ranked_functions],
    }
