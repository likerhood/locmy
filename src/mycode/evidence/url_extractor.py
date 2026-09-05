from __future__ import annotations

import re
from typing import Iterable, List


URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")


def normalize_url(url: str) -> str:
    return url.strip().rstrip(".,;:")


def extract_urls_from_text(text: str) -> List[str]:
    seen = set()
    urls = []
    for match in URL_RE.finditer(text or ""):
        url = normalize_url(match.group(0))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def merge_urls(*groups: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for group in groups:
        for raw in group:
            url = normalize_url(str(raw))
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out
