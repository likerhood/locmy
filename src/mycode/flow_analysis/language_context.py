"""Language gates for heuristic flow labels; words alone are not graph proof."""
import re
from collections.abc import Iterable


def python_binding_context(text: str, *, repo: str = "", paths: Iterable[str] = ()) -> bool:
    context = (repo + " " + text).lower()
    python = bool(re.search(r"\b(?:python|mypy)\b", context)) or any(
        str(path).lower().endswith((".py", ".pyi")) for path in paths
    )
    binding = bool(re.search(
        r"\b(?:mypy|typeinfo|typetype|typevars?|binder|binding|declaration|narrowing)\b"
        r"|deleted variable|symbol table", context,
    ))
    return python and binding
