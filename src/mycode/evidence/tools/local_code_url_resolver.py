from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mycode.evidence.tools.url_inspector import inspect_url


SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_$][\w$]*)\b"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)\b"),
)


def _enclosing_symbol(lines: list[str], line_index: int) -> str | None:
    for index in range(min(line_index, len(lines) - 1), -1, -1):
        for pattern in SYMBOL_PATTERNS:
            match = pattern.search(lines[index])
            if match:
                return match.group(1)
    return None


def _safe_local_file(repo_root: Path, relative_path: str) -> Path | None:
    root = repo_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def resolve_github_code_url(
    url: str,
    *,
    repo_root: str | Path | None,
    expected_repo: str,
    base_commit: str,
    context_lines: int = 40,
    max_bytes: int = 2_000_000,
) -> dict[str, Any]:
    """Resolve a GitHub blob URL against the benchmark's exact local checkout."""

    result = inspect_url(url)
    result.update(
        {
            "provenance": "navigation_seed",
            "local_resolution_status": "not_applicable",
            "benchmark_base_commit": base_commit,
            "requested_ref": result.get("ref"),
            "ref_policy": "benchmark_base_commit_overrides_url_ref",
        }
    )
    if result.get("github_kind") != "code":
        return result
    if expected_repo and str(result.get("repo") or "").lower() != expected_repo.lower():
        result["local_resolution_status"] = "repository_mismatch"
        result["warning"] = "github_url_repository_does_not_match_benchmark_repository"
        return result
    relative_path = str(result.get("path") or "").lstrip("/")
    if not relative_path:
        result["local_resolution_status"] = "missing_path"
        return result
    root = Path(repo_root) if repo_root else None
    if root is None or not root.is_dir():
        result["local_resolution_status"] = "checkout_unavailable"
        return result
    source_path = _safe_local_file(root, relative_path)
    if source_path is None:
        result["local_resolution_status"] = "path_not_found_in_base_commit"
        return result
    try:
        if source_path.stat().st_size > max_bytes:
            result["local_resolution_status"] = "file_too_large"
            return result
        text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["local_resolution_status"] = "read_error"
        result["warning"] = str(exc)[:300]
        return result

    lines = text.splitlines()
    requested_line = int(result.get("line") or 1)
    requested_line = max(1, min(requested_line, max(1, len(lines))))
    start = max(1, requested_line - context_lines)
    end = min(len(lines), requested_line + context_lines)
    excerpt = "\n".join(
        f"{line_no:>6}: {lines[line_no - 1]}"
        for line_no in range(start, end + 1)
    )
    symbol = _enclosing_symbol(lines, requested_line - 1)
    semantic_terms = list(result.get("semantic_terms") or [])
    if symbol and symbol not in semantic_terms:
        semantic_terms.insert(0, symbol)
    result.update(
        {
            "provenance": "direct_local_code_anchor",
            "local_resolution_status": "resolved",
            "local_path": relative_path,
            "line_start": start,
            "line_end": end,
            "source_excerpt": excerpt,
            "symbol_hint": symbol or result.get("symbol_hint"),
            "semantic_terms": semantic_terms[:32],
            "localization_use": "navigate_from_verified_local_code_anchor",
        }
    )
    return result
