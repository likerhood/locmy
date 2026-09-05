from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List

from mycode.evidence.image_extractor import classify_image, extract_image_urls
from mycode.evidence.tools.image_asset_reader import read_image_asset
from mycode.evidence.tools.vlm_image_reader import heuristic_image_understanding, try_analyze_image_with_vlm
from mycode.schemas.evidence import NormalizedSample


@dataclass
class ImageBatchRecord:
    instance_id: str
    repo: str
    dataset: str
    url: str
    source_field: str
    status: str
    asset: dict[str, Any]
    analysis: dict[str, Any]
    attempts: int = 1
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "dataset": self.dataset,
            "url": self.url,
            "source_field": self.source_field,
            "status": self.status,
            "asset": self.asset,
            "analysis": self.analysis,
            "attempts": self.attempts,
            "errors": self.errors,
        }


def _load_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    cached: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(item.get("instance_id") or ""), str(item.get("url") or ""))
        if key[0] and key[1]:
            cached[key] = item
    return cached


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def _cached_record_is_usable(record: dict[str, Any], *, require_vlm: bool) -> bool:
    if not record:
        return False
    status = str(record.get("status") or "")
    asset = record.get("asset", {}) or {}
    analysis = record.get("analysis", {}) or {}
    if status == "failed":
        return False
    if asset.get("processable") and require_vlm and analysis.get("vlm_status") != "ok":
        return False
    return status in {"ok", "skipped", "non_processable"}


def _manifest_path(cache_file: Path) -> Path:
    if cache_file.suffix == ".jsonl":
        return cache_file.with_suffix(".manifest.json")
    return cache_file.with_name(cache_file.name + ".manifest.json")


def _analyze_with_retry(
    *,
    asset: dict[str, Any],
    sample: NormalizedSample,
    use_vlm: bool,
    retries: int,
    sleep_seconds: float,
    sleep_schedule: list[float] | None = None,
) -> tuple[dict[str, Any], int, list[str]]:
    if not asset.get("processable"):
        return {
            "vlm_status": "skipped",
            "reason": "image_not_processable",
        }, 0, []

    if not use_vlm:
        return heuristic_image_understanding(
            image_format=str(asset.get("format") or ""),
            issue_summary=sample.issue_text,
            repo=sample.repo,
            local_path=asset.get("local_path"),
        ), 1, []

    errors: list[str] = []
    attempts = 0
    for attempt in range(1, max(1, retries) + 1):
        attempts = attempt
        result = try_analyze_image_with_vlm(
            image_path=asset["local_path"],
            image_format=str(asset.get("format") or ""),
            issue_summary=sample.issue_text,
            repo=sample.repo,
            source_url=str(asset.get("url") or ""),
        )
        if result.get("vlm_status") == "ok":
            return result, attempts, errors
        errors.append(str(result.get("error") or result))
        if attempt < retries:
            delay = sleep_seconds
            if sleep_schedule:
                delay = sleep_schedule[min(attempt - 1, len(sleep_schedule) - 1)]
            time.sleep(delay)
    return {
        "vlm_status": "failed",
        "errors": errors,
    }, attempts, errors


def _empty_validation_entry() -> dict[str, int]:
    return {
        "expected": 0,
        "cached": 0,
        "usable": 0,
        "processable_expected": 0,
        "processable_covered": 0,
        "failed": 0,
        "non_processable": 0,
        "missing_cache": 0,
    }


def validate_image_understanding_cache(
    samples: Iterable[NormalizedSample],
    *,
    cache_path: str | Path,
    asset_cache_dir: str | Path | None = None,
    require_vlm: bool = False,
    allow_local_asset_probe: bool = True,
) -> dict[str, Any]:
    """Validate cached image understanding without making network or LLM calls.

    The expected set is derived only from ``problem_statement`` URLs, matching
    the evidence pipeline. Completeness is stricter for processable raster
    images: a cached record must be usable and status=ok. Missing, SVG or
    non-processable assets are reported separately so a caller can decide
    whether to proceed without rerunning expensive VLM/image IR.
    """

    cache_file = Path(cache_path)
    asset_root = Path(asset_cache_dir) if asset_cache_dir is not None else None
    cached = _load_cache(cache_file)
    by_instance: dict[str, dict[str, int]] = {}
    expected_images = 0
    cached_images = 0
    usable_records = 0
    processable_expected = 0
    processable_covered = 0
    failed = 0
    non_processable = 0
    missing_cache = 0
    missing_assets = 0
    image_type_counts: Counter[str] = Counter()
    vlm_status_counts: Counter[str] = Counter()
    sample_records: list[dict[str, Any]] = []

    for sample in samples:
        for url, source_field in extract_image_urls({}, sample.issue_text):
            expected_images += 1
            entry = by_instance.setdefault(sample.instance_id, _empty_validation_entry())
            entry["expected"] += 1
            key = (sample.instance_id, url)
            record = cached.get(key)
            image = classify_image(url, source_field=source_field, issue_text=sample.issue_text)
            image_type_counts[image.image_type] += 1
            if record:
                cached_images += 1
                entry["cached"] += 1
                asset = record.get("asset", {}) or {}
                analysis = record.get("analysis", {}) or {}
                vlm_status_counts[str(analysis.get("vlm_status") or "unknown")] += 1
                usable = _cached_record_is_usable(record, require_vlm=require_vlm)
                if usable:
                    usable_records += 1
                    entry["usable"] += 1
                if asset.get("processable"):
                    processable_expected += 1
                    entry["processable_expected"] += 1
                    if usable and record.get("status") == "ok":
                        processable_covered += 1
                        entry["processable_covered"] += 1
                else:
                    non_processable += 1
                    entry["non_processable"] += 1
                if record.get("status") == "failed":
                    failed += 1
                    entry["failed"] += 1
                if len(sample_records) < 80:
                    sample_records.append(
                        {
                            "instance_id": sample.instance_id,
                            "url": url,
                            "status": record.get("status"),
                            "cached": True,
                            "usable": usable,
                            "image_type": asset.get("image_type") or image.image_type,
                            "vlm_status": analysis.get("vlm_status"),
                        }
                    )
                continue

            missing_cache += 1
            entry["missing_cache"] += 1
            local_asset: dict[str, Any] = {}
            if allow_local_asset_probe and asset_root is not None:
                try:
                    local_asset = read_image_asset(
                        url,
                        cache_dir=asset_root / sample.dataset / sample.instance_id,
                        download=False,
                    )
                except Exception as exc:  # noqa: BLE001 - validation should keep going.
                    local_asset = {"status": "tool_error", "error": str(exc)}
            if local_asset.get("processable"):
                processable_expected += 1
                entry["processable_expected"] += 1
            else:
                non_processable += 1
                entry["non_processable"] += 1
                if local_asset.get("status") in {"missing", "not_found", ""} or not local_asset:
                    missing_assets += 1
            if len(sample_records) < 80:
                sample_records.append(
                    {
                        "instance_id": sample.instance_id,
                        "url": url,
                        "status": "missing_cache",
                        "cached": False,
                        "usable": False,
                        "image_type": image.image_type,
                        "local_asset_status": local_asset.get("status"),
                    }
                )

    incomplete_instances = {
        key: value
        for key, value in sorted(by_instance.items())
        if value["failed"] or value["missing_cache"] or value["processable_expected"] != value["processable_covered"]
    }
    return {
        "cache_path": str(cache_file),
        "asset_cache_dir": str(asset_root) if asset_root else None,
        "expected_images": expected_images,
        "cached_images": cached_images,
        "usable_records": usable_records,
        "processable_images": processable_expected,
        "covered_processable_images": processable_covered,
        "non_processable_images": non_processable,
        "missing_cache_records": missing_cache,
        "missing_assets": missing_assets,
        "failed": failed,
        "complete_for_cached_processable_images": (
            expected_images == cached_images
            and failed == 0
            and processable_expected == processable_covered
        ),
        "complete_instances": sum(
            1
            for item in by_instance.values()
            if item["missing_cache"] == 0
            and item["failed"] == 0
            and item["processable_expected"] == item["processable_covered"]
        ),
        "incomplete_instances": incomplete_instances,
        "image_type_counts": dict(image_type_counts.most_common()),
        "vlm_status_counts": dict(vlm_status_counts.most_common()),
        "sample_image_records": sample_records,
    }


def build_image_understanding_batch(
    samples: Iterable[NormalizedSample],
    *,
    cache_path: str | Path,
    asset_cache_dir: str | Path,
    allow_network: bool = False,
    use_vlm: bool = False,
    retries: int = 3,
    sleep_seconds: float = 2.0,
    sleep_schedule: list[float] | None = None,
    reuse_cache: bool = True,
    validate_cache: bool = True,
    force: bool = False,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """Analyze problem_statement images with cache and completeness accounting.

    Completeness is counted over processable local raster images, not over every
    URL. This matters because issue bodies may contain SVGs, expired links, or
    files that cannot be read as images. Those should be visible diagnostics,
    but they should not block downstream localization forever.
    """

    cache_file = Path(cache_path)
    asset_root = Path(asset_cache_dir)
    cached = _load_cache(cache_file) if reuse_cache and not force else {}
    records: List[dict[str, Any]] = []
    expected = 0
    processable_expected = 0
    processable_covered = 0
    non_processable = 0
    missing = 0
    reused = 0
    processed = 0
    failed = 0

    for sample in samples:
        pairs = extract_image_urls({}, sample.issue_text)
        for url, source_field in pairs:
            expected += 1
            key = (sample.instance_id, url)
            if (
                key in cached
                and (not validate_cache or _cached_record_is_usable(cached[key], require_vlm=use_vlm))
            ):
                record = dict(cached[key])
                record["cache_hit"] = True
                records.append(record)
                reused += 1
                asset = record.get("asset", {}) or {}
                if asset.get("processable"):
                    processable_expected += 1
                    if record.get("status") == "ok":
                        processable_covered += 1
                else:
                    non_processable += 1
                    if asset.get("status") == "missing":
                        missing += 1
                continue

            try:
                asset = read_image_asset(
                    url,
                    cache_dir=asset_root / sample.dataset / sample.instance_id,
                    download=allow_network,
                )
                image = classify_image(url, source_field=source_field, issue_text=sample.issue_text)
                analysis, attempts, errors = _analyze_with_retry(
                    asset=asset,
                    sample=sample,
                    use_vlm=use_vlm,
                    retries=retries,
                    sleep_seconds=sleep_seconds,
                    sleep_schedule=sleep_schedule,
                )
                if not asset.get("processable"):
                    status = "non_processable"
                else:
                    status = "ok" if analysis.get("vlm_status") in {"ok", "heuristic_only"} else "skipped"
                if analysis.get("vlm_status") == "failed":
                    status = "failed"
                record = ImageBatchRecord(
                    instance_id=sample.instance_id,
                    repo=sample.repo,
                    dataset=sample.dataset,
                    url=url,
                    source_field=source_field,
                    status=status,
                    asset={**asset, "image_type": image.image_type, "role": image.role},
                    analysis=analysis,
                    attempts=attempts,
                    errors=errors,
                ).to_dict()
            except Exception as exc:  # noqa: BLE001 - batch should continue.
                record = ImageBatchRecord(
                    instance_id=sample.instance_id,
                    repo=sample.repo,
                    dataset=sample.dataset,
                    url=url,
                    source_field=source_field,
                    status="failed",
                    asset={"url": url, "status": "tool_error"},
                    analysis={"vlm_status": "failed"},
                    attempts=0,
                    errors=[str(exc)],
                ).to_dict()

            if record["status"] == "failed":
                failed += 1
            else:
                processed += 1
            asset = record.get("asset", {}) or {}
            if asset.get("processable"):
                processable_expected += 1
                if record["status"] == "ok":
                    processable_covered += 1
            else:
                non_processable += 1
                if asset.get("status") == "missing":
                    missing += 1
            records.append(record)
            _append_jsonl(cache_file, record)

    by_instance: dict[str, dict[str, int]] = {}
    image_type_counts: Counter[str] = Counter()
    vlm_status_counts: Counter[str] = Counter()
    likely_code_layer_counts: Counter[str] = Counter()
    search_query_counts: Counter[str] = Counter()
    for record in records:
        entry = by_instance.setdefault(
            record["instance_id"],
            {"expected": 0, "processable_expected": 0, "ok": 0, "failed": 0, "non_processable": 0},
        )
        entry["expected"] += 1
        asset = record.get("asset", {}) or {}
        if asset.get("processable"):
            entry["processable_expected"] += 1
        if record["status"] == "failed":
            entry["failed"] += 1
        elif record["status"] == "ok":
            entry["ok"] += 1
        elif record["status"] == "non_processable":
            entry["non_processable"] += 1
        image_type = str(asset.get("image_type") or "unknown")
        image_type_counts[image_type] += 1
        analysis = record.get("analysis", {}) or {}
        vlm_status_counts[str(analysis.get("vlm_status") or "unknown")] += 1
        for layer in analysis.get("likely_code_layers", []) or []:
            likely_code_layer_counts[str(layer)] += 1
        for query in analysis.get("search_queries", []) or []:
            search_query_counts[str(query)] += 1

    summary = {
        "cache_path": str(cache_file),
        "asset_cache_dir": str(asset_root),
        "expected_images": expected,
        "processable_images": processable_expected,
        "covered_processable_images": processable_covered,
        "non_processable_images": non_processable,
        "missing_images": missing,
        "records": len(records),
        "reused": reused,
        "processed": processed,
        "failed": failed,
        "complete_for_processable_images": processable_expected == processable_covered and failed == 0,
        "complete_instances": sum(
            1
            for item in by_instance.values()
            if item["processable_expected"] == item["ok"] and item["failed"] == 0
        ),
        "incomplete_instances": {
            key: value
            for key, value in sorted(by_instance.items())
            if value["failed"] or value["processable_expected"] != value["ok"]
        },
        "image_type_counts": dict(image_type_counts.most_common()),
        "vlm_status_counts": dict(vlm_status_counts.most_common()),
        "likely_code_layer_counts": dict(likely_code_layer_counts.most_common(30)),
        "top_search_queries": dict(search_query_counts.most_common(40)),
        "sample_image_records": [
            {
                "instance_id": record.get("instance_id"),
                "url": record.get("url"),
                "status": record.get("status"),
                "image_type": (record.get("asset", {}) or {}).get("image_type"),
                "vlm_status": (record.get("analysis", {}) or {}).get("vlm_status"),
                "likely_code_layers": (record.get("analysis", {}) or {}).get("likely_code_layers", [])[:8],
                "search_queries": (record.get("analysis", {}) or {}).get("search_queries", [])[:8],
            }
            for record in records[:80]
        ],
    }
    if write_manifest:
        manifest = _manifest_path(cache_file)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["manifest_path"] = str(manifest)
    return summary
