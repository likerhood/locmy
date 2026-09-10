from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
CALL_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
IMPORT_STRING_RE = re.compile(r"(?:import\s+(?:[^'\"]+\s+from\s+)?|require\()\s*['\"]([^'\"]+)['\"]")
FLOW_HINTS = {
    "round",
    "rounds",
    "kdf",
    "kdf_rounds",
    "serialize",
    "serialization",
    "serializer",
    "backend",
    "private_key",
    "redirect",
    "redirect_to",
    "client_id",
    "post_id",
    "site",
    "url",
    "href",
    "selector",
    "state",
    "dispatch",
    "reducer",
    "hover",
    "leave",
    "legend",
    "email",
    "verified",
    "verification",
    "current",
    "current_user",
    "user",
    "parser",
    "config",
    "option",
    "options",
    "margin",
    "layout",
    "style",
    "stylesheet",
    "resolve",
    "expand",
    "binder",
    "declaration",
    "typeinfo",
    "typevars",
}


def _looks_flow_token(token: str) -> bool:
    if len(token) < 3:
        return False
    low = token.lower()
    if low in {"function", "return", "const", "import", "export", "class", "from", "this", "that"}:
        return False
    if low in FLOW_HINTS:
        return True
    if "_" in token:
        return True
    if re.search(r"[a-z][A-Z]", token):
        return True
    return any(hint in low for hint in FLOW_HINTS)


def _extract_candidate_terms(
    *,
    issue_text: str,
    tool_observations: Iterable[Dict[str, Any]] | None,
    queries: Iterable[str] | None,
) -> list[str]:
    chunks = [issue_text or "", " ".join(queries or [])]
    for observation in tool_observations or []:
        extracted = observation.get("extracted", {}) or {}
        chunks.append(str(extracted.get("parsed_reproduction", "")))
        chunks.append(str(extracted.get("code_features", "")))
        chunks.append(str(extracted.get("semantic_queries", "")))
        chunks.append(str(extracted.get("vlm_analysis", "")))
    terms: list[str] = []
    seen: set[str] = set()
    for token in IDENT_RE.findall("\n".join(chunks)):
        if not _looks_flow_token(token):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(token)
    return terms[:48]


def _entity_signature(entity: CodeEntity) -> str:
    first = entity.text.strip().splitlines()
    return first[0].strip() if first else entity.name


def _match_entities(index: RepositoryIndex, term: str, limit: int) -> list[dict[str, Any]]:
    low = term.lower()
    matched: list[dict[str, Any]] = []
    for entity in index.entities:
        signature = _entity_signature(entity)
        haystack = f"{entity.name}\n{signature}\n{entity.text}".lower()
        if low not in haystack:
            continue
        scope = "signature" if low in signature.lower() else "body"
        if low in entity.name.lower():
            scope = "name"
        matched.append(
            {
                "path": entity.path,
                "kind": entity.kind,
                "name": entity.name,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "match_scope": scope,
                "signature": signature,
                "snippet": entity.text[:900],
            }
        )
        if len(matched) >= limit:
            break
    return matched


def _match_files(index: RepositoryIndex, term: str, existing_paths: set[str], limit: int) -> list[dict[str, Any]]:
    low = term.lower()
    matched: list[dict[str, Any]] = []
    for path, text in index.files.items():
        if path in existing_paths:
            continue
        lower_text = text.lower()
        if low not in lower_text and low not in path.lower():
            continue
        line_no = 1
        line = ""
        for idx, raw_line in enumerate(text.splitlines(), start=1):
            if low in raw_line.lower():
                line_no = idx
                line = raw_line.strip()
                break
        matched.append(
            {
                "path": path,
                "kind": "file",
                "name": "",
                "start_line": line_no,
                "end_line": line_no,
                "match_scope": "file_text",
                "signature": "",
                "snippet": line[:600],
            }
        )
        if len(matched) >= limit:
            break
    return matched


def _basename_stems(path: str) -> set[str]:
    clean = path.replace("\\", "/")
    stem = Path(clean).stem.lower()
    parent = Path(clean).parent.name.lower()
    return {item for item in {stem, parent, clean.lower()} if item}


def _build_edges(index: RepositoryIndex, locations: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
    path_entities: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in index.entities:
        path_entities[entity.path].append(entity)

    edges: list[dict[str, Any]] = []
    loc_paths = list(dict.fromkeys(str(item.get("path") or "") for item in locations if item.get("path")))
    term_low = term.lower()
    for source in loc_paths:
        source_text = index.files.get(source, "")
        source_low = source_text.lower()
        calls = set(CALL_RE.findall(source_text))
        imports = IMPORT_STRING_RE.findall(source_text)
        for target in loc_paths:
            if source == target:
                continue
            target_entities = path_entities.get(target, [])
            called = sorted({entity.name for entity in target_entities if entity.name in calls})
            if called:
                relation = "calls_symbol_defined_in_target"
                if term_low in source_low:
                    relation = "passes_term_to_called_symbol_or_neighbor"
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": relation,
                        "term": term,
                        "symbols": called[:12],
                    }
                )
                continue
            target_stems = _basename_stems(target)
            import_hits = [
                value
                for value in imports
                if any(stem and (stem in value.lower() or value.lower().endswith(stem)) for stem in target_stems)
            ]
            if import_hits:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": "imports_or_reexports_target_module",
                        "term": term,
                        "symbols": import_hits[:8],
                    }
                )
    return edges[:60]


def _classify_closure(term: str, locations: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    text = " ".join(
        [
            term,
            " ".join(str(item.get("path") or "") for item in locations),
            " ".join(str(item.get("signature") or "") for item in locations),
            " ".join(str(edge.get("relation") or "") for edge in edges),
        ]
    ).lower()
    if any(token in text for token in ("kdf", "serialize", "serializer", "backend", "private_key", "ssh")):
        return "serializer_backend_call_chain"
    from mycode.flow_analysis.language_context import python_binding_context

    if python_binding_context(text, paths=[item.get("path", "") for item in locations]):
        return "python_type_binding_flow"
    if any(token in text for token in ("style", "stylesheet", "resolve", "expand", "margin", "layout", "yoga")):
        return "visual_style_pipeline_flow"
    if any(token in text for token in ("selector", "state", "dispatch", "reducer", "store")):
        return "state_selector_use_chain"
    if any(token in text for token in ("hover", "leave", "legend", "event", "handler", "mousemove")):
        return "ui_event_to_handler"
    if any(token in text for token in ("url", "href", "redirect", "route", "post_id", "client_id")):
        return "url_builder_or_route_flow"
    return "parameter_or_config_flow"


def _location_role(location: dict[str, Any]) -> str:
    path = str(location.get("path") or "").replace("\\", "/").lower()
    name = str(location.get("name") or "").lower()
    signature = str(location.get("signature") or "").lower()
    text = f"{path} {name} {signature}"

    if any(part in path for part in ("/examples/", "/example/", "/demo/", "/demos/", "/docs/", "/playground/", "/sandbox/")):
        return "reproduction_or_example"
    if any(part in path for part in ("/test/", "/tests/", "/fixture/", "/fixtures/", "__tests__")):
        return "test_or_fixture"
    if any(token in text for token in ("binder", "declaration", "typeinfo", "typevars", "deleted", "narrow")):
        return "type_binder"
    if "backend" in text:
        return "backend"
    if any(token in text for token in ("serialize", "serializer", "serialization", "private_key", "openssh", "/ssh.")):
        return "serializer"
    if any(token in text for token in ("getediturl", "url", "href", "redirect", "route", "link")):
        return "url_builder"
    if any(token in text for token in ("selector", "select", "currentuser", "current-user", "emailverified", "email_verified")):
        return "state_selector"
    if any(token in text for token in ("reducer", "dispatch", "action", "store", "state/")):
        return "state_update"
    if any(token in text for token in ("component", "view", "page", "screen", "dashboard", "reader", "signup", "woocommerce", "render")):
        return "component_or_route"
    if any(token in text for token in ("hover", "leave", "click", "mousemove", "mouseout", "handler", "handleevent", "onhover", "onleave")):
        return "event_handler"
    if any(token in text for token in ("stylesheet", "style", "resolve", "expand", "margin", "layout", "yoga", "css", "scss")):
        return "style_resolver"
    if any(token in path for token in ("/api/", "/public/", "__init__.py")) or location.get("kind") == "class":
        return "public_api"
    return "implementation"


ROLE_PRIORITY = {
    "reproduction_or_example": 0,
    "test_or_fixture": 1,
    "public_api": 2,
    "component_or_route": 3,
    "state_selector": 4,
    "state_update": 5,
    "event_handler": 6,
    "url_builder": 7,
    "type_binder": 8,
    "backend": 9,
    "serializer": 10,
    "style_resolver": 11,
    "implementation": 12,
}


def _flow_obligation(flow_type: str) -> list[str]:
    if flow_type == "serializer_backend_call_chain":
        return ["public_api", "backend", "serializer"]
    if flow_type == "python_type_binding_flow":
        return ["type_binder", "implementation"]
    if flow_type == "visual_style_pipeline_flow":
        return ["style_resolver", "implementation"]
    if flow_type == "state_selector_use_chain":
        return ["state_selector", "component_or_route", "state_update"]
    if flow_type == "ui_event_to_handler":
        return ["component_or_route", "event_handler", "implementation"]
    if flow_type == "url_builder_or_route_flow":
        return ["component_or_route", "url_builder"]
    return ["public_api", "implementation"]


def _role_coverage(locations: list[dict[str, Any]], required_roles: list[str]) -> dict[str, Any]:
    roles = {str(item.get("role") or _location_role(item)) for item in locations}
    covered = [role for role in required_roles if role in roles]
    missing = [role for role in required_roles if role not in roles]
    return {
        "required_roles": required_roles,
        "covered_roles": covered,
        "missing_roles": missing,
        "coverage": round(len(covered) / max(1, len(required_roles)), 3),
    }


def _ordered_locations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for location in locations:
        item = dict(location)
        item["role"] = str(item.get("role") or _location_role(item))
        enriched.append(item)
    return sorted(enriched, key=lambda item: (ROLE_PRIORITY.get(str(item.get("role")), 99), str(item.get("path") or "")))


def _target_paths_from_roles(locations: list[dict[str, Any]]) -> list[str]:
    blocked = {"reproduction_or_example", "test_or_fixture"}
    paths: list[str] = []
    seen: set[str] = set()
    for item in locations:
        path = str(item.get("path") or "")
        role = str(item.get("role") or "")
        if not path or role in blocked:
            continue
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths[:16]


def trace_parameter_closures(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[Dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
    locations_per_term: int = 16,
) -> list[dict[str, Any]]:
    """Trace likely cross-function propagation of issue parameters/config tokens.

    This is a pragmatic repo-local approximation of dataflow: it finds important
    parameters or config names from issue/tool evidence, maps them to functions,
    then links functions/files through calls and import/re-export edges. It is
    intentionally explainable so the dynamic agent can decide what to inspect next.
    """

    closures: list[dict[str, Any]] = []
    for term in _extract_candidate_terms(issue_text=issue_text, tool_observations=tool_observations, queries=queries):
        entity_locations = _match_entities(index, term, limit=locations_per_term)
        existing_paths = {str(item.get("path") or "") for item in entity_locations}
        file_locations = _match_files(index, term, existing_paths, limit=max(2, locations_per_term // 3))
        locations = (entity_locations + file_locations)[:locations_per_term]
        if len(locations) < 2:
            continue
        edges = _build_edges(index, locations, term)
        edge_summary: dict[str, int] = defaultdict(int)
        for edge in edges:
            edge_summary[str(edge.get("relation") or "unknown")] += 1
        signature_hits = sum(1 for item in locations if item.get("match_scope") in {"signature", "name"})
        flow_type = _classify_closure(term, locations, edges)
        locations = _ordered_locations(locations)
        obligation = _flow_obligation(flow_type)
        coverage = _role_coverage(locations, obligation)
        target_paths = _target_paths_from_roles(locations)
        evidence_seed_paths = [
            str(item.get("path") or "")
            for item in locations
            if item.get("path") and item.get("role") in {"reproduction_or_example", "test_or_fixture"}
        ][:12]
        confidence = min(
            0.98,
            0.28
            + 0.08 * len(locations)
            + 0.08 * len(edges)
            + 0.08 * signature_hits
            + 0.10 * float(coverage["coverage"]),
        )
        closures.append(
            {
                "term": term,
                "flow_type": flow_type,
                "closure_kind": "cross_function_parameter_or_config_closure",
                "backend": "regex_static_parameter_closure",
                "precision_level": "best_effort_static_navigation_not_codeql",
                "confidence": round(confidence, 3),
                "flow_obligation": obligation,
                "role_coverage": coverage,
                "candidate_target_paths": target_paths,
                "evidence_seed_paths": evidence_seed_paths,
                "locations": locations,
                "edges": edges,
                "edge_summary": dict(sorted(edge_summary.items())),
                "missing_precision_backends": [
                    "language_server_reference_index",
                    "codeql_or_ast_dataflow",
                    "runtime_trace",
                ],
                "reason": (
                    f"Token `{term}` appears across {len(locations)} entities/files"
                    f" with {len(edges)} call/import propagation hints; "
                    f"role coverage={coverage['coverage']} for {flow_type}."
                ),
            }
        )

    closures.sort(
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            str(item.get("flow_type") or ""),
            str(item.get("term") or ""),
        )
    )
    return closures[:limit]
