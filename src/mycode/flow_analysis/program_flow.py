from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List

from mycode.flow_analysis.query_flows import extract_flow_terms
from mycode.repo_index.structure_index import RepositoryIndex, tokenize


CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
CALL_WITH_ARGS_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(([^)]{0,500})\)")
JS_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+\s+from\s+)?|require\()\s*['\"]([^'\"]+)['\"]"
)
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", re.MULTILINE)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.MULTILINE)
EXPORT_SYMBOL_RE = re.compile(
    r"\b(?:export\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)"
)


def _flow_type(term: str, context: str) -> str:
    lower = f"{term} {context}".lower()
    if any(token in lower for token in ("onhover", "onleave", "onclick", "mousemove", "mouseout", "handler")):
        return "ui_event_to_handler"
    if any(token in lower for token in ("selector", "redux", "state", "store", "dispatch", "reducer")):
        return "state_selector_use_chain"
    if any(token in lower for token in ("serialize", "serializer", "backend", "kdf", "private_key", "key_bytes")):
        return "serializer_backend_call_chain"
    if any(token in lower for token in ("option", "config", "parameter", "parser", "rounds", "redirect_to", "client_id")):
        return "parameter_or_config_flow"
    return "shared_symbol_flow"


def _path_language(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".js", ".jsx", ".ts", ".tsx")):
        return "javascript_typescript"
    if lower.endswith((".py", ".pyi")):
        return "python"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith((".css", ".scss", ".less")):
        return "style"
    if lower.endswith((".json", ".yaml", ".yml", ".toml")):
        return "config"
    if lower.endswith((".md", ".mdx")):
        return "docs"
    return "text"


def _path_stems(path: str) -> set[str]:
    parts = path.replace("\\", "/").split("/")
    stems = {part.rsplit(".", 1)[0].lower() for part in parts if part}
    stems.add(path.rsplit(".", 1)[0].replace("/", ".").lower())
    return {stem for stem in stems if stem and stem != "__init__"}


def _import_targets(text: str, path: str) -> list[str]:
    language = _path_language(path)
    imports: list[str] = []
    if language == "javascript_typescript":
        imports.extend(JS_IMPORT_RE.findall(text))
    elif language == "python":
        for module, names in PY_IMPORT_RE.findall(text):
            imports.append(module)
            imports.extend(name.strip() for name in names.split(",") if name.strip())
    elif language == "java":
        imports.extend(JAVA_IMPORT_RE.findall(text))
    else:
        imports.extend(JS_IMPORT_RE.findall(text))
    return list(dict.fromkeys(item for item in imports if item))[:80]


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        str(edge.get("source") or ""),
        str(edge.get("target") or ""),
        str(edge.get("relation") or ""),
        tuple(edge.get("symbols", []) or []),
    )


def _locations_for_term(index: RepositoryIndex, term: str, limit: int) -> list[dict[str, Any]]:
    token = term.lower()
    locations: list[dict[str, Any]] = []
    for entity in index.entities:
        haystack = f"{entity.name}\n{entity.text}".lower()
        if token in haystack:
            locations.append(
                {
                    "path": entity.path,
                    "kind": entity.kind,
                    "name": entity.name,
                    "start_line": entity.start_line,
                    "end_line": entity.end_line,
                    "match_scope": "entity",
                    "snippet": entity.text[:900],
                }
            )
            if len(locations) >= limit:
                return locations
    for path, text in index.files.items():
        if token not in text.lower() and token not in path.lower():
            continue
        line_no = 1
        matched_line = ""
        for idx, line in enumerate(text.splitlines(), start=1):
            if token in line.lower():
                line_no = idx
                matched_line = line.strip()
                break
        locations.append(
            {
                "path": path,
                "kind": "file",
                "name": "",
                "start_line": line_no,
                "end_line": line_no,
                "match_scope": "file_text",
                "snippet": matched_line[:500],
            }
        )
        if len(locations) >= limit:
            return locations
    return locations


def _call_edges(index: RepositoryIndex, locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path_to_entity_names: dict[str, set[str]] = defaultdict(set)
    symbol_to_paths: dict[str, set[str]] = defaultdict(set)
    for entity in index.entities:
        path_to_entity_names[entity.path].add(entity.name)
        symbol_to_paths[entity.name].add(entity.path)

    edges: list[dict[str, Any]] = []
    paths = [item["path"] for item in locations]
    path_stems = {path: _path_stems(path) for path in paths}
    for source in paths:
        text = index.files.get(source, "")
        if not text:
            continue
        calls = set(CALL_RE.findall(text))
        call_args = list(CALL_WITH_ARGS_RE.findall(text))
        imports = _import_targets(text, source)
        exported = set(EXPORT_SYMBOL_RE.findall(text))
        for target in paths:
            if source == target:
                continue
            target_names = path_to_entity_names.get(target, set())
            overlap = sorted(name for name in target_names if name in calls)
            if overlap:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": "calls_symbol_defined_in_target",
                        "symbols": overlap[:12],
                    }
                )
            imported_target = [
                item
                for item in imports
                if any(stem and (stem in item.lower() or item.lower().endswith(stem)) for stem in path_stems[target])
            ]
            if imported_target:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": "imports_target_file_or_module",
                        "symbols": imported_target[:8],
                    }
                )
            shared_exports = sorted(name for name in exported if name in index.files.get(target, ""))
            if shared_exports:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "relation": "exported_symbol_referenced_by_target",
                        "symbols": shared_exports[:8],
                    }
                )
            for call_name, args in call_args[:80]:
                arg_text = args.lower()
                shared_target_names = sorted(name for name in target_names if name == call_name)
                if shared_target_names:
                    continue
                if any(token in arg_text for token in ("kdf", "round", "option", "config", "post", "site", "state")):
                    if any(token in index.files.get(target, "").lower() for token in re.findall(r"[a-zA-Z_][\w$]{2,}", arg_text)):
                        edges.append(
                            {
                                "source": source,
                                "target": target,
                                "relation": "passes_shared_term_through_call_args",
                                "symbols": [call_name, *re.findall(r"[A-Za-z_$][\w$]{2,}", args)[:8]],
                            }
                        )
    deduped: list[dict[str, Any]] = []
    seen = set()
    for edge in edges:
        key = _edge_key(edge)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
        if len(deduped) >= 60:
            break
    return deduped


def _role_for_location(path: str, term: str, text: str) -> str:
    lower = f"{path} {term} {text[:500]}".lower()
    if any(token in lower for token in ("/example", "/examples", "sandbox", "playground", "demo")):
        return "reproduction_or_example"
    if any(token in lower for token in ("/test", "__tests__", "fixture")):
        return "test_or_fixture"
    if any(token in lower for token in ("serializer", "serialize", "/ssh.", "private_key")):
        return "serializer"
    if "backend" in lower:
        return "backend"
    if any(token in lower for token in ("selector", "state", "redux", "reducer", "dispatch")):
        return "state_or_selector"
    if any(token in lower for token in ("component", "view", "page", "route", "dashboard", "reader", "signup")):
        return "component_or_route"
    if any(token in lower for token in ("style", "stylesheet", "resolve", "expand", "margin", "layout")):
        return "style_or_layout_pipeline"
    return "implementation"


def _role_coverage(locations: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for location in locations:
        counts[str(location.get("role") or "unknown")] += 1
    return {
        "roles": dict(sorted(counts.items())),
        "coverage": len(counts),
        "has_evidence_seed": any(
            str(location.get("role") or "") in {"reproduction_or_example", "test_or_fixture"}
            for location in locations
        ),
        "has_target_like_role": any(
            str(location.get("role") or "")
            in {"implementation", "serializer", "backend", "state_or_selector", "component_or_route", "style_or_layout_pipeline"}
            for location in locations
        ),
    }


def _candidate_target_paths(locations: list[dict[str, Any]]) -> list[str]:
    target_roles = {
        "implementation",
        "serializer",
        "backend",
        "state_or_selector",
        "component_or_route",
        "style_or_layout_pipeline",
    }
    return list(
        dict.fromkeys(
            str(location.get("path") or "")
            for location in locations
            if str(location.get("role") or "") in target_roles
        )
    )[:20]


def trace_program_flows(
    index: RepositoryIndex,
    *,
    issue_text: str,
    tool_observations: Iterable[Dict[str, Any]] | None = None,
    queries: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Build explainable, language-aware flow hints from code text and repo structures.

    This is intentionally lightweight: it does not pretend to be CodeQL-level dataflow.
    It finds terms from problem_statement/tool evidence, maps them to entities/files, and
    records likely propagation chains that the dynamic localization agent can use.
    """

    query_terms = list(queries or [])
    terms = set(extract_flow_terms(issue_text, tool_observations))
    for token in tokenize(" ".join(query_terms)):
        if len(token) >= 4 and any(marker in token for marker in ("hover", "leave", "legend", "selector", "serialize", "backend", "round", "config", "option")):
            terms.add(token)

    flows: list[dict[str, Any]] = []
    for term in sorted(terms):
        locations = _locations_for_term(index, term, limit=limit)
        if not locations:
            continue
        context = " ".join(item.get("snippet", "") for item in locations[:4])
        flow_type = _flow_type(term, context)
        for location in locations:
            location["language"] = _path_language(str(location.get("path") or ""))
            location["role"] = _role_for_location(
                str(location.get("path") or ""),
                term,
                str(location.get("snippet") or ""),
            )
        edges = _call_edges(index, locations)
        edge_summary: dict[str, int] = defaultdict(int)
        for edge in edges:
            edge_summary[str(edge.get("relation") or "unknown")] += 1
        confidence = min(0.95, 0.25 + 0.12 * len(locations) + 0.08 * len(edges))
        flows.append(
            {
                "term": term,
                "flow_type": flow_type,
                "backend": "regex_ast_lightweight",
                "precision_level": "best_effort_static_navigation_not_codeql",
                "confidence": round(confidence, 3),
                "locations": locations,
                "edges": edges,
                "edge_summary": dict(sorted(edge_summary.items())),
                "role_coverage": _role_coverage(locations),
                "candidate_target_paths": _candidate_target_paths(locations),
                "missing_precision_backends": ["scip_or_lsp_references", "codeql_dataflow", "tree_sitter_precise_ast"],
                "reason": (
                    f"Term `{term}` appears in {len(locations)} code locations; "
                    f"classified as {flow_type} from issue/tool context."
                ),
            }
        )

    flows.sort(key=lambda item: (-item["confidence"], item["flow_type"], item["term"]))
    return flows[:limit]
