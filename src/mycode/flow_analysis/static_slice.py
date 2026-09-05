from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mycode.flow_analysis.query_flows import extract_flow_terms
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex, tokenize


IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
CALL_RE = re.compile(r"\b(?P<name>[A-Za-z_$][A-Za-z0-9_$.]*)\s*\(")
ASSIGN_RE = re.compile(r"\b(?P<left>[A-Za-z_$][A-Za-z0-9_$.]*)\s*(?::=|=|\+=|-=|\*=|/=)\s*(?P<right>.+)")
FUNCTION_RE = re.compile(
    r"(?:function\s+|def\s+|(?:const|let|var)\s+|public\s+|private\s+|protected\s+)"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
)
PATH_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".c",
    ".h",
    ".css",
    ".scss",
    ".json",
    ".yaml",
    ".yml",
}


@dataclass
class StatementNode:
    id: str
    path: str
    line: int
    end_line: int
    code: str
    kind: str
    defs: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    entity: dict[str, Any] | None = None
    role: str = "implementation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "code": self.code,
            "kind": self.kind,
            "defs": self.defs,
            "uses": self.uses,
            "calls": self.calls,
            "entity": self.entity,
            "role": self.role,
        }


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _normal_symbol(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    raw = raw.split(".")[-1]
    raw = raw.strip("_")
    return raw.lower()


def _dedupe(values: Iterable[str], *, limit: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _flow_terms(issue_text: str, tool_observations: Iterable[dict[str, Any]] | None, queries: Iterable[str] | None) -> list[str]:
    seed = "\n".join([issue_text or "", " ".join(queries or [])])
    for observation in tool_observations or []:
        extracted = observation.get("extracted", {}) or {}
        seed += "\n" + str(extracted.get("parsed_reproduction", ""))
        seed += "\n" + str(extracted.get("source_files", ""))
        seed += "\n" + str(extracted.get("vlm_analysis", ""))
        seed += "\n" + str(extracted.get("runtime_trace", ""))
        seed += "\n" + str(extracted.get("console", ""))

    terms: list[str] = []
    for token in list(extract_flow_terms(issue_text, tool_observations)) + IDENT_RE.findall(seed):
        low = token.lower().strip("._-$")
        if len(low) < 3:
            continue
        if low in {"http", "https", "github", "com", "src", "test", "return", "const", "function"}:
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
                    "binder",
                    "type",
                    "margin",
                    "style",
                    "config",
                    "option",
                    "url",
                    "handler",
                )
            )
        ):
            terms.append(token)
    return _dedupe(terms, limit=36)


def _entity_for_line(index: RepositoryIndex, path: str, line_no: int) -> CodeEntity | None:
    best: CodeEntity | None = None
    for entity in index.entities:
        if _norm(entity.path) != _norm(path):
            continue
        if int(entity.start_line or 0) <= line_no <= int(entity.end_line or 0):
            if best is None or (entity.end_line - entity.start_line) < (best.end_line - best.start_line):
                best = entity
    return best


class _PyNameVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.defs: set[str] = set()
        self.uses: set[str] = set()
        self.calls: set[str] = set()

    def visit_arg(self, node: ast.arg) -> None:
        self.defs.add(_normal_symbol(node.arg))

    def visit_Name(self, node: ast.Name) -> None:
        target = self.defs if isinstance(node.ctx, (ast.Store, ast.Del, ast.Param)) else self.uses
        target.add(_normal_symbol(node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        target = self.defs if isinstance(node.ctx, (ast.Store, ast.Del)) else self.uses
        target.add(_normal_symbol(node.attr))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name:
            self.calls.add(name)
        self.generic_visit(node)


def _statement_kind(code: str, defs: list[str], uses: list[str], calls: list[str]) -> str:
    low = code.strip().lower()
    if low.startswith(("def ", "async def ", "class ")):
        return "definition_header"
    if defs and calls:
        return "assign_from_call"
    if defs:
        return "define_or_assign"
    if low.startswith(("if ", "elif ", "while ", "for ", "switch", "case ")):
        return "condition_or_iteration"
    if low.startswith("return"):
        return "return_value"
    if calls:
        return "call_or_argument"
    if "import " in low or low.startswith("from "):
        return "import_or_export"
    return "read_or_reference"


def _role(path: str, code: str) -> str:
    text = f"{path}\n{code}".lower()
    if any(part in text for part in ("/example", "/examples", "playground", "sandbox", "demo")):
        return "reproduction_or_example"
    if any(part in text for part in ("/test", "/tests", "__tests__", "fixture", "fixtures")):
        return "test_or_fixture"
    if any(part in text for part in ("serialize", "serialization", "private_key", "openssh", "ssh")):
        return "serializer"
    if "backend" in text:
        return "backend"
    if any(part in text for part in ("selector", "email_verified", "state", "dispatch", "reducer")):
        return "state_or_selector"
    if any(part in text for part in ("hover", "leave", "onclick", "onmouse", "handler", "legend")):
        return "event_handler"
    if any(part in text for part in ("style", "stylesheet", "margin", "resolve", "expand", "layout")):
        return "style_or_layout_pipeline"
    if any(part in text for part in ("url", "href", "redirect", "route", "post", "site")):
        return "url_builder_or_route"
    if any(part in text for part in ("binder", "typeinfo", "declaration", "deleted", "semantic")):
        return "type_binder"
    return "implementation"


def _python_statement_nodes(index: RepositoryIndex, path: str, text: str, term: str) -> list[StatementNode]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    nodes: list[StatementNode] = []
    term_low = term.lower()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        visitor = _PyNameVisitor()
        visitor.visit(node)
        defs = sorted(sym for sym in visitor.defs if sym)
        uses = sorted(sym for sym in visitor.uses if sym)
        calls = sorted(visitor.calls)
        if term_low not in defs and term_low not in uses and term_low not in [call.lower() for call in calls]:
            snippet = "\n".join(lines[max(0, int(getattr(node, "lineno", 1)) - 1): int(getattr(node, "end_lineno", 0) or getattr(node, "lineno", 1))])
            if term_low not in snippet.lower():
                continue
        line = int(getattr(node, "lineno", 0) or 0)
        end_line = int(getattr(node, "end_lineno", line) or line)
        if not line or line > len(lines):
            continue
        code = "\n".join(lines[line - 1:min(len(lines), end_line)]).strip()
        entity = _entity_for_line(index, path, line)
        kind = _statement_kind(code, defs, uses, calls)
        nodes.append(
            StatementNode(
                id=f"{path}:{line}:{len(nodes)}",
                path=path,
                line=line,
                end_line=end_line,
                code=code[:800],
                kind=kind,
                defs=defs[:16],
                uses=uses[:24],
                calls=calls[:12],
                entity=(
                    {
                        "path": entity.path,
                        "kind": entity.kind,
                        "name": entity.name,
                        "start_line": entity.start_line,
                        "end_line": entity.end_line,
                    }
                    if entity
                    else None
                ),
                role=_role(path, code),
            )
        )
    deduped: list[StatementNode] = []
    seen: set[tuple[str, int, str]] = set()
    for item in sorted(nodes, key=lambda row: (row.path, row.line, row.kind)):
        key = (item.path, item.line, item.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:80]


def _text_statement_nodes(index: RepositoryIndex, path: str, text: str, term: str) -> list[StatementNode]:
    nodes: list[StatementNode] = []
    low = term.lower()
    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        if low not in line.lower():
            continue
        defs: list[str] = []
        assign = ASSIGN_RE.search(line)
        if assign:
            defs.append(_normal_symbol(assign.group("left")))
        header = FUNCTION_RE.search(line)
        if header:
            defs.append(_normal_symbol(header.group("name")))
        calls = [_normal_symbol(match.group("name")) for match in CALL_RE.finditer(line)]
        uses = sorted(set(tokenize(line)) - set(defs))
        entity = _entity_for_line(index, path, line_no)
        kind = _statement_kind(line, defs, uses, calls)
        nodes.append(
            StatementNode(
                id=f"{path}:{line_no}:{len(nodes)}",
                path=path,
                line=line_no,
                end_line=line_no,
                code=line.strip()[:800],
                kind=kind,
                defs=[item for item in defs if item][:16],
                uses=[item for item in uses if item][:24],
                calls=[item for item in calls if item][:12],
                entity=(
                    {
                        "path": entity.path,
                        "kind": entity.kind,
                        "name": entity.name,
                        "start_line": entity.start_line,
                        "end_line": entity.end_line,
                    }
                    if entity
                    else None
                ),
                role=_role(path, line),
            )
        )
        if len(nodes) >= 80:
            break
    return nodes


def _nodes_for_term(index: RepositoryIndex, term: str, *, max_files: int = 32) -> list[StatementNode]:
    matching_paths: list[str] = []
    low = term.lower()
    for path, text in index.files.items():
        if Path(path).suffix.lower() not in PATH_EXTENSIONS:
            continue
        if low in path.lower() or low in text.lower():
            matching_paths.append(path)
        if len(matching_paths) >= max_files:
            break

    nodes: list[StatementNode] = []
    for path in matching_paths:
        text = index.files.get(path, "")
        if Path(path).suffix.lower() in {".py", ".pyi"}:
            nodes.extend(_python_statement_nodes(index, path, text, term) or _text_statement_nodes(index, path, text, term))
        else:
            nodes.extend(_text_statement_nodes(index, path, text, term))
    return nodes[:160]


def _entity_symbols(index: RepositoryIndex) -> dict[str, list[CodeEntity]]:
    by_name: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in index.entities:
        by_name[_normal_symbol(entity.name)].append(entity)
    return by_name


def _build_slice_edges(index: RepositoryIndex, nodes: list[StatementNode]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_symbol_defs: dict[str, list[StatementNode]] = defaultdict(list)
    by_symbol_uses: dict[str, list[StatementNode]] = defaultdict(list)
    for node in nodes:
        for sym in node.defs:
            by_symbol_defs[_normal_symbol(sym)].append(node)
        for sym in node.uses:
            by_symbol_uses[_normal_symbol(sym)].append(node)

    def_use_edges: list[dict[str, Any]] = []
    for sym, defs in by_symbol_defs.items():
        if not sym or sym not in by_symbol_uses:
            continue
        for source in defs:
            for target in by_symbol_uses[sym]:
                if source.id == target.id:
                    continue
                if source.path == target.path and source.line > target.line:
                    continue
                relation = "same_function_def_use" if source.entity == target.entity else "cross_statement_def_use"
                def_use_edges.append(
                    {
                        "source": source.path,
                        "target": target.path,
                        "source_statement": source.id,
                        "target_statement": target.id,
                        "relation": relation,
                        "symbol": sym,
                    }
                )

    symbol_entities = _entity_symbols(index)
    call_edges: list[dict[str, Any]] = []
    for node in nodes:
        for call in node.calls:
            for entity in symbol_entities.get(_normal_symbol(call), []):
                if _norm(entity.path) == _norm(node.path):
                    continue
                call_edges.append(
                    {
                        "source": node.path,
                        "target": entity.path,
                        "source_statement": node.id,
                        "target_entity": {
                            "path": entity.path,
                            "kind": entity.kind,
                            "name": entity.name,
                            "start_line": entity.start_line,
                            "end_line": entity.end_line,
                        },
                        "relation": "call_boundary_argument_flow",
                        "symbol": call,
                    }
                )

    def dedupe_edges(edges: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
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
            out.append(edge)
            if len(out) >= limit:
                break
        return out

    return dedupe_edges(def_use_edges, limit=120), dedupe_edges(call_edges, limit=80)


def _flow_type(term: str, nodes: list[StatementNode], edges: list[dict[str, Any]]) -> str:
    text = f"{term}\n" + "\n".join(node.code for node in nodes[:30])
    text += "\n" + "\n".join(edge.get("relation", "") for edge in edges)
    low = text.lower()
    if any(token in low for token in ("kdf", "openssh", "private_key", "serialize", "serializer", "backend")):
        return "serializer_backend_static_slice"
    if any(token in low for token in ("binder", "typeinfo", "declaration", "deleted", "narrow")):
        return "python_type_binding_static_slice"
    if any(token in low for token in ("hover", "leave", "mousemove", "mouseout", "legend", "onclick")):
        return "ui_event_static_slice"
    if any(token in low for token in ("stylesheet", "margin", "resolve", "expand", "layout", "style")):
        return "style_pipeline_static_slice"
    if any(token in low for token in ("selector", "state", "dispatch", "reducer", "email_verified")):
        return "state_selector_static_slice"
    if any(token in low for token in ("url", "href", "redirect", "route", "post", "site")):
        return "url_builder_static_slice"
    return "parameter_state_static_slice"


def _candidate_paths(nodes: list[StatementNode], edges: list[dict[str, Any]]) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for node in nodes:
        if node.role in {"reproduction_or_example", "test_or_fixture"}:
            scores[node.path] -= 2.0
        else:
            scores[node.path] += 3.0
        if node.kind in {"assign_from_call", "call_or_argument", "return_value", "condition_or_iteration"}:
            scores[node.path] += 1.2
    for edge in edges:
        target = _norm(str(edge.get("target") or ""))
        source = _norm(str(edge.get("source") or ""))
        if target:
            scores[target] += 2.5
        if source:
            scores[source] += 1.0
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [path for path, score in ranked if score > 0][:24]


def trace_static_slices(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Build ARISE-inspired statement slices for issue localization.

    This backend is intentionally lightweight: it extracts statement nodes,
    local def-use edges, and coarse cross-function call-boundary edges. It is
    not a substitute for ARISE/CodeQL statement-level global dataflow, but it
    gives the agent concrete line-level evidence and a clear upgrade point.
    """

    flows: list[dict[str, Any]] = []
    for term in _flow_terms(issue_text, tool_observations, queries):
        nodes = _nodes_for_term(index, term)
        if len(nodes) < 1:
            continue
        def_use_edges, call_edges = _build_slice_edges(index, nodes)
        all_edges = def_use_edges + call_edges
        actionful = any(node.kind in {"define_or_assign", "assign_from_call", "call_or_argument", "return_value"} for node in nodes)
        if len(nodes) < 2 and not actionful:
            continue
        by_path: dict[str, list[StatementNode]] = defaultdict(list)
        for node in nodes:
            by_path[node.path].append(node)
        roles = {path: _role(path, "\n".join(item.code for item in items[:8])) for path, items in by_path.items()}
        candidate_paths = _candidate_paths(nodes, all_edges)
        edge_summary = {
            "def_use_edges": len(def_use_edges),
            "call_boundary_edges": len(call_edges),
            "paths": len(by_path),
            "statements": len(nodes),
        }
        confidence = min(
            0.99,
            0.30
            + min(0.24, 0.03 * len(by_path))
            + min(0.25, 0.02 * len(def_use_edges))
            + min(0.16, 0.04 * len(call_edges))
            + (0.08 if actionful else 0.0),
        )
        flows.append(
            {
                "term": term,
                "flow_type": _flow_type(term, nodes, all_edges),
                "closure_kind": "statement_level_static_slice",
                "backend": "arise_inspired_static_slice",
                "precision_level": "statement_level_def_use_with_light_call_boundaries",
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
                        "statements": [item.to_dict() for item in items[:10]],
                    }
                    for path, items in sorted(by_path.items())
                ],
                "statement_nodes": [item.to_dict() for item in nodes[:60]],
                "def_use_edges": def_use_edges,
                "call_boundary_edges": call_edges,
                "statement_edges": all_edges,
                "edge_summary": edge_summary,
                "missing_precision_backends": [
                    "codeql_global_dataflow",
                    "scip_lsp_references",
                    "tree_sitter_typed_ast",
                    "daira_runtime_trace",
                ],
                "reason": (
                    f"Term `{term}` has {len(nodes)} statement nodes, "
                    f"{len(def_use_edges)} def-use edges and {len(call_edges)} call-boundary edges."
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
