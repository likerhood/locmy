from __future__ import annotations

from pathlib import Path

from mycode.dynamic_retrieval.fast_seed_planner import plan_fast_seeds
from mycode.evidence.issue_sketch import _evidence_roles
from mycode.evidence.tools.local_code_url_resolver import resolve_github_code_url
from mycode.evidence.tools.url_inspector import inspect_url
from mycode.evidence.tools.vlm_image_reader import normalize_vlm_analysis
from mycode.repo_index.structure_index import RepositoryIndex


def test_github_blob_resolves_against_local_base_commit(tmp_path: Path) -> None:
    source = tmp_path / "src" / "state" / "actions.js"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export function savePost(post) {\n  return dispatchPost(post);\n}\n",
        encoding="utf-8",
    )
    result = resolve_github_code_url(
        "https://github.com/example/project/blob/main/src/state/actions.js#L2",
        repo_root=tmp_path,
        expected_repo="example/project",
        base_commit="abc123",
        context_lines=2,
    )
    assert result["local_resolution_status"] == "resolved"
    assert result["local_path"] == "src/state/actions.js"
    assert result["provenance"] == "direct_local_code_anchor"
    assert result["symbol_hint"] == "savePost"
    assert "dispatchPost" in result["source_excerpt"]
    assert result["ref_policy"] == "benchmark_base_commit_overrides_url_ref"


def test_github_blob_rejects_other_repository(tmp_path: Path) -> None:
    result = resolve_github_code_url(
        "https://github.com/other/project/blob/main/src/app.py#L1",
        repo_root=tmp_path,
        expected_repo="example/project",
        base_commit="abc123",
    )
    assert result["local_resolution_status"] == "repository_mismatch"
    assert "source_excerpt" not in result


def test_github_tree_search_and_root_route_to_local_index(tmp_path: Path) -> None:
    (tmp_path / "src" / "parser").mkdir(parents=True)
    tree = resolve_github_code_url(
        "https://github.com/example/project/tree/main/src/parser",
        repo_root=tmp_path,
        expected_repo="example/project",
        base_commit="abc123",
    )
    search = resolve_github_code_url(
        "https://github.com/example/project/search?q=tokenizer+emphasis",
        repo_root=tmp_path,
        expected_repo="example/project",
        base_commit="abc123",
    )
    root = resolve_github_code_url(
        "https://github.com/example/project",
        repo_root=tmp_path,
        expected_repo="example/project",
        base_commit="abc123",
    )
    assert tree["local_resolution_status"] == "local_tree_ready"
    assert tree["local_path_prefix"] == "src/parser"
    assert search["local_resolution_status"] == "local_search_ready"
    assert search["local_search_terms"] == ["tokenizer", "emphasis"]
    assert root["local_resolution_status"] == "repository_confirmed"
    assert "local_path" not in root
    assert inspect_url("https://github.com/example/project")["github_kind"] == "repository"


def test_failed_browser_observation_is_not_runtime_evidence() -> None:
    roles, _, _ = _evidence_roles(
        {"url_inspections": [], "image_inspections": []},
        [
            {
                "tool": "browser_reproduction_reader",
                "source": "https://example.test/demo",
                "success": False,
                "status": "browser_unavailable",
                "extracted": {"browser_used": False, "runtime_trace": {}},
            }
        ],
    )
    assert roles[0]["role"] == "Unavailable reproduction reference"
    assert roles[0]["navigation_value"] == "low"


def test_live_browser_observation_requires_runtime_trace() -> None:
    roles, _, _ = _evidence_roles(
        {"url_inspections": [], "image_inspections": []},
        [
            {
                "tool": "browser_reproduction_reader",
                "source": "https://example.test/demo",
                "success": True,
                "status": "ok",
                "extracted": {
                    "browser_used": True,
                    "network_used": True,
                    "runtime_trace": {"console": ["ready"]},
                },
            }
        ],
    )
    assert roles[0]["role"] == "Executable reproduction observation"
    assert roles[0]["metadata"]["runtime_trace_present"] is True


def test_vlm_normalization_adds_compact_visual_graph() -> None:
    result = normalize_vlm_analysis(
        {
            "visible_text": ["Save"],
            "visual_entities": ["settings form", "submit button"],
            "symptom": "The form remains open after saving.",
            "search_queries": ["settings save handler"],
        },
        image_format="png",
        issue_summary="Settings form does not close after save.",
        repo="example/project",
    )
    visual_ir = result["visual_ir"]
    assert result["schema_version"] == "vlm_image_understanding.v3"
    assert visual_ir["image_type"] == "ui_screenshot"
    assert any(node["id"] == "symptom" for node in visual_ir["nodes"])
    assert any(edge["target"] == "symptom" for edge in visual_ir["edges"])


def test_fast_seed_planner_is_bounded_and_rejects_invented_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MYCODE_FAST_SEED_PLANNER", "1")
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "actions.js").write_text(
        "export function savePost(post) { return dispatchPost(post); }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "reducer.js").write_text(
        "export function postsReducer(state, action) { return state; }\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "save.md").write_text("save post documentation", encoding="utf-8")
    index = RepositoryIndex(repo_root=tmp_path, repo="example/project", instance_id="x", dataset="unit")

    def fake_llm(_prompt: str):
        return {"content": '{"seed_files":["invented.js","docs/save.md","src/actions.js"]}'}

    result = plan_fast_seeds(
        index=index,
        issue_text="savePost should dispatch the post state update",
        query_groups={"symbol": ["savePost"], "concern": ["post state update"]},
        evidence_result={"tool_observations": []},
        controller_llm=fake_llm,
    )
    assert result["seed_files"][0] == "src/actions.js"
    assert "invented.js" not in result["seed_files"]
    assert "docs/save.md" not in result["seed_files"]
    assert len(result["candidate_files"]) <= 10
    assert len(result["responsibility_candidates"]) <= 6
    assert len(result["seed_files"]) <= 3
