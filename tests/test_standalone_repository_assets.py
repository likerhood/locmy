from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from mycode.agent.pipeline import run_localization_pipeline
from mycode.repo_index.repository_assets import NO_REPO_ROOT, prepare_repository_assets
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sample(repo: str, base_commit: str, instance_id: str = "standalone__demo-1") -> NormalizedSample:
    return NormalizedSample(
        instance_id=instance_id,
        repo=repo,
        dataset="standalone-unit",
        issue_text="Parser should return the normalized value.",
        raw={"base_commit": base_commit},
    )


def test_first_run_fetches_exact_commit_builds_structure_and_second_run_reuses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = "standalone-fixture/demo"
    source = tmp_path / "source"
    source.mkdir()
    _git("init", cwd=source)
    _git("config", "user.email", "test@example.com", cwd=source)
    _git("config", "user.name", "Test User", cwd=source)
    target = source / "src" / "parser.py"
    target.parent.mkdir()
    target.write_text(
        "class Parser:\n    def normalize(self, value):\n        return value.strip()\n",
        encoding="utf-8",
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "fixture", cwd=source)
    base_commit = _git("rev-parse", "HEAD", cwd=source)

    remote = tmp_path / "remotes" / "standalone-fixture" / "demo.git"
    remote.parent.mkdir(parents=True)
    _git("init", "--bare", str(remote))
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "origin", "HEAD:main", cwd=source)

    cache = tmp_path / "cache"
    monkeypatch.setenv("MYCODE_REPO_CACHE_DIR", str(cache))
    monkeypatch.setenv("MYCODE_REPO_REMOTE_TEMPLATE", f"file://{tmp_path}/remotes/{{repo}}.git")
    sample = _sample(repo, base_commit)

    first = prepare_repository_assets(sample, structure_only=True, auto_fetch=True)
    assert first.source == "generated_structure"
    assert first.repo_root is not None
    assert first.repo_root != NO_REPO_ROOT
    assert first.structure_path is not None and first.structure_path.exists()
    checkout = cache / "checkouts" / "standalone-fixture_demo" / base_commit
    assert _git("rev-parse", "HEAD", cwd=checkout) == base_commit

    index = RepositoryIndex(
        repo=repo,
        instance_id=sample.instance_id,
        dataset=sample.dataset,
        repo_root=first.repo_root,
        structure_path=first.structure_path,
    )
    assert "src/parser.py" in index.files
    assert {entity.name for entity in index.entities} >= {"Parser", "normalize"}

    shutil.rmtree(tmp_path / "remotes")
    second = prepare_repository_assets(sample, structure_only=True, auto_fetch=False)
    assert second.source == "existing_structure"
    assert second.structure_path == first.structure_path
    assert second.repo_root == first.repo_root

    result = run_localization_pipeline(
        sample,
        structure_only=True,
        lightweight=True,
        auto_fetch_repos=False,
        top_k=5,
    )
    assert result["localization"]["status"] == "ok"
    assert result["localization"]["ranked_locations"]


def test_existing_external_structure_is_preferred_without_fetch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance_id = "external__fixture-1"
    structure = (
        tmp_path
        / "LocAgent"
        / "newtest"
        / "swebench_multimodal-full-dev"
        / "repo_structures"
        / f"{instance_id}.json"
    )
    structure.parent.mkdir(parents=True)
    structure.write_text(
        json.dumps(
            {
                "base_commit": "abc123",
                "structure": {
                    "src/app.py": {
                        "text": "def run():\n    return True\n",
                        "classes": [],
                        "functions": [{"name": "run", "start_line": 1, "end_line": 2}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOC_CODE_ROOT", str(tmp_path))
    monkeypatch.setenv("MYCODE_REPO_CACHE_DIR", str(tmp_path / "cache"))

    sample = NormalizedSample(
        instance_id=instance_id,
        repo="external/fixture",
        dataset="swebench_multimodal-full-dev-clean15",
        issue_text="run should return true",
        raw={"base_commit": "abc123"},
    )
    assets = prepare_repository_assets(sample, structure_only=True, auto_fetch=True)
    assert assets.source == "existing_structure"
    assert assets.structure_path == structure
    assert not (tmp_path / "cache" / "git").exists()


def test_existing_structure_can_require_exact_source_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance_id = "external__fixture-2"
    structure = (
        tmp_path
        / "LocAgent"
        / "newtest"
        / "swebench_multimodal-full-dev"
        / "repo_structures"
        / f"{instance_id}.json"
    )
    structure.parent.mkdir(parents=True)
    structure.write_text(
        json.dumps(
            {
                "base_commit": "abc123",
                "structure": {"src/app.py": {"text": "def run(): pass"}},
            }
        ),
        encoding="utf-8",
    )
    checkout = tmp_path / "exact-checkout"
    checkout.mkdir()
    calls: list[tuple[str, str]] = []

    def fake_checkout(repo: str, base_commit: str) -> Path:
        calls.append((repo, base_commit))
        return checkout

    monkeypatch.setenv("LOC_CODE_ROOT", str(tmp_path))
    monkeypatch.setenv("MYCODE_REQUIRE_SOURCE_CHECKOUT", "1")
    monkeypatch.setattr(
        "mycode.repo_index.repository_assets.ensure_repo_checkout",
        fake_checkout,
    )
    sample = NormalizedSample(
        instance_id=instance_id,
        repo="external/fixture",
        dataset="swebench_multimodal-full-dev-clean15",
        issue_text="run should use the exact implementation",
        raw={"base_commit": "abc123"},
    )

    assets = prepare_repository_assets(sample, structure_only=True, auto_fetch=True)

    assert calls == [("external/fixture", "abc123")]
    assert assets.structure_path == structure
    assert assets.repo_root == checkout
    assert assets.source == "existing_structure_with_standalone_checkout"


def test_checkout_failure_preserves_canonical_structure(tmp_path: Path, monkeypatch) -> None:
    from mycode.repo_index import repository_assets as module

    structure = tmp_path / "snapshot.json"
    structure.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MYCODE_REQUIRE_SOURCE_CHECKOUT", "1")
    monkeypatch.setattr(module, "find_repo_structure", lambda *a, **kw: structure)
    monkeypatch.setattr(module, "find_repo_root", lambda *a, **kw: None)

    def unavailable(*args):
        raise module.RepositoryAssetError("checkout transport unavailable")

    monkeypatch.setattr(module, "ensure_repo_checkout", unavailable)
    sample = NormalizedSample(
        instance_id="external__fixture-3", repo="external/fixture",
        dataset="swebench_multimodal-full-dev-clean15", issue_text="Fix parsing",
        raw={"base_commit": "abc123"},
    )
    assets = module.prepare_repository_assets(sample, structure_only=True, auto_fetch=True)
    assert assets.repo_root == module.NO_REPO_ROOT
    assert assets.structure_path == structure
    assert assets.base_commit == "abc123"
    assert assets.source == "existing_structure_checkout_unavailable"
    assert "transport unavailable" in assets.source_error
