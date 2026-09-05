from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("LOC_CODE_ROOT")
    if env_root:
        roots.append(Path(env_root))
    here = Path(__file__).resolve()
    roots.extend(here.parents)
    roots.extend([Path.cwd(), Path("/home/like/locCode"), Path("/data2/like/loccode")])

    out: list[Path] = []
    seen = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _repo_dir_name(repo: str) -> str:
    return repo.replace("/", "_")


def _existing(paths: Iterable[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists():
            return path
    return None


def _dataset_experiments(dataset: str) -> list[str]:
    value = str(dataset or "").lower()
    swe = ["swebench_multimodal-full-dev", "swebench_multimodal-60"]
    omni = [
        "omnigirl-full-candidates-clean15",
        "omnigirl-full-candidates",
        "omnigirl-unified60",
        "omnigirl-60",
    ]
    if "swe" in value:
        return swe + omni
    if "omni" in value:
        return omni + swe
    return swe + omni


def find_repo_root(repo: str, dataset: str = "") -> Optional[Path]:
    """Find a checked-out benchmark repository without cloning anything."""
    name = _repo_dir_name(repo)
    roots = _candidate_roots()
    candidates: list[Path] = []
    for root in roots:
        for exp in _dataset_experiments(dataset):
            repo_exp = exp.replace("-clean15", "")
            candidates.extend(
                [
                    root / "LocAgent" / f"repo_newtest_{repo_exp}" / name,
                    root / "LocAgent" / f"repo_newtest_{repo_exp}" / "_shared_worktrees" / name,
                    root / f"repo_newtest_{repo_exp}" / name,
                    root / f"repo_newtest_{repo_exp}" / "_shared_worktrees" / name,
                    root / "GALA" / "mytest" / repo_exp / "repos" / repo.split("/")[-1],
                    root / "GraphLocator" / "newtest" / repo_exp / "repo_playground" / repo.split("/")[-1],
                ]
            )
    return _existing(candidates)


def find_repo_structure(instance_id: str, dataset: str = "") -> Optional[Path]:
    """Find a repo_structures JSON file for an instance."""
    filename = f"{instance_id}.json"
    roots = _candidate_roots()
    experiments = _dataset_experiments(dataset)
    candidates: list[Path] = []
    for root in roots:
        for exp in experiments:
            candidates.extend(
                [
                    root / "LocAgent" / "newtest" / exp / "repo_structures" / filename,
                    root / "MM-IR" / "data" / exp / "repo_structures" / filename,
                    root / "CoSIL" / "newtest" / exp / "repo_structures" / filename,
                    root / "GraphLocator" / "newtest" / exp / "repo_structures" / filename,
                    root / "GALA" / "mytest" / exp / "repo_structures" / filename,
                ]
            )
    return _existing(candidates)
