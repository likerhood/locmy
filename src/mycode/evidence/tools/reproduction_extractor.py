from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse


CODE_KEYS = ("code", "input", "source", "text", "ts", "js", "python", "gist")
CONFIG_KEYS = (
    "parser",
    "plugins",
    "flags",
    "python",
    "mypy",
    "version",
    "printWidth",
    "tabWidth",
    "semi",
    "singleQuote",
    "trailingComma",
)

IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+\s+from\s+)?|require\()\s*['\"]([^'\"]+)['\"]"
)
SYMBOL_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b")
ROUTE_PARAM_RE = re.compile(r"[?&#/]([A-Za-z_][A-Za-z0-9_-]{2,})(?:=|/|$)")


def _flatten_query(query: str) -> Dict[str, str]:
    parsed = parse_qs(query, keep_blank_values=True)
    return {k: unquote(v[-1]) if v else "" for k, v in parsed.items()}


def _maybe_decode_base64(value: str) -> str | None:
    if not value or len(value) < 12:
        return None
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (binascii.Error, ValueError):
        return None
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(token in text for token in ("function", "class", "import", "const", "def ", "from ")):
        return text
    return None


def _platform(domain: str, path: str) -> str:
    text = f"{domain}{path}".lower()
    if "chartjs.org" in text and "/docs/" in path.lower() and "/samples/" in path.lower():
        return "chartjs_docs_sample"
    if "mypy-play" in text:
        return "mypy_play"
    if "prettier" in text:
        return "prettier_playground"
    if "typescriptlang" in text:
        return "typescript_playground"
    if "babeljs" in text:
        return "babel_repl"
    if "codesandbox" in text:
        return "codesandbox"
    if "stackblitz" in text:
        return "stackblitz"
    if "codepen" in text:
        return "codepen"
    if "jsfiddle" in text:
        return "jsfiddle"
    return "generic_playground"


def _codesandbox_id(domain: str, path: str) -> str | None:
    if domain.endswith(".csb.app"):
        return domain.split(".", 1)[0]
    if "codesandbox" not in domain:
        return None
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    slug = parts[-1]
    if "-" in slug:
        return slug.rsplit("-", 1)[-1]
    return slug


def _route_terms(url: str) -> List[str]:
    parsed = urlparse(url)
    terms: List[str] = []
    for part in re.split(r"[/_.-]+", unquote(parsed.path)):
        if len(part) >= 3 and not part.isdigit():
            terms.append(part)
    for key in parse_qs(parsed.query):
        if len(key) >= 3:
            terms.append(key)
    for match in ROUTE_PARAM_RE.finditer(url):
        terms.append(match.group(1))
    return list(dict.fromkeys(terms))[:24]


def _code_features(snippets: List[Dict[str, Any]]) -> Dict[str, Any]:
    imports: List[str] = []
    symbols: List[str] = []
    event_terms: List[str] = []
    option_terms: List[str] = []
    for snippet in snippets:
        text = str(snippet.get("value") or "")
        imports.extend(IMPORT_RE.findall(text))
        for sym in SYMBOL_RE.findall(text):
            if sym in {
                "function",
                "return",
                "const",
                "let",
                "var",
                "class",
                "import",
                "from",
                "export",
                "default",
            }:
                continue
            if sym.startswith("on") and len(sym) > 3:
                event_terms.append(sym)
            if any(token in sym.lower() for token in ("option", "config", "parser", "plugin", "legend", "scale")):
                option_terms.append(sym)
            symbols.append(sym)
    return {
        "imports": list(dict.fromkeys(imports))[:24],
        "symbols": list(dict.fromkeys(symbols))[:40],
        "event_terms": list(dict.fromkeys(event_terms))[:20],
        "option_terms": list(dict.fromkeys(option_terms))[:20],
    }


def _browser_observation_plan(platform: str, url: str, fields: Dict[str, str]) -> Dict[str, Any]:
    requested_file = fields.get("file") or fields.get("fragment.file")
    plan: Dict[str, Any] = {
        "requires_browser": platform in {"codesandbox", "stackblitz", "codepen", "jsfiddle"},
        "requires_network": True,
        "artifacts": [
            "visible_text",
            "page_title",
            "screenshot",
            "console_log",
        ],
        "localization_goal": "turn_live_reproduction_into_issue_specific_code_and_behavior_evidence",
    }
    if platform == "codesandbox":
        sandbox_id = _codesandbox_id(urlparse(url).netloc.lower(), urlparse(url).path)
        plan.update(
            {
                "sandbox_id": sandbox_id,
                "requested_file": requested_file,
                "preview_url": f"https://{sandbox_id}.csb.app/" if sandbox_id else None,
                "artifacts": [
                    "file_tree",
                    "requested_source_file",
                    "package_json_dependencies",
                    "preview_screenshot",
                    "console_log",
                    "user_interaction_trace",
                ],
                "interaction_tasks": [
                    "open_requested_file",
                    "read_event_handlers_and_options",
                    "open_preview",
                    "simulate_issue_steps_when_described",
                    "record_expected_actual_behavior",
                ],
            }
        )
    elif platform == "stackblitz":
        plan["artifacts"] = [
            "file_tree",
            "requested_source_file",
            "package_json_dependencies",
            "preview_screenshot",
            "console_log",
        ]
    return plan


def _likely_layers(platform: str, fields: Dict[str, str]) -> List[str]:
    layers = []
    if platform == "mypy_play":
        layers.extend(["type_checker", "binder", "symbol_table", "semantic_analyzer"])
    elif platform == "prettier_playground":
        layers.extend(["parser", "printer", "format_options", "doc_builder"])
    elif platform in {"typescript_playground", "babel_repl"}:
        layers.extend(["parser", "transformer", "compiler_option", "code_generator"])
    elif platform in {"codesandbox", "stackblitz", "codepen", "jsfiddle"}:
        layers.extend(["runtime_reproduction", "component", "configuration", "browser_behavior"])
    elif platform == "chartjs_docs_sample":
        layers.extend(["docs_sample", "chart_plugin", "legend_plugin", "event_handler"])
    if any("css" in key.lower() or "style" in value.lower() for key, value in fields.items()):
        layers.append("style_pipeline")
    return layers


def extract_reproduction(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    fields = _flatten_query(parsed.query)
    fragment_fields = _flatten_query(parsed.fragment)
    merged = {**fields, **{f"fragment.{k}": v for k, v in fragment_fields.items()}}
    platform = _platform(parsed.netloc, parsed.path)

    snippets = []
    config = {}
    for key, value in merged.items():
        bare_key = key.split(".")[-1]
        if bare_key in CODE_KEYS and value:
            decoded = _maybe_decode_base64(value)
            snippets.append(
                {
                    "field": key,
                    "value": decoded or value,
                    "encoding": "base64-url" if decoded else "plain-or-urlencoded",
                }
            )
        if bare_key in CONFIG_KEYS and value:
            config[bare_key] = value
        decoded = _maybe_decode_base64(value)
        if decoded and bare_key not in CODE_KEYS:
            snippets.append({"field": key, "value": decoded, "encoding": "base64-url"})

    semantic_queries = []
    for key in ("parser", "flags", "python", "mypy", "version"):
        if key in config:
            semantic_queries.append(f"{platform} {key} {config[key]}")
    if "file" in fields:
        semantic_queries.append(f"{platform} file {fields['file']}")
    for snippet in snippets[:3]:
        text = " ".join(str(snippet["value"]).split())[:160]
        if text:
            semantic_queries.append(text)
    if not semantic_queries:
        path_terms = " ".join(part for part in parsed.path.split("/") if part)
        if path_terms:
            semantic_queries.append(f"{platform} {path_terms}")

    browser_plan = _browser_observation_plan(platform, url, merged)
    route_terms = _route_terms(url)
    code_features = _code_features(snippets)

    semantic_queries.extend(route_terms[:8])
    semantic_queries.extend(code_features["imports"][:8])
    semantic_queries.extend(code_features["event_terms"][:8])
    semantic_queries.extend(code_features["option_terms"][:8])

    return {
        "url": url,
        "platform": platform,
        "role": "reproduction_entry",
        "query_fields": sorted(merged),
        "route_terms": route_terms,
        "config": config,
        "code_snippets": snippets,
        "code_features": code_features,
        "likely_layers": _likely_layers(platform, merged),
        "semantic_queries": list(dict.fromkeys(semantic_queries))[:24],
        "needs_browser": not snippets and platform in {"codesandbox", "stackblitz", "codepen", "jsfiddle"},
        "browser_observation_plan": browser_plan,
        "localization_use": "turn_reproduction_input_into_behavior_and_symbol_queries",
    }
