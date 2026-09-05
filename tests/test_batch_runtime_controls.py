from __future__ import annotations

import time

from newtest.run_clean15_dataset import (
    SampleTimeoutError,
    _evaluation_rows,
    _sample_deadline,
    _summarize,
)


def test_sample_deadline_interrupts_stalled_work() -> None:
    try:
        with _sample_deadline(1):
            time.sleep(2)
    except SampleTimeoutError:
        return
    raise AssertionError("sample deadline did not interrupt stalled work")


def test_summary_separates_session_and_cumulative_elapsed() -> None:
    summary = _summarize(
        [
            {"elapsed_seconds": 12.5, "file_gold_count": 1, "file_pred_count": 1},
            {"elapsed_seconds": 7.5, "file_gold_count": 1, "file_pred_count": 1},
        ],
        [{"elapsed_seconds": 3.0}],
        4.0,
    )
    assert summary["elapsed_seconds"] == 23.0
    assert summary["session_elapsed_seconds"] == 4.0


def test_failed_sample_is_included_as_zero_metric_row() -> None:
    summary = _summarize(
        [
            {
                "instance_id": "ok-1",
                "status": "ok",
                "elapsed_seconds": 2.0,
                "file_gold_count": 1,
                "file_pred_count": 1,
                "file_acc@1": 1.0,
            }
        ],
        [
            {
                "instance_id": "timeout-1",
                "status": "error",
                "error_type": "SampleTimeoutError",
                "gold_files": ["src/target.py"],
                "elapsed_seconds": 3.0,
            }
        ],
        elapsed=1.0,
    )

    assert summary["sample_count"] == 2
    assert summary["evaluated_count"] == 2
    assert summary["success_count"] == 1
    assert summary["partial_count"] == 0
    assert summary["failure_count"] == 1
    assert summary["metrics"]["file"]["acc@1"] == 0.5
    assert summary["metrics"]["file"]["empty"] == 0.5


def test_partial_checkpoint_supersedes_failure_for_same_sample() -> None:
    partial = {
        "instance_id": "sample-1",
        "status": "partial",
        "elapsed_seconds": 10.0,
        "file_gold_count": 1,
        "file_pred_count": 3,
        "file_acc@1": 1.0,
    }
    rows = _evaluation_rows(
        [partial],
        [{"instance_id": "sample-1", "status": "error", "elapsed_seconds": 20.0}],
    )

    assert rows == [partial]
    summary = _summarize([partial], [{"instance_id": "sample-1", "status": "error"}], elapsed=1.0)
    assert summary["sample_count"] == 1
    assert summary["partial_count"] == 1
    assert summary["failure_count"] == 0
    assert summary["metrics"]["file"]["acc@1"] == 1.0


def test_later_success_supersedes_partial_attempt() -> None:
    rows = _evaluation_rows(
        [
            {"instance_id": "sample-1", "status": "partial", "file_acc@1": 0.0},
            {"instance_id": "sample-1", "status": "ok", "file_acc@1": 1.0},
        ],
        [],
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["file_acc@1"] == 1.0
