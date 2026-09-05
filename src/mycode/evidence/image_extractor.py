from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Iterable, List, Tuple

from mycode.evidence.url_extractor import extract_urls_from_text, merge_urls
from mycode.schemas.evidence import ImageEvidence


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _urls_from_field(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return extract_urls_from_text(text)
        return _urls_from_field(parsed)
    if isinstance(value, dict):
        urls = []
        for item in value.values():
            urls.extend(_urls_from_field(item))
        return urls
    return []


def extract_image_urls(raw: dict, issue_text: str) -> List[Tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key in ("image_urls", "images", "image_assets"):
        for url in _urls_from_field(raw.get(key)):
            pairs.append((url, key))
    for url in extract_urls_from_text(issue_text):
        if is_image_url(url):
            pairs.append((url, "issue_text"))

    seen = set()
    out = []
    for url, source in pairs:
        if url not in seen:
            seen.add(url)
            out.append((url, source))
    return out


def is_image_url(url: str) -> bool:
    lower = url.lower()
    return "user-images.githubusercontent.com" in lower or lower.split("?")[0].endswith(IMAGE_EXTENSIONS)


def classify_image(url: str, source_field: str, issue_text: str = "") -> ImageEvidence:
    # Adapter summaries contain generic words such as "visual" and "render"
    # for every image. They describe the evidence pipeline, not the image
    # domain, and must not turn ordinary UI screenshots into chart evidence.
    core_text = str(issue_text or "")
    for marker in ("\n[Multimodal Context - Compact]", "\n[Adapter Note]", "\n[adapter_fallback="):
        core_text = core_text.split(marker, 1)[0]
    lower_text = core_text.lower()
    lower_url = url.lower()
    path = PurePosixPath(lower_url.split("?")[0])
    ext = path.suffix

    image_type = "generic_visual"
    role = "visual_evidence"
    reason = "Generic image evidence."

    if ext == ".svg":
        image_type = "svg_or_vector"
        role = "non_raster_or_auxiliary"
        reason = "SVG images are often logos or vector assets and may need special handling."
    elif any(k in lower_text for k in ("pdf", "layout", "margin", "font", "text measurement", "wrap")):
        image_type = "layout_or_pdf_render"
        reason = "Issue text suggests layout/PDF/text rendering evidence."
    elif any(k in lower_text for k in ("canvas", "chart", "chart.js", "chartjs", "plot", "legend", "dataset")):
        image_type = "chart_or_canvas_render"
        reason = "Issue text suggests chart/canvas/rendering evidence."
    elif any(k in lower_text for k in ("screenshot", "screen shot", "ui", "button", "dialog", "page", "form", "owner box", "purchase")):
        image_type = "web_ui_screenshot"
        reason = "Issue text suggests a UI/page screenshot."
    elif any(k in lower_text for k in ("markdown", "mdx", "syntax highlight", "html")):
        image_type = "text_or_markdown_render"
        reason = "Issue text suggests markdown/text rendering evidence."
    elif any(k in lower_text for k in ("traceback", "error", "exception", "failed", "failure")):
        image_type = "error_or_failure_screenshot"
        reason = "Issue text suggests error/failure screenshot evidence."

    return ImageEvidence(
        raw_url=url,
        source_field=source_field,
        image_type=image_type,
        role=role,
        extension=ext,
        reason=reason,
    )
