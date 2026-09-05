from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _enabled() -> bool:
    return str(os.environ.get("MYCODE_PHASE_LOG", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _short(value: Any, limit: int = 800) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...<truncated>"
    if isinstance(value, list):
        return [_short(item, limit=limit) for item in value[:40]]
    if isinstance(value, dict):
        return {str(key): _short(val, limit=limit) for key, val in list(value.items())[:80]}
    return value


def _base_event(event: str, phase: str, **fields: Any) -> dict[str, Any]:
    now = time.time()
    row: dict[str, Any] = {
        "event": event,
        "phase": phase,
        "timestamp": now,
        "instance_id": os.environ.get("MYCODE_PHASE_INSTANCE_ID", ""),
        "repo": os.environ.get("MYCODE_PHASE_REPO", ""),
        "sample_index": os.environ.get("MYCODE_PHASE_SAMPLE_INDEX", ""),
        "sample_total": os.environ.get("MYCODE_PHASE_SAMPLE_TOTAL", ""),
    }
    row.update({key: _short(value) for key, value in fields.items() if value is not None})
    return row


def phase_event(event: str, phase: str, **fields: Any) -> None:
    if not _enabled():
        return
    row = _base_event(event, phase, **fields)
    events_path = os.environ.get("MYCODE_PHASE_EVENTS_JSONL", "")
    if events_path:
        path = Path(events_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    state_path = os.environ.get("MYCODE_PHASE_STATE_JSON", "")
    if state_path:
        state = dict(row)
        state["last_update"] = row["timestamp"]
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    if str(os.environ.get("MYCODE_PHASE_STDOUT", os.environ.get("MYCODE_PHASE_LOG_STDOUT", "1"))).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        detail = " ".join(
            f"{key}={value}"
            for key, value in row.items()
            if key
            not in {
                "event",
                "timestamp",
                "instance_id",
                "repo",
                "sample_index",
                "sample_total",
            }
            and value not in ("", [], {}, None)
        )
        print(f"[phase:{event}] {phase}" + (f" {detail}" if detail else ""), flush=True)


@contextmanager
def phase_context(phase: str, **fields: Any) -> Iterator[None]:
    start = time.perf_counter()
    phase_event("start", phase, **fields)
    try:
        yield
    except Exception as exc:
        phase_event(
            "error",
            phase,
            elapsed_seconds=round(time.perf_counter() - start, 3),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    else:
        phase_event("end", phase, elapsed_seconds=round(time.perf_counter() - start, 3), **fields)
