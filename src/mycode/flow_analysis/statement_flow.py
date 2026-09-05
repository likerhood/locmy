from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable

from mycode.flow_analysis.query_flows import extract_flow_terms, is_valid_flow_term
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
ASSIGN_RE = re.compile(r"\b(?P<left>[A-Za-z_$][A-Za-z0-9_$.]*)\s*=\s*(?P<right>.+)")
CALL_RE = re.compile(r"\b(?P<name>[A-Za-z_$][A-Za-z0-9_$.]*)\s*\((?P<args>[^)]{0,600})\)")
JS_FUNCTION_HEADER_RE = re.compile(
    r"(?:function\s+|(?:const|let|var)\s+)(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?:=\s*)?(?:\((?P<args1>[^)]*)\)|(?P<args2>[A-Za-z_$][A-Za-z0-9_$]*))"
)
JAVA_METHOD_HEADER_RE = re.compile(
    r"(?:public|protected|private|static|final|synchronized|\s)+[\w<>\[\], ?]+\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)"
)


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _flow_terms(issue_text: str, tool_observations: Iterable[Dict[str, Any]] | None, queries: Iterable[str] | None) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    seed = " ".join([issue_text or "", " ".join(queries or [])])
    for observation in tool_observations or []:
        extracted = observation.get("extracted", {}) or {}
        seed += "\n" + str(extracted.get("parsed_reproduction", ""))
        seed += "\n" + str(extracted.get("semantic_queries", ""))
        seed += "\n" + str(extracted.get("vlm_analysis", ""))
    for token in list(extract_flow_terms(issue_text, tool_observations)) + IDENT_RE.findall(seed):
        low = token.lower().strip("._-$")
        if len(low) < 3 or low in seen or not is_valid_flow_term(token):
            continue
        if (
            "_" in token
            or re.search(r"[a-z][A-Z]", token)
            or any(
                hint in low
                for hint in (
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
                    "post",
                    "site",
                    "type",
                    "binder",
                    "margin",
                    "style",
                    "option",
                    "config",
                )
            )
        ):
            seen.add(low)
            terms.append(token)
        if len(terms) >= 40:
            break
    return terms


def _entity_for_line(index: RepositoryIndex, path: str, line_no: int) -> CodeEntity | None:
    best: CodeEntity | None = None
    for entity in index.entities:
        if _norm(entity.path) != _norm(path):
            continue
        if int(entity.start_line or 0) <= line_no <= int(entity.end_line or 0):
            if best is None or (entity.end_line - entity.start_line) < (best.end_line - best.start_line):
                best = entity
    return best


def _statement_kind(line: str, term: str) -> str:
    low = line.lower()
    term_low = term.lower()
    if term_low in low and (
        re.search(r"\bdef\s+[A-Za-z_]\w*\s*\(", line)
        or JS_FUNCTION_HEADER_RE.search(line)
        or JAVA_METHOD_HEADER_RE.search(line)
    ):
        return "parameter_definition"
    if re.search(rf"\b{re.escape(term)}\b\s*=", line) or f"self.{term_low}" in low and "=" in line:
        return "define_or_assign"
    if "return" in low:
        return "return_value"
    if "if " in low or low.startswith("if(") or "elif " in low or "while " in low:
        return "condition"
    if CALL_RE.search(line):
        return "call_or_argument"
    if "import " in low or " from " in low:
        return "import_or_export"
    return "read_or_reference"


def _statement_for_line(index: RepositoryIndex, path: str, line_no: int, line: str, term: str) -> dict[str, Any]:
    entity = _entity_for_line(index, path, line_no)
    calls = [match.group("name").split(".")[-1] for match in CALL_RE.finditer(line)]
    assigned = []
    assign = ASSIGN_RE.search(line)
    if assign:
        assigned.append(assign.group("left").split(".")[-1])
    kind = _statement_kind(line, term)
    if kind == "parameter_definition" and term.lower() in {token.lower() for token in IDENT_RE.findall(line)}:
        assigned.append(term)
    return {
        "path": path,
        "line": line_no,
        "kind": kind,
        "code": line.strip()[:500],
        "term": term,
        "defs": assigned[:8],
        "uses": [tok for tok in tokenize(line) if tok == term.lower()][:8],
        "calls": calls[:8],
        "entity": (
            {
                "kind": entity.kind,
                "name": entity.name,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
            }
            if entity
            else None
        ),
    }


def _python_statements(index: RepositoryIndex, path: str, text: str, term: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    low = term.lower()
    statements: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id.lower())
            elif isinstance(child, ast.Attribute):
                names.add(child.attr.lower())
        if low not in names:
            continue
        line_no = int(getattr(node, "lineno", 0) or 0)
        if not line_no or line_no > len(lines):
            continue
        statements.append(_statement_for_line(index, path, line_no, lines[line_no - 1], term))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in sorted(statements, key=lambda row: (row["line"], row["kind"])):
        key = (item["path"], int(item["line"]), str(item["kind"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:30]


def _text_statements(index: RepositoryIndex, path: str, text: str, term: str) -> list[dict[str, Any]]:
    low = term.lower()
    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if low in line.lower():
            items.append(_statement_for_line(index, path, line_no, line, term))
        if len(items) >= 30:
            break
    return items


def _statements_for_term(index: RepositoryIndex, term: str, *, limit_files: int = 20) -> list[dict[str, Any]]:
    matching_paths: list[str] = []
    low = term.lower()
    for path, text in index.files.items():
        if low in path.lower() or low in text.lower():
            matching_paths.append(path)
        if len(matching_paths) >= limit_files:
            break
    statements: list[dict[str, Any]] = []
    for path in matching_paths:
        text = index.files.get(path, "")
        if Path(path).suffix.lower() in {".py", ".pyi"}:
            statements.extend(_python_statements(index, path, text, term) or _text_statements(index, path, text, term))
        else:
            statements.extend(_text_statements(index, path, text, term))
    return statements[:80]


def _entity_name_map(index: RepositoryIndex) -> dict[str, set[str]]:
    mapped: dict[str, set[str]] = defaultdict(set)
    for entity in index.entities:
        mapped[entity.name].add(entity.path)
    return mapped


def _statement_edges(index: RepositoryIndex, statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    name_to_paths = _entity_name_map(index)
    edges: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for statement in statements:
        by_path[str(statement.get("path") or "")].append(statement)

    # Local def-use: a concrete definition/parameter must precede the use.
    for path, path_statements in by_path.items():
        definitions: list[dict[str, Any]] = []
        for stmt in sorted(path_statements, key=lambda item: int(item.get("line") or 0)):
            if stmt.get("kind") in {"define_or_assign", "parameter_definition"}:
                definitions.append(stmt)
                continue
            if not definitions:
                continue
            source_stmt = definitions[-1]
            edges.append(
                {
                    "source": path,
                    "target": path,
                    "relation": "local_def_use",
                    "source_line": source_stmt.get("line"),
                    "target_line": stmt.get("line"),
                    "symbol": stmt.get("term"),
                }
            )

    # Interprocedural argument-to-parameter edges are emitted only for a unique
    # callee definition that also contains a parameter definition for the term.
    for stmt in statements:
        source = str(stmt.get("path") or "")
        if stmt.get("kind") != "call_or_argument":
            continue
        for call in stmt.get("calls", []) or []:
            targets = sorted(target for target in name_to_paths.get(str(call), set()) if target != source)
            if len(targets) != 1:
                continue
            target = targets[0]
            parameter = next(
                (
                    target_stmt
                    for target_stmt in by_path.get(target, [])
                    if target_stmt.get("kind") == "parameter_definition"
                ),
                None,
            )
            if parameter is None:
                continue
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "relation": "argument_to_formal_parameter",
                    "source_line": stmt.get("line"),
                    "target_line": parameter.get("line"),
                    "symbol": stmt.get("term"),
                    "callee": call,
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        key = (
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
            str(edge.get("relation") or ""),
            str(edge.get("symbol") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
        if len(deduped) >= 80:
            break
    return deduped


def _role(path: str, statements: list[dict[str, Any]]) -> str:
    text = f"{path} {' '.join(str(item.get('code') or '') for item in statements[:6])}".lower()
    if any(tok in text for tok in ("example", "demo", "playground", "sandbox")):
        return "reproduction_or_example"
    if any(tok in text for tok in ("test", "spec", "fixture")):
        return "test_or_fixture"
    if any(tok in text for tok in ("serialize", "serializer", "private_key", "openssh", "ssh")):
        return "serializer"
    if "backend" in text:
        return "backend"
    if any(tok in text for tok in ("selector", "email_verified", "state", "dispatch", "reducer")):
        return "state_or_selector"
    if any(tok in text for tok in ("hover", "leave", "onclick", "handler", "legend")):
        return "event_handler"
    if any(tok in text for tok in ("style", "stylesheet", "margin", "resolve", "expand", "layout")):
        return "style_or_layout_pipeline"
    if any(tok in text for tok in ("url", "href", "redirect", "route", "post", "site")):
        return "url_builder_or_route"
    if any(tok in text for tok in ("typeinfo", "binder", "declaration", "deleted")):
        return "type_binder"
    return "implementation"


def _classify_flow(term: str, statements: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    text = f"{term} {' '.join(str(item.get('code') or '') for item in statements[:20])} {' '.join(str(edge.get('relation') or '') for edge in edges)}".lower()
    if any(tok in text for tok in ("serialize", "serializer", "private_key", "openssh", "kdf", "round")):
        return "serializer_backend_statement_flow"
    if any(tok in text for tok in ("binder", "typeinfo", "declaration", "deleted", "narrow")):
        return "python_type_binding_statement_flow"
    if any(tok in text for tok in ("hover", "leave", "onclick", "handler", "legend")):
        return "ui_event_statement_flow"
    if any(tok in text for tok in ("style", "stylesheet", "margin", "resolve", "expand", "layout")):
        return "style_pipeline_statement_flow"
    if any(tok in text for tok in ("selector", "state", "dispatch", "reducer")):
        return "state_selector_statement_flow"
    if any(tok in text for tok in ("url", "href", "redirect", "route", "post", "site")):
        return "url_builder_statement_flow"
    return "parameter_statement_flow"


def trace_statement_flows(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[Dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Build statement-level static flow hints for the dynamic localization agent.

    This is a stronger local approximation than file/entity keyword flow: it records
    concrete lines where an issue term is assigned, passed to a call, returned, or used
    in a condition, then links those lines to likely callee files through repository
    entities. It is still heuristic and intentionally labels itself as non-CodeQL.
    """

    flows: list[dict[str, Any]] = []
    for term in _flow_terms(issue_text, tool_observations, queries):
        statements = _statements_for_term(index, term)
        has_action_statement = any(
            item.get("kind") in {"define_or_assign", "parameter_definition", "call_or_argument", "return_value", "condition"}
            or item.get("calls")
            for item in statements
        )
        if len(statements) < 2 and not has_action_statement:
            continue
        edges = _statement_edges(index, statements)
        by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for statement in statements:
            by_path[str(statement.get("path") or "")].append(statement)
        roles = {path: _role(path, items) for path, items in by_path.items()}
        candidate_paths = [
            path
            for path, role in sorted(roles.items(), key=lambda pair: (pair[1] in {"reproduction_or_example", "test_or_fixture"}, pair[0]))
            if role not in {"reproduction_or_example", "test_or_fixture"}
        ][:20]
        flow_type = _classify_flow(term, statements, edges)
        confidence = min(
            0.98,
            0.34
            + 0.04 * len(by_path)
            + 0.05 * len(edges)
            + 0.04 * sum(1 for item in statements if item.get("kind") in {"define_or_assign", "parameter_definition", "call_or_argument", "return_value"}),
        )
        flows.append(
            {
                "term": term,
                "flow_type": flow_type,
                "closure_kind": "statement_level_def_use_and_call_flow",
                "backend": "statement_static_flow",
                "precision_level": "statement_level_local_def_use_with_unique_callee_resolution",
                "confidence": round(confidence, 3),
                "candidate_target_paths": candidate_paths,
                "path_roles": roles,
                "locations": [
                    {
                        "path": path,
                        "kind": "file",
                        "name": "",
                        "role": roles.get(path, "implementation"),
                        "statement_count": len(items),
                        "statements": items[:8],
                    }
                    for path, items in by_path.items()
                ],
                "statement_edges": edges,
                "edge_summary": dict(sorted({rel: sum(1 for edge in edges if edge.get("relation") == rel) for rel in {str(edge.get("relation") or "") for edge in edges}}.items())),
                "missing_precision_backends": ["codeql_statement_dataflow", "lsp_reference_index", "runtime_trace"],
                "reason": f"Term `{term}` has {len(statements)} statement hits across {len(by_path)} files.",
            }
        )
    flows.sort(key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("flow_type") or ""), str(item.get("term") or "")))
    return flows[:limit]
