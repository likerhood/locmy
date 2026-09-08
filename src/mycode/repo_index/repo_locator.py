from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional


class RepositoryAssetError(RuntimeError):
    """Raised when an exact benchmark checkout cannot be prepared."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def repository_cache_root() -> Path:
    configured = os.environ.get("MYCODE_REPO_CACHE_DIR", "").strip()
    if not configured:
        return project_root() / ".mycode_cache"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


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


def _safe_component(value: str, fallback: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return cleaned.strip("._-") or fallback


def _existing(paths: Iterable[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists():
            return path
    return None


def _dataset_experiments(dataset: str) -> list[str]:
    value = str(dataset or "").lower()
    swe = [
        "swebench_multimodal-full-dev",
        "swebench_multimodal-full-candidates",
        "swebench_multimodal-60",
    ]
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


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _matches_commit(path: Path, base_commit: str) -> bool:
    return not base_commit or _git_head(path).lower() == base_commit.lower()


def find_repo_root(repo: str, dataset: str = "", base_commit: str = "") -> Optional[Path]:
    """Find a checked-out benchmark repository without cloning anything."""
    name = _repo_dir_name(repo)
    roots = _candidate_roots()
    candidates: list[Path] = []
    explicit = os.environ.get("MYCODE_REPO_ROOT", "").strip()
    if explicit:
        explicit_root = Path(explicit).expanduser()
        candidates.extend([explicit_root, explicit_root / name, explicit_root / repo.split("/")[-1]])
    if base_commit:
        candidates.append(repository_cache_root() / "checkouts" / name / _safe_component(base_commit, "head"))
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
    return _existing(path for path in candidates if _matches_commit(path, base_commit))


def standalone_structure_path(instance_id: str, dataset: str, base_commit: str) -> Path:
    dataset_name = _safe_component(dataset, "dataset")
    instance_name = _safe_component(instance_id, "instance")
    commit_name = _safe_component(base_commit[:16], "head")
    return repository_cache_root() / "repo_structures" / dataset_name / f"{instance_name}-{commit_name}.json"


def find_repo_structure(
    instance_id: str,
    dataset: str = "",
    base_commit: str = "",
) -> Optional[Path]:
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
    if base_commit:
        candidates.append(standalone_structure_path(instance_id, dataset, base_commit))
    return _existing(candidates)


def _run_git(args: list[str], *, timeout: int = 900) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RepositoryAssetError("git is required to download benchmark repositories") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryAssetError(f"git command timed out after {timeout}s: git {' '.join(args[:4])}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git command failed").strip().splitlines()[-1]
        raise RepositoryAssetError(f"git {' '.join(args[:4])} failed: {detail}") from exc
    return result.stdout.strip()


@contextmanager
def _asset_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _remote_url(repo: str) -> str:
    template = os.environ.get(
        "MYCODE_REPO_REMOTE_TEMPLATE",
        "https://github.com/{repo}.git",
    ).strip()
    try:
        return template.format(repo=repo)
    except (KeyError, ValueError) as exc:
        raise RepositoryAssetError(
            "MYCODE_REPO_REMOTE_TEMPLATE must contain a valid {repo} placeholder"
        ) from exc


def ensure_repo_checkout(repo: str, base_commit: str) -> Path:
    """Materialize and reuse a detached checkout for one exact benchmark commit."""
    if not repo or "/" not in repo:
        raise RepositoryAssetError(f"invalid repository name: {repo!r}")
    if not base_commit:
        raise RepositoryAssetError("sample has no base_commit; exact repository checkout is impossible")

    cache_root = repository_cache_root()
    repo_name = _repo_dir_name(repo)
    commit_name = _safe_component(base_commit, "head")
    checkout = cache_root / "checkouts" / repo_name / commit_name
    lock = cache_root / "locks" / f"{repo_name}.lock"
    with _asset_lock(lock):
        if checkout.is_dir() and _matches_commit(checkout, base_commit):
            return checkout

        mirror = cache_root / "git" / f"{repo_name}.git"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        if not mirror.exists():
            _run_git(["init", "--bare", str(mirror)])
            _run_git(["-C", str(mirror), "remote", "add", "origin", _remote_url(repo)])
        else:
            _run_git(["-C", str(mirror), "remote", "set-url", "origin", _remote_url(repo)])

        try:
            _run_git(["-C", str(mirror), "cat-file", "-e", f"{base_commit}^{{commit}}"], timeout=30)
        except RepositoryAssetError:
            _run_git(
                ["-C", str(mirror), "fetch", "--no-tags", "--depth", "1", "origin", base_commit]
            )

        if checkout.exists():
            import shutil

            shutil.rmtree(checkout)
            _run_git(["-C", str(mirror), "worktree", "prune"], timeout=60)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["-C", str(mirror), "worktree", "add", "--detach", str(checkout), base_commit])
        if not _matches_commit(checkout, base_commit):
            raise RepositoryAssetError(
                f"prepared checkout does not match base_commit {base_commit}: {checkout}"
            )
    return checkout
