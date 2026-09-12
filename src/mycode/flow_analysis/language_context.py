"""Language gates for heuristic flow labels; words alone are not graph proof."""
import re
from collections.abc import Iterable


def python_binding_context(text: str, *, repo: str = "", paths: Iterable[str] = ()) -> bool:
    context = (repo + " " + text).lower()
    source_paths = [str(path).lower() for path in paths if path]
    # Concrete source language takes precedence over words in comments or queries.
    python = any(path.endswith((".py", ".pyi")) for path in source_paths) if source_paths else bool(
        re.search(r"\b(?:python|mypy)\b", context)
    )
    binding = bool(re.search(
        r"\b(?:mypy|typeinfo|typetype|typevars?|binder|binding|declaration|narrowing)\b"
        r"|deleted variable|symbol table", context,
    ))
    return python and binding
