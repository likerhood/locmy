from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._tag_stack: list[str] = []
        self.title: str = ""
        self.headings: list[str] = []
        self.code_blocks: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self._tag_stack.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for idx in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[idx] == tag:
                del self._tag_stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        current = self._tag_stack[-1] if self._tag_stack else ""
        if current == "title" and not self.title:
            self.title = text
        elif current in {"h1", "h2", "h3"}:
            self.headings.append(text)
        elif current in {"code", "pre"}:
            self.code_blocks.append(text)
        elif current not in {"script", "style", "noscript"}:
            self.text_parts.append(text)


def _keywords(text: str, limit: int = 20) -> List[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "into", "are", "you"}
    counts: dict[str, int] = {}
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.$-]{2,}", text):
        low = word.lower().strip("._-$")
        if len(low) < 4 or low in stop or low.startswith("http"):
            continue
        counts[low] = counts.get(low, 0) + 1
    return [word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _planned_snapshot(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    path_terms = [
        part
        for part in re.split(r"[/_.-]+", parsed.path)
        if len(part) >= 3 and part.lower() not in {"docs", "latest", "html"}
    ]
    is_docs = "/docs" in parsed.path.lower() or any(
        marker in parsed.netloc.lower()
        for marker in ("readthedocs", "docs.", "developer.", "api.", "devdocs")
    )
    return {
        "url": url,
        "status": "planned",
        "network_used": False,
        "title": "",
        "headings": [],
        "code_blocks": [],
        "semantic_queries": path_terms[:12],
        "keywords": path_terms[:12],
        "route_terms": path_terms[:12],
        "doc_kind": "api_or_docs" if is_docs else "external_page",
        "extraction_plan": [
            "fetch_title_and_main_headings",
            "extract_code_blocks_and_inline_api_names",
            "extract_parameters_options_versions_and_behavior_constraints",
            "convert_docs_terms_to_repo_search_queries",
        ]
        if is_docs
        else [
            "fetch_title_and_visible_text",
            "extract_route_path_query_and_business_entities",
            "convert_page_terms_to_repo_search_queries",
        ],
        "localization_use": "fetch_title_code_blocks_api_terms_when_enabled",
    }


def build_web_snapshot(url: str, allow_network: bool = False, timeout: int = 10) -> Dict[str, Any]:
    if not allow_network:
        return _planned_snapshot(url)

    request = Request(url, headers={"User-Agent": "mycode-evidence-agent/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            content_type = response.headers.get("content-type", "")
    except Exception as exc:  # noqa: BLE001 - snapshot should not break evidence building.
        return {
            "url": url,
            "status": "fetch_error",
            "network_used": True,
            "error": str(exc),
            "title": "",
            "headings": [],
            "code_blocks": [],
            "semantic_queries": [],
        }

    text = raw.decode("utf-8", errors="replace")
    parser = _TextHTMLParser()
    if "html" in content_type or "<html" in text[:2000].lower():
        parser.feed(text)
        plain = " ".join(parser.text_parts[:200])
        queries = _keywords(" ".join([parser.title, *parser.headings, plain, *parser.code_blocks[:20]]))
        return {
            "url": url,
            "status": "ok",
            "network_used": True,
            "title": parser.title,
            "headings": parser.headings[:12],
            "code_blocks": parser.code_blocks[:12],
            "semantic_queries": queries,
            "keywords": queries,
            "localization_use": "semantic_query_expansion",
        }

    preview = " ".join(text.split())[:2000]
    return {
        "url": url,
        "status": "ok",
        "network_used": True,
        "title": "",
        "headings": [],
        "code_blocks": [],
        "semantic_queries": _keywords(preview),
        "keywords": _keywords(preview),
        "text_preview": preview,
        "localization_use": "semantic_query_expansion",
    }
