from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _resolve_rg() -> str | None:
    explicit = os.environ.get("MYCODE_RG_BIN", "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    discovered = shutil.which("rg")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend((Path("/usr/bin/rg"), Path("/usr/local/bin/rg")))
    home = Path.home()
    for extension_root in (home / ".vscode-server/extensions", home / ".vscode/extensions"):
        if extension_root.is_dir():
            candidates.extend(sorted(extension_root.glob("*/bin/*/rg"), reverse=True))
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def _browser_probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "python_package": importlib.util.find_spec("playwright") is not None,
        "launch_ok": False,
        "error": "",
    }
    if not result["python_package"]:
        result["error"] = "playwright_python_package_missing"
        return result
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<h1>MAGNET browser ready</h1>")
            result["launch_ok"] = page.locator("h1").inner_text() == "MAGNET browser ready"
            result["executable"] = playwright.chromium.executable_path
            browser.close()
    except Exception as exc:  # noqa: BLE001 - this is an environment diagnostic.
        message = " ".join(str(exc).split())
        marker = "error while loading shared libraries:"
        if marker in message:
            message = message[message.index(marker) :]
        result["error"] = message[:1200]
    return result


def collect_runtime_capabilities() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    git_commit = ""
    try:
        git_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    git_dirty = False
    git_diff_sha256 = ""
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
        git_dirty = bool(diff)
        git_diff_sha256 = hashlib.sha256(diff).hexdigest() if diff else ""
    except (OSError, subprocess.SubprocessError):
        pass

    runtime_keys = (
        "MODEL_API_NAME",
        "VLM_MODEL_API_NAME",
        "DATASET",
        "SAMPLES",
        "TOP_K",
        "DEEP_AGENT",
        "LIGHTWEIGHT",
        "STRUCTURE_ONLY",
        "FULL_MM",
        "ALLOW_NETWORK",
        "ALLOW_BROWSER",
        "DOWNLOAD_IMAGES",
        "USE_VLM",
        "DYNAMIC_ROUNDS",
        "MAX_TOOL_ROUNDS",
        "MAX_REACT_STEPS",
        "MYCODE_FLOW_MODE",
        "MYCODE_DEEP_FLOW_AUTO",
        "MYCODE_FAST_SEED_PLANNER",
        "MYCODE_FAST_SEED_LLM",
        "MYCODE_FAST_SEED_SHORTLIST",
        "MYCODE_VLM_IMAGE_TRANSPORT",
        "MYCODE_AUTO_FETCH_REPOS",
        "MYCODE_REQUIRE_SOURCE_CHECKOUT",
        "MYCODE_BROWSER_REQUIRED",
        "MYCODE_CROSS_ROUND_PROTECTED_PREFIX",
        "MYCODE_HEAD_REPLACEMENT_MARGIN",
        "MYCODE_PLATEAU_REVIEW_OVERRIDE_ROUND",
    )
    return {
        "status": "ok",
        "project_root": str(root),
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "packages": {
            "requests": _version("requests"),
            "playwright": _version("playwright"),
            "pytest": _version("pytest"),
        },
        "commands": {"git": shutil.which("git"), "rg": _resolve_rg()},
        "browser": _browser_probe(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "git_diff_sha256": git_diff_sha256,
        "runtime_config": {key: os.environ[key] for key in runtime_keys if key in os.environ},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the MAGNET runtime before an experiment.")
    parser.add_argument("--require-browser", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = collect_runtime_capabilities()
    missing = []
    if not result["commands"].get("git"):
        missing.append("git")
    missing_optional = [
        name for name in ("rg",) if not result["commands"].get(name)
    ]
    if not result["packages"]["requests"]:
        missing.append("requests")
    if args.require_browser and not result["browser"]["launch_ok"]:
        missing.append("playwright_chromium_runtime")
    result["missing_required"] = missing
    result["missing_optional"] = missing_optional
    browser_requested = os.environ.get("ALLOW_BROWSER", "0") == "1"
    result["status"] = (
        "error"
        if missing
        else "degraded"
        if missing_optional or (browser_requested and not result["browser"]["launch_ok"])
        else "ok"
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"MAGNET runtime: {result['status']}")
        print(f"Python: {result['python']['executable']} ({result['python']['version']})")
        print(f"Git commit: {result['git_commit'] or 'unknown'}")
        print(f"Playwright Chromium launch: {result['browser']['launch_ok']}")
        if result["browser"]["error"]:
            print(f"Browser diagnostic: {result['browser']['error']}")
        if missing:
            print("Missing required capabilities: " + ", ".join(missing))
        if missing_optional:
            print("Missing optional capabilities: " + ", ".join(missing_optional))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
