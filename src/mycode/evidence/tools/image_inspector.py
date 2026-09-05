from __future__ import annotations

from typing import Any, Dict, List

from mycode.schemas.evidence import ImageEvidence


def _layers_for_image(image_type: str) -> List[str]:
    if image_type == "web_ui_screenshot":
        return ["route", "component", "state", "event_handler", "form_or_guard"]
    if image_type == "chart_or_canvas_render":
        return ["chart_config", "scale", "layout", "plugin", "canvas_renderer"]
    if image_type == "layout_or_pdf_render":
        return ["style_expand", "style_resolve", "layout_engine", "renderer"]
    if image_type == "text_or_markdown_render":
        return ["parser", "tokenizer", "renderer", "syntax_highlight"]
    if image_type == "error_or_failure_screenshot":
        return ["error_message", "exception_path", "runtime_state"]
    if image_type == "svg_or_vector":
        return ["asset_reference", "style", "documentation_or_logo"]
    return ["visual_symptom", "rendering_or_ui_layer"]


def _queries_for_image(image_type: str, issue_text: str) -> List[str]:
    text = (issue_text or "").lower()
    queries = []
    if "tooltip" in text:
        queries.append("tooltip")
    if "axis" in text:
        queries.append("axis")
    if "legend" in text:
        queries.append("legend")
    if "margin" in text:
        queries.append("margin")
    if "font" in text:
        queries.append("font")
    if "button" in text:
        queries.append("button")
    if "redirect" in text:
        queries.append("redirect")
    if image_type == "web_ui_screenshot":
        queries.extend(["component", "route", "state"])
    elif image_type == "chart_or_canvas_render":
        queries.extend(["canvas", "render", "scale"])
    elif image_type == "layout_or_pdf_render":
        queries.extend(["layout", "style", "resolve"])
    elif image_type == "text_or_markdown_render":
        queries.extend(["markdown", "render", "html"])
    return list(dict.fromkeys(queries))[:12]


def inspect_image(image: ImageEvidence, issue_text: str) -> Dict[str, Any]:
    return {
        "url": image.raw_url,
        "source_field": image.source_field,
        "image_type": image.image_type,
        "role": image.role,
        "extension": image.extension,
        "reason": image.reason,
        "likely_layers": _layers_for_image(image.image_type),
        "visual_queries": _queries_for_image(image.image_type, issue_text),
        "needs_vlm": True,
        "vlm_task": "extract_visible_text_ui_entities_visual_symptom_expected_actual_difference",
        "localization_use": "visual_symptom_to_program_layer_mapping",
    }

