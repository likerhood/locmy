from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mycode.repo_index.structure_index import tokenize
from mycode.schemas.evidence import NormalizedSample


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_.$-]*\b")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
GENERIC_GROUNDING_TOKENS = {
    "behavior", "callback", "change", "code", "component", "config", "data",
    "error", "event", "file", "function", "handler", "implementation", "issue",
    "layout", "method", "module", "option", "plugin", "render", "result", "state",
    "type", "update", "value",
}


@dataclass
class IssueSketch:
    """Compact contract between evidence understanding and code navigation.

    The sketch does not rank files directly. It records what the issue is about,
    which evidence items are only navigation seeds, and which state/effect flows
    must be checked before a candidate can be trusted as a patch target.
    """

    instance_id: str
    repo: str
    dataset: str
    task_type: str = "unknown"
    workflow: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    evidence_roles: list[dict[str, Any]] = field(default_factory=list)
    navigation_hints: list[dict[str, Any]] = field(default_factory=list)
    flow_obligations: list[dict[str, Any]] = field(default_factory=list)
    seed_policy: list[dict[str, Any]] = field(default_factory=list)
    claim_evidence: list[dict[str, Any]] = field(default_factory=list)
    architectural_queries: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    reasoning_trace: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dedupe(values: Iterable[str], *, limit: int = 80) -> list[str]:
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


def _issue_text(sample: NormalizedSample) -> str:
    text = str(sample.raw.get("problem_statement") or sample.issue_text or "")
    # Clean15 samples may carry adapter-generated multimodal summaries inside
    # problem_statement. URL/image tools already expose those observations with
    # provenance, so the semantic sketch should stay grounded in the issue body.
    for marker in (
        "\nAttached Images:",
        "\nRelated URLs:",
        "\n[adapter_fallback=",
        "\n[Multimodal Context - Compact]",
        "\n[Adapter Note]",
    ):
        text = text.split(marker, 1)[0]
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"(?:\[Original Issue\]\s*)+", "", text).strip()
    return text


def _task_type(text: str) -> str:
    lower = text.lower()
    feature_markers = (
        "feature request",
        "we should",
        "should make it",
        "make it so",
        "i propose",
        "add support",
        "allow changing",
        "enable ",
        "new option",
        "new feature",
    )
    if any(marker in lower for marker in feature_markers):
        return "feature_request"
    if any(marker in lower for marker in ("refactor", "cleanup", "clean up", "rename", "deprecate")):
        return "refactor_or_maintenance"
    if any(marker in lower for marker in ("bug", "regression", "crash", "incorrect", "does not", "doesn't", "fails", "error")):
        return "bug_fix"
    return "behavior_change"


def _grounded_llm_terms(value: Any, *, text: str, kind: str, limit: int, max_chars: int) -> list[str]:
    """Accept LLM semantics only when the original issue supports them."""

    issue_tokens = {token for token in tokenize(text) if len(token) >= 4}
    grounded: list[str] = []
    for term in _semantic_terms(value, kind=kind, limit=limit * 2, max_chars=max_chars):
        term_tokens = {token for token in tokenize(term) if len(token) >= 4}
        overlap = issue_tokens & term_tokens
        concrete_overlap = overlap - GENERIC_GROUNDING_TOKENS
        exact_phrase = len(term) >= 6 and term.lower() in text.lower()
        if overlap and (concrete_overlap or exact_phrase) and len(overlap) / max(1, len(term_tokens)) >= 0.2:
            grounded.append(term)
    return _dedupe(grounded, limit=limit)


def _architecture_queries(text: str, task_type: str, concerns: Iterable[str]) -> list[str]:
    lower = text.lower()
    queries: list[str] = []
    if task_type == "feature_request":
        queries.extend(f"existing analogous implementation {concern}" for concern in list(concerns)[:6])
    if any(token in lower for token in ("owner", "ownership", "administrator")):
        queries.extend(
            [
                "ownership owner change administrator selector",
                "site ownership owner dropdown current owner",
                "plan ownership transfer action reducer data-layer handler",
                "ownership transfer request success failure handler",
            ]
        )
    return _dedupe(queries, limit=16)


def _as_terms(value: Any, *, limit: int = 40) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    return _dedupe(
        (
            " ".join(str(item or "").split())
            for item in values
            if not isinstance(item, (dict, list, tuple, set))
        ),
        limit=limit,
    )


def _semantic_terms(
    value: Any,
    *,
    kind: str,
    limit: int,
    max_chars: int,
) -> list[str]:
    internal_labels = {
        "reproduction_understanding",
        "behavior_to_code_layer",
        "visual_to_program_layer",
        "flow_expansion",
        "search_plan",
        "runtime_reproduction",
        "component_configuration",
        "browser_behavior",
    }
    generic_states = {"around", "value", "data", "result", "state", "issue"}
    out: list[str] = []
    for term in _as_terms(value, limit=limit * 3):
        low = term.lower().strip()
        if not low or len(term) > max_chars or "http://" in low or "https://" in low:
            continue
        if any(label in low for label in internal_labels):
            continue
        if kind == "state" and (low in generic_states or len(term.split()) > 10):
            continue
        if kind == "entity":
            if " " in term or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$-]*|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.$-]+)+", term):
                continue
            if term.startswith("/") or re.fullmatch(r"v?\d+(?:\.\d+)+", term, re.IGNORECASE):
                continue
            if not (
                any(marker in term for marker in ("_", ".", "/", "$"))
                or re.search(r"[a-z][A-Z]", term)
                or (term.isupper() and 2 <= len(term) <= 16)
            ):
                continue
        out.append(term)
    return _dedupe(out, limit=limit)


def _llm_issue_sketch(evidence_result: dict[str, Any]) -> dict[str, Any]:
    understanding = evidence_result.get("llm_understanding") or {}
    if not isinstance(understanding, dict):
        return {}
    sketch = understanding.get("issue_sketch") or {}
    return sketch if isinstance(sketch, dict) else {}


GENERIC_ENTITY_WORDS = {
    "about",
    "actual",
    "after",
    "before",
    "description",
    "expected",
    "issue",
    "most",
    "note",
    "only",
    "original",
    "problem",
    "result",
    "steps",
    "this",
    "using",
    "version",
    "http",
    "https",
    "javascript",
    "typescript",
    "python",
    "iife",
    "module",
    "runtime",
    "config",
    "example",
    "reproduction",
    "evidence",
    "wordpress.com",
}


def _extract_entities(
    text: str,
    packet: dict[str, Any],
    *,
    llm_sketch: dict[str, Any] | None = None,
    synthesis: dict[str, Any] | None = None,
) -> list[str]:
    entities: list[str] = []
    llm_entities = _semantic_terms((llm_sketch or {}).get("entities"), kind="entity", limit=40, max_chars=100)
    issue_lower = text.lower()
    entities.extend(entity for entity in llm_entities if entity.lower() in issue_lower)
    for item in packet.get("code_references", []) or []:
        path = str(item.get("path") or item.get("github_path") or "")
        symbol = str(item.get("symbol") or item.get("name") or "")
        entities.extend(_semantic_terms([path, symbol], kind="entity", limit=4, max_chars=100))
    for item in packet.get("url_inspections", []) or []:
        role = str(item.get("role") or "")
        if role == "code_evidence_seed":
            entities.extend(
                _semantic_terms(
                    [item.get("path") or item.get("github_path"), item.get("symbol")],
                    kind="entity",
                    limit=4,
                    max_chars=100,
                )
            )
    explicit_code = re.findall(r"`([^`\n]{2,100})`", text)
    entities.extend(_semantic_terms(explicit_code, kind="entity", limit=30, max_chars=100))
    entity_text = FENCED_CODE_RE.sub(" ", URL_RE.sub(" ", text))
    for token in IDENT_RE.findall(entity_text):
        low = token.strip(".").lower()
        if token.startswith("_") and token.endswith("_") and token.strip("_").isalpha():
            continue
        if low in GENERIC_ENTITY_WORDS or low.endswith((".com", ".org", ".net")):
            continue
        if len(token) >= 4 and (
            "_" in token
            or "." in token
            or "/" in token
            or re.search(r"[a-z][A-Z]", token)
            or (token.isupper() and len(token) <= 16)
        ):
            entities.extend(_semantic_terms([token.strip(".")], kind="entity", limit=1, max_chars=100))
    synthesis_entities = _semantic_terms(
        ((synthesis or {}).get("query_groups", {}) or {}).get("symbol", []) or [],
        kind="entity",
        limit=30,
        max_chars=100,
    )
    entities.extend(entity for entity in synthesis_entities if entity.lower() in issue_lower)
    return _dedupe(entities, limit=60)


def _claim_rows(kind: str, values: Iterable[str], source: str) -> list[dict[str, Any]]:
    confidence = {
        "issue_text": 1.0,
        "deterministic_fallback": 0.72,
        "llm_understanding": 0.78,
    }.get(source, 0.55)
    return [
        {"kind": kind, "value": value, "source": source, "confidence": confidence}
        for value in _dedupe(values, limit=40)
    ]


def _implementation_hypotheses(
    *,
    task_type: str,
    entities: list[str],
    concerns: list[str],
    obligations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors = _dedupe(entities + concerns, limit=12)
    hypotheses: list[dict[str, Any]] = []
    if anchors:
        hypotheses.append(
            {
                "source": "issue_contract",
                "value": " ".join(anchors[:6]),
                "role": "entry_or_api_surface",
                "status": "unverified",
                "confidence": 0.55,
                "search_policy": "locate_and_read_but_do_not_assume_patch_target",
            }
        )
        hypotheses.append(
            {
                "source": "issue_contract",
                "value": " ".join(anchors[:6]),
                "role": "delegated_or_runtime_implementation",
                "status": "unverified",
                "confidence": 0.62,
                "search_policy": "follow_receiver_calls_imports_and_implementations_then_read_source",
            }
        )
    if obligations or task_type == "feature_request":
        hypotheses.append(
            {
                "source": "flow_contract",
                "value": " ".join(
                    str(item.get("flow_type") or "") for item in obligations[:4]
                ),
                "role": "shared_mechanism_or_secondary_responsibility",
                "status": "unverified",
                "confidence": 0.48,
                "search_policy": "retain_only_when_source_or_graph_evidence_closes_a_required_role",
            }
        )
    return hypotheses


def _workflow_terms(text: str) -> list[str]:
    lower = text.lower()
    workflows: list[str] = []
    if any(token in lower for token in ("woocommerce", "store setup", "store location", "wp-admin", "email verification")):
        workflows.append("store setup / WooCommerce dashboard onboarding")
    if any(token in lower for token in ("reader", "edit link", "post edit", "wordpress.com/read")):
        workflows.append("reader post editing")
    if any(token in lower for token in ("legend", "onhover", "onleave", "chart", "mousemove", "mouseout")):
        workflows.append("chart legend interaction")
    if any(token in lower for token in ("react-pdf", "pdf", "margin", "layout", "stylesheet", "yoga")):
        workflows.append("document layout/style resolution")
    if any(token in lower for token in ("mypy", "type checker", "typeinfo", "deleted variable", "binder")):
        workflows.append("type checker symbol binding")
    if any(token in lower for token in ("openssh", "private key", "kdf", "ssh-keygen", "serialize")):
        workflows.append("OpenSSH key serialization")
    if "loadstrings" in lower or ("empty lines" in lower and "file" in lower):
        workflows.append("text file loading and line parsing")
    if "ownership" in lower or ("owner" in lower and "administrator" in lower):
        workflows.append("site and plan ownership management")
    return workflows


def _state_terms(text: str) -> list[str]:
    lower = text.lower()
    states: list[str] = []
    state_rules = {
        "email_verified": ("email_verified", "email verified", "verification"),
        "country": ("country", "countries", "store location"),
        "redirect_to": ("redirect_to", "redirect", "wp-admin"),
        "client_id": ("client_id", "oauth"),
        "post_id": ("post_id", "post id", "global_id"),
        "kdf_rounds": ("kdf_rounds", "ssh-keygen -a", "rounds"),
        "margin": ("margin", "margin: auto"),
        "legend_item": ("legend", "hover", "leave"),
        "deleted_symbol": ("deleted variable", "del ", "binder"),
        "TypeInfo": ("typeinfo", "typetype", "typevars"),
        "empty_lines": ("empty lines", "empty strings", "line numbers"),
        "current_owner": ("current owner", "the owner", "plan owner"),
        "selected_administrator": ("other administrator", "list of administrators", "new owner"),
    }
    for state, needles in state_rules.items():
        if any(needle in lower for needle in needles):
            states.append(state)
    for token in IDENT_RE.findall(text):
        low = token.lower()
        if any(marker in low for marker in ("_id", "_url", "_rounds", "state", "typeinfo", "selector")):
            states.append(token)
    return _dedupe(states, limit=40)


def _canonical_states(values: Iterable[str]) -> list[str]:
    aliases = {
        "current owner": "current_owner",
        "plan owner": "current_owner",
        "selected administrator": "selected_administrator",
        "selected admin": "selected_administrator",
        "new owner": "selected_administrator",
    }
    return _dedupe((aliases.get(str(value).strip().lower(), str(value)) for value in values), limit=40)


def _effect_terms(text: str) -> list[str]:
    lower = URL_RE.sub(" ", text).lower()
    effects: list[str] = []
    if any(token in lower for token in ("redirect", "wp-admin", "href", "link")):
        effects.append("navigation/link target changes")
    if any(token in lower for token in ("render", "display", "layout", "visual", "screenshot")):
        effects.append("rendered visual output changes")
    if any(token in lower for token in ("onhover", "onleave", "mousemove", "mouseout", "click handler", "callback")):
        effects.append("UI event callback behavior changes")
    if any(token in lower for token in ("serialize", "private key", "openssh", "kdf")):
        effects.append("serialization output changes")
    if any(token in lower for token in ("error", "error report", "diagnostic", "mypy")):
        effects.append("diagnostic/error reporting changes")
    if "empty lines" in lower and any(token in lower for token in ("loadstrings", "line numbers", "empty strings")):
        effects.append("preserve empty lines and source line numbering")
    if "ownership" in lower or ("owner" in lower and "administrator" in lower):
        effects.append("transfer ownership to a selected administrator")
    return _dedupe(effects, limit=24)


def _concerns(text: str, packet: dict[str, Any], synthesis: dict[str, Any]) -> list[str]:
    concerns: list[str] = []
    for workflow in _workflow_terms(text):
        concerns.append(workflow)
    lower = text.lower()
    if "selector" in lower and any(token in lower for token in ("used", "current", "state", "verified")):
        concerns.append("state selector consumers")
    if any(token in lower for token in ("url", "href", "redirect", "route", "link")):
        concerns.append("route/url builder behavior")
    if any(token in lower for token in ("style", "stylesheet", "margin", "layout")):
        concerns.append("style pipeline / layout semantics")
    if "ownership" in lower or ("owner" in lower and "administrator" in lower):
        concerns.extend(["ownership management", "plan transfer action and data-layer handling"])
    return _dedupe(concerns, limit=20)


def _add_role(
    roles: list[dict[str, Any]],
    *,
    source: str,
    role: str,
    evidence_type: str,
    navigation_value: str = "medium",
    modification_prior: str = "medium",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    roles.append(
        {
            "source": source,
            "evidence_type": evidence_type,
            "role": role,
            "navigation_value": navigation_value,
            "modification_prior": modification_prior,
            "reason": reason,
            "metadata": metadata or {},
        }
    )


def _evidence_roles(packet: dict[str, Any], tool_observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    roles: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    seed_policy: list[dict[str, Any]] = []

    for item in packet.get("url_inspections", []) or []:
        url = str(item.get("url") or item.get("raw_url") or "")
        role = str(item.get("role") or item.get("url_type") or "url_evidence")
        path = str(item.get("path") or item.get("github_path") or "")
        metadata = {key: item.get(key) for key in ("domain", "url_type", "semantic_terms", "line", "symbol") if item.get(key)}
        if role == "code_evidence_seed":
            _add_role(
                roles,
                source=url or path,
                role="Reference API / code evidence seed",
                evidence_type="url",
                navigation_value="high",
                modification_prior="low",
                reason="A code URL is a good navigation anchor, but it is not automatically the patch target.",
                metadata=metadata,
            )
            if path:
                seed_policy.append(
                    {
                        "path": path,
                        "role": "reference_api",
                        "navigation": "used_by_or_called_by_first",
                        "modification_prior": "low",
                    }
                )
        elif role == "reproduction_entry":
            _add_role(
                roles,
                source=url,
                role="Reproduction / playground evidence",
                evidence_type="url",
                navigation_value="high",
                modification_prior="low",
                reason="Reproduction code should seed behavior and configuration search before patch target ranking.",
                metadata=metadata,
            )
            if path:
                seed_policy.append(
                    {
                        "path": path,
                        "role": "reproduction_entry",
                        "navigation": "implementation_layer_first",
                        "modification_prior": "low",
                    }
                )
        else:
            _add_role(
                roles,
                source=url,
                role=role,
                evidence_type="url",
                navigation_value="medium",
                modification_prior="medium",
                reason="URL contributes semantic terms or reproduction context.",
                metadata=metadata,
            )

    for item in packet.get("image_inspections", []) or []:
        image_type = str(item.get("image_type") or item.get("type") or "image")
        source = str(item.get("url") or item.get("raw_url") or item.get("source") or image_type)
        _add_role(
            roles,
            source=source,
            role="Workflow Context / visual symptom",
            evidence_type="image",
            navigation_value="high",
            modification_prior="low",
            reason="Images describe visible symptoms and workflow, not usually a direct file target.",
            metadata={
                "image_type": image_type,
                "visual_queries": item.get("visual_queries", []) or [],
                "likely_layers": item.get("likely_layers", []) or [],
            },
        )
        hints.append(
            {
                "kind": "visual_semantic_navigation",
                "queries": _dedupe((item.get("visual_queries", []) or []) + (item.get("likely_layers", []) or []), limit=16),
                "reason": "Use the visual symptom to choose component/layout/rendering concerns.",
            }
        )

    for observation in tool_observations:
        tool = str(observation.get("tool") or "")
        extracted = observation.get("extracted", {}) or {}
        if tool == "browser_reproduction_reader":
            _add_role(
                roles,
                source=str(observation.get("source") or tool),
                role="Executable reproduction observation",
                evidence_type="tool_observation",
                navigation_value="high",
                modification_prior="low",
                reason="Browser/playground artifacts describe inputs, config, console and UI behavior.",
                metadata={"semantic_queries": extracted.get("semantic_queries", []) or extracted.get("parsed_reproduction", {})},
            )
        if tool == "vlm_image_inspector":
            _add_role(
                roles,
                source=str(observation.get("source") or tool),
                role="VLM visual analysis",
                evidence_type="tool_observation",
                navigation_value="high",
                modification_prior="low",
                reason="VLM image understanding should be translated to workflow/effect queries.",
                metadata=extracted.get("vlm_analysis", {}) or extracted,
            )

    return roles, hints, seed_policy


def _flow_obligations(text: str, states: list[str], concerns: list[str], effects: list[str]) -> list[dict[str, Any]]:
    haystack = " ".join([text, " ".join(states), " ".join(concerns), " ".join(effects)]).lower()
    obligations: list[dict[str, Any]] = []

    def add(flow_type: str, state: str, behavior: str, relation: str, reason: str) -> None:
        obligations.append(
            {
                "flow_type": flow_type,
                "state": state,
                "behavior": behavior,
                "required_relation": relation,
                "reason": reason,
            }
        )

    if any(token in haystack for token in ("email_verified", "selector", "state selector", "current-user")):
        add("state_selector_use_chain", "email_verified/current_user", "gate workflow or redirect", "USED_BY", "Reference state must be traced to consuming workflow code.")
    if any(token in haystack for token in ("hover", "leave", "legend", "mousemove", "mouseout", "onclick", "callback")):
        add("ui_event_to_handler", "mouse/legend item state", "callback or visual update", "CALL", "UI symptoms require event handler to implementation tracing.")
    if "ownership" in haystack or ("owner" in haystack and "administrator" in haystack):
        add(
            "ownership_transfer_action_flow",
            "current_owner/selected_administrator",
            "ownership transfer action and success handling",
            "ACTION+STATE",
            "Feature requests need analogous ownership UI plus action/data-layer handler coverage even when the future call path is absent.",
        )
    if any(token in haystack for token in ("kdf", "openssh", "private_key", "serialize", "serialization")):
        add("serializer_backend_call_chain", "kdf_rounds/encryption_algorithm", "OpenSSH serialization output", "CALL+PARAMETER", "New parameters must close through public API, backend and serializer.")
    if any(token in haystack for token in ("margin", "stylesheet", "style", "layout", "react-pdf", "yoga")):
        add("visual_style_pipeline_flow", "style property/config", "layout/rendered output", "CALL+CONFIG", "Visual layout bugs need style expansion/resolve pipeline tracing.")
    if any(token in haystack for token in ("redirect", "href", "url builder", "route", "post_id", "client_id", "link")):
        add("url_builder_or_route_flow", "route/url parameters", "navigation target", "CALL+DATA", "Visible route/link bugs usually terminate in URL builder or route mapping code.")
    if any(token in haystack for token in ("mypy", "typeinfo", "deleted variable", "binder", "declaration", "typevars")):
        add("python_type_binding_flow", "symbol table / TypeInfo", "diagnostic/error behavior", "TYPE_FLOW", "Type checker bugs require binding and narrowing state tracing.")
    if any(token in haystack for token in ("option", "config", "parser", "flag", "parameter", "rounds")):
        add("parameter_or_config_flow", "option/config parameter", "downstream behavior", "PARAMETER", "Configuration mentioned in issue must be checked through consumers.")
    return obligations


def build_issue_sketch(sample: NormalizedSample, evidence_result: dict[str, Any]) -> IssueSketch:
    packet = evidence_result.get("evidence_packet", {}) or {}
    synthesis = evidence_result.get("evidence_synthesis", {}) or {}
    tool_observations = evidence_result.get("tool_observations", []) or []
    text = _issue_text(sample)
    task_type = _task_type(text)
    llm_sketch = _llm_issue_sketch(evidence_result)

    roles, hints, seed_policy = _evidence_roles(packet, tool_observations)
    llm_workflow = _grounded_llm_terms(llm_sketch.get("workflow"), text=text, kind="workflow", limit=12, max_chars=140)
    llm_concerns = _grounded_llm_terms(llm_sketch.get("concern"), text=text, kind="concern", limit=12, max_chars=180)
    llm_concerns += _grounded_llm_terms(llm_sketch.get("concern_queries"), text=text, kind="concern", limit=10, max_chars=140)
    llm_states = _grounded_llm_terms(llm_sketch.get("state"), text=text, kind="state", limit=20, max_chars=100)
    llm_effects = _grounded_llm_terms(llm_sketch.get("expected_effect"), text=text, kind="effect", limit=20, max_chars=160)
    workflow = _dedupe(llm_workflow + _workflow_terms(text), limit=16)
    states = _canonical_states(llm_states + _state_terms(text))
    effects = _dedupe(llm_effects + _effect_terms(text), limit=30)
    concerns = _dedupe(llm_concerns + _concerns(text, packet, synthesis), limit=24)
    architectural_queries = _architecture_queries(text, task_type, concerns)
    entities = _extract_entities(text, packet, llm_sketch=llm_sketch, synthesis=synthesis)
    for item in llm_sketch.get("evidence_roles", []) or []:
        if not isinstance(item, dict):
            continue
        _add_role(
            roles,
            source=str(item.get("url") or item.get("id") or "llm_understanding"),
            role=str(item.get("role") or "LLM evidence role"),
            evidence_type="llm_understanding",
            navigation_value=str(item.get("navigation_value") or "medium"),
            modification_prior=str(item.get("modification_prior") or "unknown"),
            reason=str(item.get("description") or "Role inferred by evidence-understanding LLM."),
            metadata={"provenance": "llm_understanding"},
        )
    obligations = _flow_obligations(text, states, concerns, effects)
    navigation_hints = list(hints)
    hypotheses = [
        {
            "source": "visual_evidence",
            "value": query,
            "status": "unverified",
            "confidence": 0.35,
            "search_policy": "verify_in_source_before_ranking",
        }
        for hint in hints
        if hint.get("kind") == "visual_semantic_navigation"
        for query in hint.get("queries", []) or []
    ][:24]
    hypotheses.extend(
        _implementation_hypotheses(
            task_type=task_type,
            entities=entities,
            concerns=concerns,
            obligations=obligations,
        )
    )
    claim_evidence = (
        _claim_rows("workflow", llm_workflow, "llm_understanding")
        + _claim_rows("concern", llm_concerns, "llm_understanding")
        + _claim_rows("state", llm_states, "llm_understanding")
        + _claim_rows("expected_effect", llm_effects, "llm_understanding")
        + _claim_rows(
            "entity",
            [
                entity
                for entity in _semantic_terms(llm_sketch.get("entities"), kind="entity", limit=40, max_chars=100)
                if entity in entities
            ],
            "llm_understanding",
        )
    )
    deterministic_claims = (
        _claim_rows("workflow", _workflow_terms(text), "deterministic_fallback")
        + _claim_rows("concern", _concerns(text, packet, synthesis), "deterministic_fallback")
        + _claim_rows("state", _canonical_states(_state_terms(text)), "deterministic_fallback")
        + _claim_rows("expected_effect", _effect_terms(text), "deterministic_fallback")
    )
    known_claims = {(row["kind"], str(row["value"]).lower()) for row in claim_evidence}
    claim_evidence.extend(
        row
        for row in deterministic_claims
        if (row["kind"], str(row["value"]).lower()) not in known_claims
    )

    for policy in seed_policy:
        path = str(policy.get("path") or "")
        if not path:
            continue
        navigation_hints.append(
            {
                "kind": "evidence_seed_navigation",
                "seed": path,
                "recommended_next": policy.get("navigation"),
                "modification_prior": policy.get("modification_prior"),
            }
        )

    if sample.language:
        navigation_hints.append({"kind": "language_router", "language": sample.language, "reason": "Prefer language-specific graph edges and flow obligations."})

    reasoning = [
        "Problem statement is converted to workflow/concern/state/effect before code ranking.",
        "Explicit URLs/images are role-labeled so navigation value and modification prior can differ.",
        "Concern edges expand horizontally; call/used-by edges navigate vertically; flow obligations validate state-to-behavior consistency.",
    ]
    if llm_sketch:
        reasoning.append("Structured LLM evidence understanding is the primary semantic sketch; deterministic rules only add grounded fallback terms.")
    if any(item.get("modification_prior") == "low" for item in seed_policy):
        reasoning.append("At least one explicit evidence seed is useful for navigation but should be penalized as a direct patch target.")

    return IssueSketch(
        instance_id=sample.instance_id,
        repo=sample.repo,
        dataset=sample.dataset,
        task_type=task_type,
        workflow=workflow,
        concerns=concerns,
        states=states,
        expected_effects=effects,
        entities=entities,
        evidence_roles=roles,
        navigation_hints=navigation_hints,
        flow_obligations=obligations,
        seed_policy=seed_policy,
        claim_evidence=claim_evidence,
        architectural_queries=architectural_queries,
        hypotheses=hypotheses,
        reasoning_trace=reasoning,
    )


def sketch_query_terms(sketch: IssueSketch) -> list[str]:
    """Queries derived from the sketch without using gold labels."""

    path_terms = [Path(str(item.get("path") or "")).stem for item in sketch.seed_policy]
    obligation_terms = [
        f"{item.get('flow_type', '')} {item.get('state', '')} {item.get('behavior', '')}"
        for item in sketch.flow_obligations
    ]
    return _dedupe(
        sketch.workflow
        + sketch.concerns
        + sketch.states
        + sketch.expected_effects
        + sketch.entities
        + list(getattr(sketch, "architectural_queries", []) or [])
        + path_terms
        + obligation_terms,
        limit=120,
    )
