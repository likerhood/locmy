from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from mycode.evidence.tools.browser_reproduction_reader import read_browser_reproduction
from mycode.evidence.tools.image_asset_reader import read_image_asset
from mycode.evidence.tools.local_code_url_resolver import resolve_github_code_url
from mycode.evidence.tools.reproduction_extractor import extract_reproduction
from mycode.evidence.tools.url_inspector import inspect_url
from mycode.evidence.tools.vlm_image_reader import try_analyze_image_with_vlm
from mycode.evidence.tools.vlm_image_reader import heuristic_image_understanding
from mycode.evidence.tools.web_snapshot import build_web_snapshot
from mycode.schemas.evidence import EvidenceCollectionPlan, ToolObservation, ToolRequest


REMOTE_URL_TOOLS = {
    "github_url_parser",
    "leakage_url_filter",
    "playground_decoder",
    "browser_reproduction_reader",
    "web_doc_reader",
    "discussion_reader",
    "web_snapshot_fetcher",
}


def _is_remote_url(source: str) -> bool:
    parsed = urlparse(source.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _safe_source_dir(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _observation_dir(cache_root: Path, plan: EvidenceCollectionPlan, request: ToolRequest) -> Path:
    return cache_root / plan.dataset / plan.instance_id / request.tool / _safe_source_dir(request.source)


def execute_tool_request(
    request: ToolRequest,
    *,
    cache_dir: str | Path,
    plan: EvidenceCollectionPlan | None = None,
    allow_network: bool = False,
    allow_browser: bool = False,
    download_images: bool = True,
    use_vlm: bool = False,
    repo_root: str | Path | None = None,
    base_commit: str = "",
    expected_repo: str = "",
) -> ToolObservation:
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []
    warnings: List[str] = list(request.caution)

    if not request.should_execute:
        return ToolObservation(
            tool=request.tool,
            source=request.source,
            success=True,
            status="skipped_by_planning_agent",
            extracted={"reason": request.reason, "parameters": request.parameters},
            warnings=warnings,
            metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
        )

    if request.tool in REMOTE_URL_TOOLS and not _is_remote_url(request.source):
        return ToolObservation(
            tool=request.tool,
            source=request.source,
            success=False,
            status="invalid_non_url_source",
            extracted={
                "parameters": request.parameters,
                "local_code_hint": request.source,
            },
            warnings=warnings + ["remote_tool_rejected_local_code_reference"],
            metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
        )

    try:
        if request.tool in {"github_url_parser", "leakage_url_filter"}:
            extracted = (
                resolve_github_code_url(
                    request.source,
                    repo_root=repo_root,
                    expected_repo=expected_repo or (plan.repo if plan else ""),
                    base_commit=base_commit,
                )
                if request.tool == "github_url_parser"
                else inspect_url(request.source)
            )
            status = "ok" if request.tool != "leakage_url_filter" else "skipped_leakage_url"
            return ToolObservation(
                tool=request.tool,
                source=request.source,
                success=True,
                status=status,
                extracted=extracted,
                warnings=warnings,
                metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
            )

        if request.tool == "playground_decoder":
            extracted = extract_reproduction(request.source)
            return ToolObservation(
                tool=request.tool,
                source=request.source,
                success=True,
                status="ok",
                extracted=extracted,
                warnings=warnings,
                metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
            )

        if request.tool == "browser_reproduction_reader":
            extracted = read_browser_reproduction(
                request.source,
                cache_dir=root / request.tool / _safe_source_dir(request.source),
                allow_network=allow_network,
                allow_browser=allow_browser,
            )
            external_sources = []
            for source_file in extracted.get("source_files", []) or []:
                if not isinstance(source_file, dict):
                    continue
                tagged = dict(source_file)
                tagged["provenance"] = "external_reproduction"
                tagged["eligible_as_patch_target"] = False
                external_sources.append(tagged)
            if external_sources:
                extracted["source_files"] = external_sources
                extracted["external_reproduction_evidence"] = external_sources
            success = extracted.get("status") == "ok" or bool(extracted.get("parsed_reproduction"))
            return ToolObservation(
                tool=request.tool,
                source=request.source,
                success=success,
                status=str(extracted.get("status", "unknown")),
                extracted=extracted,
                warnings=warnings + extracted.get("warnings", []),
                cache_path=extracted.get("cache_path"),
                metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
            )

        if request.tool in {"web_doc_reader", "discussion_reader", "web_snapshot_fetcher"}:
            extracted = build_web_snapshot(request.source, allow_network=allow_network)
            success = extracted.get("status") in {"ok", "planned"}
            return ToolObservation(
                tool=request.tool,
                source=request.source,
                success=success,
                status=str(extracted.get("status", "unknown")),
                extracted=extracted,
                warnings=warnings,
                metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
            )

        if request.tool == "vlm_image_inspector":
            extracted = read_image_asset(
                request.source,
                cache_dir=root / request.tool,
                download=download_images and allow_network,
            )
            if not extracted.get("processable"):
                warnings.append("image_not_available_or_not_processable_without_vlm")
            elif use_vlm:
                vlm_result = try_analyze_image_with_vlm(
                    image_path=extracted["local_path"],
                    image_format=str(extracted.get("format") or ""),
                    issue_summary=plan.issue_summary if plan else "",
                    repo=plan.repo if plan else "",
                    source_url=str(extracted.get("url") or request.source),
                )
                extracted["vlm_analysis"] = vlm_result
                extracted["vlm_status"] = vlm_result.get("vlm_status", "unknown")
                if extracted["vlm_status"] != "ok":
                    warnings.append("vlm_failed_using_heuristic_image_evidence")
            else:
                warnings.append("vlm_semantic_image_understanding_not_yet_executed")
                heuristic = heuristic_image_understanding(
                    image_format=str(extracted.get("format") or ""),
                    issue_summary=plan.issue_summary if plan else "",
                    repo=plan.repo if plan else "",
                    local_path=extracted.get("local_path"),
                )
                extracted["vlm_analysis"] = heuristic
                extracted["vlm_status"] = heuristic.get("vlm_status", "heuristic_only")
            extracted["planned_vlm_task"] = request.expected_outputs
            observation_status = str(extracted.get("status", "unknown"))
            if use_vlm and extracted.get("processable") and extracted.get("vlm_status") != "ok":
                observation_status = "partial_vlm_fallback"
            return ToolObservation(
                tool=request.tool,
                source=request.source,
                success=extracted.get("status") == "ok",
                status=observation_status,
                extracted=extracted,
                warnings=warnings,
                cache_path=extracted.get("local_path"),
                metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
            )

        return ToolObservation(
            tool=request.tool,
            source=request.source,
            success=False,
            status="unsupported_tool",
            extracted={"parameters": request.parameters},
            errors=[f"Unsupported tool: {request.tool}"],
            warnings=warnings,
            metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
        )
    except Exception as exc:  # noqa: BLE001 - tool failures should be visible to the agent, not fatal.
        errors.append(str(exc))
        return ToolObservation(
            tool=request.tool,
            source=request.source,
            success=False,
            status="tool_error",
            extracted={"parameters": request.parameters},
            errors=errors,
            warnings=warnings,
            metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
        )


def execute_collection_plan(
    plan: EvidenceCollectionPlan,
    *,
    cache_dir: str | Path,
    allow_network: bool = False,
    allow_browser: bool = False,
    download_images: bool = True,
    use_vlm: bool = False,
    repo_root: str | Path | None = None,
    base_commit: str = "",
) -> List[ToolObservation]:
    root = Path(cache_dir)
    observations: List[ToolObservation] = []
    completed_requests: set[tuple[str, str, str]] = set()
    for request in plan.tool_requests:
        request_key = (
            request.tool,
            request.source,
            json.dumps(request.parameters, sort_keys=True, ensure_ascii=True, default=str),
        )
        if request_key in completed_requests:
            observations.append(
                ToolObservation(
                    tool=request.tool,
                    source=request.source,
                    success=True,
                    status="duplicate_no_gain",
                    extracted={"reason": "identical_tool_request_already_executed"},
                    metadata={"priority": request.priority, "role_hypothesis": request.role_hypothesis},
                )
            )
            continue
        completed_requests.add(request_key)
        request_cache = _observation_dir(root, plan, request)
        observations.append(
            execute_tool_request(
                request,
                cache_dir=request_cache,
                plan=plan,
                allow_network=allow_network,
                allow_browser=allow_browser,
                download_images=download_images,
                use_vlm=use_vlm,
                repo_root=repo_root,
                base_commit=base_commit,
                expected_repo=plan.repo,
            )
        )
    return observations
