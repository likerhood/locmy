from __future__ import annotations

import ast
import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from mycode.flow_analysis.query_flows import extract_flow_terms
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
CALL_RE = re.compile(
    r"\b(?P<callee>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?)\s*\((?P<args>[^)]{0,800})\)"
)
GENERIC_CALL_SYMBOLS = {
    "add", "append", "apply", "build", "call", "close", "constructor", "create", "delete",
    "dispatch", "draw", "emit", "filter", "find", "get", "handle", "init", "load", "map",
    "open", "parse", "process", "push", "read", "remove", "render", "resolve", "run", "save",
    "set", "setup", "sort", "start", "stop", "update", "validate", "write",
}
PATH_NOISE_TOKENS = {"app", "client", "components", "index", "lib", "packages", "src", "state", "test", "tests"}


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _dedupe(values: Iterable[str], *, limit: int = 120) -> list[str]:
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


def _normal_symbol(value: str) -> str:
    value = str(value or "").strip()
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value.strip("_$").lower()


def _entity_id(entity: CodeEntity) -> str:
    return f"{entity.path}::{entity.kind}:{entity.name}@{entity.start_line}-{entity.end_line}"


def _entity_role(entity: CodeEntity) -> str:
    text = f"{entity.path}\n{entity.name}\n{entity.text}".lower()
    if any(token in text for token in ("example", "sandbox", "playground", "demo")):
        return "reproduction_or_example"
    if any(token in text for token in ("test", "__tests__", "fixture", "spec")):
        return "test_or_fixture"
    if any(token in text for token in ("serialize", "serialization", "private_key", "openssh", "ssh")):
        return "serializer"
    if "backend" in text:
        return "backend"
    if any(token in text for token in ("selector", "email_verified", "dispatch", "reducer", "/state/")):
        return "state_or_selector"
    if any(token in text for token in ("hover", "leave", "onclick", "onmouse", "handler", "legend")):
        return "event_handler"
    if any(token in text for token in ("binder", "typeinfo", "typetype", "declaration", "deleted")):
        return "type_binder"
    if any(token in text for token in ("style", "stylesheet", "resolve", "expand", "margin", "layout")):
        return "style_or_layout_pipeline"
    if any(token in text for token in ("url", "href", "route", "redirect", "post", "site")):
        return "url_builder_or_route"
    return "implementation"


def _terms(
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None,
    queries: Iterable[str] | None,
) -> list[str]:
    seed_parts = [issue_text or "", " ".join(queries or [])]
    for observation in tool_observations or []:
        extracted = observation.get("extracted", {}) or {}
        seed_parts.append(str(extracted.get("parsed_reproduction", "")))
        seed_parts.append(str(extracted.get("source_files", "")))
        seed_parts.append(str(extracted.get("vlm_analysis", "")))
        seed_parts.append(str(extracted.get("runtime_trace", "")))
    raw = "\n".join(seed_parts)
    terms: list[str] = list(extract_flow_terms(issue_text, tool_observations))
    stop = {
        "http",
        "https",
        "github",
        "com",
        "src",
        "test",
        "tests",
        "return",
        "const",
        "function",
        "class",
        "import",
        "export",
        "from",
        "with",
        "this",
        "that",
        "true",
        "false",
        "none",
        "null",
        "url",
        "javascript",
        "typescript",
        "python",
        "java",
        "original",
        "issue",
    }
    semantic_hints = (
        "round",
        "serialize",
        "backend",
        "selector",
        "state",
        "dispatch",
        "hover",
        "leave",
        "legend",
        "redirect",
        "client",
        "binder",
        "type",
        "margin",
        "style",
        "config",
        "option",
        "url",
        "handler",
        "email",
        "country",
        "plugin",
        "private",
        "openssh",
    )
    for token in IDENT_RE.findall(raw):
        low = token.lower().strip("_$")
        if len(low) < 3 or low in stop:
            continue
        if "_" in token or re.search(r"[a-z][A-Z]", token) or any(hint in low for hint in semantic_hints):
            terms.append(token)
    return _dedupe(terms, limit=42)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _python_calls(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return []
    calls: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if not name:
            continue
        arg_texts: list[str] = []
        for arg in list(node.args) + [kw.value for kw in node.keywords if kw.value is not None]:
            try:
                arg_texts.append(ast.unparse(arg))
            except Exception:
                arg_texts.append("")
        calls.append(
            {
                "callee": name,
                "callee_symbol": _normal_symbol(name),
                "args": ", ".join(item for item in arg_texts if item),
                "line": int(getattr(node, "lineno", 0) or 0),
                "backend": "python_ast_call",
            }
        )
    return calls


def _regex_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    ignored = {"if", "for", "while", "switch", "return", "function", "catch", "class", "new"}
    for match in CALL_RE.finditer(text):
        callee = match.group("callee")
        low = callee.lower().rsplit(".", 1)[-1]
        if low in ignored:
            continue
        line = text.count("\n", 0, match.start()) + 1
        calls.append(
            {
                "callee": callee,
                "callee_symbol": _normal_symbol(callee),
                "args": match.group("args") or "",
                "line": line,
                "backend": "regex_call",
            }
        )
    return calls


def _entity_calls(entity: CodeEntity) -> list[dict[str, Any]]:
    ext = Path(entity.path).suffix.lower()
    text = entity.text[:30000]
    if ext in {".py", ".pyi"}:
        calls = _python_calls(text)
        if calls:
            return calls
    return _regex_calls(text)


def _symbol_targets(index: RepositoryIndex) -> dict[str, list[CodeEntity]]:
    targets: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in index.entities:
        if entity.kind in {"function", "method", "class", "module"}:
            targets[_normal_symbol(entity.name)].append(entity)
    return targets


def _resolved_targets_for_call(
    targets: dict[str, list[CodeEntity]],
    callee_symbol: str,
    source: CodeEntity,
    *,
    limit: int = 4,
) -> list[CodeEntity]:
    """Resolve only bounded, path-plausible callees for lightweight flow."""

    if not callee_symbol or callee_symbol in GENERIC_CALL_SYMBOLS:
        return []
    matches = [
        target
        for target in targets.get(callee_symbol, [])
        if not (target.path == source.path and target.name == source.name)
    ]
    if len(matches) <= 1:
        return matches
    source_tokens = {
        token for token in re.split(r"[/_.-]+", source.path.lower())
        if len(token) >= 3 and token not in PATH_NOISE_TOKENS
    }
    local = [target for target in matches if Path(target.path).parent == Path(source.path).parent]
    related = [
        target
        for target in matches
        if source_tokens.intersection(
            token for token in re.split(r"[/_.-]+", target.path.lower())
            if len(token) >= 3 and token not in PATH_NOISE_TOKENS
        )
    ]
    narrowed = local or related
    return narrowed[:limit] if 0 < len(narrowed) <= limit else []


def _term_matches_entity(entity: CodeEntity, term: str) -> bool:
    low = str(term or "").lower()
    return bool(low) and low in f"{entity.path}\n{entity.name}\n{entity.text}".lower()


def _entity_context_relevance(entity: CodeEntity, term: str, issue_text: str) -> float:
    haystack = f"{entity.path}\n{entity.name}\n{entity.text[:4000]}".lower()
    tokens = {token for token in tokenize(f"{term} {issue_text}") if len(token) >= 3}
    if not tokens:
        return 0.0
    matched = {token for token in tokens if token in haystack}
    return min(1.0, len(matched) / max(3, min(len(tokens), 12)))


def _resolved_call_edges(
    entities: list[CodeEntity],
    calls_by_entity: dict[int, list[dict[str, Any]]],
    targets: dict[str, list[CodeEntity]],
    *,
    max_entities: int = 1800,
    max_edges: int = 6000,
) -> list[tuple[CodeEntity, CodeEntity, dict[str, Any]]]:
    edges: list[tuple[CodeEntity, CodeEntity, dict[str, Any]]] = []
    for source in entities[:max_entities]:
        for call in calls_by_entity.get(id(source), []):
            callee_symbol = str(call.get("callee_symbol") or "")
            if not callee_symbol:
                continue
            for target in _resolved_targets_for_call(targets, callee_symbol, source):
                if source.path == target.path and source.name == target.name:
                    continue
                edges.append((source, target, call))
                if len(edges) >= max_edges:
                    return edges
    return edges


def _classify_flow(term: str, issue_text: str) -> str:
    text = f"{term}\n{issue_text}".lower()
    if any(token in text for token in ("kdf", "serialize", "backend", "openssh", "private key")):
        return "interprocedural_parameter_flow"
    if any(token in text for token in ("email_verified", "selector", "state", "dispatch", "country")):
        return "interprocedural_state_flow"
    if any(token in text for token in ("hover", "leave", "legend", "handler", "click", "mousemove")):
        return "interprocedural_ui_event_flow"
    if any(token in text for token in ("url", "href", "redirect", "route", "post_id", "client_id")):
        return "interprocedural_url_or_route_flow"
    if any(token in text for token in ("binder", "typeinfo", "typetype", "declaration", "deleted")):
        return "interprocedural_type_binding_flow"
    return "interprocedural_symbol_flow"


def _edge(
    *,
    source: CodeEntity,
    target: CodeEntity,
    relation: str,
    term: str,
    call: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    return {
        "source": source.path,
        "target": target.path,
        "source_path": source.path,
        "target_path": target.path,
        "source_entity": _entity_id(source),
        "target_entity": _entity_id(target),
        "source_name": source.name,
        "target_name": target.name,
        "source_role": _entity_role(source),
        "target_role": _entity_role(target),
        "relation": relation,
        "term": term,
        "line": call.get("line", 0),
        "call": call.get("callee"),
        "call_args": call.get("args"),
        "call_backend": call.get("backend"),
        "confidence": confidence,
    }


def trace_interprocedural_flows(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    candidate_paths: Iterable[str] | None = None,
    max_hops: int = 2,
    max_terms: int = 16,
    max_source_entities: int = 180,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Trace lightweight cross-function parameter/state propagation.

    This is ARISE-inspired, not ARISE-equivalent. It builds a best-effort
    interprocedural closure from repository entities, Python AST calls, and
    conservative lexical calls for JS/TS/Java. It is designed to give the agent
    auditable navigation and verification evidence while leaving room for
    tree-sitter, LSP/SCIP, and CodeQL backends.
    """

    terms = _terms(issue_text, tool_observations, queries)[:max_terms]
    if not terms:
        return []

    callable_entities = [entity for entity in index.entities if entity.kind in {"class", "function", "method"}]
    callable_paths = {_norm(entity.path) for entity in callable_entities}
    module_fallbacks = [
        entity
        for entity in index.entities
        if entity.kind == "module" and _norm(entity.path) not in callable_paths
    ]
    entities = callable_entities + module_fallbacks
    entity_ids = {id(entity): _entity_id(entity) for entity in entities}
    entity_haystacks = {
        id(entity): f"{entity.path}\n{entity.name}\n{entity.text[:12000]}".lower()
        for entity in entities
    }
    targets: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in entities:
        targets[_normal_symbol(entity.name)].append(entity)
    beam_paths = {_norm(path) for path in (candidate_paths or []) if _norm(path)}
    beam_entities = [entity for entity in entities if _norm(entity.path) in beam_paths]

    # Interprocedural analysis is deliberately local. Candidate entities come
    # first, followed by entities that mention a flow term. This preserves the
    # useful cross-file closure without parsing every large generated/module
    # entity in the scoped repository for every term.
    active_entities: list[CodeEntity] = []
    active_seen: set[str] = set()

    def add_active(entity: CodeEntity) -> None:
        key = entity_ids[id(entity)]
        if key in active_seen or len(active_entities) >= max_source_entities:
            return
        active_seen.add(key)
        active_entities.append(entity)

    for entity in beam_entities:
        add_active(entity)
    for term in terms:
        low = term.lower()
        for entity in entities:
            if low in entity_haystacks[id(entity)]:
                add_active(entity)
            if len(active_entities) >= max_source_entities:
                break
        if len(active_entities) >= max_source_entities:
            break

    calls_by_entity = {id(entity): _entity_calls(entity) for entity in active_entities}
    resolved_edges = _resolved_call_edges(
        active_entities,
        calls_by_entity,
        targets,
        max_entities=max_source_entities,
        max_edges=max(1000, max_source_entities * 20),
    )
    adjacency: dict[str, list[tuple[CodeEntity, CodeEntity, dict[str, Any]]]] = defaultdict(list)
    for source, target, call in resolved_edges:
        adjacency[entity_ids[id(source)]].append((source, target, call))
        adjacency[entity_ids[id(target)]].append((source, target, call))
    traces: list[dict[str, Any]] = []

    for term in terms:
        term_low = term.lower()
        relevance_tokens = {
            token
            for token in tokenize(f"{term} {issue_text}")
            if len(token) >= 3
        }
        relevance_cache: dict[int, float] = {}

        def relevance(entity: CodeEntity) -> float:
            key = id(entity)
            if key in relevance_cache:
                return relevance_cache[key]
            if not relevance_tokens:
                score = 0.0
            else:
                haystack = entity_haystacks[key]
                matched = sum(1 for token in relevance_tokens if token in haystack)
                score = min(1.0, matched / max(3, min(len(relevance_tokens), 12)))
            relevance_cache[key] = score
            return score
        term_entities = [
            entity
            for entity in active_entities
            if term_low in entity_haystacks[id(entity)]
        ]
        if not term_entities:
            continue

        candidate_paths: list[str] = [entity.path for entity in term_entities]
        locations: list[dict[str, Any]] = [
            {
                "path": entity.path,
                "kind": entity.kind,
                "name": entity.name,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "role": _entity_role(entity),
                "reason": f"term `{term}` appears in entity text/name/path",
            }
            for entity in term_entities[: limit * 5]
        ]
        edges: list[dict[str, Any]] = []
        local_closure_edges: list[dict[str, Any]] = []

        max_term_edges = max(40, limit * 24)
        for source in active_entities:
            for call in calls_by_entity.get(id(source), []):
                callee_symbol = str(call.get("callee_symbol") or "")
                arg_text = str(call.get("args") or "")
                call_text = f"{call.get('callee', '')} {arg_text}".lower()
                matched_targets = _resolved_targets_for_call(targets, callee_symbol, source)
                target_has_term = any(
                    term_low in entity_haystacks[id(target)]
                    for target in matched_targets
                )

                if matched_targets and (term_low in call_text or target_has_term):
                    for target in matched_targets:
                        relation = "passes_term_to_callee" if term_low in arg_text.lower() else "calls_term_related_entity"
                        confidence = 0.76 if term_low in arg_text.lower() else 0.64
                        candidate_paths.extend([source.path, target.path])
                        edges.append(
                            _edge(
                                source=source,
                                target=target,
                                relation=relation,
                                term=term,
                                call=call,
                                confidence=confidence,
                            )
                        )
                        if len(edges) >= max_term_edges:
                            break

                if not matched_targets and term_low in call_text:
                    candidate_paths.append(source.path)
                    edges.append(
                        _edge(
                            source=source,
                            target=source,
                            relation="term_argument_without_resolved_callee",
                            term=term,
                            call=call,
                            confidence=0.52,
                        )
                    )
                if len(edges) >= max_term_edges:
                    break
            if len(edges) >= max_term_edges:
                break

        focus_entity_objects: list[CodeEntity] = []
        seen_focus: set[str] = set()
        for entity in term_entities + [item for item in beam_entities if id(item) in calls_by_entity]:
            key = entity_ids[id(entity)]
            if key in seen_focus:
                continue
            if entity in term_entities or term_low in entity_haystacks[id(entity)] or relevance(entity) > 0.18:
                seen_focus.add(key)
                focus_entity_objects.append(entity)
        frontier = {entity_ids[id(entity)] for entity in focus_entity_objects[:48]}
        seen_frontier = set(frontier)
        for hop in range(max(1, min(max_hops, 4))):
            next_frontier: set[str] = set()
            frontier_edges: list[tuple[CodeEntity, CodeEntity, dict[str, Any]]] = []
            edge_seen: set[tuple[str, str, str, int]] = set()
            for node_id in frontier:
                for source, target, call in adjacency.get(node_id, []):
                    edge_key = (
                        entity_ids[id(source)],
                        entity_ids[id(target)],
                        str(call.get("callee") or ""),
                        int(call.get("line") or 0),
                    )
                    if edge_key in edge_seen:
                        continue
                    edge_seen.add(edge_key)
                    frontier_edges.append((source, target, call))
            for source, target, call in frontier_edges:
                sid = entity_ids[id(source)]
                tid = entity_ids[id(target)]
                arg_text = str(call.get("args") or "")
                source_rel = relevance(source)
                target_rel = relevance(target)
                if term_low not in f"{call.get('callee', '')} {arg_text}".lower() and max(source_rel, target_rel) < 0.18:
                    continue
                relation = "candidate_beam_parameter_closure" if term_low in arg_text.lower() else "candidate_beam_call_closure"
                confidence = 0.72 if relation == "candidate_beam_parameter_closure" else 0.58 + 0.08 * max(source_rel, target_rel)
                flow_edge = _edge(
                    source=source,
                    target=target,
                    relation=f"{relation}:hop{hop + 1}",
                    term=term,
                    call=call,
                    confidence=min(0.88, confidence),
                )
                local_closure_edges.append(flow_edge)
                edges.append(flow_edge)
                candidate_paths.extend([source.path, target.path])
                if sid not in seen_frontier:
                    next_frontier.add(sid)
                if tid not in seen_frontier:
                    next_frontier.add(tid)
                if len(edges) >= max_term_edges:
                    break
            if len(edges) >= max_term_edges:
                break
            if not next_frontier:
                break
            seen_frontier.update(next_frontier)
            frontier = next_frontier

        term_names = {_normal_symbol(entity.name) for entity in term_entities}
        for source in active_entities:
            for call in calls_by_entity.get(id(source), []):
                if str(call.get("callee_symbol") or "") not in term_names:
                    continue
                if source in term_entities:
                    continue
                candidate_paths.append(source.path)
                for target in term_entities[:8]:
                    edges.append(
                        _edge(
                            source=source,
                            target=target,
                            relation="caller_reaches_term_entity",
                            term=term,
                            call=call,
                            confidence=0.62,
                        )
                    )
                    if len(edges) >= max_term_edges:
                        break
                if len(edges) >= max_term_edges:
                    break
            if len(edges) >= max_term_edges:
                break

        dedup_paths = _dedupe((_norm(path) for path in candidate_paths), limit=80)
        if not dedup_paths:
            continue

        edge_summary = {
            "edge_count": len(edges),
            "relations": sorted({str(edge.get("relation")) for edge in edges}),
            "source_paths": _dedupe((str(edge.get("source_path") or "") for edge in edges), limit=20),
            "target_paths": _dedupe((str(edge.get("target_path") or "") for edge in edges), limit=20),
        }
        confidence_values = [float(edge.get("confidence") or 0.0) for edge in edges]
        base_confidence = max(confidence_values) if confidence_values else 0.48
        path_bonus = min(0.18, 0.03 * len(set(dedup_paths)))
        traces.append(
            {
                "flow_type": _classify_flow(term, issue_text),
                "term": term,
                "closure_kind": "interprocedural_parameter_state_closure",
                "backend": "arise_inspired_interprocedural_flow",
                "precision_level": "best_effort_ast_plus_symbol_resolution",
                "confidence": round(min(0.96, base_confidence + path_bonus), 3),
                "candidate_beam_paths": sorted(beam_paths)[:80],
                "candidate_target_paths": dedup_paths[: limit * 4],
                "locations": locations[: limit * 5],
                "edges": edges[: limit * 8],
                "local_closure_edges": local_closure_edges[: limit * 8],
                "interprocedural_edges": edges[: limit * 8],
                "edge_summary": edge_summary,
                "reason": (
                    f"Term `{term}` appears in {len(term_entities)} code entities; "
                    f"{len(edges)} cross-entity call/reference edges were found; "
                    f"{len(local_closure_edges)} edges came from the local candidate beam closure."
                ),
                "missing_precision_backends": [
                    "tree_sitter_language_specific_call_resolution",
                    "LSP_or_SCIP_find_references",
                    "CodeQL_global_dataflow",
                ],
            }
        )

    traces.sort(
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            str(item.get("flow_type")),
            str(item.get("term")),
        )
    )
    return traces[:limit]
