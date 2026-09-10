from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from mycode.flow_analysis.query_flows import extract_flow_terms, is_valid_flow_term
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
CALL_WITH_ARGS_RE = re.compile(r"\b(?P<callee>[A-Za-z_$][A-Za-z0-9_$.]*)\s*\((?P<args>[^)]{0,900})\)")
ASSIGN_RE = re.compile(r"\b(?P<left>[A-Za-z_$][A-Za-z0-9_$.]*)\s*(?::=|=|\+=|-=|\*=|/=)\s*(?P<right>.+)")
RETURN_RE = re.compile(r"\breturn\b(?P<expr>.*)")
FUNCTION_NAME_RE = re.compile(
    r"(?:function\s+|def\s+|(?:export\s+)?(?:const|let|var)\s+|"
    r"(?:public|private|protected|static|final|\s)+[\w<>\[\], ?]+\s+)"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?:=|\()"
)

STOP_TOKENS = {
    "http",
    "https",
    "github",
    "com",
    "org",
    "src",
    "lib",
    "test",
    "tests",
    "function",
    "return",
    "const",
    "class",
    "import",
    "export",
    "from",
    "this",
    "that",
    "true",
    "false",
    "none",
    "null",
    "click",
    "clicked",
    "wordpress",
    "site",
    "div",
    "span",
}

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
    "openssh",
    "selector",
    "state",
    "dispatch",
    "reducer",
    "email",
    "verified",
    "email_verified",
    "redirect",
    "redirect_to",
    "client_id",
    "post_id",
    "url",
    "href",
    "link",
    "hover",
    "leave",
    "legend",
    "handler",
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
    "deleted",
    "variable",
}

ROLE_PRIORITY = {
    "reproduction_or_example": 0,
    "test_or_fixture": 1,
    "reference_api": 2,
    "public_api": 3,
    "component_or_route": 4,
    "state_selector": 5,
    "state_update": 6,
    "event_handler": 7,
    "url_builder": 8,
    "type_binder": 9,
    "backend": 10,
    "serializer": 11,
    "style_resolver": 12,
    "implementation": 13,
}


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _normal_symbol(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    raw = raw.split(".")[-1]
    raw = raw.strip("_$")
    return raw.lower()


def _dedupe(values: Iterable[str], *, limit: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _looks_like_flow_term(token: str) -> bool:
    low = token.lower().strip("._-$")
    if len(low) < 3 or low in STOP_TOKENS or not is_valid_flow_term(token):
        return False
    if low in FLOW_HINTS:
        return True
    if "_" in token or re.search(r"[a-z][A-Z]", token):
        return True
    return any(hint in low for hint in FLOW_HINTS)


def _flow_terms(
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None,
    queries: Iterable[str] | None,
) -> list[str]:
    chunks = [issue_text or "", " ".join(queries or [])]
    for observation in tool_observations or []:
        extracted = observation.get("extracted", {}) or {}
        chunks.append(str(extracted.get("parsed_reproduction", "")))
        chunks.append(str(extracted.get("source_files", "")))
        chunks.append(str(extracted.get("code_features", "")))
        chunks.append(str(extracted.get("semantic_queries", "")))
        chunks.append(str(extracted.get("vlm_analysis", "")))
        chunks.append(str(extracted.get("runtime_trace", "")))
        chunks.append(str(extracted.get("console", "")))
    seed = "\n".join(chunks)
    terms: list[str] = []
    for token in list(extract_flow_terms(issue_text, tool_observations)) + IDENT_RE.findall(seed):
        if _looks_like_flow_term(token):
            terms.append(token)
    return _dedupe(terms, limit=42)


def _entity_for_line(index: RepositoryIndex, path: str, line_no: int) -> CodeEntity | None:
    best: CodeEntity | None = None
    for entity in index.entities:
        if _norm(entity.path) != _norm(path):
            continue
        if int(entity.start_line or 0) <= line_no <= int(entity.end_line or 0):
            if best is None or (entity.end_line - entity.start_line) < (best.end_line - best.start_line):
                best = entity
    return best


def _entity_dict(entity: CodeEntity | None) -> dict[str, Any] | None:
    if entity is None:
        return None
    return {
        "path": entity.path,
        "kind": entity.kind,
        "name": entity.name,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
    }


def _role_for_path(path: str, code: str = "", name: str = "", kind: str = "") -> str:
    low = f"{path} {code} {name} {kind}".replace("\\", "/").lower()
    if any(part in low for part in ("/examples/", "/example/", "/demo/", "/demos/", "/docs/", "/playground/", "/sandbox/")):
        return "reproduction_or_example"
    if any(part in low for part in ("/test/", "/tests/", "__tests__", "/fixture/", "/fixtures/", ".spec.", ".test.")):
        return "test_or_fixture"
    if any(token in low for token in ("getediturl", "url", "href", "redirect", "route", "link", "permalink")):
        return "url_builder"
    if any(token in low for token in ("selector", "select", "current-user", "currentuser", "emailverified", "email_verified")):
        return "state_selector"
    if any(token in low for token in ("reducer", "dispatch", "action", "store", "state/")):
        return "state_update"
    if any(token in low for token in ("component", "view", "page", "screen", "dashboard", "reader", "signup", "woocommerce", "render")):
        return "component_or_route"
    if any(token in low for token in ("hover", "leave", "click", "mousemove", "mouseout", "handler", "onhover", "onleave")):
        return "event_handler"
    if any(token in low for token in ("stylesheet", "style", "resolve", "expand", "margin", "layout", "yoga", "css", "scss")):
        return "style_resolver"
    if any(token in low for token in ("binder", "declaration", "typeinfo", "typetype", "typevars", "deleted", "narrow")):
        return "type_binder"
    if "backend" in low:
        return "backend"
    if any(token in low for token in ("serialize", "serializer", "serialization", "private_key", "openssh", "/ssh.")):
        return "serializer"
    if kind == "class" or any(part in low for part in ("/api/", "/public/", "__init__.py", ".pyi")):
        return "public_api"
    return "implementation"


def _calls_from_line(line: str) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for match in CALL_WITH_ARGS_RE.finditer(line):
        callee = match.group("callee")
        calls.append(
            {
                "callee": callee,
                "symbol": _normal_symbol(callee),
                "args": match.group("args"),
            }
        )
    return calls[:12]


def _defs_uses_from_line(line: str) -> tuple[list[str], list[str]]:
    defs: list[str] = []
    assign = ASSIGN_RE.search(line)
    if assign:
        defs.append(_normal_symbol(assign.group("left")))
    uses = [_normal_symbol(token) for token in IDENT_RE.findall(line)]
    if assign:
        right_tokens = [_normal_symbol(token) for token in IDENT_RE.findall(assign.group("right"))]
        uses = right_tokens
    defs = _dedupe([item for item in defs if item], limit=12)
    uses = _dedupe([item for item in uses if item and item not in defs], limit=30)
    return defs, uses


def _kind_for_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped.startswith(("import ", "from ")) or " require(" in stripped:
        return "import"
    if FUNCTION_NAME_RE.search(stripped) or stripped.startswith(("def ", "class ", "function ")):
        return "definition"
    if RETURN_RE.search(stripped):
        return "return"
    if ASSIGN_RE.search(stripped):
        return "assignment"
    if CALL_WITH_ARGS_RE.search(stripped):
        return "call"
    return "statement"


def _step_id(path: str, line_no: int, term: str, ordinal: int) -> str:
    return f"{_norm(path)}:{line_no}:{_normal_symbol(term)}:{ordinal}"


def _step_for_line(index: RepositoryIndex, path: str, line_no: int, line: str, term: str, ordinal: int) -> dict[str, Any]:
    entity = _entity_for_line(index, path, line_no)
    defs, uses = _defs_uses_from_line(line)
    calls = _calls_from_line(line)
    kind = _kind_for_line(line)
    entity_info = _entity_dict(entity)
    role = _role_for_path(
        path,
        line,
        name=str(entity.name if entity else ""),
        kind=str(entity.kind if entity else ""),
    )
    return {
        "id": _step_id(path, line_no, term, ordinal),
        "path": _norm(path),
        "line": line_no,
        "kind": kind,
        "code": line.strip()[:700],
        "term": term,
        "defs": defs,
        "uses": uses,
        "calls": calls,
        "entity": entity_info,
        "role": role,
    }


def _path_term_score(path: str, text: str, term: str, queries: Iterable[str] | None) -> float:
    hay = f"{path}\n{text[:20000]}".lower()
    score = 0.0
    term_low = term.lower()
    if term_low in path.lower():
        score += 8.0
    score += min(hay.count(term_low), 8) * 2.0
    for token in tokenize(" ".join(queries or [])):
        if len(token) < 3:
            continue
        if token in path.lower():
            score += 0.8
        elif token in hay:
            score += 0.25
    role = _role_for_path(path, text[:5000])
    if role in {"backend", "serializer", "style_resolver", "url_builder", "type_binder", "component_or_route"}:
        score += 2.0
    if role in {"reproduction_or_example", "test_or_fixture"}:
        score -= 2.0
    return score


def _steps_for_term(
    index: RepositoryIndex,
    term: str,
    queries: Iterable[str] | None,
    *,
    max_files: int = 34,
    max_steps: int = 160,
) -> list[dict[str, Any]]:
    term_low = term.lower()
    ranked_paths: list[tuple[str, float]] = []
    for path, text in index.files.items():
        if term_low not in f"{path}\n{text}".lower():
            continue
        score = _path_term_score(path, text, term, queries)
        ranked_paths.append((_norm(path), score))
    ranked_paths.sort(key=lambda item: (-item[1], item[0]))

    steps: list[dict[str, Any]] = []
    ordinal = 0
    for path, _score in ranked_paths[:max_files]:
        text = index.files.get(path, "")
        lower_lines = text.lower().splitlines()
        for idx, lower_line in enumerate(lower_lines, start=1):
            if term_low not in lower_line and term_low not in path.lower():
                continue
            raw_line = text.splitlines()[idx - 1] if idx - 1 < len(text.splitlines()) else ""
            ordinal += 1
            steps.append(_step_for_line(index, path, idx, raw_line, term, ordinal))
            if len(steps) >= max_steps:
                return steps
    return steps


def _symbol_entities(index: RepositoryIndex) -> dict[str, list[CodeEntity]]:
    mapping: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in index.entities:
        key = _normal_symbol(entity.name)
        if key:
            mapping[key].append(entity)
    return mapping


def _add_edge(
    edges: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    relation: str,
    term: str,
    reason: str,
    symbol: str = "",
    weight: float = 1.0,
) -> None:
    if source.get("id") == target.get("id"):
        return
    edges.append(
        {
            "source": source.get("path"),
            "target": target.get("path"),
            "source_step": source.get("id"),
            "target_step": target.get("id"),
            "source_line": source.get("line"),
            "target_line": target.get("line"),
            "relation": relation,
            "term": term,
            "symbol": symbol,
            "reason": reason,
            "weight": round(weight, 3),
        }
    )


def _step_entity_name(step: dict[str, Any]) -> str:
    entity = step.get("entity") or {}
    if isinstance(entity, dict):
        return _normal_symbol(str(entity.get("name") or ""))
    return ""


def _build_chain_edges(index: RepositoryIndex, steps: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
    symbol_to_entities = _symbol_entities(index)
    entity_to_steps: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    path_steps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    def_steps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    use_steps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attr_steps: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for step in steps:
        path_steps[str(step.get("path") or "")].append(step)
        entity = step.get("entity") or {}
        if isinstance(entity, dict):
            entity_to_steps[(str(entity.get("path") or ""), _normal_symbol(str(entity.get("name") or "")))].append(step)
        for symbol in step.get("defs", []) or []:
            if symbol:
                def_steps[symbol].append(step)
        for symbol in step.get("uses", []) or []:
            if symbol:
                use_steps[symbol].append(step)
        for token in re.findall(r"(?:self|this)\.([A-Za-z_$][A-Za-z0-9_$]*)", str(step.get("code") or "")):
            attr_steps[_normal_symbol(token)].append(step)

    edges: list[dict[str, Any]] = []
    term_key = _normal_symbol(term)
    for step in steps:
        path = str(step.get("path") or "")
        code_low = str(step.get("code") or "").lower()
        for call in step.get("calls", []) or []:
            callee_symbol = str(call.get("symbol") or "")
            if not callee_symbol:
                continue
            for entity in symbol_to_entities.get(callee_symbol, [])[:8]:
                target_steps = entity_to_steps.get((entity.path, _normal_symbol(entity.name))) or [
                    _step_for_line(index, entity.path, entity.start_line, entity.text.strip().splitlines()[0] if entity.text else entity.name, term, 9999)
                ]
                for target in target_steps[:2]:
                    relation = "call_boundary"
                    reason = f"call `{call.get('callee')}` reaches entity `{entity.name}`"
                    if term_key and term_key in str(call.get("args") or "").lower():
                        relation = "call_argument_to_formal"
                        reason = f"`{term}` appears in arguments passed to `{entity.name}`"
                    if entity.path != path or relation == "call_argument_to_formal":
                        _add_edge(
                            edges,
                            source=step,
                            target=target,
                            relation=relation,
                            term=term,
                            reason=reason,
                            symbol=entity.name,
                            weight=2.5 if relation == "call_argument_to_formal" else 1.6,
                        )

        for defined in step.get("defs", []) or []:
            if not defined:
                continue
            for target in use_steps.get(defined, [])[:12]:
                if target is step:
                    continue
                relation = "same_symbol_def_use"
                if path != str(target.get("path") or ""):
                    relation = "cross_file_same_symbol_flow"
                _add_edge(
                    edges,
                    source=step,
                    target=target,
                    relation=relation,
                    term=term,
                    reason=f"definition `{defined}` is used by another candidate step",
                    symbol=defined,
                    weight=2.0 if relation.startswith("cross_file") else 1.3,
                )

        if RETURN_RE.search(str(step.get("code") or "")):
            entity_name = _step_entity_name(step)
            if entity_name:
                for target in use_steps.get(entity_name, [])[:12]:
                    if target is step:
                        continue
                    _add_edge(
                        edges,
                        source=step,
                        target=target,
                        relation="return_value_to_call_site",
                        term=term,
                        reason=f"return value from `{entity_name}` can flow to call site",
                        symbol=entity_name,
                        weight=2.2,
                    )

        for attr, attr_related in attr_steps.items():
            if attr not in code_low:
                continue
            for target in attr_related[:10]:
                if target is step:
                    continue
                _add_edge(
                    edges,
                    source=step,
                    target=target,
                    relation="field_write_to_read_or_shared_attribute",
                    term=term,
                    reason=f"shared attribute `{attr}` links state across methods",
                    symbol=attr,
                    weight=1.8,
                )

    paths = list(path_steps)
    if len(paths) > 1:
        ordered_steps = sorted(steps, key=lambda item: (ROLE_PRIORITY.get(str(item.get("role")), 99), str(item.get("path")), int(item.get("line") or 0)))
        for source in ordered_steps[:10]:
            for target in ordered_steps[-12:]:
                if source.get("path") == target.get("path"):
                    continue
                _add_edge(
                    edges,
                    source=source,
                    target=target,
                    relation="same_issue_term_cross_file",
                    term=term,
                    reason=f"`{term}` appears in both files; keep as weak cross-file flow hint",
                    symbol=term,
                    weight=0.45,
                )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for edge in sorted(edges, key=lambda item: (-float(item.get("weight") or 0.0), str(item.get("relation")), str(item.get("source")), str(item.get("target")))):
        key = (edge.get("source_step"), edge.get("target_step"), edge.get("relation"), edge.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
        if len(deduped) >= 90:
            break
    return deduped


def _classify_flow(term: str, steps: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    text = " ".join(
        [
            term,
            " ".join(str(step.get("path") or "") for step in steps),
            " ".join(str(step.get("code") or "") for step in steps[:50]),
            " ".join(str(edge.get("relation") or "") for edge in edges),
        ]
    ).lower()
    if any(token in text for token in ("kdf", "serialize", "serializer", "backend", "private_key", "openssh", "ssh")):
        return "serializer_backend_flow_chain"
    from mycode.flow_analysis.language_context import python_binding_context

    if python_binding_context(text, paths=[step.get("path", "") for step in steps]):
        return "python_type_binding_flow_chain"
    if any(token in text for token in ("stylesheet", "style", "resolve", "expand", "margin", "layout", "yoga", "pdf")):
        return "style_pipeline_flow_chain"
    if any(token in text for token in ("selector", "state", "dispatch", "reducer", "email_verified", "current-user")):
        return "state_selector_flow_chain"
    if any(token in text for token in ("hover", "leave", "legend", "event", "handler", "mousemove", "onhover", "onleave")):
        return "ui_event_flow_chain"
    if any(token in text for token in ("url", "href", "redirect", "route", "post_id", "client_id", "getediturl", "link")):
        return "url_builder_flow_chain"
    return "parameter_state_flow_chain"


def _required_roles(flow_type: str) -> list[str]:
    if flow_type == "serializer_backend_flow_chain":
        return ["public_api", "backend", "serializer"]
    if flow_type == "python_type_binding_flow_chain":
        return ["type_binder", "implementation"]
    if flow_type == "style_pipeline_flow_chain":
        return ["style_resolver", "implementation"]
    if flow_type == "state_selector_flow_chain":
        return ["state_selector", "component_or_route"]
    if flow_type == "ui_event_flow_chain":
        return ["component_or_route", "event_handler"]
    if flow_type == "url_builder_flow_chain":
        return ["component_or_route", "url_builder"]
    return ["public_api", "implementation"]


def _role_coverage(steps: list[dict[str, Any]], required_roles: list[str]) -> dict[str, Any]:
    roles: dict[str, int] = defaultdict(int)
    for step in steps:
        role = str(step.get("role") or "implementation")
        roles[role] += 1
    covered = [role for role in required_roles if roles.get(role, 0)]
    missing = [role for role in required_roles if role not in covered]
    return {
        "required_roles": required_roles,
        "covered_roles": covered,
        "missing_roles": missing,
        "roles": dict(sorted(roles.items())),
        "coverage": round(len(covered) / max(1, len(required_roles)), 3),
    }


def _group_locations(steps: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_score: dict[str, float] = defaultdict(float)
    for step in steps:
        by_path[str(step.get("path") or "")].append(step)
    for edge in edges:
        edge_score[str(edge.get("source") or "")] += float(edge.get("weight") or 0.0)
        edge_score[str(edge.get("target") or "")] += float(edge.get("weight") or 0.0)

    locations: list[dict[str, Any]] = []
    for path, path_steps in by_path.items():
        role_counts: dict[str, int] = defaultdict(int)
        for step in path_steps:
            role_counts[str(step.get("role") or "implementation")] += 1
        role = sorted(role_counts, key=lambda item: (-role_counts[item], ROLE_PRIORITY.get(item, 99), item))[0]
        entity_names = _dedupe(
            [
                str((step.get("entity") or {}).get("name") or "")
                for step in path_steps
                if isinstance(step.get("entity"), dict)
            ],
            limit=8,
        )
        locations.append(
            {
                "path": path,
                "kind": "file",
                "name": "",
                "role": role,
                "statement_count": len(path_steps),
                "edge_score": round(edge_score.get(path, 0.0), 3),
                "entities": entity_names,
                "statements": path_steps[:8],
            }
        )
    locations.sort(
        key=lambda item: (
            ROLE_PRIORITY.get(str(item.get("role") or ""), 99),
            -float(item.get("edge_score") or 0.0),
            str(item.get("path") or ""),
        )
    )
    return locations


def _candidate_target_paths(steps: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    blocked = {"reproduction_or_example", "test_or_fixture", "reference_api"}
    score: dict[str, float] = defaultdict(float)
    for step in steps:
        path = str(step.get("path") or "")
        role = str(step.get("role") or "implementation")
        if not path or role in blocked:
            continue
        score[path] += 1.0 + ROLE_PRIORITY.get(role, 8) * 0.08
        if role in {"backend", "serializer", "style_resolver", "url_builder", "type_binder", "component_or_route"}:
            score[path] += 2.0
    for edge in edges:
        for key in ("target", "source"):
            path = str(edge.get(key) or "")
            if not path:
                continue
            weight = float(edge.get("weight") or 0.0)
            relation = str(edge.get("relation") or "")
            if relation == "same_issue_term_cross_file":
                weight *= 0.35
            score[path] += weight
    ranked = sorted(score, key=lambda path: (-score[path], path))
    return ranked[:18]


def _source_sink_steps(steps: list[dict[str, Any]], candidate_paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_roles = {"reproduction_or_example", "reference_api", "public_api", "state_selector", "component_or_route"}
    sink_roles = {"backend", "serializer", "style_resolver", "url_builder", "type_binder", "state_update", "implementation"}
    source = [step for step in steps if str(step.get("role") or "") in source_roles][:10]
    candidate_set = set(candidate_paths)
    sink = [step for step in steps if str(step.get("path") or "") in candidate_set and str(step.get("role") or "") in sink_roles][:12]
    if not source:
        source = steps[:6]
    if not sink:
        sink = steps[-8:]
    return source, sink


def trace_flow_chains(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Build issue-guided cross-function flow chains.

    This is the bridge between coarse call navigation and ARISE-style slices. It
    records explicit source/sink steps and chain edges so the localization agent
    can explain how an issue state, parameter, URL or UI event may travel from
    evidence into modification targets. It is still a static heuristic, not a
    CodeQL/SCIP-grade global dataflow engine.
    """

    flows: list[dict[str, Any]] = []
    for term in _flow_terms(issue_text, tool_observations, queries):
        steps = _steps_for_term(index, term, queries)
        if len({step.get("path") for step in steps}) < 2 and len(steps) < 3:
            continue
        edges = _build_chain_edges(index, steps, term)
        flow_type = _classify_flow(term, steps, edges)
        required = _required_roles(flow_type)
        coverage = _role_coverage(steps, required)
        candidate_paths = _candidate_target_paths(steps, edges)
        source_steps, sink_steps = _source_sink_steps(steps, candidate_paths)
        edge_summary: dict[str, int] = defaultdict(int)
        for edge in edges:
            edge_summary[str(edge.get("relation") or "unknown")] += 1
        chain_strength = sum(float(edge.get("weight") or 0.0) for edge in edges[:30])
        confidence = min(
            0.99,
            0.24
            + 0.025 * min(len(steps), 18)
            + 0.035 * min(len(edges), 18)
            + 0.18 * float(coverage.get("coverage") or 0.0)
            + 0.015 * min(chain_strength, 12.0),
        )
        locations = _group_locations(steps, edges)
        flows.append(
            {
                "term": term,
                "flow_type": flow_type,
                "closure_kind": "issue_guided_cross_function_flow_chain",
                "backend": "issue_guided_flow_chain",
                "precision_level": "cross_function_static_heuristic_not_full_def_use",
                "confidence": round(confidence, 3),
                "flow_obligation": required,
                "role_coverage": coverage,
                "candidate_target_paths": candidate_paths,
                "source_steps": source_steps,
                "sink_steps": sink_steps,
                "locations": locations,
                "chain_edges": edges,
                "edges": edges,
                "edge_summary": dict(sorted(edge_summary.items())),
                "missing_precision_backends": [
                    "tree_sitter_typed_ast",
                    "scip_lsp_references",
                    "codeql_global_dataflow",
                    "runtime_trace",
                ],
                "reason": (
                    f"`{term}` forms {len(edges)} cross-step chain hints across "
                    f"{len({step.get('path') for step in steps})} files; "
                    f"role coverage={coverage['coverage']} for {flow_type}."
                ),
            }
        )

    flows.sort(
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            str(item.get("flow_type") or ""),
            str(item.get("term") or ""),
        )
    )
    return flows[:limit]
