from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import urlparse

from mycode.evidence.tools.llm_client import (
    LLMClientError,
    chat_completion,
    first_text,
    model_for_stage,
)

MIME_BY_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

LIST_FIELDS = {
    "visible_text",
    "visual_entities",
    "likely_code_layers",
    "search_queries",
    "cautions",
}
TEXT_FIELDS = {"symptom", "expected_actual_difference"}


def _infer_image_type(payload: Dict[str, Any], issue_summary: str) -> str:
    supplied = _as_text(payload.get("image_type")).lower().replace(" ", "_")
    if supplied:
        return supplied
    text = " ".join(
        [
            issue_summary,
            " ".join(_as_list(payload.get("visual_entities"))),
            _as_text(payload.get("symptom")),
        ]
    ).lower()
    if any(token in text for token in ("traceback", "exception", "error dialog", "stack trace")):
        return "error_or_diagnostic"
    if any(token in text for token in ("chart", "plot", "legend", "canvas", "axis")):
        return "chart_or_canvas"
    comparison_pairs = (
        ("before" in text and "after" in text)
        or ("expected" in text and "actual" in text)
        or "side by side" in text
        or "difference view" in text
    )
    if comparison_pairs:
        return "comparison"
    if any(token in text for token in ("code", "terminal", "console", "editor")):
        return "code_or_console"
    return "ui_screenshot"


def _visual_graph(payload: Dict[str, Any], issue_summary: str) -> Dict[str, Any]:
    image_type = _infer_image_type(payload, issue_summary)
    supplied_nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    supplied_edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    nodes: list[dict[str, str]] = []
    for index, item in enumerate(supplied_nodes[:40], start=1):
        if isinstance(item, dict):
            label = _as_text(item.get("label") or item.get("text") or item.get("name"))
            if label:
                nodes.append(
                    {
                        "id": _as_text(item.get("id"), f"node_{index}"),
                        "type": _as_text(item.get("type"), "visual_entity"),
                        "label": label[:240],
                    }
                )
    if not nodes:
        for index, label in enumerate(_as_list(payload.get("visual_entities"))[:20], start=1):
            nodes.append({"id": f"entity_{index}", "type": "visual_entity", "label": label[:240]})
        offset = len(nodes)
        for index, label in enumerate(_as_list(payload.get("visible_text"))[:20], start=1):
            nodes.append({"id": f"text_{index}", "type": "visible_text", "label": label[:240]})
        symptom = _as_text(payload.get("symptom"))
        if symptom:
            nodes.append({"id": "symptom", "type": "observed_symptom", "label": symptom[:300]})

    node_ids = {item["id"] for item in nodes}
    edges: list[dict[str, str]] = []
    for item in supplied_edges[:60]:
        if not isinstance(item, dict):
            continue
        source = _as_text(item.get("source"))
        target = _as_text(item.get("target"))
        if source in node_ids and target in node_ids:
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "type": _as_text(item.get("type"), "related_to"),
                }
            )
    if not edges and "symptom" in node_ids:
        edges = [
            {"source": item["id"], "target": "symptom", "type": "supports"}
            for item in nodes
            if item["id"] != "symptom"
        ][:30]
    roots = _as_list(payload.get("root_objects"))[:12]
    if not roots:
        roots = [item["id"] for item in nodes if item["type"] == "visual_entity"][:6]
    return {
        "image_type": image_type,
        "root_objects": roots,
        "nodes": nodes[:40],
        "edges": edges[:60],
    }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    out: list[str] = []
    seen = set()
    for item in items:
        text = " ".join(str(item or "").split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _as_text(value: Any, default: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text or default


def normalize_vlm_analysis(
    data: Dict[str, Any] | str,
    *,
    image_format: str,
    issue_summary: str,
    repo: str,
    local_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Normalize VLM output into the schema consumed by retrieval.

    Different OpenAI-compatible services sometimes return fenced JSON, a raw
    paragraph, or partial fields. The localization agent should still receive a
    predictable structure and explicit diagnostics.
    """

    if isinstance(data, str):
        payload: Dict[str, Any] = {"raw_text": data}
    elif isinstance(data, dict):
        payload = dict(data)
    else:
        payload = {"raw_text": str(data)}

    for field in LIST_FIELDS:
        payload[field] = _as_list(payload.get(field))
    for field in TEXT_FIELDS:
        payload[field] = _as_text(payload.get(field), default="not_specified_by_vlm")

    if not payload["search_queries"]:
        seed = heuristic_image_understanding(
            image_format=image_format,
            issue_summary=issue_summary,
            repo=repo,
            local_path=local_path,
        )
        payload["search_queries"] = _as_list(seed.get("search_queries"))[:12]
        payload["likely_code_layers"] = _as_list(payload.get("likely_code_layers")) + _as_list(
            seed.get("likely_code_layers")
        )[:8]
        payload["cautions"].append("search_queries_backfilled_from_heuristic")

    payload["visible_text"] = payload["visible_text"][:60]
    payload["visual_entities"] = payload["visual_entities"][:60]
    payload["likely_code_layers"] = list(dict.fromkeys(payload["likely_code_layers"]))[:40]
    payload["search_queries"] = list(dict.fromkeys(payload["search_queries"]))[:60]
    payload["cautions"] = list(dict.fromkeys(payload["cautions"]))[:40]
    payload["visual_ir"] = _visual_graph(payload, issue_summary)
    payload["image_type"] = payload["visual_ir"]["image_type"]
    payload["image_format"] = image_format
    payload["repo"] = repo
    payload["local_path"] = str(local_path) if local_path else payload.get("local_path")
    payload["schema_version"] = "vlm_image_understanding.v3"
    return payload


def heuristic_image_understanding(
    *,
    image_format: str,
    issue_summary: str,
    repo: str,
    local_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Produce a deterministic visual-evidence sketch when VLM is unavailable.

    This is not a substitute for reading the pixels. It exists so the evidence
    pipeline can still distinguish UI, chart, PDF/layout, and error screenshots
    and pass useful search intents to the localization agent.
    """

    text = (issue_summary or "").lower()
    layers: list[str] = []
    entities: list[str] = []
    queries: list[str] = []
    symptom = "visual evidence needs VLM inspection"

    if any(token in text for token in ("chart", "legend", "canvas", "tooltip", "pie", "bar")):
        layers.extend(["chart_plugin", "canvas_render", "event_handler"])
        entities.extend(["chart", "legend", "canvas"])
        queries.extend(["legend", "tooltip", "onHover", "onLeave", "plugin.legend"])
        symptom = "chart/canvas visual behavior or rendering symptom"
    if any(token in text for token in ("pdf", "layout", "margin", "font", "wrap", "text measurement")):
        layers.extend(["layout_engine", "stylesheet_resolver", "text_measurement"])
        entities.extend(["layout", "text", "style"])
        queries.extend(["layout", "margin", "font", "resolveStyles", "processBoxModel"])
        symptom = "layout or PDF/text rendering difference"
    if any(token in text for token in ("button", "form", "dialog", "page", "screen", "screenshot", "click")):
        layers.extend(["route", "component", "ui_state", "event_handler"])
        entities.extend(["page", "button", "form"])
        queries.extend(["component", "route", "state", "onClick", "submit"])
        symptom = "web UI state or interaction symptom"
    if any(token in text for token in ("error", "exception", "traceback", "warning", "failed")):
        layers.extend(["error_handling", "validation", "diagnostics"])
        entities.extend(["error message", "stack trace"])
        queries.extend(["error", "exception", "warning", "diagnostic"])
        symptom = "error/failure screenshot"

    repo_terms = [part for part in repo.replace("-", "/").split("/") if len(part) > 2]
    queries.extend(repo_terms[:4])

    return {
        "vlm_status": "heuristic_only",
        "visible_text": [],
        "visual_entities": list(dict.fromkeys(entities))[:20],
        "symptom": symptom,
        "expected_actual_difference": "not_read_from_pixels_without_vlm",
        "likely_code_layers": list(dict.fromkeys(layers))[:20],
        "search_queries": list(dict.fromkeys(queries))[:30],
        "cautions": [
            "heuristic_image_understanding_does_not_read_pixels",
            "use_real_vlm_for_visible_text_and_expected_actual_diff",
        ],
        "image_format": image_format,
        "local_path": str(local_path) if local_path else None,
    }


def _data_uri(path: str | Path, image_format: str) -> str:
    raw = Path(path).read_bytes()
    mime = MIME_BY_FORMAT.get(image_format, "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _remote_image_url(source_url: str | None) -> str | None:
    value = str(source_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return value
    return None


def _image_transport() -> str:
    raw = os.environ.get("MYCODE_VLM_IMAGE_TRANSPORT", "data_uri").strip().lower()
    aliases = {"base64": "data_uri", "http": "url", "https": "url"}
    transport = aliases.get(raw, raw)
    if transport not in {"data_uri", "url", "auto"}:
        raise ValueError(
            "MYCODE_VLM_IMAGE_TRANSPORT must be one of: data_uri, url, auto"
        )
    return transport


def _transport_fallback_allowed(exc: LLMClientError) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "http 400",
            "http 415",
            "http 422",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "chunkedencodingerror",
            "response ended prematurely",
        )
    )


def _vlm_messages(*, uri: str, issue_summary: str, repo: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the visual-evidence interpreter for a code-localization task. Describe only "
                "phenomena visible in the image, UI or chart entities, visible text, interaction state, "
                "and potentially relevant code layers. Do not predict gold files. Return one compact "
                "JSON object only, with no Markdown, preamble, prompt restatement, or hidden "
                "chain-of-thought. Write generated descriptions and search queries in English. Preserve "
                "visible text and program identifiers verbatim."
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Analyze this issue image and return these JSON fields: visible_text, "
                        "visual_entities, symptom, expected_actual_difference, likely_code_layers, "
                        "search_queries, cautions, image_type, root_objects, nodes, edges. "
                        "Nodes and edges should capture only issue-relevant visual structure. Keep each "
                        "field concise and evidence-grounded.\n"
                        f"repo={repo}\nissue_summary={issue_summary}"
                    ),
                },
                {"type": "image_url", "image_url": {"url": uri}},
            ],
        },
    ]


def analyze_image_with_vlm(
    *,
    image_path: str | Path,
    image_format: str,
    issue_summary: str,
    repo: str,
    source_url: str | None = None,
    max_tokens: int = 900,
) -> Dict[str, Any]:
    """Ask a VLM to turn a screenshot into localization-oriented evidence."""

    transport = _image_transport()
    remote_url = _remote_image_url(source_url)
    if transport == "url" and not remote_url:
        raise ValueError("VLM image transport 'url' requires an HTTP(S) source_url")

    attempts: list[tuple[str, str]] = []
    if transport in {"data_uri", "auto"}:
        attempts.append(("data_uri", _data_uri(image_path, image_format)))
    if transport == "url" or (transport == "auto" and remote_url):
        attempts.append(("url", str(remote_url)))

    response: Dict[str, Any] | None = None
    used_transport = attempts[0][0]
    fallback_error: str | None = None
    for index, (candidate_transport, uri) in enumerate(attempts):
        try:
            response = chat_completion(
                _vlm_messages(uri=uri, issue_summary=issue_summary, repo=repo),
                model=model_for_stage("vlm"),
                max_tokens=max_tokens,
            )
            used_transport = candidate_transport
            break
        except LLMClientError as exc:
            can_retry_url = (
                transport == "auto"
                and candidate_transport == "data_uri"
                and index + 1 < len(attempts)
                and attempts[index + 1][0] == "url"
                and _transport_fallback_allowed(exc)
            )
            if not can_retry_url:
                raise
            fallback_error = str(exc)

    if response is None:
        raise LLMClientError("VLM request failed without a response")
    text = first_text(response)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = re.sub(r"^\s*json\s*", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed: Dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = {"raw_text": text}
    parsed = normalize_vlm_analysis(
        parsed,
        image_format=image_format,
        issue_summary=issue_summary,
        repo=repo,
        local_path=image_path,
    )
    parsed["_llm_response_id"] = response.get("id")
    parsed["_model"] = response.get("model")
    parsed["_usage"] = response.get("usage")
    parsed["_image_transport"] = used_transport
    if fallback_error:
        parsed["_image_transport_fallback"] = {
            "from": "data_uri",
            "to": "url",
            "reason": fallback_error[:500],
        }
    return parsed


def try_analyze_image_with_vlm(**kwargs: Any) -> Dict[str, Any]:
    try:
        result = analyze_image_with_vlm(**kwargs)
        result["vlm_status"] = "ok"
        return result
    except (LLMClientError, OSError, ValueError) as exc:
        fallback = heuristic_image_understanding(
            image_format=str(kwargs.get("image_format") or ""),
            issue_summary=str(kwargs.get("issue_summary") or ""),
            repo=str(kwargs.get("repo") or ""),
            local_path=kwargs.get("image_path"),
        )
        fallback.update(
            {
                "vlm_status": "failed",
                "fallback": "heuristic_image_understanding",
                "error": str(exc)[:1200],
            }
        )
        return fallback
