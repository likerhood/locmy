"""Shared evidence gate for ranking, not a proof of patch correctness."""

from typing import Any


def responsibility_evidence(decision: dict[str, Any]) -> dict[str, Any]:
    """Keep navigation relevance distinct from a reviewed edit mechanism.

    These flags must come from the source/flow reviewer. Graph scores alone
    are deliberately not accepted as substitutes for grounded flow evidence.
    """
    missing = []
    if decision.get("role") != "patch_target":
        missing.append("not_patch_target")
    for field in ("grounded", "quote_supported", "entity_supported", "direct_flow_supported"):
        if not decision.get(field):
            missing.append(field)
    mechanism = str(decision.get("patch_mechanism") or "").strip()
    if not mechanism or mechanism.lower() in {"none", "null", "unknown"}:
        missing.append("patch_mechanism")
    counterevidence = [
        str(value).strip()
        for key in ("counterevidence", "counter_evidence")
        for value in (decision.get(key) or [])
        if str(value).strip().lower() not in {"", "none", "no_source", "no_entity", "no_flow"}
    ]
    if counterevidence:
        missing.append("unresolved_counterevidence")
    return {"supported": not missing, "missing": missing}
