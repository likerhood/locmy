from __future__ import annotations

from pathlib import Path

from mycode.data.dataset_loader import load_samples


DEFAULT_CLEAN15_PATHS = {
    "swe": Path("/home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl"),
    "omni": Path("/home/like/locCode/clean_subsets_new/omnigirl-full-candidates.clean15.v458.samples.jsonl"),
}


def get_clean15_path(dataset: str) -> Path:
    try:
        return DEFAULT_CLEAN15_PATHS[dataset]
    except KeyError as exc:
        raise ValueError(f"Unknown Clean15 dataset: {dataset}") from exc


def load_clean15(dataset: str, path: str | Path | None = None):
    return load_samples(path or get_clean15_path(dataset), dataset=dataset)

