from __future__ import annotations

import re
import posixpath
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Set

from mycode.repo_index.structure_index import RepositoryIndex, tokenize


IMPORT_RE = re.compile(r"(?:from|import)\s+['\"](?P<path>[./][^'\"]+)['\"]|from\s+(?P<py>[A-Za-z_][\w.]*)\s+import")


class LightRepositoryGraph:
    """A small typed graph for navigation, not a full call graph."""

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index
        self.edges: Dict[str, Set[str]] = defaultdict(set)
        self._build()

    def _resolve_relative(self, source: str, ref: str) -> str | None:
        if not ref.startswith("."):
            return None
        base = Path(source).parent
        candidate = posixpath.normpath((base / ref).as_posix())
        prefixes = [candidate, candidate + ".js", candidate + ".ts", candidate + ".jsx", candidate + ".tsx", candidate + "/index.js", candidate + "/index.ts"]
        for item in prefixes:
            normalized = posixpath.normpath(item)
            if normalized in self.index.files:
                return normalized
        return None

    def _build(self) -> None:
        by_dir: Dict[str, list[str]] = defaultdict(list)
        for path in self.index.files:
            by_dir[str(Path(path).parent)].append(path)
        for siblings in by_dir.values():
            if len(siblings) <= 20:
                for path in siblings:
                    self.edges[path].update(item for item in siblings if item != path)

        for path, text in self.index.files.items():
            for match in IMPORT_RE.finditer(text):
                target = None
                ref = match.group("path")
                if ref:
                    target = self._resolve_relative(path, ref)
                if target:
                    self.edges[path].add(target)
                    self.edges[target].add(path)

        definitions: Dict[str, Set[str]] = defaultdict(set)
        for entity in self.index.entities:
            name = entity.name
            if len(name) >= 4 and not name.startswith("_"):
                definitions[name].add(entity.path)
        for path, text in self.index.files.items():
            if not definitions:
                break
            haystack = text
            for name, targets in definitions.items():
                if name in haystack:
                    for target in targets:
                        if target != path:
                            self.edges[path].add(target)
                            self.edges[target].add(path)

    def expand(self, seeds: Iterable[str], query_terms: Iterable[str], limit: int = 20) -> list[tuple[str, float, list[str]]]:
        terms = set(tokenize(" ".join(query_terms)))
        scored: Dict[str, tuple[float, list[str]]] = {}
        for seed in seeds:
            for neighbor in self.edges.get(seed, set()):
                text = f"{neighbor} {self.index.files.get(neighbor, '')}".lower()
                score = 1.0
                reasons = [f"neighbor_of:{seed}"]
                for term in terms:
                    if term in text:
                        score += 0.5
                current = scored.get(neighbor)
                if current is None or score > current[0]:
                    scored[neighbor] = (score, reasons)
        ranked = [(path, score, reasons) for path, (score, reasons) in scored.items()]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[:limit]
