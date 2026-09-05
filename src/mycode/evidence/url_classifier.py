from __future__ import annotations

from urllib.parse import urlparse

from mycode.schemas.evidence import UrlEvidence


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def classify_url(url: str) -> UrlEvidence:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    full = url.lower()

    url_type = "external_page"
    role = "weak_context"
    risk = "low"
    reason = "Generic external URL."

    if "user-images.githubusercontent.com" in domain or path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        url_type = "image_asset"
        role = "visual_evidence"
        reason = "Image URL; handled by image evidence pipeline."
    elif "github.com" in domain:
        if "/pull/" in path:
            url_type = "github_pr"
            role = "leakage_risk"
            risk = "high"
            reason = "Pull request URL may reveal changed files or discussion after the fix."
        elif "/commit/" in path or "/commits/" in path:
            url_type = "github_commit"
            role = "leakage_risk"
            risk = "high"
            reason = "Commit URL may reveal exact patch content."
        elif "/compare/" in path or "/files" in path or path.endswith(".diff") or path.endswith(".patch"):
            url_type = "github_diff"
            role = "leakage_risk"
            risk = "high"
            reason = "Diff/files URL may reveal exact changed files."
        elif "/blob/" in path:
            url_type = "github_code"
            role = "code_evidence_seed"
            risk = "medium"
            reason = "Code URL points to a file or line; it can be target or contextual evidence."
        elif "/issues/" in path:
            url_type = "github_issue"
            role = "historical_discussion"
            risk = "medium"
            reason = "Issue URL may contain related discussion or historical hints."
        else:
            url_type = "github_repo_or_search"
            role = "repository_context"
            risk = "low"
            reason = "GitHub URL without direct patch/file signal."
    elif any(host in domain for host in ("play.net", "playground", "codesandbox", "stackblitz", "jsfiddle", "codepen")) or "playground" in full:
        url_type = "reproduction_playground"
        role = "reproduction_entry"
        reason = "Runnable reproduction or encoded playground state."
    elif "/docs/" in path and "/samples/" in path:
        url_type = "docs_reproduction_sample"
        role = "reproduction_entry"
        reason = "Documentation sample URL; it describes a runnable reproduction and related API behavior."
    elif any(host in domain for host in ("readthedocs", "docs.", "developer.", "api.", "devdocs")) or "/docs" in path:
        url_type = "docs_api"
        role = "spec_or_api_semantics"
        reason = "Documentation/API URL; useful for semantic constraints."
    elif domain.endswith("wordpress.com") or domain.endswith("prettier.io") or domain.endswith("babeljs.io"):
        url_type = "product_or_repro_page"
        role = "reproduction_entry"
        reason = "Product, route, or tool page used to reproduce behavior."

    return UrlEvidence(
        raw_url=url,
        normalized_url=url,
        domain=domain,
        url_type=url_type,
        role=role,
        leakage_risk=risk,
        reason=reason,
    )
