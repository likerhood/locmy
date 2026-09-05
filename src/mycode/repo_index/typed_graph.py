from __future__ import annotations

import posixpath
import re
import ast
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Set

from mycode.repo_index.structure_index import RepositoryIndex, tokenize


JS_IMPORT_RE = re.compile(
    r"(?:import\s+(?P<names>[\w${}\s,*]+?)\s+from\s+|export\s+[^;]*?\s+from\s+|require\s*\()\s*['\"](?P<ref>[^'\"]+)['\"]",
    re.MULTILINE,
)
JS_SIDE_EFFECT_IMPORT_RE = re.compile(r"import\s+['\"](?P<ref>[^'\"]+)['\"]", re.MULTILINE)
JSX_TAG_RE = re.compile(r"<(?P<name>[A-Z][A-Za-z0-9_$]*)\b")
CALL_RE = re.compile(r"\b(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
MEMBER_CALL_RE = re.compile(r"\.\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
JS_MEMBER_RECEIVER_CALL_RE = re.compile(
    r"\b(?P<receiver>[A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
JS_REQUIRE_BIND_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"require\s*\(\s*['\"](?P<ref>[^'\"]+)['\"]\s*\)"
)
JS_EVENT_BIND_RE = re.compile(r"\b(?P<event>on[A-Z][A-Za-z0-9_$]*)\s*=\s*\{\s*(?P<handler>[A-Za-z_$][A-Za-z0-9_$]*)")
JS_HOOK_RE = re.compile(r"\b(?P<name>use[A-Z][A-Za-z0-9_$]*)\s*\(")
JS_ACTION_TYPE_RE = re.compile(r"\b(?P<name>[A-Z][A-Z0-9_]{5,})\b")
JS_ROUTE_COMPONENT_RE = re.compile(
    r"<Route\b[^>]*(?:component\s*=\s*\{\s*(?P<component>[A-Z][A-Za-z0-9_$]*)\s*\}|"
    r"element\s*=\s*\{\s*<(?P<element>[A-Z][A-Za-z0-9_$]*)\b)|"
    r"\b(?:component|element)\s*:\s*(?P<object>[A-Z][A-Za-z0-9_$]*)",
    re.MULTILINE | re.DOTALL,
)
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+(?P<from>[A-Za-z_][\w.]*)\s+import|import\s+(?P<import>[A-Za-z_][\w.]*))", re.MULTILINE)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?(?P<name>[A-Za-z_][\w.]*);", re.MULTILINE)
JAVA_TYPE_EDGE_RE = re.compile(r"\b(?:extends|implements)\s+(?P<names>[A-Za-z_][\w.,\s<>]*)")
JAVA_OVERRIDE_METHOD_RE = re.compile(r"@Override\s+(?:public|protected|private|static|final|synchronized|\s)*[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE)
JAVA_VARIABLE_TYPE_RE = re.compile(
    r"\b(?P<type>[A-Z][A-Za-z0-9_$]*(?:\s*<[^;={}()]+>)?(?:\[\])?)\s+"
    r"(?P<name>[a-z_$][A-Za-z0-9_$]*)\s*(?=[=;,)])"
)
JAVA_MEMBER_CALL_RE = re.compile(
    r"\b(?P<receiver>(?:this\.)?[A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
CSS_SELECTOR_RE = re.compile(r"(?P<prefix>[.#])(?P<name>[A-Za-z_][A-Za-z0-9_-]*)")
CLASSNAME_RE = re.compile(r"className\s*=\s*(?:['\"](?P<literal>[^'\"]+)['\"]|\{[`'\"](?P<expr>[^`'\"]+)[`'\"]\})")
CONFIG_NAME_RE = re.compile(r"[\"'](?P<name>@?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]{3,})[\"']\s*:")


SOURCE_EXTS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".vue", ".svelte"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
STYLE_EXTS = {".css", ".scss", ".less"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml"}
DOC_EXTS = {".md", ".mdx"}
CALL_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new"}
GENERIC_CALL_SYMBOLS = {
    "add", "append", "apply", "build", "call", "close", "constructor", "create", "delete",
    "dispatch", "draw", "emit", "filter", "find", "get", "handle", "init", "load", "map",
    "open", "parse", "process", "push", "read", "remove", "render", "resolve", "run", "save",
    "set", "setup", "sort", "start", "stop", "update", "validate", "write",
}
PATH_NOISE_TOKENS = {
    "app", "client", "component", "components", "index", "lib", "package", "packages", "source", "src",
    "state", "store", "test", "tests", "util", "utils",
}
CONFIG_NOISE_KEYS = {
    "author", "browser", "build", "description", "devdependencies", "directories", "engines", "files",
    "homepage", "keywords", "license", "main", "module", "name", "peerdependencies", "private", "scripts",
    "sideeffects", "type", "types", "version",
}


@dataclass(frozen=True)
class TypedGraphEdge:
    source: str
    target: str
    edge_type: str
    weight: float
    evidence: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/").strip().lstrip("./"))


def _stem_tokens(path: str) -> set[str]:
    parts = re.split(r"[/_.-]+", path.lower())
    return {part for part in parts if len(part) >= 3}


def _semantic_path_tokens(path: str) -> set[str]:
    return _stem_tokens(path) - PATH_NOISE_TOKENS


def _path_language(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}:
        return "javascript/typescript"
    if ext in {".py", ".pyi"}:
        return "python"
    if ext == ".java":
        return "java"
    if ext in STYLE_EXTS:
        return "style"
    if ext in CONFIG_EXTS:
        return "config"
    if ext in DOC_EXTS:
        return "documentation"
    return "other"


class TypedRepositoryGraph:
    """Language-aware heterogeneous repository graph used as an agent navigator.

    The graph stays file-centered for ranking compatibility, but its edges carry
    repository semantics: imports, renders, selector/state usage, config/style
    coupling, calls, inheritance, and documentation links. It is intentionally a
    lightweight extractor rather than a full compiler-grade CPG.
    """

    def __init__(self, index: RepositoryIndex, scope_paths: Iterable[str] | None = None) -> None:
        self.index = index
        self.edges_by_source: Dict[str, list[TypedGraphEdge]] = defaultdict(list)
        self.edges_by_target: Dict[str, list[TypedGraphEdge]] = defaultdict(list)
        self._edge_keys: set[tuple[str, str, str, str]] = set()
        self._lower_file_cache: dict[str, str] = {}
        self._node_token_cache: dict[str, set[str]] = {}
        self._query_terms_cache: dict[tuple[str, ...], list[str]] = {}
        scope = {_norm(path) for path in (scope_paths or []) if path}
        scope = {path for path in scope if path in self.index.files}
        self.scope_paths: set[str] | None = scope or None
        self.graph_files = sorted(self.scope_paths) if self.scope_paths is not None else sorted(self.index.files)
        self.file_languages = {path: _path_language(path) for path in self.graph_files}
        self._build()

    @property
    def edges(self) -> Mapping[str, Set[str]]:
        """Compatibility view for older code/tests expecting untyped neighbors."""
        return {source: {edge.target for edge in edges} for source, edges in self.edges_by_source.items()}

    def _add_edge(self, source: str, target: str, edge_type: str, *, weight: float = 1.0, evidence: str = "") -> None:
        source = _norm(source)
        target = _norm(target)
        if not source or not target or source == target:
            return
        if source not in self.index.files or target not in self.index.files:
            return
        if self.scope_paths is not None and (source not in self.scope_paths or target not in self.scope_paths):
            return
        key = (source, target, edge_type, evidence[:120])
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        edge = TypedGraphEdge(source=source, target=target, edge_type=edge_type, weight=weight, evidence=evidence[:240])
        self.edges_by_source[source].append(edge)
        self.edges_by_target[target].append(edge)

    def _add_bidirectional(self, source: str, target: str, edge_type: str, *, weight: float = 1.0, evidence: str = "") -> None:
        self._add_edge(source, target, edge_type, weight=weight, evidence=evidence)
        self._add_edge(target, source, f"reverse_{edge_type}", weight=max(0.35, weight * 0.72), evidence=evidence)

    def _iter_paths(self) -> Iterable[str]:
        return self.graph_files

    def _iter_file_items(self) -> Iterable[tuple[str, str]]:
        for path in self.graph_files:
            text = self.index.files.get(path)
            if text is not None:
                yield path, text

    def _resolve_relative(self, source: str, ref: str) -> str | None:
        if not ref.startswith("."):
            return None
        base = Path(source).parent
        candidate = posixpath.normpath((base / ref).as_posix())
        suffixes = [
            "",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".mjs",
            ".cjs",
            ".py",
            ".java",
            ".css",
            ".scss",
            ".json",
            "/index.js",
            "/index.jsx",
            "/index.ts",
            "/index.tsx",
            "/__init__.py",
        ]
        for suffix in suffixes:
            normalized = _norm(candidate + suffix)
            if normalized in self.index.files:
                return normalized
        return None

    def _module_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        python_modules: dict[str, str] = {}
        java_types: dict[str, str] = {}
        for path in self._iter_paths():
            ext = Path(path).suffix.lower()
            if ext in {".py", ".pyi"}:
                module = path.rsplit(".", 1)[0].replace("/", ".")
                python_modules[module] = path
                if module.endswith(".__init__"):
                    python_modules[module[: -len(".__init__")]] = path
            if ext == ".java":
                java_types[Path(path).stem] = path
                for entity in self.index.entities:
                    if entity.path == path and entity.kind == "class":
                        java_types[entity.name] = path
        return python_modules, java_types

    def _symbol_file_map(self) -> dict[str, set[str]]:
        symbol_files: dict[str, set[str]] = defaultdict(set)
        for entity in self.index.entities:
            if len(entity.name) >= 3 and (self.scope_paths is None or entity.path in self.scope_paths):
                symbol_files[entity.name].add(entity.path)
        for path in self._iter_paths():
            stem = Path(path).stem
            if len(stem) >= 3:
                symbol_files[stem].add(path)
        return symbol_files

    def _targets_for_symbol(self, symbol_files: Mapping[str, set[str]], name: str, source: str) -> set[str]:
        if not name or len(name) < 3:
            return set()
        return {target for target in symbol_files.get(name, set()) if target != source}

    def _bounded_symbol_targets(
        self,
        symbol_files: Mapping[str, set[str]],
        name: str,
        source: str,
        *,
        max_ambiguous: int = 2,
        allow_distant_unique: bool = True,
    ) -> tuple[list[str], str]:
        """Resolve a call without turning common names into all-to-all edges."""

        if name.lower() in GENERIC_CALL_SYMBOLS:
            return [], "generic_symbol_suppressed"

        targets = sorted(self._targets_for_symbol(symbol_files, name, source))
        if len(targets) <= 1:
            if not targets or allow_distant_unique:
                return targets, "unique_symbol"
            target = targets[0]
            if str(Path(target).parent) == str(Path(source).parent) or (_stem_tokens(source) & _stem_tokens(target)):
                return targets, "path_resolved_unique_symbol"
            return [], "distant_unique_symbol_suppressed"
        source_dir = str(Path(source).parent)
        local = [target for target in targets if str(Path(target).parent) == source_dir]
        if 0 < len(local) <= max_ambiguous:
            return local, "same_directory_symbol"
        source_tokens = _stem_tokens(source)
        related = [target for target in targets if source_tokens & _stem_tokens(target)]
        if 0 < len(related) <= max_ambiguous:
            return related, "path_related_symbol"
        return [], "ambiguous_symbol_suppressed"

    def _build_sibling_edges(self) -> None:
        by_dir: dict[str, list[str]] = defaultdict(list)
        for path in self._iter_paths():
            by_dir[str(Path(path).parent)].append(path)
        for siblings in by_dir.values():
            if len(siblings) <= 18:
                for source in siblings:
                    for target in siblings:
                        if source != target:
                            self._add_edge(source, target, "same_directory", weight=0.35, evidence="small directory neighborhood")

    def _build_js_edges(self, symbol_files: Mapping[str, set[str]]) -> None:
        import_symbols: dict[str, dict[str, str]] = defaultdict(dict)
        path_tokens = {path: _semantic_path_tokens(path) for path in self._iter_paths()}
        selector_state_targets = [
            path
            for path in self._iter_paths()
            if any(token in path.lower() for token in ("selector", "selectors", "/state/", "/store/"))
        ][:350]
        action_state_targets = [
            path
            for path in self._iter_paths()
            if any(token in path.lower() for token in ("action", "actions", "reducer", "reducers", "/state/"))
        ][:350]
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() not in JS_EXTS:
                continue
            matches = list(JS_IMPORT_RE.finditer(text)) + list(JS_SIDE_EFFECT_IMPORT_RE.finditer(text))
            for match in matches:
                ref = match.group("ref")
                if not ref:
                    continue
                target = self._resolve_relative(path, ref)
                if not target:
                    continue
                edge_type = "styles" if Path(target).suffix.lower() in STYLE_EXTS else "imports"
                self._add_bidirectional(path, target, edge_type, weight=1.9, evidence=f"import {ref}")
                raw_names = match.groupdict().get("names") or ""
                for name in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", raw_names):
                    if name not in {"from", "as", "import", "default", "type"}:
                        import_symbols[path][name] = target
            for match in JS_REQUIRE_BIND_RE.finditer(text):
                target = self._resolve_relative(path, match.group("ref"))
                if target:
                    import_symbols[path][match.group("name")] = target

            jsx_tags = {m.group("name") for m in JSX_TAG_RE.finditer(text)}
            for name, target in import_symbols.get(path, {}).items():
                if name in jsx_tags:
                    self._add_bidirectional(path, target, "renders", weight=2.8, evidence=f"JSX tag <{name}>")
                if JS_HOOK_RE.search(text) and name.startswith("use"):
                    self._add_bidirectional(path, target, "uses_hook", weight=2.35, evidence=f"hook {name}()")
                if re.search(rf"\b{re.escape(name)}\s*\(", text) and (
                    name.lower().startswith(("select", "getstate"))
                    or any(token in target.lower() for token in ("selector", "/state/", "/store/"))
                ):
                    self._add_bidirectional(path, target, "selects_state", weight=2.65, evidence=f"resolved selector import {name}")
                if re.search(rf"\b{re.escape(name)}\s*\(", text) and (
                    name.lower().startswith(("dispatch", "set", "update", "remove", "add"))
                    and any(token in target.lower() for token in ("action", "reducer", "/state/", "/store/"))
                ):
                    self._add_bidirectional(path, target, "dispatches_action", weight=2.45, evidence=f"resolved action import {name}")

            for event_match in JS_EVENT_BIND_RE.finditer(text):
                handler = event_match.group("handler")
                event = event_match.group("event")
                for target in self._targets_for_symbol(symbol_files, handler, path):
                    self._add_bidirectional(path, target, "binds_ui_event", weight=2.4, evidence=f"{event} -> {handler}")

            for route_match in JS_ROUTE_COMPONENT_RE.finditer(text):
                component = route_match.group("component") or route_match.group("element") or route_match.group("object")
                target = import_symbols.get(path, {}).get(component or "")
                if target:
                    self._add_bidirectional(path, target, "routes_to", weight=2.7, evidence=f"route maps to {component}")

            source_lower = f"{path}\n{text}".lower()
            if any(token in source_lower for token in ("useselector", "mapstatetoprops", "selector", "select", "reselect")):
                coupled = 0
                for target in selector_state_targets:
                    overlap = path_tokens.get(path, set()) & path_tokens.get(target, set())
                    same_dir = Path(path).parent == Path(target).parent
                    if len(overlap) >= 2 or (same_dir and overlap):
                        self._add_bidirectional(path, target, "selects_state", weight=1.25, evidence="local selector/state path coupling")
                        coupled += 1
                        if coupled >= 12:
                            break

            if any(token in source_lower for token in ("dispatch", "action", "reducer")):
                coupled = 0
                for target in action_state_targets:
                    overlap = path_tokens.get(path, set()) & path_tokens.get(target, set())
                    same_dir = Path(path).parent == Path(target).parent
                    if len(overlap) >= 2 or (same_dir and overlap):
                        self._add_bidirectional(path, target, "dispatches_action", weight=1.15, evidence="local dispatch/action path coupling")
                        coupled += 1
                        if coupled >= 12:
                            break

            direct_calls = {
                match.group("name")
                for match in CALL_RE.finditer(text)
                if match.group("name") not in CALL_KEYWORDS
                and (match.start() == 0 or text[match.start() - 1] not in ".$")
            }
            member_calls = {
                match.group("name")
                for match in MEMBER_CALL_RE.finditer(text)
                if match.group("name") not in CALL_KEYWORDS
            }
            for match in JS_MEMBER_RECEIVER_CALL_RE.finditer(text):
                receiver = match.group("receiver")
                target = import_symbols.get(path, {}).get(receiver)
                if target:
                    self._add_bidirectional(
                        path,
                        target,
                        "calls",
                        weight=3.1,
                        evidence=f"resolved imported member call {receiver}.{match.group('name')}()",
                    )
            calls = direct_calls | member_calls
            for call in direct_calls:
                imported_target = import_symbols.get(path, {}).get(call)
                if imported_target:
                    self._add_bidirectional(
                        path,
                        imported_target,
                        "calls",
                        weight=3.0,
                        evidence=f"resolved imported call {call}()",
                    )
                    continue
                targets, resolution = self._bounded_symbol_targets(
                    symbol_files,
                    call,
                    path,
                    allow_distant_unique=False,
                )
                for target in targets:
                    self._add_bidirectional(
                        path,
                        target,
                        "calls",
                        weight=2.0 if resolution == "unique_symbol" else 1.35,
                        evidence=f"{resolution} call {call}()",
                    )

            if any(token in source_lower for token in ("useeffect", "usestate", "usememo", "usecallback")):
                for call in calls:
                    imported_target = import_symbols.get(path, {}).get(call)
                    targets = [imported_target] if imported_target else self._bounded_symbol_targets(
                        symbol_files,
                        call,
                        path,
                        allow_distant_unique=False,
                    )[0]
                    for target in targets:
                        if not target:
                            continue
                        self._add_bidirectional(path, target, "hook_flow", weight=2.15, evidence=f"hook context calls {call}()")

    def _build_js_action_flow_edges(self) -> None:
        """Connect Redux-style action constants to handlers and data layers.

        These relationships are convention based rather than direct function
        calls.  They are especially important for feature requests where the UI
        call path does not exist yet but actions/reducers/data-layer handlers do.
        """

        action_files: dict[str, set[str]] = defaultdict(set)
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() not in JS_EXTS:
                continue
            for name in set(match.group("name") for match in JS_ACTION_TYPE_RE.finditer(text)):
                action_files[name].add(path)
        for name, paths in action_files.items():
            if not 2 <= len(paths) <= 12:
                continue
            if not any(
                any(marker in path.lower() for marker in ("action", "reducer", "data-layer", "/state/"))
                for path in paths
            ):
                continue
            ordered = sorted(paths)
            for source in ordered:
                for target in ordered:
                    if source == target:
                        continue
                    self._add_edge(
                        source,
                        target,
                        "handles_action",
                        weight=2.75,
                        evidence=f"shared Redux/action constant {name}",
                    )
    def _build_python_edges(self, python_modules: Mapping[str, str], symbol_files: Mapping[str, set[str]]) -> None:
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() not in {".py", ".pyi"}:
                continue
            alias_to_path: dict[str, str] = {}
            for match in PY_IMPORT_RE.finditer(text):
                module = match.group("from") or match.group("import") or ""
                target = python_modules.get(module)
                if not target:
                    target = next((item_path for item_module, item_path in python_modules.items() if item_module.endswith("." + module)), None)
                if target:
                    self._add_bidirectional(path, target, "imports", weight=1.8, evidence=f"python import {module}")
                    alias_to_path[module.rsplit(".", 1)[-1]] = target

            for call in self._python_calls(text):
                target = alias_to_path.get(call.get("base") or "")
                if target:
                    self._add_bidirectional(path, target, "calls", weight=2.8, evidence=f"resolved python module call {call['display']}")
                    continue
                if call.get("attr"):
                    continue
                symbol = call.get("name") or ""
                targets, resolution = self._bounded_symbol_targets(symbol_files, symbol, path)
                for item_path in targets:
                    self._add_bidirectional(
                        path,
                        item_path,
                        "calls",
                        weight=1.9 if resolution == "unique_symbol" else 1.25,
                        evidence=f"python {resolution} call {call['display']}",
                    )
            for base_name in self._python_base_names(text):
                for target in self._targets_for_symbol(symbol_files, base_name, path):
                    self._add_bidirectional(path, target, "inherits_or_implements", weight=2.4, evidence=f"python class base {base_name}")
            for decorator in self._python_decorator_names(text):
                for target in self._targets_for_symbol(symbol_files, decorator, path):
                    self._add_bidirectional(path, target, "decorates", weight=2.0, evidence=f"python decorator @{decorator}")
            if any(token in text.lower() for token in ("binder", "typeinfo", "symboltable", "typevar", "typetype", "narrow")):
                for target in self._iter_paths():
                    if target != path and any(token in target.lower() for token in ("binder", "type", "checker", "nodes", "semanal")):
                        self._add_bidirectional(path, target, "type_flow", weight=2.0, evidence="python type/binder concern coupling")

    def _python_calls(self, text: str) -> list[dict[str, str]]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [
                {"name": match.group("name"), "attr": "", "base": "", "display": f"{match.group('name')}()"}
                for match in CALL_RE.finditer(text)
            ]
        calls: list[dict[str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                calls.append({"name": func.id, "attr": "", "base": "", "display": f"{func.id}()"})
            elif isinstance(func, ast.Attribute):
                base = ""
                if isinstance(func.value, ast.Name):
                    base = func.value.id
                calls.append({"name": func.attr, "attr": func.attr, "base": base, "display": f"{base + '.' if base else ''}{func.attr}()"})
        return calls

    def _python_base_names(self, text: str) -> set[str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Name):
                    names.add(base.id)
                elif isinstance(base, ast.Attribute):
                    names.add(base.attr)
        return names

    def _python_decorator_names(self, text: str) -> set[str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        names: set[str] = set()

        def add_name(node: ast.AST) -> None:
            if isinstance(node, ast.Call):
                add_name(node.func)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for decorator in node.decorator_list:
                add_name(decorator)
        return names

    def _build_java_edges(self, java_types: Mapping[str, str], symbol_files: Mapping[str, set[str]]) -> None:
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() != ".java":
                continue
            implemented_type_paths: set[str] = set()
            receiver_types: dict[str, str] = {}
            for match in JAVA_IMPORT_RE.finditer(text):
                type_name = match.group("name").split(".")[-1]
                target = java_types.get(type_name)
                if target:
                    self._add_bidirectional(path, target, "imports", weight=1.7, evidence=f"java import {match.group('name')}")
            for match in JAVA_VARIABLE_TYPE_RE.finditer(text):
                type_name = re.sub(r"\s*<.*>|\[\]", "", match.group("type")).strip()
                target = java_types.get(type_name)
                if target and target != path:
                    receiver_types[match.group("name")] = target
            for match in JAVA_MEMBER_CALL_RE.finditer(text):
                raw_receiver = match.group("receiver")
                receiver = raw_receiver.split(".")[-1]
                target = receiver_types.get(receiver)
                if target is None and receiver[:1].isupper():
                    target = java_types.get(receiver)
                if not target or target == path:
                    continue
                method = match.group("name")
                self._add_bidirectional(
                    path,
                    target,
                    "calls",
                    weight=3.25,
                    evidence=f"java receiver-resolved call {raw_receiver}.{method}()",
                )
            for match in JAVA_TYPE_EDGE_RE.finditer(text):
                for type_name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group("names")):
                    target = java_types.get(type_name)
                    if target:
                        implemented_type_paths.add(target)
                        self._add_bidirectional(path, target, "inherits_or_implements", weight=2.8, evidence=f"extends/implements {type_name}")
            for match in JAVA_OVERRIDE_METHOD_RE.finditer(text):
                name = match.group("name")
                for target in implemented_type_paths:
                    self._add_bidirectional(path, target, "overrides", weight=2.4, evidence=f"@Override {name}() on implemented type")
                for target in self._targets_for_symbol(symbol_files, name, path):
                    self._add_bidirectional(path, target, "overrides", weight=2.6, evidence=f"@Override {name}()")
            calls = {
                match.group("name")
                for match in CALL_RE.finditer(text)
                if match.group("name") not in CALL_KEYWORDS
            }
            for call in calls:
                targets, resolution = self._bounded_symbol_targets(
                    symbol_files,
                    call,
                    path,
                    allow_distant_unique=False,
                )
                for target in targets:
                    self._add_bidirectional(
                        path,
                        target,
                        "calls",
                        weight=1.8 if resolution == "unique_symbol" else 1.2,
                        evidence=f"java {resolution} method call {call}()",
                    )

    def _build_style_config_doc_edges(self) -> None:
        selector_to_style: dict[str, set[str]] = defaultdict(set)
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() in STYLE_EXTS:
                for match in CSS_SELECTOR_RE.finditer(text):
                    selector_to_style[match.group("name")].add(path)

        searchable_files = {
            path: f"{path}\n{text[:120000]}".lower()
            for path, text in self._iter_file_items()
        }
        for path, text in self._iter_file_items():
            if Path(path).suffix.lower() in JS_EXTS:
                classes: set[str] = set()
                for match in CLASSNAME_RE.finditer(text):
                    value = match.group("literal") or match.group("expr") or ""
                    classes.update(part for part in re.split(r"\s+", value) if part)
                for class_name in classes:
                    for target in selector_to_style.get(class_name, set()):
                        self._add_bidirectional(path, target, "styles", weight=2.5, evidence=f"className {class_name}")

            if Path(path).suffix.lower() in CONFIG_EXTS or Path(path).name.lower() in {"package.json", "tsconfig.json", "pyproject.toml"}:
                names = [
                    m.group("name")
                    for m in CONFIG_NAME_RE.finditer(text)
                    if m.group("name").lower() not in CONFIG_NOISE_KEYS and len(m.group("name")) >= 5
                ]
                for name in names[:80]:
                    needle = name.lower()
                    matched = 0
                    for target, haystack in searchable_files.items():
                        if target == path:
                            continue
                        if needle in haystack:
                            self._add_bidirectional(path, target, "configures", weight=1.4, evidence=f"config key/package {name}")
                            matched += 1
                            if matched >= 16:
                                break

            if Path(path).suffix.lower() in DOC_EXTS:
                for target in self._iter_paths():
                    if target == path:
                        continue
                    target_name = Path(target).name
                    if target_name in text or Path(target).stem in text:
                        self._add_bidirectional(path, target, "documents", weight=1.1, evidence=f"doc references {target_name}")

    def _build(self) -> None:
        symbol_files = self._symbol_file_map()
        python_modules, java_types = self._module_maps()
        self._build_sibling_edges()
        self._build_js_edges(symbol_files)
        self._build_js_action_flow_edges()
        self._build_python_edges(python_modules, symbol_files)
        self._build_java_edges(java_types, symbol_files)
        self._build_style_config_doc_edges()

    def _query_terms(self, query_terms: Iterable[str], *, max_terms: int = 64) -> list[str]:
        key = tuple(str(term or "") for term in query_terms)
        cached = self._query_terms_cache.get(key)
        if cached is not None:
            return cached[:max_terms]
        terms: list[str] = []
        seen_terms: set[str] = set()
        for term in tokenize(" ".join(key)):
            if len(term) < 3 or term in seen_terms:
                continue
            seen_terms.add(term)
            terms.append(term)
            if len(terms) >= max_terms:
                break
        self._query_terms_cache[key] = terms
        return terms

    def _node_text(self, path: str) -> str:
        text = self._lower_file_cache.get(path)
        if text is None:
            entity_names = " ".join(entity.name for entity in self.index.entities if entity.path == path)
            text = f"{path} {entity_names} {self.index.files.get(path, '')}".lower()
            self._lower_file_cache[path] = text
        return text

    def _node_tokens(self, path: str) -> set[str]:
        tokens = self._node_token_cache.get(path)
        if tokens is None:
            tokens = set(tokenize(self._node_text(path)))
            self._node_token_cache[path] = tokens
        return tokens

    def expand(
        self,
        seeds: Iterable[str],
        query_terms: Iterable[str],
        limit: int = 20,
        allowed_edge_types: Iterable[str] | None = None,
        *,
        max_seed_nodes: int = 16,
        max_edges_per_seed: int = 120,
        beam_width: int = 160,
    ) -> list[tuple[str, float, list[str]]]:
        terms = self._query_terms(query_terms)
        term_set = set(terms)
        allowed = set(allowed_edge_types or [])
        scored: MutableMapping[str, tuple[float, list[str]]] = {}
        seen_seeds: set[str] = set()
        for raw_seed in seeds:
            seed = _norm(raw_seed)
            if not seed or seed in seen_seeds:
                continue
            seen_seeds.add(seed)
            if len(seen_seeds) > max_seed_nodes:
                break
            outgoing = list(self.edges_by_source.get(seed, [])) + [
                TypedGraphEdge(edge.target, edge.source, f"incoming_{edge.edge_type}", edge.weight * 0.7, edge.evidence)
                for edge in self.edges_by_target.get(seed, [])
            ]
            outgoing.sort(key=lambda edge: (-edge.weight, edge.edge_type, edge.target))
            for edge in outgoing[:max_edges_per_seed]:
                edge_type = edge.edge_type.replace("incoming_", "")
                if allowed and edge_type not in allowed and edge.edge_type not in allowed:
                    continue
                token_hits = term_set & self._node_tokens(edge.target)
                term_bonus = min(5.4, 0.45 * len(token_hits))
                semantic_bonus = 0.4 if edge_type in {"calls", "renders", "selects_state", "type_flow", "inherits_or_implements"} else 0.0
                score = edge.weight + term_bonus + semantic_bonus
                reasons = [f"{edge.edge_type}:{seed}"]
                if edge.evidence:
                    reasons.append(edge.evidence)
                current = scored.get(edge.target)
                if current is None:
                    scored[edge.target] = (score, reasons)
                else:
                    best_score = max(score, current[0])
                    merged_reasons = list(current[1])
                    for reason in reasons:
                        if reason not in merged_reasons:
                            merged_reasons.append(reason)
                    scored[edge.target] = (best_score, merged_reasons[:8])
                if len(scored) >= max(beam_width, limit):
                    threshold_items = sorted(scored.items(), key=lambda item: (-item[1][0], item[0]))[:beam_width]
                    scored = dict(threshold_items)
        ranked = [(path, score, reasons) for path, (score, reasons) in scored.items()]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[:limit]

    def edge_summary(self, paths: Iterable[str] | None = None) -> dict[str, object]:
        selected = {_norm(path) for path in paths or [] if path}
        edges: list[TypedGraphEdge] = []
        if selected:
            for path in selected:
                edges.extend(self.edges_by_source.get(path, []))
                edges.extend(self.edges_by_target.get(path, []))
        else:
            for group in self.edges_by_source.values():
                edges.extend(group)
        counts = Counter(edge.edge_type for edge in edges)
        return {
            "graph_type": "typed_heterogeneous_repository_graph",
            "file_count": len(self.index.files),
            "graph_file_count": len(self.graph_files),
            "scope_limited": self.scope_paths is not None,
            "edge_count": sum(len(group) for group in self.edges_by_source.values()),
            "language_counts": dict(Counter(self.file_languages.values())),
            "edge_type_counts": dict(sorted(counts.items())),
            "sample_edges": [edge.to_dict() for edge in edges[:12]],
        }
