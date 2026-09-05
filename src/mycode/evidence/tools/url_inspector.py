from __future__ import annotations

import re
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse


PLAYGROUND_DOMAINS = (
    "play.net",
    "playground",
    "codesandbox",
    "stackblitz",
    "jsfiddle",
    "codepen",
    "typescriptlang.org",
    "babeljs.io",
    "prettier.io",
)


def _is_docs_sample(domain: str, path: str) -> bool:
    return "/docs/" in path.lower() and "/samples/" in path.lower()


def _domain_kind(domain: str, path: str = "") -> str:
    if domain.endswith("github.com"):
        return "github"
    if any(token in domain for token in PLAYGROUND_DOMAINS):
        return "playground"
    if domain.endswith("wordpress.com") or domain.endswith("prettier.io") or domain.endswith("babeljs.io"):
        return "product_or_repro_page"
    if _is_docs_sample(domain, path):
        return "docs_sample"
    if any(token in domain for token in ("readthedocs", "docs.", "developer.", "api.", "devdocs")):
        return "docs"
    if "/docs" in path.lower():
        return "docs"
    return "web"


def _parse_github(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    result: Dict[str, Any] = {
        "github_kind": "unknown",
        "repo": None,
        "path": None,
        "ref": None,
        "line": None,
        "symbol_hint": None,
    }
    if len(parts) >= 2:
        result["repo"] = f"{parts[0]}/{parts[1]}"

    if len(parts) >= 4 and parts[2] == "blob":
        result["github_kind"] = "code"
        result["ref"] = parts[3]
        result["path"] = "/".join(parts[4:]) if len(parts) > 4 else None
        line_match = re.search(r"L(\d+)", parsed.fragment or "")
        if line_match:
            result["line"] = int(line_match.group(1))
    elif len(parts) >= 3 and parts[2] == "issues":
        result["github_kind"] = "issue"
        result["issue_number"] = parts[3] if len(parts) > 3 else None
    elif len(parts) >= 3 and parts[2] == "pull":
        result["github_kind"] = "pull_request"
        result["pull_number"] = parts[3] if len(parts) > 3 else None
    elif len(parts) >= 3 and parts[2] in {"commit", "commits"}:
        result["github_kind"] = "commit"
        result["commit"] = parts[3] if len(parts) > 3 else None
    elif len(parts) >= 3 and parts[2] == "compare":
        result["github_kind"] = "diff_or_compare"

    return result


def _route_terms(url: str) -> list[str]:
    parsed = urlparse(url)
    terms: list[str] = []
    for part in re.split(r"[/_.-]+", unquote(parsed.path)):
        low = part.lower()
        if len(low) >= 3 and not low.isdigit() and low not in {"http", "https", "www", "com"}:
            terms.append(part)
    for key, values in parse_qs(parsed.query).items():
        if len(key) >= 3:
            terms.append(key)
        for value in values[:2]:
            value = unquote(value)
            if len(value) >= 3 and len(value) <= 80:
                terms.append(value)
    return list(dict.fromkeys(terms))[:24]


def _semantic_terms_for_url(url: str, kind: str, role: str) -> list[str]:
    parsed = urlparse(url)
    terms = _route_terms(url)
    domain_parts = [
        part
        for part in re.split(r"[.-]+", parsed.netloc.lower())
        if len(part) >= 3 and part not in {"www", "com", "org", "net"}
    ]
    if role == "spec_or_api_semantics":
        terms.extend(["api", "docs", "option", "parameter", "behavior"])
    if role == "reproduction_entry":
        terms.extend(["reproduction", "example", "runtime", "config"])
    if kind == "github":
        terms.extend(domain_parts[:2])
    else:
        terms.extend(domain_parts[:4])
    return list(dict.fromkeys(terms))[:32]


def inspect_url(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    kind = _domain_kind(domain, parsed.path)
    query = {k: [unquote(vv) for vv in v] for k, v in parse_qs(parsed.query).items()}

    inspection: Dict[str, Any] = {
        "url": url,
        "domain": domain,
        "kind": kind,
        "path": parsed.path,
        "query_keys": sorted(query),
        "fragment_present": bool(parsed.fragment),
        "role": "weak_context",
        "risk": "low",
        "tool_recommendation": "web_snapshot",
        "localization_use": "semantic_context",
        "query": query,
        "route_terms": _route_terms(url),
        "semantic_terms": [],
    }

    if kind == "github":
        gh = _parse_github(url)
        inspection.update(gh)
        if gh.get("github_kind") in {"pull_request", "commit", "diff_or_compare"}:
            inspection.update(
                {
                    "role": "leakage_skip",
                    "risk": "high",
                    "tool_recommendation": "skip_for_localization",
                    "localization_use": "do_not_use",
                }
            )
        elif gh.get("github_kind") == "code":
            inspection.update(
                {
                    "role": "code_evidence_seed",
                    "risk": "medium",
                    "tool_recommendation": "repo_seed_expand",
                    "localization_use": "use_as_seed_not_target",
                }
            )
        elif gh.get("github_kind") == "issue":
            inspection.update(
                {
                    "role": "historical_discussion",
                    "risk": "medium",
                    "tool_recommendation": "web_snapshot",
                    "localization_use": "extract_issue_terms_without_patch_links",
                }
            )
    elif kind == "playground":
        inspection.update(
            {
                "role": "reproduction_entry",
                "tool_recommendation": "reproduction_extractor",
                "localization_use": "extract_input_config_behavior",
            }
        )
    elif kind == "product_or_repro_page":
        inspection.update(
            {
                "role": "reproduction_entry",
                "tool_recommendation": "web_snapshot",
                "localization_use": "extract_route_query_business_entities_and_reproduction_behavior",
            }
        )
    elif kind == "docs_sample":
        inspection.update(
            {
                "role": "reproduction_entry",
                "tool_recommendation": "reproduction_extractor",
                "localization_use": "extract_docs_sample_api_behavior_and_event_config",
                "secondary_tool_recommendation": "web_snapshot",
            }
        )
    elif kind == "docs":
        inspection.update(
            {
                "role": "spec_or_api_semantics",
                "tool_recommendation": "web_snapshot",
                "localization_use": "extract_api_names_and_behavior_rules",
            }
        )
    elif parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        inspection.update(
            {
                "role": "visual_evidence",
                "tool_recommendation": "image_inspector",
                "localization_use": "extract_visual_symptom",
            }
        )

    inspection["semantic_terms"] = _semantic_terms_for_url(
        url,
        str(inspection.get("kind") or ""),
        str(inspection.get("role") or ""),
    )

    return inspection
