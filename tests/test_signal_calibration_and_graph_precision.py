from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mycode.dynamic_retrieval.search_agent import (
    RankedLocation,
    _apply_architecture_lane_policy,
    _apply_llm_candidate_review,
    _cosil_style_prune_candidates,
    _domain_path_probes,
    _exact_symbol_queries,
    _merge_search_hits_by_channel,
    _multi_channel_global_recall,
    _path_probe_hits,
    _rank_confidence,
    _rank_memory_bonus,
)
from mycode.repo_index.structure_index import RepositoryIndex, SearchHit
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


class _Sketch:
    workflow: list[str] = []
    concerns: list[str] = []
    states: list[str] = []
    expected_effects: list[str] = []
    entities: list[str] = []


def _sample(instance_id: str, repo: str, issue_text: str) -> NormalizedSample:
    return NormalizedSample(
        instance_id=instance_id,
        repo=repo,
        dataset="unit",
        issue_text=issue_text,
        raw={"problem_statement": issue_text},
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_wp_domain_probe_requires_store_setup_evidence_from_issue() -> None:
    unrelated = _sample(
        "Automattic__wp-calypso-21648",
        "Automattic/wp-calypso",
        "Store settings email From-Name placeholder contains an undecoded HTML entity.",
    )
    noisy_evidence = {
        "evidence_synthesis": {
            "summary": "Maybe store location, unsupported countries, signup, email verification and wp-admin."
        }
    }
    assert not any(
        "dashboard/store-location" in probe
        for probe in _domain_path_probes(unrelated, noisy_evidence, _Sketch())
    )

    store_setup = _sample(
        "Automattic__wp-calypso-21409",
        "Automattic/wp-calypso",
        "Store signup flow must route unsupported countries after the address page to wp-admin.",
    )
    probes = _domain_path_probes(store_setup, {}, _Sketch())
    assert "client/extensions/woocommerce/app/dashboard/store-location" in probes


def test_path_probe_and_round_memory_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/plugins/plugin.legend.js", "export function handleEvent() {}\n")
        _write(root / "src/plugins/plugin.title.js", "export function drawTitle() {}\n")
        index = RepositoryIndex(repo="chartjs/Chart.js", instance_id="unit", repo_root=root)
        hits = _path_probe_hits(index, ["src/plugins/plugin.legend.js"])
        assert hits[0][0] == "src/plugins/plugin.legend.js"
        assert hits[0][1] <= 96.0

    monkeypatch.setenv("MYCODE_MEMORY_MAX_BONUS", "24")
    monkeypatch.setenv("MYCODE_MEMORY_RANK_DECAY", "0.5")
    assert _rank_memory_bonus(0) == pytest.approx(24.0)
    assert _rank_memory_bonus(1) == pytest.approx(12.0)
    assert _rank_memory_bonus(10) < 0.1
    assert _rank_memory_bonus(0, no_gain_rounds=2) == pytest.approx(24.0 * 0.45 * 0.45)
    assert _rank_memory_bonus(0, path_role="reproduction_or_example") == pytest.approx(24.0 * 0.35)


def test_exact_symbol_lane_preserves_explicit_entity_amid_noisy_queries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for index in range(80):
            _write(root / f"src/noise_{index}.js", f"export function helper{index}() {{ return 'load strings'; }}\n")
        _write(root / "src/io/files.js", "export function loadStrings(path) { return path; }\n")
        repo_index = RepositoryIndex(repo="processing/p5.js", instance_id="symbols", repo_root=root)
        groups = {
            "explicit_entity": ["loadStrings()"],
            "concern": ["load text files strings callback empty lines"],
            "effect": [],
            "flow": [],
            "evidence_role": [],
        }
        _files, entities, diagnostics = _multi_channel_global_recall(
            index=repo_index,
            retrieval_queries=["load text files strings callback empty lines"],
            frontier_queries=groups,
            file_limit=5,
            entity_limit=5,
        )
        assert _exact_symbol_queries(groups["explicit_entity"]) == ["loadStrings"]
        assert any(hit.path == "src/io/files.js" and hit.name == "loadStrings" for hit in entities)
        assert diagnostics["exact_symbol_lane"]["hits"] == 1


def test_channel_fusion_bounds_raw_lexical_scale() -> None:
    fused = _merge_search_hits_by_channel(
        [
            ("all", [SearchHit(path="src/large.js", score=10_000.0, reasons=["large file"])]),
            ("architecture", [SearchHit(path="src/target.js", score=80.0, reasons=["role match"])]),
        ],
        limit=10,
    )
    assert all(hit.score < 100.0 for hit in fused)
    assert {hit.path for hit in fused} == {"src/large.js", "src/target.js"}


def test_pruning_reserves_read_slots_for_architecture_lane() -> None:
    aggregate = {
        f"src/noise_{index}.js": RankedLocation(path=f"src/noise_{index}.js", score=1000.0 - index)
        for index in range(20)
    }
    aggregate["src/ownership-ui.jsx"] = RankedLocation(path="src/ownership-ui.jsx", score=40.0)
    aggregate["src/ownership-action.js"] = RankedLocation(path="src/ownership-action.js", score=35.0)
    components = {
        path: {"bm25_score": item.score}
        for path, item in aggregate.items()
    }
    components["src/ownership-ui.jsx"]["architecture_path_probe"] = 70.0
    components["src/ownership-action.js"]["architecture_path_probe"] = 65.0
    kept, diagnostics = _cosil_style_prune_candidates(
        aggregate,
        components,
        top_k=5,
        read_budget=10,
    )
    paths = {item.path for item in kept}
    assert {"src/ownership-ui.jsx", "src/ownership-action.js"} <= paths
    assert diagnostics["component_limits"]["architecture_path_probe"] == 6


def test_architecture_lane_requires_independent_corroboration_for_large_bonus() -> None:
    aggregate = {
        "src/guess.js": RankedLocation(path="src/guess.js", score=80.0),
        "src/target.js": RankedLocation(path="src/target.js", score=100.0),
    }
    components = {
        "src/guess.js": {"architecture_path_probe": 80.0},
        "src/target.js": {"architecture_path_probe": 80.0, "symbol_score": 20.0},
    }

    diagnostics = _apply_architecture_lane_policy(aggregate, components)

    assert diagnostics["adjusted_count"] == 2
    assert components["src/guess.js"]["architecture_path_probe"] == 6.0
    assert components["src/target.js"]["architecture_path_probe"] == 24.0
    assert aggregate["src/guess.js"].score == 6.0
    assert aggregate["src/target.js"].score == 44.0


def test_llm_review_cannot_promote_unread_candidate_over_grounded_code() -> None:
    unread_ranked = [
        RankedLocation(path="src/controller.js", score=100.0),
        RankedLocation(path="src/element.js", score=95.0),
    ]
    review = {
        "status": "ok",
        "candidates": [
            {
                "path": "src/element.js",
                "role": "patch_target",
                "confidence": 1.0,
                "matched_issue_axes": ["concern", "flow"],
                "entities": [{"kind": "function", "name": "draw"}],
            }
        ],
    }
    components = {item.path: {} for item in unread_ranked}
    reranked = _apply_llm_candidate_review(
        unread_ranked,
        review,
        component_scores=components,
        top_k=2,
        code_contexts=[],
    )
    assert reranked[0].path == "src/controller.js"
    assert components["src/element.js"]["llm_candidate_review"] <= 4.0

    grounded_ranked = [
        RankedLocation(path="src/controller.js", score=100.0),
        RankedLocation(path="src/element.js", score=95.0),
    ]
    grounded_components = {item.path: {} for item in grounded_ranked}
    grounded = _apply_llm_candidate_review(
        grounded_ranked,
        review,
        component_scores=grounded_components,
        top_k=2,
        code_contexts=[
            {
                "path": "src/element.js",
                "snippets": [{"text": "function draw() { return borderRadius; }"}],
            }
        ],
    )
    assert grounded[0].path == "src/element.js"
    assert grounded[0].belief["llm_candidate_review"]["grounded"] is True


def test_rank_confidence_uses_relative_gap_not_absolute_score() -> None:
    ranked = [
        RankedLocation(path="src/a.js", score=10000.0, score_components={"bm25_score": 9000.0}),
        RankedLocation(path="src/b.js", score=9990.0, score_components={"bm25_score": 8990.0}),
    ]
    confidence = _rank_confidence(ranked, code_contexts=[])
    assert confidence["relative_gap"] < 0.01
    assert confidence["confidence"] < 0.5


def test_graph_suppresses_generic_calls_and_generic_config_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/update.js", "export function update() { return true; }\n")
        _write(root / "app/main.js", "export function runFeature() { return update(); }\n")
        _write(root / "package.json", '{"name":"demo","type":"module","main":"src/update.js","files":[]}\n')
        for index in range(20):
            _write(
                root / f"src/state/selectors_{index}.js",
                f"export function selectValue{index}(state) {{ return state.value; }}\n",
            )
        _write(root / "src/view.js", "export function view(selector) { return selector; }\n")

        repo_index = RepositoryIndex(repo="example/project", instance_id="precision", repo_root=root)
        graph = TypedRepositoryGraph(repo_index)

        assert not any(
            edge.edge_type == "calls" and edge.target == "src/update.js"
            for edge in graph.edges_by_source.get("app/main.js", [])
        )
        assert not any(
            edge.edge_type == "configures"
            for edge in graph.edges_by_source.get("package.json", [])
        )
        selector_edges = [
            edge
            for edge in graph.edges_by_source.get("src/view.js", [])
            if edge.edge_type == "selects_state"
        ]
        assert selector_edges == []
