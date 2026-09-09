from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mycode.repo_index.repo_locator import (
    RepositoryAssetError,
    ensure_repo_checkout,
    find_repo_root,
    find_repo_structure,
    standalone_structure_path,
)
from mycode.repo_index.structure_index import write_repository_structure
from mycode.schemas.evidence import NormalizedSample


NO_REPO_ROOT = Path("/__mycode_no_repo_root__")


@dataclass(frozen=True)
class RepositoryAssets:
    repo_root: Path | None
    structure_path: Path | None
    source: str
    base_commit: str


def prepare_repository_assets(
    sample: NormalizedSample,
    *,
    structure_only: bool,
    auto_fetch: bool,
) -> RepositoryAssets:
    """Resolve existing assets first, then create exact standalone assets if needed."""
    base_commit = str(sample.raw.get("base_commit") or "").strip()
    structure_path = find_repo_structure(
        sample.instance_id,
        sample.dataset,
        base_commit=base_commit,
    )
    if structure_path is not None:
        repo_root = find_repo_root(sample.repo, sample.dataset, base_commit=base_commit)
        source = "existing_structure"
        require_source_checkout = os.environ.get(
            "MYCODE_REQUIRE_SOURCE_CHECKOUT", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if repo_root is None and auto_fetch and require_source_checkout:
            repo_root = ensure_repo_checkout(sample.repo, base_commit)
            source = "existing_structure_with_standalone_checkout"
        return RepositoryAssets(
            # Reuse the canonical structure snapshot for indexing. Full evidence
            # runs may additionally require an exact checkout for source quotes,
            # GitHub blob routing, and call/dataflow verification.
            repo_root=repo_root or (NO_REPO_ROOT if structure_only else None),
            structure_path=structure_path,
            source=source,
            base_commit=base_commit,
        )

    repo_root = find_repo_root(sample.repo, sample.dataset, base_commit=base_commit)
    source = "existing_checkout" if repo_root is not None else "missing"
    if repo_root is None and auto_fetch:
        repo_root = ensure_repo_checkout(sample.repo, base_commit)
        source = "standalone_checkout"

    if repo_root is None:
        return RepositoryAssets(
            repo_root=NO_REPO_ROOT,
            structure_path=None,
            source=source,
            base_commit=base_commit,
        )

    if not structure_only:
        return RepositoryAssets(
            repo_root=repo_root,
            structure_path=None,
            source=source,
            base_commit=base_commit,
        )

    if not base_commit:
        raise RepositoryAssetError(
            "STRUCTURE_ONLY requires base_commit when no prebuilt repo_structures file exists"
        )
    structure_path = standalone_structure_path(
        sample.instance_id,
        sample.dataset,
        base_commit,
    )
    existed = structure_path.exists()
    write_repository_structure(
        repo_root,
        structure_path,
        repo=sample.repo,
        instance_id=sample.instance_id,
        base_commit=base_commit,
    )
    return RepositoryAssets(
        repo_root=repo_root,
        structure_path=structure_path,
        source="standalone_structure_cache" if existed else "generated_structure",
        base_commit=base_commit,
    )
