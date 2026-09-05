from __future__ import annotations

from pathlib import Path
from typing import Iterator

from mycode.schemas.evidence import NormalizedSample
from mycode.utils.io import read_jsonl


def _join_issue_text(raw: dict) -> str:
    value = raw.get("problem_statement")
    if isinstance(value, str):
        return value.strip()
    return ""


def _gold_files(raw: dict) -> list[str]:
    value = raw.get("files")
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [v.strip() for v in value.splitlines() if v.strip()]
    return []


def load_samples(path: str | Path, dataset: str) -> Iterator[NormalizedSample]:
    for raw in read_jsonl(path):
        instance_id = str(raw.get("instance_id") or raw.get("id") or "")
        repo = str(raw.get("repo") or raw.get("repository") or "")
        if not instance_id:
            raise ValueError(f"Sample without instance_id in {path}")
        yield NormalizedSample(
            instance_id=instance_id,
            repo=repo,
            dataset=dataset,
            issue_text=_join_issue_text(raw),
            raw=raw,
            gold_files=_gold_files(raw),
            language=raw.get("language"),
        )


def count_samples(path: str | Path) -> int:
    return sum(1 for _ in read_jsonl(path))
