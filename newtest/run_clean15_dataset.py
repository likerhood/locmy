from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mycode.agent.pipeline import run_localization_pipeline
from mycode.data.dataset_loader import count_samples, load_samples
from mycode.evaluation.localization_eval import evaluate_entity_ranking, evaluate_file_ranking
from mycode.utils.phase_logger import phase_event
from mycode.utils.env import llm_config_from_env
from mycode.utils.trace_recorder import collect_llm_events, collect_token_usage, compact_agent_trace


KS = tuple(range(1, 16))
LEVELS = ("file", "module", "function")
SET_CUTOFFS = ("8", "10", "15", "all")
SET_FIELDS = ("sl", "rec", "pre", "f1")


def _env_enabled(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run mycode evidence-aware dynamic localization on a Clean15 JSONL dataset."
    )
    parser.add_argument("--samples", required=True, help="Clean15 samples.jsonl.")
    parser.add_argument("--dataset", required=True, help="Dataset label written to outputs.")
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL/CSV/summary outputs.")
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--max-samples", type=int, default=0, help="0 means all samples.")
    parser.add_argument("--instance-id", action="append", default=[], help="Run only selected instance id(s).")
    parser.add_argument("--start-index", type=int, default=0, help="Zero-based start index after filtering.")
    parser.add_argument("--resume", action="store_true", help="Skip ids already present in localization_results.jsonl.")
    parser.add_argument("--force", action="store_true", help="Overwrite previous output files.")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--allow-browser", action="store_true")
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--use-vlm", action="store_true")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--use-llm-planning", action="store_true")
    parser.add_argument("--use-llm-controller", action="store_true")
    parser.add_argument("--max-tool-rounds", type=int, default=2)
    parser.add_argument("--dynamic-rounds", type=int, default=3)
    parser.add_argument(
        "--max-react-steps",
        type=int,
        default=0,
        help="Maximum ReAct controller tool steps. 0 derives it from dynamic-rounds.",
    )
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="Use a reusable repo structure snapshot; generate it from base_commit when missing.",
    )
    parser.set_defaults(auto_fetch_repos=_env_enabled("MYCODE_AUTO_FETCH_REPOS", True))
    parser.add_argument(
        "--auto-fetch-repos",
        dest="auto_fetch_repos",
        action="store_true",
        help="Download an exact repo/base_commit checkout when no reusable structure exists (default).",
    )
    parser.add_argument(
        "--no-auto-fetch-repos",
        dest="auto_fetch_repos",
        action="store_false",
        help="Require pre-existing repository assets; never download repositories.",
    )
    parser.add_argument("--lightweight", action="store_true", help="Use fast full-run localization without deep graph/flow rounds.")
    parser.add_argument("--cache-dir", default="", help="Tool cache dir. Defaults to <output-dir>/tool_cache.")
    parser.add_argument("--trace-jsonl", default="", help="Compact agent trace JSONL. Defaults to <output-dir>/agent_traces.jsonl.")
    parser.add_argument("--token-jsonl", default="", help="Per-instance token usage JSONL. Defaults to <output-dir>/token_usage.jsonl.")
    parser.add_argument("--llm-events-jsonl", default="", help="Extracted LLM outputs/events JSONL. Defaults to <output-dir>/llm_events.jsonl.")
    parser.add_argument("--progress-jsonl", default="", help="Per-instance progress JSONL. Defaults to <output-dir>/progress.jsonl.")
    parser.add_argument("--verbose-agent-log", action="store_true", help="Print compact LLM/agent/tool traces to terminal after each sample.")
    parser.add_argument("--verbose-agent-limit", type=int, default=5, help="Maximum items per verbose terminal section.")
    parser.add_argument("--verbose-llm-text-limit", type=int, default=500, help="Maximum characters printed for each LLM event.")
    parser.add_argument(
        "--sample-heartbeat-interval",
        type=int,
        default=0,
        help="Print a still-running heartbeat every N seconds while one sample is being localized. 0 disables it.",
    )
    parser.add_argument(
        "--sample-timeout-seconds",
        type=int,
        default=3000,
        help="Hard wall-clock limit for one sample. 0 disables it.",
    )
    return parser.parse_args()


class SampleTimeoutError(TimeoutError):
    pass


@contextmanager
def _sample_deadline(seconds: int):
    """Interrupt a stalled sample while keeping the batch process alive."""

    seconds = max(0, int(seconds or 0))
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(_signum, _frame):
        raise SampleTimeoutError(f"sample exceeded wall-clock budget of {seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _safe_label(value: str) -> str:
    value = value.strip() or "offline"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "offline"


def _read_completed(path: Path) -> set[str]:
    completed: set[str] = set()
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance_id = row.get("instance_id")
            if instance_id and row.get("status") == "ok":
                completed.add(str(instance_id))
    return completed


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _metric(metrics: dict[str, Any], level: str, name: str) -> Any:
    return ((metrics or {}).get(level) or {}).get(name, "")


def _ranked_paths(items: Iterable[dict[str, Any]], key: str = "path") -> list[str]:
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key) or item.get("id") or item.get("path")
        if value:
            out.append(str(value))
    return out


def _complete_three_level_metrics(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Recompute complete k=1..15 metrics from saved predictions and gold sets.

    Older experiment rows may have been generated before the reporting script
    tracked every k value. The JSONL result still contains ranked predictions
    and gold entities, so summary regeneration can fill the full LocAgent-style
    metric surface without rerunning localization.
    """

    three = result.get("evaluation_3level") or {}
    gold = three.get("gold") or {}
    localization = result.get("localization") or {}
    ranked_locations = localization.get("ranked_locations") or []
    ranked_modules = localization.get("ranked_modules") or []
    ranked_functions = localization.get("ranked_functions") or []
    pred_files = _ranked_paths(ranked_locations, "path")
    pred_modules = _ranked_paths(ranked_modules, "id")
    pred_functions = _ranked_paths(ranked_functions, "id")
    gold_files = gold.get("files") or result.get("gold_files") or []
    gold_modules = gold.get("modules") or []
    gold_functions = gold.get("functions") or []
    computed = {
        "file": evaluate_file_ranking(pred_files, gold_files, ks=KS),
        "module": evaluate_entity_ranking(pred_modules, gold_modules, ks=KS),
        "function": evaluate_entity_ranking(pred_functions, gold_functions, ks=KS),
    }
    for level in LEVELS:
        saved = three.get(level) or {}
        for key, value in saved.items():
            computed[level].setdefault(key, value)
    return computed


def _flatten_metrics(result: dict[str, Any]) -> dict[str, Any]:
    three = result.get("evaluation_3level") or {}
    complete_metrics = _complete_three_level_metrics(result)
    localization = result.get("localization") or {}
    ranked_files = localization.get("ranked_locations") or []
    evidence = result.get("evidence") or {}
    packet = evidence.get("evidence_packet") or evidence.get("packet") or {}
    applicability = three.get("applicability") or {}
    token_usage = result.get("token_usage_summary") or {}
    final_patch_files = localization.get("final_patch_set") or []
    final_patch_eval = result.get("final_patch_set_evaluation") or {}
    modification_closure = localization.get("modification_closure") or {}
    closure_coverage = modification_closure.get("coverage") or {}

    row: dict[str, Any] = {
        "instance_id": result.get("instance_id", ""),
        "repo": result.get("repo", ""),
        "dataset": result.get("dataset", ""),
        "language": (result.get("evidence") or {}).get("language", ""),
        "status": result.get("status", "ok"),
        "top1_file": ranked_files[0].get("path", "") if ranked_files else "",
        "top5_files": " | ".join(str(item.get("path", "")) for item in ranked_files[:5]),
        "gold_files": " | ".join(result.get("gold_files") or []),
        "llm_controller_used": result.get("llm_controller_used", False),
        "problem_statement_only": result.get("problem_statement_only", False),
        "modality": packet.get("modality", ""),
        "url_count": len(packet.get("url_inspections") or []),
        "image_count": len(packet.get("image_inspections") or []),
        "dynamic_round_count": len(localization.get("dynamic_rounds") or []),
        "flow_trace_count": len(localization.get("flow_traces") or []),
        "module_applicable": applicability.get("module_applicable", ""),
        "function_applicable": applicability.get("function_applicable", ""),
        "function_empty_reason": applicability.get("function_empty_reason", ""),
        "elapsed_seconds": result.get("elapsed_seconds", 0),
        "prompt_tokens": token_usage.get("prompt_tokens", 0),
        "completion_tokens": token_usage.get("completion_tokens", 0),
        "total_tokens": token_usage.get("total_tokens", 0),
        "final_patch_count": len(final_patch_files),
        "final_patch_files": " | ".join(str(path) for path in final_patch_files),
        "final_patch_set_sl": final_patch_eval.get("set_sl@all", 0.0),
        "final_patch_set_rec": final_patch_eval.get("set_rec@all", 0.0),
        "final_patch_set_pre": final_patch_eval.get("set_pre@all", 0.0),
        "final_patch_set_f1": final_patch_eval.get("set_f1@all", 0.0),
        "closure_status": modification_closure.get("status", ""),
        "closure_complete": bool(modification_closure.get("complete", False)),
        "closure_rounds": int(modification_closure.get("rounds_run") or 0),
        "closure_required_coverage": float(closure_coverage.get("required_coverage") or 0.0),
        "closure_missing_required": " | ".join(str(item) for item in closure_coverage.get("missing_required", []) or []),
    }
    for level in LEVELS:
        level_metrics = complete_metrics.get(level) or {}
        row[f"{level}_gold_count"] = level_metrics.get("gold_count", _metric(three, level, "gold_count"))
        row[f"{level}_pred_count"] = level_metrics.get("pred_count", _metric(three, level, "pred_count"))
        for k in KS:
            row[f"{level}_acc@{k}"] = level_metrics.get(f"acc@{k}", _metric(three, level, f"acc@{k}"))
            row[f"{level}_strict_acc@{k}"] = level_metrics.get(f"strict_acc@{k}", _metric(three, level, f"strict_acc@{k}"))
            row[f"{level}_recall@{k}"] = level_metrics.get(f"recall@{k}", _metric(three, level, f"recall@{k}"))
        for cutoff in SET_CUTOFFS:
            for field in SET_FIELDS:
                row[f"{level}_set_{field}@{cutoff}"] = level_metrics.get(
                    f"set_{field}@{cutoff}",
                    _metric(three, level, f"set_{field}@{cutoff}"),
                )
        row[f"{level}_mrr@15"] = level_metrics.get("mrr@15", _metric(three, level, "mrr@15"))
        row[f"{level}_map@15"] = level_metrics.get("map@15", _metric(three, level, "map@15"))
    return row


def _failure_metric_row(failure: dict[str, Any]) -> dict[str, Any]:
    """Represent a sample with no usable checkpoint as an evaluated zero row."""

    gold_files = [str(path) for path in failure.get("gold_files", []) or [] if path]
    row: dict[str, Any] = {
        "instance_id": failure.get("instance_id", ""),
        "repo": failure.get("repo", ""),
        "dataset": failure.get("dataset", ""),
        "language": failure.get("language", ""),
        "status": "error",
        "error_type": failure.get("error_type", ""),
        "error": failure.get("error", ""),
        "top1_file": "",
        "top5_files": "",
        "gold_files": " | ".join(gold_files),
        "llm_controller_used": False,
        "problem_statement_only": False,
        "modality": "",
        "url_count": 0,
        "image_count": 0,
        "dynamic_round_count": 0,
        "flow_trace_count": 0,
        "module_applicable": "",
        "function_applicable": "",
        "function_empty_reason": "sample_failed_before_evaluable_checkpoint",
        "elapsed_seconds": failure.get("elapsed_seconds", 0),
        "prompt_tokens": failure.get("prompt_tokens", 0),
        "completion_tokens": failure.get("completion_tokens", 0),
        "total_tokens": failure.get("total_tokens", 0),
        "final_patch_count": 0,
        "final_patch_files": "",
        "final_patch_set_sl": 0.0,
        "final_patch_set_rec": 0.0,
        "final_patch_set_pre": 0.0,
        "final_patch_set_f1": 0.0,
        "closure_status": "not_run_sample_error",
        "closure_complete": False,
        "closure_rounds": 0,
        "closure_required_coverage": 0.0,
        "closure_missing_required": "sample_error",
    }
    for level in LEVELS:
        row[f"{level}_gold_count"] = len(gold_files) if level == "file" else 0
        row[f"{level}_pred_count"] = 0
        for k in KS:
            row[f"{level}_acc@{k}"] = 0.0
            row[f"{level}_strict_acc@{k}"] = 0.0
            row[f"{level}_recall@{k}"] = 0.0
        for cutoff in SET_CUTOFFS:
            for field in SET_FIELDS:
                row[f"{level}_set_{field}@{cutoff}"] = 0.0
        row[f"{level}_mrr@15"] = 0.0
        row[f"{level}_map@15"] = 0.0
    return row


def _evaluation_rows(rows: list[dict[str, Any]], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate attempts and include unresolved failures in metric denominators."""

    keyed_rows: dict[str, dict[str, Any]] = {}
    anonymous_rows: list[dict[str, Any]] = []
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        if not instance_id:
            anonymous_rows.append(row)
            continue
        previous = keyed_rows.get(instance_id)
        if previous is None or row.get("status", "ok") == "ok" or previous.get("status", "ok") != "ok":
            keyed_rows[instance_id] = row

    latest_failures: dict[str, dict[str, Any]] = {}
    anonymous_failures: list[dict[str, Any]] = []
    for failure in failures:
        instance_id = str(failure.get("instance_id") or "")
        if instance_id:
            latest_failures[instance_id] = failure
        else:
            anonymous_failures.append(failure)
    for instance_id, failure in latest_failures.items():
        if instance_id not in keyed_rows:
            keyed_rows[instance_id] = _failure_metric_row(failure)
    return [*keyed_rows.values(), *anonymous_rows, *(_failure_metric_row(item) for item in anonymous_failures)]


def _summarize(rows: list[dict[str, Any]], failures: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    evaluated_rows = _evaluation_rows(rows, failures)
    success_rows = [row for row in evaluated_rows if row.get("status", "ok") == "ok"]
    partial_rows = [row for row in evaluated_rows if row.get("status") == "partial"]
    failure_rows = [row for row in evaluated_rows if row.get("status", "ok") not in {"ok", "partial"}]
    cumulative_elapsed = sum(float(row.get("elapsed_seconds") or 0.0) for row in rows)
    cumulative_elapsed += sum(float(row.get("elapsed_seconds") or 0.0) for row in failures)
    summary: dict[str, Any] = {
        "sample_count": len(evaluated_rows),
        "evaluated_count": len(evaluated_rows),
        "success_count": len(success_rows),
        "partial_count": len(partial_rows),
        "failure_count": len(failure_rows),
        "incomplete_sample_policy": "partial_checkpoint_else_zero",
        "metric_denominator": "all_deduplicated_samples",
        "elapsed_seconds": round(cumulative_elapsed, 3),
        "session_elapsed_seconds": round(elapsed, 3),
        "metrics": {},
    }
    patch_rows = [row for row in evaluated_rows if int(row.get("final_patch_count") or 0) > 0]
    summary["final_patch_set"] = {
        "applicable_count": len(patch_rows),
        "avg_count": round(sum(float(row.get("final_patch_count") or 0) for row in patch_rows) / len(patch_rows), 6) if patch_rows else 0.0,
        "complete_count": sum(1 for row in patch_rows if bool(row.get("closure_complete", False))),
        "complete_rate": round(sum(1 for row in patch_rows if bool(row.get("closure_complete", False))) / len(patch_rows), 6) if patch_rows else 0.0,
        "avg_required_coverage": round(sum(float(row.get("closure_required_coverage") or 0.0) for row in patch_rows) / len(patch_rows), 6) if patch_rows else 0.0,
        "avg_rounds": round(sum(float(row.get("closure_rounds") or 0.0) for row in patch_rows) / len(patch_rows), 6) if patch_rows else 0.0,
        **{
            name: round(sum(float(row.get(f"final_patch_set_{name}") or 0.0) for row in patch_rows) / len(patch_rows), 6) if patch_rows else 0.0
            for name in ("sl", "rec", "pre", "f1")
        },
    }
    for level in LEVELS:
        level_summary: dict[str, Any] = {}
        metric_names = [
            *(f"acc@{k}" for k in KS),
            *(f"strict_acc@{k}" for k in KS),
            *(f"recall@{k}" for k in KS),
            *(f"set_{field}@{cutoff}" for cutoff in SET_CUTOFFS for field in SET_FIELDS),
            "mrr@15",
            "map@15",
        ]
        for name in metric_names:
            key = f"{level}_{name}"
            values = [float(row[key]) for row in evaluated_rows if row.get(key) not in ("", None)]
            level_summary[name] = round(sum(values) / len(values), 6) if values else 0.0
        gold_counts = [float(row[f"{level}_gold_count"]) for row in evaluated_rows if row.get(f"{level}_gold_count") not in ("", None)]
        pred_counts = [float(row[f"{level}_pred_count"]) for row in evaluated_rows if row.get(f"{level}_pred_count") not in ("", None)]
        level_summary["avg_gold_count"] = round(sum(gold_counts) / len(gold_counts), 6) if gold_counts else 0.0
        level_summary["avg_pred_count"] = round(sum(pred_counts) / len(pred_counts), 6) if pred_counts else 0.0
        level_summary["empty"] = (
            round(sum(1 for count in pred_counts if count <= 0.0) / len(pred_counts), 6) if pred_counts else 0.0
        )
        summary["metrics"][level] = level_summary
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100.0:.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return f"{0.0:.{digits}f}"


def _set_metric_row(summary: dict[str, Any], cutoff: str) -> str:
    values: list[str] = []
    for level in LEVELS:
        metrics = summary["metrics"].get(level, {})
        values.extend(_pct(metrics.get(f"set_{field}@{cutoff}", 0.0)) for field in SET_FIELDS)
    return "| " + " | ".join(values) + " |"


def _locagent_style_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {"evaluated": summary.get("evaluated_count", summary.get("success_count", 0))}
    for level in LEVELS:
        metrics = summary.get("metrics", {}).get(level, {})
        for k in KS:
            flat[f"{level}_acc@{k}"] = metrics.get(f"acc@{k}", 0.0)
            flat[f"{level}_strict_acc@{k}"] = metrics.get(f"strict_acc@{k}", 0.0)
            flat[f"{level}_recall@{k}"] = metrics.get(f"recall@{k}", 0.0)
        flat[f"{level}_mrr"] = metrics.get("mrr@15", 0.0)
        flat[f"{level}_map"] = metrics.get("map@15", 0.0)
        flat[f"{level}_empty"] = metrics.get("empty", 0.0)
        flat[f"{level}_avg_gold_count"] = metrics.get("avg_gold_count", 0.0)
        flat[f"{level}_avg_pred_count"] = metrics.get("avg_pred_count", 0.0)
        for cutoff in SET_CUTOFFS:
            for field in SET_FIELDS:
                flat[f"{level}_{field}@{cutoff}"] = metrics.get(f"set_{field}@{cutoff}", 0.0)
    return flat


def _short_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...<truncated>"


def _print_list(prefix: str, values: list[Any], *, limit: int, text_limit: int = 160) -> None:
    preview = [_short_text(value, text_limit) for value in values[:limit] if str(value or "").strip()]
    print(f"{prefix}{preview}", flush=True)


def _print_verbose_agent_log(
    result: dict[str, Any],
    *,
    llm_events: list[dict[str, Any]],
    token_usage: dict[str, int],
    item_limit: int,
    text_limit: int,
) -> None:
    trace = compact_agent_trace(result)
    evidence = result.get("evidence") or {}
    packet = evidence.get("evidence_packet") or evidence.get("packet") or {}
    localization = result.get("localization") or {}
    issue_sketch = localization.get("issue_sketch") or trace.get("issue_sketch") or {}

    print("  [agent] evidence:", flush=True)
    print(
        "    "
        f"modality={packet.get('modality', '')} "
        f"urls={len(packet.get('url_inspections') or [])} "
        f"images={len(packet.get('image_inspections') or [])} "
        f"problem_statement_only={result.get('problem_statement_only', False)}",
        flush=True,
    )
    url_roles = [
        f"{item.get('role', 'unknown')}::{item.get('tool_recommendation', 'unknown')}"
        for item in (packet.get("url_inspections") or [])[:item_limit]
    ]
    image_roles = [
        f"{item.get('image_type', 'unknown')}::{item.get('tool_recommendation', 'unknown')}"
        for item in (packet.get("image_inspections") or [])[:item_limit]
    ]
    if url_roles:
        _print_list("    url_roles=", url_roles, limit=item_limit)
    if image_roles:
        _print_list("    image_types=", image_roles, limit=item_limit)

    if issue_sketch:
        print("  [agent] issue sketch:", flush=True)
        if issue_sketch.get("task_type"):
            print(f"    task_type={issue_sketch.get('task_type')}", flush=True)
        for key in (
            "workflow",
            "concerns",
            "states",
            "expected_effects",
            "entities",
            "architectural_queries",
        ):
            value = issue_sketch.get(key)
            if isinstance(value, list):
                _print_list(f"    {key}=", value, limit=item_limit)
            elif value:
                print(f"    {key}={_short_text(value, 220)}", flush=True)
        obligations = [
            str(item.get("flow_type") or "")
            for item in issue_sketch.get("flow_obligations", []) or []
            if item.get("flow_type")
        ]
        if obligations:
            _print_list("    flow_obligations=", obligations, limit=item_limit)
        hypotheses = issue_sketch.get("hypotheses", []) or []
        if hypotheses:
            print(f"    unverified_hypotheses={len(hypotheses)}", flush=True)

    graph_scope = localization.get("graph_scope") or {}
    if graph_scope:
        print(
            "  [agent] graph scope: "
            f"strategy={graph_scope.get('strategy')} "
            f"selected={graph_scope.get('selected_paths')} "
            f"limit={graph_scope.get('scope_limit')} "
            f"repo_files={graph_scope.get('full_repo_files')}",
            flush=True,
        )
        preview = [
            f"{item.get('path')}({item.get('score')})"
            for item in (graph_scope.get("top_scope_preview") or [])[:item_limit]
        ]
        if preview:
            _print_list("    scope_preview=", preview, limit=item_limit, text_limit=140)

    rounds = localization.get("dynamic_rounds") or []
    if rounds:
        first_pool = ((rounds[0].get("frontier_state") or {}).get("candidate_pool") or {})
        if first_pool:
            print(
                "  [agent] round pool: "
                f"pool={first_pool.get('pool_size')} "
                f"graph_scope={first_pool.get('graph_scope_size')} "
                f"flow_scope={first_pool.get('flow_scope_size')} "
                f"global_file_hits={first_pool.get('global_file_hits')} "
                f"global_entity_hits={first_pool.get('global_entity_hits')}",
                flush=True,
            )

    phase_timings = localization.get("phase_timings") or []
    if phase_timings:
        print("  [agent] phase timings:", flush=True)
        for phase in phase_timings[: max(item_limit, 6)]:
            extra = ""
            if "selected_paths" in phase:
                extra = f" selected={phase.get('selected_paths')}"
            elif "candidate_count" in phase:
                extra = f" candidates={phase.get('candidate_count')}"
            print(
                "    "
                f"{phase.get('phase')} elapsed={phase.get('elapsed_seconds')}s{extra}",
                flush=True,
            )

    react = trace.get("react_agent") or {}
    steps = react.get("steps") or []
    if steps:
        react_summary = react.get("summary") or {}
        print(
            f"  [agent] react/tool steps ({len(steps)} shown): "
            f"stop_reason={react_summary.get('stop_reason', '')} "
            f"consecutive_no_gain={react_summary.get('consecutive_no_gain', 0)}",
            flush=True,
        )
        for step in steps[:item_limit]:
            usage = step.get("token_usage") or {}
            print(
                "    "
                f"round={step.get('round_no')} tool={step.get('tool')} action={step.get('action')} "
                f"elapsed={step.get('elapsed_seconds', 0)}s tokens={usage.get('total_tokens', 0)}",
                flush=True,
            )
            thought = _short_text(step.get("thought"), text_limit)
            if thought:
                print(f"      thought: {thought}", flush=True)
            _print_list("      candidates=", step.get("candidate_paths") or [], limit=item_limit, text_limit=120)
            _print_list("      next_queries=", step.get("next_queries") or [], limit=item_limit, text_limit=120)

    search_trace = trace.get("search_trace") or []
    if search_trace:
        dynamic_steps = [
            step for step in search_trace if str(step.get("step") or "").startswith("round_")
        ]
        displayed_steps = dynamic_steps[-item_limit:] if dynamic_steps else search_trace[:item_limit]
        print(f"  [agent] search rounds ({len(displayed_steps)} shown):", flush=True)
        for step in displayed_steps:
            print(
                "    "
                f"step={step.get('step')} strategy={step.get('strategy', '')} "
                f"top_files={step.get('top_files', [])[:item_limit]}",
                flush=True,
            )
            stop = step.get("stop_decision") or {}
            if stop:
                print(f"      stop={_short_text(stop, 220)}", flush=True)
            progress = step.get("round_progress") or {}
            if progress:
                print(
                    "      progress: "
                    f"new_candidates={progress.get('new_top_candidate_count', 0)} "
                    f"evidence_gain={progress.get('candidate_evidence_gain_count', 0)} "
                    f"strong_gain={progress.get('strong_evidence_gain_count', 0)} "
                    f"new_flows={progress.get('new_flow_count', 0)} "
                    f"novel_queries={progress.get('novel_query_count', 0)} "
                    f"stable_top3={progress.get('stable_top3', False)} "
                    f"top5_overlap={progress.get('top5_overlap', 0)} "
                    f"plateau_streak={progress.get('plateau_streak', 0)}",
                    flush=True,
                )

    flow_summary = trace.get("flow_summary") or {}
    if flow_summary:
        print(
            "  [agent] flow: "
            f"count={flow_summary.get('count', 0)} "
            f"backends={flow_summary.get('backend_counts', {})} "
            f"types={flow_summary.get('flow_type_counts', {})}",
            flush=True,
        )
        for flow in (flow_summary.get("preview") or [])[:item_limit]:
            print(
                "    "
                f"{flow.get('flow_type', '')}:{flow.get('term', '')} "
                f"targets={flow.get('candidate_target_paths', [])[:item_limit]} "
                f"reason={_short_text(flow.get('reason'), 180)}",
                flush=True,
            )

    closure = trace.get("modification_closure") or {}
    if closure:
        coverage = closure.get("coverage") or {}
        print(
            "  [agent] modification closure: "
            f"status={closure.get('status')} complete={closure.get('complete')} "
            f"rounds={closure.get('rounds_run')} coverage={coverage.get('required_coverage')} "
            f"files={closure.get('files', [])[:item_limit]}",
            flush=True,
        )
        if coverage.get("missing_required"):
            _print_list(
                "    missing_required=",
                coverage.get("missing_required") or [],
                limit=item_limit,
                text_limit=180,
            )
        for closure_round in (closure.get("trace") or [])[:item_limit]:
            print(
                "    "
                f"round={closure_round.get('round')} decision={closure_round.get('decision')} "
                f"expanded={[item.get('path') for item in (closure_round.get('expanded') or [])]} "
                f"pruned={[item.get('path') for item in (closure_round.get('pruned') or [])]}",
                flush=True,
            )
        ranking_adjustment = closure.get("ranking_adjustment") or {}
        print(
            "    ranking_adjustment: "
            f"applied={ranking_adjustment.get('applied', False)} "
            f"reason={ranking_adjustment.get('reason', '')} "
            f"adjustments={ranking_adjustment.get('adjustments', [])[:item_limit]}",
            flush=True,
        )

    ranked = trace.get("ranked") or {}
    print("  [agent] top ranked files:", flush=True)
    for item in (ranked.get("files") or [])[:item_limit]:
        print(
            "    "
            f"{item.get('path')} score={item.get('score')} "
            f"reasons={[_short_text(reason, 90) for reason in (item.get('reasons') or [])[:3]]}",
            flush=True,
        )

    print(
        "  [llm] "
        f"events={len(llm_events)} prompt={token_usage.get('prompt_tokens', 0)} "
        f"completion={token_usage.get('completion_tokens', 0)} total={token_usage.get('total_tokens', 0)}",
        flush=True,
    )
    for event in llm_events[:item_limit]:
        print(
            "    "
            f"#{event.get('event_no')} {event.get('event_type')} "
            f"path={event.get('path')} usage={event.get('usage') or {}}",
            flush=True,
        )
        content = _short_text(event.get("content"), text_limit)
        if content:
            print(f"      {content}", flush=True)


class _SampleHeartbeat:
    def __init__(
        self,
        *,
        enabled: bool,
        interval: int,
        index: int,
        total: int,
        instance_id: str,
        repo: str,
        output_dir: Path,
        state_json: Path | None = None,
    ) -> None:
        self.enabled = enabled and interval > 0
        self.interval = max(1, int(interval or 0))
        self.index = index
        self.total = total
        self.instance_id = instance_id
        self.repo = repo
        self.output_dir = output_dir
        self.state_json = state_json
        self.started = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "_SampleHeartbeat":
        if not self.enabled:
            return self
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{self.instance_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            elapsed = time.time() - self.started
            phase_bits = ""
            if self.state_json and self.state_json.exists():
                try:
                    state = json.loads(self.state_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
                if state:
                    fields = []
                    for key in (
                        "event",
                        "phase",
                        "tool",
                        "subphase",
                        "backend",
                        "mode",
                        "candidate_count",
                        "pool_size",
                        "flow_scope_size",
                        "file_count",
                        "entity_count",
                    ):
                        value = state.get(key)
                        if value not in ("", None, [], {}):
                            fields.append(f"{key}={value}")
                    if state.get("last_update"):
                        fields.append(f"phase_age={_format_duration(time.time() - float(state['last_update']))}")
                    if fields:
                        phase_bits = " " + " ".join(fields[:12])
            print(
                f"  [heartbeat] sample={self.index}/{self.total} "
                f"instance={self.instance_id} repo={self.repo} "
                f"elapsed={_format_duration(elapsed)} still running; output={self.output_dir}{phase_bits}",
                flush=True,
            )


def _write_summary_md(path: Path, summary: dict[str, Any], args: argparse.Namespace) -> None:
    model_label = summary.get("model_name") or summary.get("model_api_name") or "offline"
    ranking_headers = ["Level"] + [f"Acc@{k}" for k in KS] + ["MRR@15", "MAP@15", "Empty"]
    strict_headers = ["Level"] + [f"Strict Acc@{k}" for k in KS]
    recall_headers = ["Level"] + [f"Recall@{k}" for k in KS]
    set_headers = [
        "File SL",
        "File REC",
        "File PRE",
        "File F1",
        "Module SL",
        "Module REC",
        "Module PRE",
        "Module F1",
        "Function SL",
        "Function REC",
        "Function PRE",
        "Function F1",
    ]
    lines = [
        "# Three-Level Localization Metrics",
        "",
        f"- Evaluated: {summary.get('evaluated_count', summary['success_count'])}",
        f"- Dataset: `{args.dataset}`",
        f"- Samples: `{args.samples}`",
        f"- Output dir: `{args.output_dir}`",
        f"- Success / Partial / Failure: `{summary['success_count']}` / `{summary.get('partial_count', 0)}` / `{summary['failure_count']}`",
        f"- Incomplete sample policy: `{summary.get('incomplete_sample_policy', 'legacy')}`",
        f"- Cumulative sample seconds: `{summary['elapsed_seconds']}`",
        f"- Current session seconds: `{summary.get('session_elapsed_seconds', summary['elapsed_seconds'])}`",
        f"- top_k: `{args.top_k}`, dynamic_rounds: `{args.dynamic_rounds}`, max_tool_rounds: `{args.max_tool_rounds}`, max_react_steps: `{args.max_react_steps or 'auto'}`",
        f"- use_llm: `{args.use_llm}`, use_llm_planning: `{args.use_llm_planning}`, use_llm_controller: `{args.use_llm_controller}`",
        f"- allow_network/browser/vlm: `{args.allow_network}` / `{args.allow_browser}` / `{args.use_vlm}`",
        "",
        "## Ranking Metrics",
        "",
        "| " + " | ".join(ranking_headers) + " |",
        "|---|" + "|".join(["---:"] * (len(ranking_headers) - 1)) + "|",
    ]
    for level, title in (("file", "File"), ("module", "Module"), ("function", "Function")):
        metrics = summary["metrics"].get(level, {})
        lines.append(
            "| " + " | ".join(
                [title]
                + [_pct(metrics.get(f"acc@{k}", 0.0)) for k in KS]
                + [_pct(metrics.get("mrr@15", 0.0)), _pct(metrics.get("map@15", 0.0)), _pct(metrics.get("empty", 0.0))]
            ) + " |"
        )
    lines.extend(
        [
            "",
            "## Strict Ranking Metrics",
            "",
            "`Strict Acc@k` is 1 only when top-k covers all gold locations at that level.",
            "",
            "| " + " | ".join(strict_headers) + " |",
            "|---|" + "|".join(["---:"] * (len(strict_headers) - 1)) + "|",
        ]
    )
    for level, title in (("file", "File"), ("module", "Module"), ("function", "Function")):
        metrics = summary["metrics"].get(level, {})
        lines.append(
            "| " + " | ".join([title] + [_pct(metrics.get(f"strict_acc@{k}", 0.0)) for k in KS]) + " |"
        )
    lines.extend(
        [
            "",
            "## Recall Metrics",
            "",
            "`Recall@k` is the average fraction of gold locations covered by top-k.",
            "",
            "| " + " | ".join(recall_headers) + " |",
            "|---|" + "|".join(["---:"] * (len(recall_headers) - 1)) + "|",
        ]
    )
    for level, title in (("file", "File"), ("module", "Module"), ("function", "Function")):
        metrics = summary["metrics"].get(level, {})
        lines.append("| " + " | ".join([title] + [_pct(metrics.get(f"recall@{k}", 0.0)) for k in KS]) + " |")
    lines.extend(
        [
            "",
            "## Diagnostic Counts",
            "",
            "| Level | Avg Gold | Avg Pred | Empty |",
            "|---|---:|---:|---:|",
        ]
    )
    for level, title in (("file", "File"), ("module", "Module"), ("function", "Function")):
        metrics = summary["metrics"].get(level, {})
        lines.append(
            "| "
            + " | ".join(
                [
                    title,
                    _num(metrics.get("avg_gold_count", 0.0)),
                    _num(metrics.get("avg_pred_count", 0.0)),
                    _pct(metrics.get("empty", 0.0)),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Metric Notes",
            "",
            "`Set Metrics @8/@10/@15` evaluate only the top-k predictions. `Set Metrics @All` evaluates the full prediction set without truncation.",
            "",
            "`SL` is strict full-coverage success: it is 1 only when all gold locations for that level are included in the evaluated prediction set.",
            "",
        ]
    )
    for cutoff in SET_CUTOFFS:
        cutoff_label = "All" if cutoff == "all" else cutoff
        lines.extend(
            [
                f"## Set Metrics @{cutoff_label}",
                "",
                "| " + " | ".join(set_headers) + " |",
                "|" + "|".join(["---:"] * len(set_headers)) + "|",
                _set_metric_row(summary, cutoff),
                "",
            ]
        )
    patch_metrics = summary.get("final_patch_set", {}) or {}
    lines.extend(
        [
            "## Adaptive Final Patch Set",
            "",
            "The final patch set is produced by a bounded select/verify/expand/ablate closure; only a complete, verified patch target may receive a bounded final-ranking promotion.",
            "",
            "| Applicable | Complete | Complete Rate | Avg Coverage | Avg Rounds | Avg Files | SL | REC | PRE | F1 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            "| "
            + " | ".join(
                [
                    str(patch_metrics.get("applicable_count", 0)),
                    str(patch_metrics.get("complete_count", 0)),
                    _pct(patch_metrics.get("complete_rate", 0.0)),
                    _pct(patch_metrics.get("avg_required_coverage", 0.0)),
                    _num(patch_metrics.get("avg_rounds", 0.0)),
                    _num(patch_metrics.get("avg_count", 0.0)),
                    _pct(patch_metrics.get("sl", 0.0)),
                    _pct(patch_metrics.get("rec", 0.0)),
                    _pct(patch_metrics.get("pre", 0.0)),
                    _pct(patch_metrics.get("f1", 0.0)),
                ]
            )
            + " |",
            "",
        ]
    )
    lines.extend(
        [
            "## mycode Run Details",
            "",
            f"- Model label: `{model_label}`",
            f"- Settings: `top_k={args.top_k}`, `dynamic_rounds={args.dynamic_rounds}`, `max_tool_rounds={args.max_tool_rounds}`, `sample_timeout_seconds={args.sample_timeout_seconds}`, `structure_only={args.structure_only}`, `auto_fetch_repos={getattr(args, 'auto_fetch_repos', True)}`, `lightweight={args.lightweight}`",
            "",
            "## Output Files",
            "",
            "- `localization_results.jsonl`: 每个样本的完整 evidence、动态定位、flow trace、三层评估。",
            "- `agent_traces.jsonl`: 精简后的 ReAct/搜索/flow 轨迹，适合人工看失败案例。",
            "- `llm_events.jsonl`: 每个样本抽取出的 LLM planning、understanding、controller、ReAct 输出。",
            "- `token_usage.jsonl`: 每个样本显式记录到的 LLM token 用量。",
            "- `progress.jsonl`: 每个样本开始/结束/失败的进度记录，适合长跑时监控。",
            "- `per_instance_metrics.csv`: 每个样本的一行指标，适合后续汇总画表。",
            "- `per_instance_metrics_3level.csv`: LocAgent 风格三层逐样本指标。",
            "- `metrics_summary.json`: 机器可读整体指标。",
            "- `metrics_summary.md`: 当前摘要。",
            "- `metrics_3level.json`: LocAgent 风格三层汇总指标。",
            "- `metrics_3level.md`: LocAgent 风格三层 Markdown 报告。",
            "- `failures.jsonl`: 运行异常样本。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _selected_samples(samples_path: Path, dataset: str, instance_ids: Iterable[str], start_index: int, max_samples: int):
    wanted = set(instance_ids)
    samples = list(load_samples(samples_path, dataset=dataset))
    if wanted:
        samples = [sample for sample in samples if sample.instance_id in wanted]
    if start_index:
        samples = samples[start_index:]
    if max_samples > 0:
        samples = samples[:max_samples]
    return samples


def main() -> None:
    args = _parse_args()
    samples_path = Path(args.samples).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    args.output_dir = str(output_dir)
    if not samples_path.exists():
        raise SystemExit(f"Samples file does not exist: {samples_path}")

    config = llm_config_from_env()
    model_runtime_enabled = any(
        (args.use_llm, args.use_llm_planning, args.use_llm_controller, args.use_vlm)
    )
    if model_runtime_enabled:
        missing = [
            name
            for name, value in (
                ("BASE_URL", config.get("base_url")),
                ("API_KEY", config.get("api_key")),
                ("MODEL_API_NAME", config.get("model_api_name")),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise SystemExit(
                "Model-backed features are enabled but model configuration is incomplete. "
                f"Missing: {', '.join(missing)}. Launch with --env-file PATH."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    results_jsonl = output_dir / "localization_results.jsonl"
    failures_jsonl = output_dir / "failures.jsonl"
    metrics_csv = output_dir / "per_instance_metrics.csv"
    metrics3_csv = output_dir / "per_instance_metrics_3level.csv"
    summary_json = output_dir / "metrics_summary.json"
    summary_md = output_dir / "metrics_summary.md"
    metrics3_json = output_dir / "metrics_3level.json"
    metrics3_md = output_dir / "metrics_3level.md"
    trace_jsonl = Path(args.trace_jsonl).expanduser().resolve() if args.trace_jsonl else output_dir / "agent_traces.jsonl"
    token_jsonl = Path(args.token_jsonl).expanduser().resolve() if args.token_jsonl else output_dir / "token_usage.jsonl"
    llm_events_jsonl = (
        Path(args.llm_events_jsonl).expanduser().resolve() if args.llm_events_jsonl else output_dir / "llm_events.jsonl"
    )
    progress_jsonl = (
        Path(args.progress_jsonl).expanduser().resolve() if args.progress_jsonl else output_dir / "progress.jsonl"
    )
    phase_events_jsonl = output_dir / "phase_events.jsonl"
    phase_state_json = output_dir / "current_phase_state.json"
    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else output_dir / "tool_cache"

    if args.force:
        for path in (
            results_jsonl,
            failures_jsonl,
            metrics_csv,
            metrics3_csv,
            summary_json,
            summary_md,
            metrics3_json,
            metrics3_md,
            trace_jsonl,
            token_jsonl,
            llm_events_jsonl,
            progress_jsonl,
            phase_events_jsonl,
            phase_state_json,
        ):
            if path.exists():
                path.unlink()

    selected = _selected_samples(samples_path, args.dataset, args.instance_id, args.start_index, args.max_samples)
    completed = _read_completed(results_jsonl) if args.resume and not args.force else set()
    total_rows = count_samples(samples_path)

    print(f"mycode Clean15 batch runner", flush=True)
    print(f"Root: {ROOT}", flush=True)
    print(f"Dataset: {args.dataset}", flush=True)
    print(f"Samples: {samples_path} ({total_rows} rows)", flush=True)
    print(f"Selected: {len(selected)}", flush=True)
    print(f"Output: {output_dir}", flush=True)
    print(f"Trace JSONL: {trace_jsonl}", flush=True)
    print(f"Token JSONL: {token_jsonl}", flush=True)
    print(f"LLM Events JSONL: {llm_events_jsonl}", flush=True)
    print(f"Progress JSONL: {progress_jsonl}", flush=True)
    print(f"Phase Events JSONL: {phase_events_jsonl}", flush=True)
    print(f"Phase State JSON: {phase_state_json}", flush=True)
    print(f"Cache: {cache_dir}", flush=True)
    print(f"Model profile: {os.environ.get('MYCODE_ACTIVE_ENV_FILE') or '<not-selected>'}", flush=True)
    print(f"Model label: {config.get('model_name') or config.get('model_api_name') or 'offline'}", flush=True)
    print(f"LLM/API model: {config.get('model_api_name') or '<none>'}", flush=True)
    print(
        "Switches: "
        f"use_llm={args.use_llm}, planning={args.use_llm_planning}, controller={args.use_llm_controller}, "
        f"network={args.allow_network}, browser={args.allow_browser}, vlm={args.use_vlm}, "
        f"download_images={args.download_images}, structure_only={args.structure_only}, auto_fetch_repos={args.auto_fetch_repos}, lightweight={args.lightweight}",
        flush=True,
    )
    print(
        "Budgets: "
        f"top_k={args.top_k}, dynamic_rounds={args.dynamic_rounds}, "
        f"max_tool_rounds={args.max_tool_rounds}, max_react_steps={args.max_react_steps or 'auto'}",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    start = time.time()
    for index, sample in enumerate(selected, start=1):
        elapsed_all = time.time() - start
        completed_before = max(index - 1, 0)
        rate = completed_before / elapsed_all if elapsed_all > 0 and completed_before else 0.0
        eta = (len(selected) - completed_before) / rate if rate > 0 else 0.0
        percent = 100.0 * completed_before / len(selected) if selected else 100.0
        if sample.instance_id in completed:
            print(
                f"[{index}/{len(selected)} {percent:.1f}% eta={_format_duration(eta)}] "
                f"skip completed {sample.instance_id}",
                flush=True,
            )
            continue
        sample_start = time.time()
        print(
            f"[{index}/{len(selected)} {percent:.1f}% eta={_format_duration(eta)}] "
            f"localize {sample.instance_id} repo={sample.repo} gold_files={len(sample.gold_files)}",
            flush=True,
        )
        _append_jsonl(
            progress_jsonl,
            {
                "event": "start",
                "index": index,
                "total": len(selected),
                "completed_before": completed_before,
                "percent_before": round(percent, 3),
                "eta_seconds": round(eta, 3),
                "instance_id": sample.instance_id,
                "repo": sample.repo,
                "dataset": sample.dataset,
                "timestamp": time.time(),
            },
        )
        os.environ["MYCODE_PHASE_EVENTS_JSONL"] = str(phase_events_jsonl)
        os.environ["MYCODE_PHASE_STATE_JSON"] = str(phase_state_json)
        os.environ["MYCODE_PHASE_INSTANCE_ID"] = sample.instance_id
        os.environ["MYCODE_PHASE_REPO"] = sample.repo
        os.environ["MYCODE_PHASE_SAMPLE_INDEX"] = str(index)
        os.environ["MYCODE_PHASE_SAMPLE_TOTAL"] = str(len(selected))
        phase_event(
            "start",
            "sample",
            dataset=sample.dataset,
            gold_file_count=len(sample.gold_files or []),
            output_dir=str(output_dir),
        )
        heartbeat = _SampleHeartbeat(
            enabled=args.sample_heartbeat_interval > 0,
            interval=args.sample_heartbeat_interval,
            index=index,
            total=len(selected),
            instance_id=sample.instance_id,
            repo=sample.repo,
            output_dir=output_dir,
            state_json=phase_state_json,
        ).start()
        try:
            with _sample_deadline(args.sample_timeout_seconds):
                result = run_localization_pipeline(
                    sample,
                    allow_network=args.allow_network,
                    allow_browser=args.allow_browser,
                    use_llm=args.use_llm,
                    use_llm_planning=args.use_llm_planning if args.use_llm else False,
                    use_llm_controller=args.use_llm_controller if args.use_llm else False,
                    execute_tools=True,
                    cache_dir=str(cache_dir),
                    download_images=args.download_images,
                    use_vlm=args.use_vlm,
                    max_tool_rounds=args.max_tool_rounds,
                    top_k=args.top_k,
                    dynamic_rounds=args.dynamic_rounds,
                    max_react_steps=args.max_react_steps or None,
                    structure_only=args.structure_only,
                    lightweight=args.lightweight,
                    auto_fetch_repos=args.auto_fetch_repos,
                )
            heartbeat.stop()
            result_status = str(result.get("status") or "ok")
            result["status"] = result_status
            result["elapsed_seconds"] = round(time.time() - sample_start, 3)
            token_usage = collect_token_usage(result)
            result["token_usage_summary"] = token_usage
            _append_jsonl(results_jsonl, result)
            _append_jsonl(trace_jsonl, compact_agent_trace(result))
            llm_events = collect_llm_events(result)
            _append_jsonl(
                llm_events_jsonl,
                {
                    "instance_id": sample.instance_id,
                    "repo": sample.repo,
                    "dataset": sample.dataset,
                    "llm_event_count": len(llm_events),
                    "events": llm_events,
                },
            )
            _append_jsonl(
                token_jsonl,
                {
                    "instance_id": sample.instance_id,
                    "repo": sample.repo,
                    "dataset": sample.dataset,
                    "elapsed_seconds": result["elapsed_seconds"],
                    "token_usage": token_usage,
                    "llm_event_count": len(llm_events),
                    "llm_controller_used": result.get("llm_controller_used", False),
                    "use_llm": args.use_llm,
                    "use_llm_planning": args.use_llm_planning,
                    "use_llm_controller": args.use_llm_controller,
                },
            )
            row = _flatten_metrics(result)
            row["elapsed_seconds"] = result["elapsed_seconds"]
            row["prompt_tokens"] = token_usage.get("prompt_tokens", 0)
            row["completion_tokens"] = token_usage.get("completion_tokens", 0)
            row["total_tokens"] = token_usage.get("total_tokens", 0)
            rows.append(row)
            file_metrics = result.get("evaluation_3level", {}).get("file", {})
            phase_event(
                "end" if result_status == "ok" else "partial",
                "sample",
                status=result_status,
                elapsed_seconds=result["elapsed_seconds"],
                file_acc_at_1=file_metrics.get("acc@1"),
                file_recall_at_15=file_metrics.get("recall@15"),
                llm_event_count=len(llm_events),
                total_tokens=token_usage.get("total_tokens", 0),
            )
            if args.verbose_agent_log:
                _print_verbose_agent_log(
                    result,
                    llm_events=llm_events,
                    token_usage=token_usage,
                    item_limit=max(1, args.verbose_agent_limit),
                    text_limit=max(120, args.verbose_llm_text_limit),
                )
            print(
                f"  {result_status} elapsed={result['elapsed_seconds']}s "
                f"file_acc@1={file_metrics.get('acc@1')} file_recall@15={file_metrics.get('recall@15')} "
                f"tokens={token_usage.get('total_tokens', 0)} llm_events={len(llm_events)}",
                flush=True,
            )
            _append_jsonl(
                progress_jsonl,
                {
                    "event": result_status,
                    "index": index,
                    "total": len(selected),
                    "instance_id": sample.instance_id,
                    "repo": sample.repo,
                    "dataset": sample.dataset,
                    "elapsed_seconds": result["elapsed_seconds"],
                    "token_usage": token_usage,
                    "llm_event_count": len(llm_events),
                    "file_acc@1": file_metrics.get("acc@1"),
                    "file_recall@15": file_metrics.get("recall@15"),
                    "timestamp": time.time(),
                },
            )
        except Exception as exc:  # Keep full-run experiments moving by default.
            heartbeat.stop()
            failure = {
                "status": "error",
                "instance_id": sample.instance_id,
                "repo": sample.repo,
                "dataset": sample.dataset,
                "language": sample.language or "",
                "gold_files": list(sample.gold_files or []),
                "evaluation_policy": "zero_no_evaluable_checkpoint",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=8),
                "elapsed_seconds": round(time.time() - sample_start, 3),
            }
            failures.append(failure)
            _append_jsonl(failures_jsonl, failure)
            _append_jsonl(progress_jsonl, {"event": "error", **failure, "index": index, "total": len(selected), "timestamp": time.time()})
            phase_event("error", "sample", **failure)
            print(f"  ERROR {sample.instance_id}: {type(exc).__name__}: {exc}", flush=True)
            if args.fail_fast:
                raise

    if results_jsonl.exists():
        all_rows = []
        with results_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    all_rows.append(_flatten_metrics(json.loads(line)))
                except Exception:
                    continue
        rows = all_rows

    if failures_jsonl.exists():
        all_failures = []
        with failures_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    all_failures.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        failures = all_failures

    report_rows = _evaluation_rows(rows, failures)
    summary = _summarize(rows, failures, time.time() - start)
    summary["samples_path"] = str(samples_path)
    summary["dataset"] = args.dataset
    summary["output_dir"] = str(output_dir)
    summary["model_name"] = config.get("model_name")
    summary["model_api_name"] = config.get("model_api_name")
    summary["settings"] = {
        "top_k": args.top_k,
        "max_samples": args.max_samples,
        "start_index": args.start_index,
        "resume": args.resume,
        "force": args.force,
        "allow_network": args.allow_network,
        "allow_browser": args.allow_browser,
        "download_images": args.download_images,
        "use_vlm": args.use_vlm,
        "use_llm": args.use_llm,
        "use_llm_planning": args.use_llm_planning,
        "use_llm_controller": args.use_llm_controller,
        "max_tool_rounds": args.max_tool_rounds,
        "dynamic_rounds": args.dynamic_rounds,
        "max_react_steps": args.max_react_steps or "auto",
        "structure_only": args.structure_only,
        "auto_fetch_repos": args.auto_fetch_repos,
        "lightweight": args.lightweight,
        "verbose_agent_log": args.verbose_agent_log,
        "verbose_agent_limit": args.verbose_agent_limit,
        "verbose_llm_text_limit": args.verbose_llm_text_limit,
        "sample_timeout_seconds": args.sample_timeout_seconds,
        "final_patch_set_limit": int(os.environ.get("MYCODE_FINAL_PATCH_SET_LIMIT", "6") or 6),
        "closure_max_rounds": int(os.environ.get("MYCODE_CLOSURE_MAX_ROUNDS", "3") or 3),
        "closure_expand_per_round": int(os.environ.get("MYCODE_CLOSURE_EXPAND_PER_ROUND", "2") or 2),
        "llm_review_candidates": int(os.environ.get("MYCODE_LLM_REVIEW_CANDIDATES", "6") or 6),
        "llm_review_repair_attempts": int(os.environ.get("MYCODE_LLM_REVIEW_REPAIR_ATTEMPTS", "1") or 1),
        "flow_pool_limit": int(os.environ.get("MYCODE_FLOW_POOL_LIMIT", "48") or 48),
        "round_read_budget": int(os.environ.get("MYCODE_ROUND_READ_BUDGET", "24") or 24),
        "architecture_path_weight": float(os.environ.get("MYCODE_ARCHITECTURE_PATH_WEIGHT", "1.0") or 1.0),
        "architecture_path_limit": int(os.environ.get("MYCODE_ROUND_ARCHITECTURE_PATH_LIMIT", "16") or 16),
        "architecture_corroboration": os.environ.get("MYCODE_ARCHITECTURE_CORROBORATION", "1") == "1",
        "best_round_checkpoint": os.environ.get("MYCODE_BEST_ROUND_CHECKPOINT", "1") == "1",
        "cross_round_protected_prefix": int(
            os.environ.get("MYCODE_CROSS_ROUND_PROTECTED_PREFIX", "6") or 6
        ),
        "cross_round_max_replacements": int(
            os.environ.get("MYCODE_CROSS_ROUND_MAX_REPLACEMENTS", "4") or 4
        ),
        "head_replacement_margin": float(
            os.environ.get("MYCODE_HEAD_REPLACEMENT_MARGIN", "3.0") or 3.0
        ),
        "plateau_review_override_round": int(
            os.environ.get("MYCODE_PLATEAU_REVIEW_OVERRIDE_ROUND", "12") or 12
        ),
        "browser_required": os.environ.get("MYCODE_BROWSER_REQUIRED", "0") == "1",
        "require_source_checkout": os.environ.get("MYCODE_REQUIRE_SOURCE_CHECKOUT", "0") == "1",
        "trace_jsonl": str(trace_jsonl),
        "token_jsonl": str(token_jsonl),
        "llm_events_jsonl": str(llm_events_jsonl),
        "progress_jsonl": str(progress_jsonl),
    }
    _write_csv(metrics_csv, report_rows)
    _write_csv(metrics3_csv, report_rows)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics3_json.write_text(json.dumps(_locagent_style_metrics(summary), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_summary_md(summary_md, summary, args)
    _write_summary_md(metrics3_md, summary, args)
    print(
        f"Done. success={summary['success_count']} partial={summary.get('partial_count', 0)} "
        f"failure={summary['failure_count']} evaluated={summary.get('evaluated_count', 0)}",
        flush=True,
    )
    print(f"Summary: {summary_md}", flush=True)
    print(f"Three-level metrics: {metrics3_md}", flush=True)
    if summary["failure_count"]:
        print(f"Failures: {failures_jsonl}", flush=True)


if __name__ == "__main__":
    main()
