from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


PROPERTY_RE = re.compile(r"\b(?:on[A-Z][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*_(?:id|url|path|state)|redirect_to|client_id)\b")

FLOW_STOP_TERMS = {
    "around",
    "behavior",
    "component",
    "evidence",
    "function",
    "implementation",
    "main",
    "only",
    "reproduction_understanding",
    "behavior_to_code_layer",
    "url_evidence_interpretation",
    "click",
    "clicked",
    "wordpress",
    "wordpress.com",
    "site",
    "div",
    "span",
}


def is_valid_flow_term(value: str) -> bool:
    term = str(value or "").strip()
    low = term.lower().strip("._-$")
    if not low or low in FLOW_STOP_TERMS:
        return False
    if "://" in term or low.startswith(("http", "www")):
        return False
    return True


def extract_flow_terms(issue_text: str, tool_observations: Iterable[Dict[str, Any]] | None = None) -> list[str]:
    terms = set(PROPERTY_RE.findall(issue_text or ""))
    lower = (issue_text or "").lower()
    if "mousemove" in lower or "mouse" in lower:
        terms.update(["mousemove", "mouseout", "hover", "leave"])
    if "legend" in lower:
        terms.update(["legend", "legendHitBoxes", "_hoveredItem"])
    if "onleave" in lower:
        terms.update(["onLeave"])
    if "onhover" in lower:
        terms.update(["onHover"])

    for observation in tool_observations or []:
        extracted = observation.get("extracted", {})
        text = repr(extracted)
        terms.update(PROPERTY_RE.findall(text))
        if "requested_file" in text:
            terms.add("requested_source_file")
    return sorted(term for term in terms if is_valid_flow_term(term))


def build_flow_queries(issue_text: str, tool_observations: Iterable[Dict[str, Any]] | None = None) -> list[str]:
    terms = extract_flow_terms(issue_text, tool_observations)
    queries = list(terms)
    lower = (issue_text or "").lower()
    if {"onLeave", "onHover"} & set(terms) or "legend" in lower:
        queries.extend(
            [
                "legend onHover onLeave mousemove mouseout handleEvent afterEvent",
                "legend _hoveredItem _getLegendItemAt itemsEqual",
                "plugin legend afterEvent handleEvent onLeave",
            ]
        )
    if "url" in lower or "redirect" in lower or "link" in lower:
        queries.append("url route href builder redirect link")
    if "parameter" in lower or "option" in lower or "config" in lower:
        queries.append("option config parameter defaults resolve")
    return queries
