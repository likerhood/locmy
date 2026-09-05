from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List

from mycode.evidence.image_extractor import classify_image, extract_image_urls, is_image_url
from mycode.evidence.url_classifier import classify_url
from mycode.evidence.url_extractor import extract_urls_from_text
from mycode.schemas.evidence import EvidenceSketch, NormalizedSample


SYMBOL_RE = re.compile(r"`([^`]{2,120})`")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$/-]{2,}")


def collect_urls(sample: NormalizedSample) -> list[str]:
    issue_urls = extract_urls_from_text(sample.issue_text)
    return [u for u in issue_urls if not is_image_url(u)]


def build_symbol_queries(text: str, limit: int = 12) -> list[str]:
    symbols = []
    for match in SYMBOL_RE.finditer(text or ""):
        value = match.group(1).strip()
        if any(ch in value for ch in (" ", "\n", "\t")) and len(value) > 60:
            continue
        symbols.append(value)
    seen = set()
    out = []
    for sym in symbols:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def build_concern_queries(text: str, repo: str, limit: int = 16) -> list[str]:
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "into", "when",
        "where", "should", "could", "would", "have", "has", "not", "are",
        "but", "you", "your", "their", "then", "than", "will", "can",
    }
    counts = Counter()
    for word in WORD_RE.findall(text or ""):
        low = word.lower().strip("_-./")
        if len(low) < 4 or low in stop or low.startswith("http"):
            continue
        counts[low] += 1
    repo_terms = [part.lower() for part in re.split(r"[/_.-]+", repo or "") if len(part) > 2]
    for term in repo_terms:
        counts[term] += 2
    return [word for word, _ in counts.most_common(limit)]


def build_flow_hypotheses(text: str) -> list[str]:
    lower = (text or "").lower()
    hypotheses = []
    if any(k in lower for k in ("redirect", "url", "link", "route", "href")):
        hypotheses.append("url_builder_or_route_flow")
    if any(k in lower for k in ("parameter", "argument", "option", "config", "flag")):
        hypotheses.append("parameter_or_config_flow")
    if any(k in lower for k in ("state", "selector", "redux", "store", "cache")):
        hypotheses.append("state_selector_flow")
    if any(k in lower for k in ("click", "button", "submit", "form", "dialog")):
        hypotheses.append("ui_event_flow")
    if any(k in lower for k in ("type", "typing", "class", "method", "interface")):
        hypotheses.append("symbol_or_type_flow")
    if any(k in lower for k in ("layout", "render", "style", "css", "margin", "font")):
        hypotheses.append("render_style_pipeline_flow")
    return hypotheses


def build_evidence_sketch(sample: NormalizedSample) -> EvidenceSketch:
    urls = [classify_url(url) for url in collect_urls(sample)]
    images = [
        classify_image(url, source_field=source, issue_text=sample.issue_text)
        for url, source in extract_image_urls({}, sample.issue_text)
    ]
    return EvidenceSketch(
        instance_id=sample.instance_id,
        repo=sample.repo,
        dataset=sample.dataset,
        issue_text=sample.issue_text,
        urls=urls,
        images=images,
        concern_queries=build_concern_queries(sample.issue_text, sample.repo),
        symbol_queries=build_symbol_queries(sample.issue_text),
        flow_hypotheses=build_flow_hypotheses(sample.issue_text),
        metadata={
            "language": sample.language,
            "url_count": len(urls),
            "image_count": len(images),
        },
    )


def build_evidence_sketches(samples: Iterable[NormalizedSample]) -> list[EvidenceSketch]:
    return [build_evidence_sketch(sample) for sample in samples]
