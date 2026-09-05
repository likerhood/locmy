from __future__ import annotations

import json
import re
import ast
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from mycode.repo_index.repo_locator import find_repo_root, find_repo_structure


TEXT_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h",
    ".css", ".scss", ".less", ".json", ".yaml", ".yml", ".md", ".mdx",
    ".html", ".vue", ".svelte",
}
SKIP_DIRS = {
    ".git", "node_modules", ".cache", "dist", "build", "coverage", ".next",
    "__pycache__", "target", ".tox", ".venv",
}
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$-]*")
JS_METHOD_RE = re.compile(
    r"^[ \t]*(?:async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    re.MULTILINE,
)
JS_FUNCTION_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(",
    re.MULTILINE,
)
JS_CONST_FUNCTION_RE = re.compile(
    r"^[ \t]*(?:export\s+)?const\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.MULTILINE,
)
JS_ASSIGNED_FUNCTION_RE = re.compile(
    r"^[ \t]*(?:[A-Za-z_$][\w$]*\.)+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\s*\(",
    re.MULTILINE,
)
JS_ASSIGNED_ARROW_RE = re.compile(
    r"^[ \t]*(?:[A-Za-z_$][\w$]*\.)+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.MULTILINE,
)
JS_CLASS_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b",
    re.MULTILINE,
)
PY_FUNCTION_RE = re.compile(r"^[ \t]*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE)
PY_CLASS_RE = re.compile(r"^[ \t]*class\s+(?P<name>[A-Za-z_]\w*)\b", re.MULTILINE)
JAVA_CLASS_RE = re.compile(
    r"^[ \t]*(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_$][\w$]*)\b",
    re.MULTILINE,
)
JAVA_CONTROL_PREFIXES = {
    "assert", "catch", "do", "else", "for", "if", "new", "return", "switch",
    "synchronized", "throw", "try", "while", "yield",
}
JAVA_METHOD_MODIFIERS = {
    "abstract", "default", "final", "native", "private", "protected", "public",
    "static", "strictfp", "synchronized",
}
JAVA_DECLARATION_SUFFIX_RE = re.compile(r"^(?:throws\s+[^{};]+\s*)?[{;]$")


@dataclass
class CodeEntity:
    path: str
    kind: str
    name: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SearchHit:
    path: str
    score: float
    reasons: List[str]
    kind: str = "file"
    name: str = ""
    start_line: int = 0
    end_line: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.findall(text or ""):
        token = match.lower().strip("._-$")
        if len(token) >= 2:
            tokens.append(token)
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", match).lower().split()
        tokens.extend(part for part in parts if len(part) >= 2)
    return tokens


def _read_text(path: Path, max_bytes: int = 1_000_000) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _iter_repo_files(repo_root: Path) -> Iterable[tuple[str, str]]:
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(repo_root).parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        text = _read_text(path)
        if text:
            yield path.relative_to(repo_root).as_posix(), text


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_c_style_comments(text: str) -> str:
    """Mask // and /* */ comments while preserving offsets and line numbers."""

    text = re.sub(
        r"/\*[\s\S]*?\*/",
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        text,
    )
    chars = list(text)
    state = "code"
    idx = 0
    while idx < len(chars):
        char = chars[idx]
        nxt = chars[idx + 1] if idx + 1 < len(chars) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                chars[idx] = chars[idx + 1] = " "
                state = "line_comment"
                idx += 2
                continue
            if char == "/" and nxt == "*":
                chars[idx] = chars[idx + 1] = " "
                state = "block_comment"
                idx += 2
                continue
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == "`":
                state = "template"
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                chars[idx] = " "
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                chars[idx] = chars[idx + 1] = " "
                state = "code"
                idx += 2
                continue
            if char != "\n":
                chars[idx] = " "
        else:
            if char == "\\":
                idx += 2
                continue
            if char == "\n" and state in {"single_quote", "double_quote"}:
                state = "code"
                idx += 1
                continue
            if (state == "single_quote" and char == "'") or (
                state == "double_quote" and char == '"'
            ) or (state == "template" and char == "`"):
                state = "code"
        idx += 1
    return "".join(chars)


def _brace_end_line(text: str, start_offset: int) -> int:
    first_brace = text.find("{", start_offset)
    if first_brace < 0:
        return _line_of(text, start_offset)
    depth = 0
    for idx in range(first_brace, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth <= 0:
                return _line_of(text, idx)
    return text.count("\n") + 1


def _indent_end_line(lines: list[str], start_line: int) -> int:
    if not lines:
        return start_line
    base = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
    end = start_line
    for idx in range(start_line, len(lines)):
        raw = lines[idx]
        if raw.strip() and len(raw) - len(raw.lstrip()) <= base:
            break
        end = idx + 1
    return end


def _python_ast_entities(path: str, text: str) -> list[CodeEntity]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    entities: list[CodeEntity] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
        else:
            continue
        start = int(getattr(node, "lineno", 1) or 1)
        end = int(getattr(node, "end_lineno", 0) or _indent_end_line(lines, start))
        snippet = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
        entities.append(CodeEntity(path, kind, node.name, start, end, snippet))
    entities.sort(key=lambda item: (item.start_line, item.kind, item.name))
    return entities


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    for idx in range(open_index, len(text)):
        char = text[idx]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _java_declaration_name(header: str, class_names: set[str]) -> str:
    """Return a Java method/constructor name from one bounded declaration header."""

    normalized = " ".join(header.split())
    if not normalized or "->" in normalized:
        return ""
    open_index = normalized.find("(")
    if open_index < 0:
        return ""
    close_index = _matching_paren(normalized, open_index)
    if close_index < 0:
        return ""
    suffix = normalized[close_index + 1:].strip()
    if not JAVA_DECLARATION_SUFFIX_RE.fullmatch(suffix):
        return ""

    before = normalized[:open_index].strip()
    name_match = re.search(r"(?P<name>[A-Za-z_$][\w$]*)\s*$", before)
    if not name_match:
        return ""
    name = name_match.group("name")
    prefix = before[:name_match.start()].strip()
    if "=" in prefix or prefix.endswith((".", "::")):
        return ""
    first_token = prefix.split(None, 1)[0].lstrip("@") if prefix else ""
    if first_token in JAVA_CONTROL_PREFIXES:
        return ""

    # Constructors do not have a return type. Accept them only when their name
    # matches a class declared in this file; ordinary methods need a return type
    # or a declaration modifier before their name.
    prefix_tokens = {
        token
        for token in re.findall(r"[A-Za-z_$][\w$-]*", prefix)
        if not token.startswith("Override")
    }
    has_modifier = bool(prefix_tokens & JAVA_METHOD_MODIFIERS)
    has_return_type = bool(prefix_tokens - JAVA_METHOD_MODIFIERS)
    if name not in class_names and not (has_modifier or has_return_type):
        return ""
    return name


def _java_source_entities(path: str, text: str) -> list[CodeEntity]:
    """Extract Java entities without any unbounded whole-file method regex."""

    scan_text = _mask_c_style_comments(text)
    lines = text.splitlines()
    scan_lines = scan_text.splitlines(keepends=True)
    line_offsets: list[int] = []
    offset = 0
    for line in scan_lines:
        line_offsets.append(offset)
        offset += len(line)

    entities: list[CodeEntity] = []
    seen: set[tuple[str, str, int]] = set()
    class_names: set[str] = set()
    for match in JAVA_CLASS_RE.finditer(scan_text):
        name = match.group("name")
        start = _line_of(scan_text, match.start())
        end = _brace_end_line(scan_text, match.start())
        class_names.add(name)
        seen.add(("class", name, start))
        snippet = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
        entities.append(CodeEntity(path, "class", name, start, end, snippet))

    # Java declarations may span several lines. Assemble only a small header
    # window and stop at the first body/semicolon, keeping worst-case work linear.
    for start_index, raw_line in enumerate(scan_lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "@", "{", "}")):
            continue
        header_parts: list[str] = []
        header_chars = 0
        terminal_index = -1
        for current_index in range(start_index, min(len(scan_lines), start_index + 12)):
            part = scan_lines[current_index].strip()
            header_parts.append(part)
            header_chars += len(part)
            if header_chars > 4096:
                break
            if "{" in part or ";" in part:
                terminal_index = current_index
                break
        if terminal_index < 0 or "(" not in " ".join(header_parts):
            continue
        header = " ".join(header_parts)
        terminators = [idx for idx in (header.find("{"), header.find(";")) if idx >= 0]
        if not terminators:
            continue
        header = header[:min(terminators) + 1]
        name = _java_declaration_name(header, class_names)
        if not name:
            continue
        start = start_index + 1
        kind = "method"
        key = (kind, name, start)
        if key in seen:
            continue
        seen.add(key)
        if header.rstrip().endswith("{"):
            start_offset = line_offsets[start_index] if start_index < len(line_offsets) else 0
            end = _brace_end_line(scan_text, start_offset)
        else:
            end = terminal_index + 1
        if end <= start:
            end = min(len(lines), start + 80)
        snippet = "\n".join(lines[start - 1:min(len(lines), end)])
        entities.append(CodeEntity(path, kind, name, start, end, snippet))

    entities.sort(key=lambda item: (item.start_line, item.kind, item.name))
    return entities


def _extract_source_entities(path: str, text: str) -> list[CodeEntity]:
    ext = Path(path).suffix.lower()
    if ext in {".py", ".pyi"}:
        ast_entities = _python_ast_entities(path, text)
        if ast_entities:
            return ast_entities
    if ext == ".java":
        return _java_source_entities(path, text)

    patterns = []
    class_patterns = []
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        patterns = [JS_FUNCTION_RE, JS_CONST_FUNCTION_RE, JS_ASSIGNED_FUNCTION_RE, JS_ASSIGNED_ARROW_RE, JS_METHOD_RE]
        class_patterns = [JS_CLASS_RE]
    elif ext in {".py", ".pyi"}:
        patterns = [PY_FUNCTION_RE]
        class_patterns = [PY_CLASS_RE]
    scan_text = (
        _mask_c_style_comments(text)
        if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".c", ".h"}
        else text
    )
    entities: list[CodeEntity] = []
    seen = set()
    lines = text.splitlines()
    for pattern in class_patterns:
        for match in pattern.finditer(scan_text):
            name = match.group("name")
            start = _line_of(text, match.start())
            end = _brace_end_line(text, match.start()) if ext != ".py" else _indent_end_line(lines, start)
            key = ("class", name, start)
            if key in seen:
                continue
            seen.add(key)
            snippet = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
            entities.append(CodeEntity(path, "class", name, start, end, snippet))
    for pattern in patterns:
        for match in pattern.finditer(scan_text):
            name = match.group("name")
            if name in {"if", "for", "while", "switch", "catch", "function"}:
                continue
            start = _line_of(text, match.start())
            end = _brace_end_line(text, match.start()) if ext != ".py" else _indent_end_line(lines, start)
            if end <= start:
                end = min(len(lines), start + 80)
            key = ("function", name, start)
            if key in seen:
                continue
            seen.add(key)
            snippet = "\n".join(lines[start - 1:end])
            entities.append(CodeEntity(path, "function", name, start, end, snippet))
    entities.sort(key=lambda item: (item.start_line, item.kind, item.name))
    return entities


def _flatten_structure_node(path: str, node: Any) -> Iterable[tuple[str, Dict[str, Any]]]:
    if not isinstance(node, dict):
        return
    if "text" in node and ("functions" in node or "classes" in node):
        yield path, node
        return
    for key, value in node.items():
        child = f"{path}/{key}" if path else str(key)
        yield from _flatten_structure_node(child, value)


def _entities_from_structure(path: str, node: Dict[str, Any]) -> list[CodeEntity]:
    text = str(node.get("text") or "")
    lines = text.splitlines()
    ext = Path(path).suffix.lower()
    code_lines = (
        _mask_c_style_comments(text).splitlines()
        if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".c", ".h"}
        else lines
    )
    entities: list[CodeEntity] = []
    structured_callable_count = 0
    kind_map = {"functions": "function", "classes": "class"}
    for kind in ("functions", "classes"):
        for item in node.get(kind, []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            start = int(item.get("start_line") or item.get("line") or 1)
            if 0 < start <= len(code_lines) and not code_lines[start - 1].strip():
                continue
            end = int(item.get("end_line") or min(len(lines), start + 80) or start)
            snippet = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
            entities.append(CodeEntity(path, kind_map[kind], name, start, end, snippet))
            if kind == "functions":
                structured_callable_count += 1

            if kind != "classes":
                continue
            for method in item.get("methods", []) or []:
                if not isinstance(method, dict):
                    continue
                method_name = str(method.get("name") or "")
                if not method_name:
                    continue
                method_start = int(method.get("start_line") or method.get("line") or start)
                method_end = int(method.get("end_line") or min(len(lines), method_start + 80) or method_start)
                method_snippet = "\n".join(
                    lines[max(0, method_start - 1):min(len(lines), method_end)]
                )
                entities.append(
                    CodeEntity(path, "method", method_name, method_start, method_end, method_snippet)
                )
                structured_callable_count += 1

    # A benchmark structure snapshot is canonical and already contains parsed
    # entities. Re-scanning every large source file both changes that snapshot's
    # semantics and used to trigger catastrophic Java regex backtracking. Keep a
    # source fallback only for snapshots that contain no callable information.
    if structured_callable_count:
        unique = {
            (entity.kind, entity.name, entity.start_line): entity
            for entity in entities
        }
        return sorted(unique.values(), key=lambda item: (item.start_line, item.kind, item.name))

    known = {(entity.kind, entity.name, entity.start_line) for entity in entities}
    known_names = {(entity.kind, entity.name) for entity in entities}
    for entity in _extract_source_entities(path, text):
        key = (entity.kind, entity.name, entity.start_line)
        if key in known or (entity.kind, entity.name) in known_names:
            continue
        entities.append(entity)
        known.add(key)
        known_names.add((entity.kind, entity.name))
    entities.sort(key=lambda item: (item.start_line, item.kind, item.name))
    return entities


class RepositoryIndex:
    def __init__(
        self,
        *,
        repo: str,
        instance_id: str,
        dataset: str = "",
        repo_root: Optional[Path] = None,
        structure_path: Optional[Path] = None,
    ) -> None:
        self.repo = repo
        self.instance_id = instance_id
        self.dataset = dataset
        self.repo_root = repo_root or find_repo_root(repo, dataset)
        self.structure_path = structure_path or find_repo_structure(instance_id, dataset)
        self.files: Dict[str, str] = {}
        self.entities: list[CodeEntity] = []
        self._load()

    def _load(self) -> None:
        # repo_structures are generated from the benchmark instance's base
        # commit. A shared checkout can point at a different commit, so the
        # structure snapshot must be canonical whenever it is available.
        if self.structure_path and self.structure_path.exists():
            data = json.loads(self.structure_path.read_text(encoding="utf-8"))
            for path, node in _flatten_structure_node("", data.get("structure", {})):
                text = str(node.get("text") or "")
                if text:
                    self.files[path] = text
                self.entities.extend(_entities_from_structure(path, node))
        if self.repo_root and self.repo_root.exists():
            for path, text in _iter_repo_files(self.repo_root):
                if path in self.files:
                    continue
                self.files[path] = text
                self.entities.extend(_extract_source_entities(path, text))

    @property
    def ready(self) -> bool:
        return bool(self.files)

    def search_files(self, queries: Iterable[str], limit: int = 20) -> list[SearchHit]:
        query_list = list(queries)
        query_tokens = tokenize(" ".join(query_list))
        token_counts = {
            token: 1.0 + min(1.0, math.log2(max(1, query_tokens.count(token))) * 0.25)
            for token in set(query_tokens)
        }
        hits: list[SearchHit] = []
        for path, text in self.files.items():
            lower_path = path.lower()
            lower_text = text.lower()
            score = 0.0
            reasons: list[str] = []
            for token, weight in token_counts.items():
                if token in lower_path:
                    score += 8.0 * weight
                    reasons.append(f"path:{token}")
                count = lower_text.count(token)
                if count:
                    score += min(count, 12) * weight
                    if len(reasons) < 8:
                        reasons.append(f"text:{token}x{min(count, 12)}")
            for query in query_list:
                phrase = query.lower().strip()
                if len(phrase) > 4 and phrase in lower_text:
                    score += 5.0
                    if len(reasons) < 8:
                        reasons.append(f"phrase:{phrase[:40]}")
            if score > 0:
                hits.append(SearchHit(path=path, score=score, reasons=reasons))
        hits.sort(key=lambda item: (-item.score, item.path))
        return hits[:limit]

    def search_entities(self, queries: Iterable[str], limit: int = 30) -> list[SearchHit]:
        query_list = list(queries)
        query_tokens = tokenize(" ".join(query_list))
        token_counts = {
            token: 1.0 + min(1.0, math.log2(max(1, query_tokens.count(token))) * 0.25)
            for token in set(query_tokens)
        }
        exact_names = {
            match.lower()
            for query in query_list
            for match in re.findall(r"[A-Za-z_$][\w$]*", str(query))
            if len(match) >= 6
            or any(char.isupper() for char in match[1:])
            or "_" in match
            or "$" in match
            or f"{match}(" in str(query)
        }
        hits: list[SearchHit] = []
        for entity in self.entities:
            haystack = f"{entity.path} {entity.name} {entity.text}".lower()
            exact_score = 0.0
            name_score = 0.0
            path_score = 0.0
            body_score = 0.0
            reasons: list[str] = []
            if entity.name.lower() in exact_names:
                exact_score = 30.0
                reasons.append(f"exact_name:{entity.name}")
            for token, weight in token_counts.items():
                if token in entity.name.lower():
                    name_score += 10.0 * weight
                    reasons.append(f"name:{token}")
                if token in entity.path.lower():
                    path_score += 4.0 * weight
                    if len(reasons) < 8:
                        reasons.append(f"path:{token}")
                count = haystack.count(token)
                if count:
                    body_score += min(count, 8) * weight
                    if len(reasons) < 8:
                        reasons.append(f"body:{token}x{min(count, 8)}")
            score = exact_score + name_score + min(path_score, 24.0) + min(body_score, 36.0)
            if score > 0:
                hits.append(
                    SearchHit(
                        path=entity.path,
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

    def metadata(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "instance_id": self.instance_id,
            "dataset": self.dataset,
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "structure_path": str(self.structure_path) if self.structure_path else None,
            "file_count": len(self.files),
            "entity_count": len(self.entities),
        }
