from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests

from mycode.evidence.tools.reproduction_extractor import extract_reproduction


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+\s+from\s+)?|require\()\s*['\"]([^'\"]+)['\"]"
)
SYMBOL_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b")


def _directory_path(directory: Dict[str, Any], directory_by_id: Dict[str, Dict[str, Any]]) -> str:
    parts = [str(directory.get("title") or "").strip("/")]
    parent = directory_by_id.get(str(directory.get("directory_shortid") or ""))
    seen = {str(directory.get("shortid") or "")}
    while parent and str(parent.get("shortid") or "") not in seen:
        seen.add(str(parent.get("shortid") or ""))
        parts.append(str(parent.get("title") or "").strip("/"))
        parent = directory_by_id.get(str(parent.get("directory_shortid") or ""))
    return "/".join(reversed([part for part in parts if part]))


def _module_path(module: Dict[str, Any], directory_by_id: Dict[str, Dict[str, Any]]) -> str:
    title = str(module.get("title") or module.get("name") or "untitled").strip("/")
    parent_id = str(module.get("directory_shortid") or "")
    parent = directory_by_id.get(parent_id)
    if not parent:
        return title
    prefix = _directory_path(parent, directory_by_id)
    return f"{prefix}/{title}" if prefix else title


def _extract_code_features(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    imports: List[str] = []
    symbols: List[str] = []
    event_terms: List[str] = []
    option_terms: List[str] = []
    config_terms: List[str] = []
    for file_item in files:
        path = str(file_item.get("path") or "")
        text = str(file_item.get("code") or file_item.get("code_preview") or "")
        imports.extend(IMPORT_RE.findall(text))
        if path.endswith("package.json"):
            try:
                package = json.loads(text)
            except json.JSONDecodeError:
                package = {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                value = package.get(section)
                if isinstance(value, dict):
                    config_terms.extend(value.keys())
        for symbol in SYMBOL_RE.findall(text):
            if symbol in {
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
            symbols.append(symbol)
            low = symbol.lower()
            if low.startswith("on") or low.startswith("handle"):
                event_terms.append(symbol)
            if any(token in low for token in ("option", "config", "plugin", "legend", "scale", "parser")):
                option_terms.append(symbol)
    return {
        "imports": list(dict.fromkeys(imports))[:40],
        "symbols": list(dict.fromkeys(symbols))[:80],
        "event_terms": list(dict.fromkeys(event_terms))[:40],
        "option_terms": list(dict.fromkeys(option_terms))[:40],
        "dependency_terms": list(dict.fromkeys(config_terms))[:40],
    }


def _source_tree_summary(files: List[Dict[str, Any]], requested_file: str | None = None) -> Dict[str, Any]:
    source_exts = {".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".css", ".scss", ".json", ".md", ".mdx"}
    language_counts: Dict[str, int] = {}
    source_like = 0
    package_json_count = 0
    requested_norm = str(requested_file or "").lstrip("/")
    requested_found = False
    largest: List[Dict[str, Any]] = []
    for file_item in files:
        path = str(file_item.get("path") or "")
        suffix = Path(path).suffix.lower()
        label = suffix.lstrip(".") or "no_extension"
        language_counts[label] = language_counts.get(label, 0) + 1
        if suffix in source_exts:
            source_like += 1
        if path.endswith("package.json"):
            package_json_count += 1
        if requested_norm and path.lstrip("/") == requested_norm:
            requested_found = True
        largest.append({"path": path, "bytes": int(file_item.get("bytes") or 0)})
    largest.sort(key=lambda item: (-int(item["bytes"]), item["path"]))
    return {
        "total_files": len(files),
        "source_like_files": source_like,
        "package_json_files": package_json_count,
        "language_counts": dict(sorted(language_counts.items())),
        "requested_file": requested_norm or None,
        "requested_file_found": requested_found if requested_norm else None,
        "largest_files": largest[:12],
    }


def _write_source_files(cache_root: Path, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    source_root = cache_root / "source_files"
    source_root.mkdir(parents=True, exist_ok=True)
    written: List[Dict[str, Any]] = []
    for file_item in files:
        path = str(file_item.get("path") or "")
        code = str(file_item.get("code") or "")
        if not path or not code:
            continue
        safe_parts = [part for part in path.split("/") if part and part not in {"..", "."}]
        target = source_root.joinpath(*safe_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        written.append({"path": path, "local_path": str(target), "bytes": len(code.encode("utf-8"))})
    return written


def _normalize_browser_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    if "?" in text:
        text = text.split("?", 1)[0]
    if "#" in text:
        text = text.split("#", 1)[0]
    for marker in ("/src/", "/public/", "/packages/"):
        if marker in text:
            text = text[text.index(marker) + 1 :]
            break
    if text.startswith("file://"):
        parsed = urlparse(text)
        text = parsed.path or text
    text = posixpath.normpath(text).lstrip("/")
    if text == "." or text.startswith("../"):
        return ""
    return text


def _compact_text(text: str, limit: int = 2000) -> str:
    return " ".join(str(text or "").split())[:limit]


def _safe_locator_texts(page: Any, selectors: List[str], *, limit: int = 80) -> List[str]:
    texts: List[str] = []
    seen = set()
    for selector in selectors:
        try:
            values = page.locator(selector).all_text_contents()
        except Exception:
            continue
        for value in values:
            text = _compact_text(value, 600)
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
            if len(texts) >= limit:
                return texts
    return texts


def _extract_monaco_models(page: Any) -> List[Dict[str, Any]]:
    script = """
    () => {
      const out = [];
      try {
        if (window.monaco && window.monaco.editor && window.monaco.editor.getModels) {
          for (const model of window.monaco.editor.getModels()) {
            const value = model.getValue ? model.getValue() : "";
            const uri = model.uri ? String(model.uri) : "";
            const path = model.uri && model.uri.path ? String(model.uri.path) : uri;
            out.push({uri, path, code: value, bytes: value.length});
          }
        }
      } catch (err) {
        out.push({error: String(err)});
      }
      return out;
    }
    """
    try:
        raw_models = page.evaluate(script)
    except Exception as exc:  # noqa: BLE001
        return [{"status": "failed", "error": str(exc)[:500]}]
    models: List[Dict[str, Any]] = []
    for item in raw_models or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        path = _normalize_browser_path(str(item.get("path") or item.get("uri") or ""))
        if not code:
            continue
        models.append(
            {
                "path": path or f"browser_model_{len(models) + 1}.txt",
                "code": code,
                "code_preview": code[:3000],
                "bytes": int(item.get("bytes") or len(code)),
                "source": "browser_monaco_model",
                "uri": str(item.get("uri") or "")[:500],
            }
        )
    return models[:80]


def _extract_editor_text_fallback(page: Any) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    selectors = ["textarea", "pre code", "pre", "code"]
    for idx, text in enumerate(_safe_locator_texts(page, selectors, limit=20), start=1):
        if len(text) < 80:
            continue
        if not any(token in text for token in ("import ", "function ", "const ", "class ", "def ", "export ")):
            continue
        files.append(
            {
                "path": f"browser_extracted_{idx}.txt",
                "code": text,
                "code_preview": text[:3000],
                "bytes": len(text),
                "source": "browser_dom_text_fallback",
            }
        )
    return files[:20]


def _extract_preview_frames(page: Any) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []
    try:
        all_frames = list(page.frames)
    except Exception:
        return frames
    for frame in all_frames[:12]:
        if frame == page.main_frame:
            continue
        try:
            text = frame.locator("body").inner_text(timeout=1500)
        except Exception:
            text = ""
        try:
            title = frame.title()
        except Exception:
            title = ""
        frames.append(
            {
                "url": str(getattr(frame, "url", ""))[:500],
                "title": str(title)[:200],
                "visible_text_preview": _compact_text(text, 2000),
            }
        )
    return frames


def _merge_source_files(
    api_files: List[Dict[str, Any]] | None,
    browser_files: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for file_item in api_files or []:
        path = _normalize_browser_path(str(file_item.get("path") or ""))
        if path:
            merged[path] = {**file_item, "path": path}
    for file_item in browser_files or []:
        path = _normalize_browser_path(str(file_item.get("path") or ""))
        if not path:
            continue
        current = merged.get(path)
        if current and current.get("code"):
            continue
        merged[path] = {**(current or {}), **file_item, "path": path}
    return list(merged.values())


def _browser_completeness(
    *,
    api_result: Dict[str, Any] | None = None,
    browser_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    api_result = api_result or {}
    browser_result = browser_result or {}
    source_summary = api_result.get("source_tree_summary", {}) or {}
    return {
        "api_file_tree": api_result.get("status") == "ok" and bool(api_result.get("files")),
        "source_files_read": api_result.get("status") == "ok" and bool(api_result.get("files")),
        "package_json_read": bool(api_result.get("package_dependencies")),
        "requested_source_file_found": source_summary.get("requested_file_found"),
        "preview_console": browser_result.get("status") == "ok",
        "preview_screenshot": bool(browser_result.get("screenshot")),
        "interaction_trace": bool(browser_result.get("interaction_trace")),
        "network_trace": bool(browser_result.get("network_responses") or browser_result.get("failed_requests")),
        "browser_file_tree": bool(browser_result.get("file_tree_candidates")),
        "active_editor_text": bool(browser_result.get("dom_source_files")),
        "preview_visible_text": bool(browser_result.get("preview_frames") or browser_result.get("visible_text_preview")),
    }


def _load_cached_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data["cache_hit"] = True
        return data
    return None


def _cached_reproduction_is_usable(
    cached: Dict[str, Any],
    *,
    allow_network: bool,
    allow_browser: bool,
    require_source_files: bool,
    require_browser: bool,
) -> bool:
    if not cached:
        return False
    if cached.get("status") != "ok" and (require_source_files or require_browser):
        return False
    if require_source_files and not (cached.get("source_files") or cached.get("source_files_written")):
        return False
    if require_browser and not cached.get("live_browser"):
        return False
    if allow_browser and not cached.get("browser_used") and not require_browser:
        return False
    if allow_network and not cached.get("network_used") and not (cached.get("source_files") or cached.get("live_browser")):
        return False
    return True


def _runtime_trace_from_browser(browser_result: Dict[str, Any]) -> Dict[str, Any]:
    console_logs = [str(item) for item in browser_result.get("console_logs", []) or []]
    page_errors = [str(item) for item in browser_result.get("page_errors", []) or []]
    failed_requests = [
        str(item.get("url") or item)
        for item in browser_result.get("failed_requests", []) or []
        if item
    ]
    interactions = [
        json.dumps(item, ensure_ascii=False, sort_keys=True)
        for item in browser_result.get("interaction_trace", []) or []
        if item
    ]
    preview_texts = [
        str(frame.get("visible_text_preview") or "")
        for frame in browser_result.get("preview_frames", []) or []
        if isinstance(frame, dict)
    ]
    if browser_result.get("visible_text_preview"):
        preview_texts.append(str(browser_result.get("visible_text_preview") or ""))
    trace_lines: List[str] = []
    for label, values in (
        ("console", console_logs),
        ("page_error", page_errors),
        ("failed_request", failed_requests),
        ("interaction", interactions),
        ("preview", preview_texts),
    ):
        for value in values:
            compact = _compact_text(value, 1200)
            if compact:
                trace_lines.append(f"{label}: {compact}")
    return {
        "source": "browser_reproduction_reader",
        "trace": "\n".join(trace_lines),
        "console_logs": console_logs[:80],
        "page_errors": page_errors[:40],
        "failed_requests": failed_requests[:40],
        "interaction_trace": browser_result.get("interaction_trace", [])[:80],
        "preview_texts": [_compact_text(text, 1200) for text in preview_texts[:20] if text],
    }


def _try_codesandbox_api(sandbox_id: str, timeout: int) -> Dict[str, Any]:
    api_url = f"https://codesandbox.io/api/v1/sandboxes/{sandbox_id}"
    try:
        response = requests.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": "mycode-evidence-agent/0.1"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - this is an opportunistic extractor.
        return {
            "status": "api_unavailable",
            "api_url": api_url,
            "error": str(exc),
        }

    data = payload.get("data", payload)
    modules = data.get("modules", []) if isinstance(data, dict) else []
    directories = data.get("directories", []) if isinstance(data, dict) else []
    directory_by_id = {
        str(directory.get("shortid") or ""): directory
        for directory in directories
        if isinstance(directory, dict)
    }
    files: List[Dict[str, Any]] = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        title = _module_path(module, directory_by_id)
        code = module.get("code") or ""
        if title:
            files.append(
                {
                    "path": title,
                    "code": code,
                    "code_preview": code[:3000],
                    "bytes": len(code.encode("utf-8")),
                    "module_id": module.get("id") or module.get("shortid"),
                }
            )
    features = _extract_code_features(files)
    dependencies = []
    for file_item in files:
        if str(file_item.get("path") or "").endswith("package.json"):
            try:
                package = json.loads(str(file_item.get("code") or "{}"))
            except json.JSONDecodeError:
                package = {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                values = package.get(section)
                if isinstance(values, dict):
                    dependencies.extend(
                        {"name": key, "version": str(value), "section": section}
                        for key, value in values.items()
                    )
    summary = _source_tree_summary(files)
    return {
        "status": "ok",
        "api_url": api_url,
        "file_count": len(files),
        "source_tree_summary": summary,
        "files": [
            {key: value for key, value in file_item.items() if key != "code"}
            for file_item in files[:80]
        ],
        "full_files": files,
        "package_dependencies": dependencies[:80],
        "code_features": features,
        "semantic_queries": list(
            dict.fromkeys(
                [
                    *features["imports"],
                    *features["event_terms"],
                    *features["option_terms"],
                    *features["dependency_terms"],
                ]
            )
        )[:60],
    }


def _read_with_playwright(
    url: str,
    *,
    cache_dir: Path,
    timeout_ms: int,
    interaction_tasks: List[str] | None = None,
) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright

    screenshot = cache_dir / "preview.png"
    after_screenshot = cache_dir / "preview_after_interactions.png"
    html_path = cache_dir / "page.html"
    interaction_tasks = interaction_tasks or []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        console_logs: List[str] = []
        page_errors: List[str] = []
        network_responses: List[Dict[str, Any]] = []
        failed_requests: List[Dict[str, Any]] = []
        page.on("console", lambda msg: console_logs.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "response",
            lambda response: network_responses.append(
                {
                    "status": response.status,
                    "url": response.url[:300],
                    "content_type": response.headers.get("content-type", "")[:80],
                }
            )
            if len(network_responses) < 120
            else None,
        )
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                {
                    "url": request.url[:300],
                    "failure": str(request.failure or "")[:200],
                }
            )
            if len(failed_requests) < 80
            else None,
        )
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        title = page.title()
        visible_text = page.locator("body").inner_text(timeout=5000)
        file_tree_candidates = _safe_locator_texts(
            page,
            [
                "[role='treeitem']",
                "[data-testid*='file']",
                "[data-test-id*='file']",
                "[class*='File']",
                "[class*='file']",
                "aside",
                "nav",
            ],
            limit=100,
        )
        dom_source_files = _merge_source_files(
            _extract_monaco_models(page),
            _extract_editor_text_fallback(page),
        )
        preview_frames = _extract_preview_frames(page)
        html_path.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(screenshot), full_page=True)

        interaction_trace: List[Dict[str, Any]] = []
        snapshots: List[Dict[str, Any]] = [
            {
                "stage": "before_interactions",
                "visible_text_preview": " ".join(visible_text.split())[:2000],
                "console_count": len(console_logs),
                "page_error_count": len(page_errors),
            }
        ]
        if interaction_tasks:
            viewport = page.viewport_size or {"width": 1440, "height": 1000}
            points = [
                (viewport["width"] // 2, viewport["height"] // 2),
                (viewport["width"] // 3, viewport["height"] // 2),
                (viewport["width"] * 2 // 3, viewport["height"] // 2),
            ]
            for x, y in points:
                try:
                    page.mouse.move(x, y)
                    page.wait_for_timeout(250)
                    interaction_trace.append(
                        {
                            "action": "mouse_move",
                            "x": x,
                            "y": y,
                            "console_count": len(console_logs),
                            "visible_text_preview": " ".join(
                                page.locator("body").inner_text(timeout=5000).split()
                            )[:1000],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - keep trace instead of failing the tool.
                    interaction_trace.append({"action": "mouse_move", "x": x, "y": y, "error": str(exc)})
            if any("click" in task or "simulate" in task for task in interaction_tasks):
                try:
                    page.mouse.click(viewport["width"] // 2, viewport["height"] // 2)
                    page.wait_for_timeout(300)
                    interaction_trace.append(
                        {
                            "action": "click_center",
                            "console_count": len(console_logs),
                            "visible_text_preview": " ".join(
                                page.locator("body").inner_text(timeout=5000).split()
                            )[:1000],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    interaction_trace.append({"action": "click_center", "error": str(exc)})
            try:
                page.screenshot(path=str(after_screenshot), full_page=True)
            except Exception:
                after_screenshot = screenshot
        snapshots.append(
            {
                "stage": "after_interactions",
                "visible_text_preview": " ".join(page.locator("body").inner_text(timeout=5000).split())[:2000],
                "console_count": len(console_logs),
                "page_error_count": len(page_errors),
            }
        )

        browser.close()
    return {
        "status": "ok",
        "page_title": title,
        "visible_text_preview": " ".join(visible_text.split())[:4000],
        "console_logs": console_logs[:80],
        "page_errors": page_errors[:40],
        "screenshot": str(screenshot),
        "after_interaction_screenshot": str(after_screenshot) if after_screenshot.exists() else None,
        "html_path": str(html_path),
        "file_tree_candidates": file_tree_candidates,
        "dom_source_files": [
            {key: value for key, value in file_item.items() if key != "code"}
            for file_item in dom_source_files[:80]
        ],
        "full_dom_source_files": dom_source_files,
        "preview_frames": preview_frames,
        "interaction_trace": interaction_trace,
        "browser_snapshots": snapshots,
        "network_responses": network_responses[:80],
        "failed_requests": failed_requests[:40],
    }


def read_browser_reproduction(
    url: str,
    *,
    cache_dir: str | Path,
    allow_network: bool = False,
    allow_browser: bool = False,
    timeout: int = 30,
    force_refresh: bool = False,
    require_source_files: bool = False,
    require_browser: bool = False,
) -> Dict[str, Any]:
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest = cache_root / "browser_reproduction.json"
    cached = None if force_refresh else _load_cached_json(manifest)
    if cached and _cached_reproduction_is_usable(
        cached,
        allow_network=allow_network,
        allow_browser=allow_browser,
        require_source_files=require_source_files,
        require_browser=require_browser,
    ):
        return cached

    parsed = urlparse(url)
    reproduction = extract_reproduction(url)
    browser_plan = reproduction.get("browser_observation_plan", {})

    result: Dict[str, Any] = {
        "url": url,
        "platform": reproduction.get("platform"),
        "role": "runtime_reproduction_evidence",
        "parsed_reproduction": reproduction,
        "browser_observation_plan": browser_plan,
        "network_used": False,
        "browser_used": False,
        "localization_use": "convert_reproduction_page_to_code_config_behavior_queries",
        "warnings": [],
    }

    sandbox_id = browser_plan.get("sandbox_id")
    if allow_network and sandbox_id and reproduction.get("platform") == "codesandbox":
        api_result = _try_codesandbox_api(str(sandbox_id), timeout=timeout)
        if api_result.get("status") == "ok":
            api_result["source_tree_summary"] = _source_tree_summary(
                api_result.get("full_files", []) or [],
                requested_file=str(browser_plan.get("requested_file") or ""),
            )
        result["codesandbox_api"] = api_result
        result["network_used"] = True
        if api_result.get("status") == "ok":
            full_files = api_result.pop("full_files", [])
            written_files = _write_source_files(cache_root, full_files)
            result["status"] = "ok"
            result["source_files"] = api_result.get("files", [])
            result["source_files_written"] = written_files
            result["package_dependencies"] = api_result.get("package_dependencies", [])
            result["code_features"] = api_result.get("code_features", {})
            result["semantic_queries"] = api_result.get("semantic_queries", [])
            result["requested_source_file"] = next(
                (
                    item
                    for item in result["source_files"]
                    if str(item.get("path") or "").lstrip("/")
                    == str(browser_plan.get("requested_file") or "").lstrip("/")
                ),
                None,
            )
            result["file_tree"] = sorted(str(item.get("path") or "") for item in result["source_files"])
            result["source_tree_summary"] = api_result.get("source_tree_summary", {})
            result["browser_capabilities"] = _browser_completeness(api_result=api_result)
            manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            api_manifest = cache_root / "codesandbox_api.json"
            api_manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["cache_path"] = str(manifest)
            if not allow_browser:
                return result
        else:
            result["warnings"].append("codesandbox_api_unavailable")

    if allow_browser:
        if _playwright_available():
            live_url = browser_plan.get("preview_url") or url
            try:
                browser_result = _read_with_playwright(
                    str(live_url),
                    cache_dir=cache_root,
                    timeout_ms=timeout * 1000,
                    interaction_tasks=list(browser_plan.get("interaction_tasks") or []),
                )
                result["live_browser"] = browser_result
                result["runtime_trace"] = _runtime_trace_from_browser(browser_result)
                result["browser_used"] = True
                result["network_used"] = True
                result["browser_capabilities"] = {
                    **_browser_completeness(
                        api_result=result.get("codesandbox_api", {}) or {},
                        browser_result=browser_result,
                    ),
                }
                browser_full_files = browser_result.pop("full_dom_source_files", []) or []
                if browser_full_files:
                    existing_source_files = result.get("source_files", []) or []
                    merged_full = _merge_source_files(existing_source_files, browser_full_files)
                    result["source_files"] = [
                        {key: value for key, value in item.items() if key != "code"}
                        for item in merged_full[:80]
                    ]
                    result["source_files_written"] = _write_source_files(cache_root, merged_full)
                    result["file_tree"] = sorted(str(item.get("path") or "") for item in result["source_files"])
                    result["code_features"] = _extract_code_features(merged_full)
                    result["semantic_queries"] = list(
                        dict.fromkeys(
                            [
                                *(result.get("semantic_queries", []) or []),
                                *result["code_features"].get("imports", []),
                                *result["code_features"].get("event_terms", []),
                                *result["code_features"].get("option_terms", []),
                            ]
                        )
                    )[:80]
                if browser_result.get("status") == "ok":
                    result["status"] = "ok"
                    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    result["cache_path"] = str(manifest)
                    return result
            except Exception as exc:  # noqa: BLE001 - preserve failure as evidence state.
                result["warnings"].append(f"browser_read_failed: {exc}")
        else:
            result["warnings"].append("playwright_not_installed")

    # If the API already gave us files but browser preview failed, keep the useful code evidence.
    if result.get("status") == "ok":
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["cache_path"] = str(manifest)
        return result

    result["status"] = "parsed_needs_browser_or_network"
    result["missing_tools"] = [
        "browser_page_reader",
        "source_file_reader_for_online_sandbox",
        "runtime_interaction_recorder",
    ]
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["cache_path"] = str(manifest)
    return result
