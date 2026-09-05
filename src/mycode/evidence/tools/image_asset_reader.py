from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Any, Dict
from urllib.parse import unquote, urlparse

import requests


def _safe_name(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        suffix = ".img"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(parsed.path).stem)[:80] or "image"
    return f"{stem}_{digest}{suffix}"


def _candidate_existing_paths(url: str, cache_root: Path) -> list[Path]:
    parsed = urlparse(url)
    candidates: list[Path] = []
    if parsed.scheme == "file":
        candidates.append(Path(unquote(parsed.path)))
    elif not parsed.scheme and url:
        candidates.append(Path(url))

    safe = _safe_name(url)
    candidates.append(cache_root / safe)

    env_dir = os.environ.get("MYCODE_IMAGE_DIR") or os.environ.get("GALA_IMAGE_DIR")
    if env_dir:
        root = Path(env_dir)
        candidates.append(root / safe)
        basename = Path(parsed.path).name
        if basename:
            candidates.append(root / basename)
            for child in root.rglob(basename):
                candidates.append(child)
                break
    return candidates


def _detect_format(data: bytes, path: str) -> str:
    head = data[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return "webp"
    if path.lower().endswith(".svg") or data[:512].lstrip().startswith((b"<svg", b"<?xml")):
        return "svg"
    return "unknown"


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data.startswith((b"GIF87a", b"GIF89a")):
        return struct.unpack("<HH", data[6:10])
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    idx = 2
    while idx + 9 < len(data):
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        idx += 2
        if marker in {0xD8, 0xD9}:
            continue
        if idx + 2 > len(data):
            break
        length = struct.unpack(">H", data[idx: idx + 2])[0]
        if length < 2 or idx + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if idx + 7 <= len(data):
                height, width = struct.unpack(">HH", data[idx + 3: idx + 7])
                return width, height
        idx += length
    return None


def _dimensions(fmt: str, data: bytes) -> tuple[int, int] | None:
    if fmt == "png":
        return _png_size(data)
    if fmt == "jpeg":
        return _jpeg_size(data)
    if fmt == "gif":
        return _gif_size(data)
    return None


def _pil_validation(path: Path) -> Dict[str, Any]:
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - Pillow is optional for the light toolchain.
        return {"pil_available": False, "pil_readable": None, "error": str(exc)}

    try:
        with Image.open(path) as image:
            image.verify()
            return {
                "pil_available": True,
                "pil_readable": True,
                "pil_format": str(image.format or "").lower(),
                "pil_size": {"width": image.width, "height": image.height},
            }
    except Exception as exc:  # noqa: BLE001 - preserve diagnostics for batch completeness.
        return {"pil_available": True, "pil_readable": False, "error": str(exc)}


def read_image_asset(
    url: str,
    *,
    cache_dir: str | Path,
    timeout: int = 30,
    download: bool = True,
) -> Dict[str, Any]:
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    local_path = cache_root / _safe_name(url)

    for candidate in _candidate_existing_paths(url, cache_root):
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            local_path = candidate
            break

    if download and not local_path.exists():
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "mycode-evidence-agent/0.1"})
        response.raise_for_status()
        local_path.write_bytes(response.content)

    if not local_path.exists():
        return {
            "url": url,
            "status": "missing",
            "local_path": str(local_path),
            "processable": False,
            "processable_reason": "local_file_missing_and_download_disabled_or_failed",
        }

    data = local_path.read_bytes()
    fmt = _detect_format(data, str(local_path))
    dims = _dimensions(fmt, data)
    pil = _pil_validation(local_path) if data else {"pil_readable": False}
    processable_by_format = fmt in {"png", "jpeg", "gif", "webp"}
    pil_readable = pil.get("pil_readable")
    processable = bool(data) and processable_by_format and pil_readable is not False
    if not data:
        reason = "empty_file"
    elif not processable_by_format:
        reason = f"unsupported_format:{fmt}"
    elif pil_readable is False:
        reason = "pil_cannot_read_image"
    else:
        reason = "processable_raster_image"
    return {
        "url": url,
        "status": "ok" if data else "empty",
        "local_path": str(local_path),
        "bytes": len(data),
        "format": fmt,
        "dimensions": {"width": dims[0], "height": dims[1]} if dims else pil.get("pil_size"),
        "pil_validation": pil,
        "processable": bool(data) and processable,
        "processable_reason": reason,
        "needs_vlm": bool(data) and processable,
        "localization_use": "image_asset_ready_for_vlm" if processable else "image_asset_not_vlm_processable",
    }
