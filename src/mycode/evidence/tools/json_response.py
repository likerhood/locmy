"""Conservative recovery of structured JSON returned by LLM providers."""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def parse_json_response(
    text: Any,
    *,
    required_keys: tuple[str, ...] = (),
    allow_list: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Return a parsed object and diagnostics for a model JSON response.

    Providers sometimes wrap otherwise valid JSON in Markdown fences. Recovery
    only accepts a complete JSON object; it never fabricates missing fields.
    """

    raw = str(text or "").strip()
    failed = {"parse_status": "failed", "repair_method": "none", "schema_valid": False}
    if not raw:
        return {}, failed

    def accept(value: Any, method: str) -> tuple[Any, dict[str, Any]]:
        if allow_list and isinstance(value, list) and not required_keys:
            return value, {
                "parse_status": "parsed" if method == "direct" else "repaired",
                "repair_method": method,
                "schema_valid": True,
            }
        if not isinstance(value, dict):
            return {}, failed
        valid = all(key in value for key in required_keys)
        if required_keys and not valid:
            return {}, failed
        return value, {
            "parse_status": "parsed" if method == "direct" else "repaired",
            "repair_method": method,
            "schema_valid": valid if required_keys else True,
        }

    try:
        return accept(json.loads(raw), "direct")
    except (json.JSONDecodeError, TypeError):
        pass

    cleaned = _FENCE_RE.sub("", raw).strip()
    if cleaned != raw:
        try:
            parsed, diagnostics = accept(json.loads(cleaned), "markdown_fence")
            if parsed:
                return parsed, diagnostics
        except (json.JSONDecodeError, TypeError):
            pass

    decoder = json.JSONDecoder()
    object_start = cleaned.find("{")
    if object_start >= 0:
        try:
            value, _end = decoder.raw_decode(cleaned[object_start:])
        except json.JSONDecodeError:
            value = None
        parsed, diagnostics = accept(value, "object_extraction")
        if parsed:
            return parsed, diagnostics
    return {}, failed
