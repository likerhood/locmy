from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from mycode.dynamic_retrieval.candidate_reviewer import review_candidates
from mycode.dynamic_retrieval.controller import _parse_llm_decisions
from mycode.dynamic_retrieval.react_agent import run_react_tool_agent
from mycode.dynamic_retrieval.search_agent import (
    RankedLocation,
    _apply_llm_candidate_review,
    _build_modification_closure,
    _cross_round_candidate_frontier,
    _merge_closure_candidate_reviews,
    _precision_rerank_locations,
    _rerank_with_modification_closure,
    _round_progress,
    _stop_decision,
    _rank_entities,
    _round_checkpoint_quality,
    _path_role,
)
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.repo_index import structure_index
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


class _Sketch:
    workflow = []
    concerns = ["loading text lines"]
    states = ["empty line"]
    expected_effects = ["preserve empty lines"]
    entities = ["loadStrings"]
    seed_policy = []
    flow_obligations = []

    def to_dict(self):
        return {
            "concerns": ["loading text lines"],
            "states": ["empty line"],
            "expected_effects": ["preserve empty lines"],
        }


def test_candidate_review_recovers_complete_candidate_from_truncated_json() -> None:
    ranked = [
        RankedLocation(path="src/io/files.js", score=90.0, reasons=["entity:loadStrings"]),
        RankedLocation(path="docs/loadStrings.md", score=80.0, reasons=["lexical"]),
    ]
    contexts = [
        {
            "path": "src/io/files.js",
            "entities": [{"kind": "function", "name": "loadStrings"}],
            "snippets": [{"text": "function loadStrings() { return lines.filter(Boolean); }"}],
        }
    ]

    def truncated_llm(_prompt: str) -> str:
        return (
            '{"candidates":[{"path":"src/io/files.js","role":"patch_target",'
            '"confidence":0.96,"rationale":"filter drops empty lines",'
            '"evidence_quote":"return lines.filter(Boolean)","mechanism_verified":true,'
            '"patch_mechanism":"filter(Boolean) removes empty lines",'
            '"counterevidence":[],"matched_issue_axes":["concern","flow"],'
            '"entities":[{"kind":"function","name":"loadStrings"}]},'
            '{"path":"docs/loadStrings.md","role":"navigation_only"'
        )

    review = review_candidates(
        llm=truncated_llm,
        issue_text="loadStrings should preserve empty lines",
        issue_sketch=_Sketch(),
        ranked=ranked,
        code_contexts=contexts,
        flow_traces=[{"candidate_target_paths": ["src/io/files.js"], "flow_type": "dataflow"}],
        round_no=1,
        candidate_limit=2,
        repair_attempts=0,
    )

    assert review["status"] == "ok"
    assert review["parse_mode"] == "partial_candidate_recovery"
    assert review["recovered_from_truncated_json"] is True
    assert [item["path"] for item in review["candidates"]] == ["src/io/files.js"]
    assert review["candidates"][0]["mechanism_verified"] is True


def test_candidate_review_keeps_supported_entities_when_one_is_invented() -> None:
    ranked = [
        RankedLocation(
            path="src/parser.js",
            score=90.0,
            score_components={"flow_score": 8.0},
        )
    ]

    def fake_llm(_prompt: str) -> str:
        return (
            '{"candidates":[{"path":"src/parser.js","role":"patch_target",'
            '"confidence":0.9,"evidence_quote":"function tokenize(input)",'
            '"mechanism_verified":true,"patch_mechanism":"tokenize applies the parser rule",'
            '"matched_issue_axes":["concern","flow"],'
            '"entities":[{"kind":"function","name":"tokenize"},'
            '{"kind":"function","name":"inventedHelper"}]}],'
            '"continue_search":false,"missing_evidence":[],"next_queries":[]}'
        )

    review = review_candidates(
        llm=fake_llm,
        issue_text="The tokenizer applies the wrong parser rule.",
        issue_sketch=_Sketch(),
        ranked=ranked,
        code_contexts=[
            {"path": "src/parser.js", "snippets": [{"text": "function tokenize(input) { return parse(input); }"}]}
        ],
        flow_traces=[{"candidate_target_paths": ["src/parser.js"], "flow_type": "parser_tokenizer_flow"}],
        round_no=1,
    )
    candidate = review["candidates"][0]
    assert candidate["mechanism_verified"] is True
    assert candidate["supported_entities"] == [{"kind": "function", "name": "tokenize"}]
    assert candidate["unsupported_entities"] == [{"kind": "function", "name": "inventedHelper"}]


def test_candidate_review_uses_compact_evidence_ids_and_stage_token_budget(monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_CANDIDATE_REVIEW_MAX_TOKENS", "1200")
    calls: list[tuple[str, int]] = []

    def fake_llm(prompt: str, *, max_tokens: int):
        calls.append((prompt, max_tokens))
        return {
            "content": (
                '{"selected_head":"src/parser.js","reviews":['
                '{"path":"src/parser.js","verdict":"verified","confidence":0.93,'
                '"snippet_id":"C1S1","entity_id":"C1E1","flow_id":"F1",'
                '"causal_chain":"tokenize drops empty input before parser output",'
                '"rejection_code":"none"}],"continue_search":false,'
                '"missing_evidence":[],"next_queries":[]}'
            )
        }

    review = review_candidates(
        llm=fake_llm,
        issue_text="The tokenizer must preserve empty input.",
        issue_sketch=_Sketch(),
        ranked=[RankedLocation(path="src/parser.js", score=90.0)],
        code_contexts=[
            {
                "path": "src/parser.js",
                "entities": [{"kind": "function", "name": "tokenize"}],
                "snippets": [{"text": "return input.filter(Boolean);"}],
            }
        ],
        flow_traces=[
            {
                "flow_type": "parser_tokenizer_flow",
                "candidate_target_paths": ["src/parser.js"],
            }
        ],
        round_no=1,
    )

    assert calls and calls[0][1] == 1200
    assert "Use snippet_id, entity_id, and flow_id exactly as supplied" in calls[0][0]
    assert review["prompt_schema"] == "evidence_ids_v2"
    assert review["selected_head"] == "src/parser.js"
    assert review["candidates"][0]["mechanism_verified"] is True
    assert review["continue_search"] is False


def test_candidate_review_rejects_compact_head_with_unknown_flow_id() -> None:
    review = review_candidates(
        llm=lambda _prompt: (
            '{"selected_head":"src/parser.js","reviews":['
            '{"path":"src/parser.js","verdict":"verified","confidence":0.99,'
            '"snippet_id":"C1S1","entity_id":"C1E1","flow_id":"F99",'
            '"causal_chain":"claimed chain","rejection_code":"none"}],'
            '"continue_search":false,"missing_evidence":[],"next_queries":[]}'
        ),
        issue_text="The tokenizer must preserve empty input.",
        issue_sketch=_Sketch(),
        ranked=[RankedLocation(path="src/parser.js", score=90.0)],
        code_contexts=[
            {
                "path": "src/parser.js",
                "entities": [{"kind": "function", "name": "tokenize"}],
                "snippets": [{"text": "function tokenize(input) { return input; }"}],
            }
        ],
        flow_traces=[],
        round_no=1,
    )

    assert review["selected_head"] is None
    assert review["candidates"][0]["mechanism_verified"] is False
    assert review["continue_search"] is True


def test_candidate_review_reuses_unchanged_source_evidence() -> None:
    calls = 0

    def fake_llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return (
            '{"candidates":[{"path":"src/cache_target.py","role":"navigation_only",'
            '"confidence":0.4,"matched_issue_axes":[],"entities":[]}],'
            '"continue_search":true,"missing_evidence":[],"next_queries":[]}'
        )

    kwargs = {
        "llm": fake_llm,
        "issue_text": "Unique cache review issue 91427",
        "issue_sketch": _Sketch(),
        "code_contexts": [{"path": "src/cache_target.py", "snippets": [{"text": "def target(): pass"}]}],
        "flow_traces": [],
        "candidate_limit": 3,
    }
    first = review_candidates(
        ranked=[RankedLocation(path="src/cache_target.py", score=10.0)],
        round_no=1,
        **kwargs,
    )
    second = review_candidates(
        ranked=[RankedLocation(path="src/cache_target.py", score=999.0)],
        round_no=2,
        **kwargs,
    )
    assert calls == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True


def test_round_checkpoint_prefers_verified_mechanism_over_later_architecture_guess() -> None:
    verified = RankedLocation(
        path="src/io/files.js",
        score=90.0,
        score_components={"symbol_score": 20.0, "flow_score": 12.0},
        belief={
            "read_verified": True,
            "supporting_axes": ["vertical_program", "flow_validation"],
            "llm_candidate_review": {"grounded": True, "mechanism_verified": True},
        },
    )
    guessed = RankedLocation(
        path="src/architecture/guess.js",
        score=500.0,
        score_components={"architecture_path_probe": 24.0},
        belief={"read_verified": True, "supporting_axes": [], "llm_candidate_review": {}},
    )
    earlier = SimpleNamespace(
        round_no=1,
        ranked_locations=[verified],
        stop_decision={"confidence": {"confidence": 0.82}},
        verifier={"llm_candidate_review": {"critical_missing_evidence": []}},
    )
    later = SimpleNamespace(
        round_no=2,
        ranked_locations=[guessed],
        stop_decision={"confidence": {"confidence": 0.9}},
        verifier={"llm_candidate_review": {"critical_missing_evidence": []}},
    )

    assert _round_checkpoint_quality(earlier)["score"] > _round_checkpoint_quality(later)["score"]


def test_candidate_review_repairs_json_and_reranks_patch_target() -> None:
    ranked = [
        RankedLocation(path="docs/loadStrings.md", score=100.0, reasons=["many lexical matches"]),
        RankedLocation(path="src/io/files.js", score=72.0, reasons=["entity:loadStrings"]),
        RankedLocation(path="test/io/files.js", score=65.0, reasons=["test mention"]),
    ]
    contexts = [
        {
            "path": "src/io/files.js",
            "entities": [{"kind": "function", "name": "loadStrings"}],
            "snippets": [{"start_line": 1, "end_line": 4, "text": "function loadStrings() { return lines.filter(Boolean); }"}],
        }
    ]
    calls = []

    def fake_llm(prompt: str):
        calls.append(prompt)
        if len(calls) == 1:
            return {"content": "not valid json", "usage": {"total_tokens": 5}}
        return {
            "content": (
                '{"candidates":['
                '{"path":"src/io/files.js","role":"patch_target","confidence":0.96,'
                '"rationale":"The filtering implementation drops empty lines.",'
                '"evidence_quote":"return lines.filter(Boolean);",'
                '"mechanism_verified":true,'
                '"patch_mechanism":"filter(Boolean) removes empty strings before they are returned.",'
                '"matched_issue_axes":["concern","flow"],'
                '"entities":[{"kind":"function","name":"loadStrings"}]},'
                '{"path":"docs/loadStrings.md","role":"test_or_docs","confidence":0.95,'
                '"rationale":"Documentation repeats the symptom.","matched_issue_axes":[],"entities":[]}'
                '],"continue_search":false,"missing_evidence":[],"next_queries":[]}'
            ),
            "usage": {"total_tokens": 10},
        }

    review = review_candidates(
        llm=fake_llm,
        issue_text="loadStrings should preserve empty lines",
        issue_sketch=_Sketch(),
        ranked=ranked,
        code_contexts=contexts,
        flow_traces=[{"candidate_target_paths": ["src/io/files.js"], "flow_type": "dataflow"}],
        round_no=1,
        repair_attempts=1,
    )
    assert review["status"] == "ok"
    assert len(review["attempts"]) == 2
    assert review["candidates"][0]["mechanism_verified"] is True
    assert review["candidates"][0]["quote_supported"] is True

    components = {item.path: {} for item in ranked}
    reranked = _apply_llm_candidate_review(
        ranked,
        review,
        component_scores=components,
        top_k=3,
        code_contexts=contexts,
    )
    assert reranked[0].path == "src/io/files.js"
    assert reranked[0].score_components["llm_candidate_review"] > 0
    assert components["docs/loadStrings.md"]["llm_candidate_review"] < 0


def test_invalid_candidate_review_keeps_deterministic_order() -> None:
    ranked = [
        RankedLocation(path="src/a.py", score=20.0),
        RankedLocation(path="src/b.py", score=10.0),
    ]

    review = review_candidates(
        llm=lambda _prompt: {"content": '{"candidates":[{"path":"invented.py","role":"patch_target"}]}'},
        issue_text="fix state update",
        issue_sketch=_Sketch(),
        ranked=ranked,
        code_contexts=[],
        flow_traces=[],
        round_no=1,
        repair_attempts=0,
    )
    assert review["status"] == "fallback"
    assert review["continue_search"] is True
    assert review["critical_missing_evidence"]
    assert review["stop_ready"] is False
    reranked = _apply_llm_candidate_review(ranked, review, component_scores={}, top_k=2)
    assert [item.path for item in reranked] == ["src/a.py", "src/b.py"]


def test_invalid_candidate_review_blocks_high_confidence_stop() -> None:
    ranked = [RankedLocation(path="src/a.py", score=100.0)]
    review = {
        "status": "fallback",
        "continue_search": True,
        "critical_missing_evidence": ["Actual source implementation remains unverified."],
        "stop_ready": False,
    }

    decision = _stop_decision(
        round_no=1,
        max_rounds=4,
        ranked=ranked,
        previous_top_paths=["src/a.py"],
        next_queries=["implementation flow"],
        confidence={
            "confidence": 0.99,
            "axes": {
                "source": True,
                "symbol": True,
                "path": True,
                "review": False,
                "read": True,
                "program": True,
                "flow": True,
            },
        },
        candidate_review=review,
    )

    assert decision["stop"] is False
    assert decision["reason"] == "candidate_review_has_critical_evidence_gap"


def test_candidate_review_rejects_unverifiable_patch_mechanism() -> None:
    ranked = [RankedLocation(path="src/io/files.js", score=50.0)]
    review = review_candidates(
        llm=lambda _prompt: {
            "content": (
                '{"candidates":[{"path":"src/io/files.js","role":"patch_target","confidence":0.99,'
                '"rationale":"This must be the implementation.",'
                '"evidence_quote":"nonexistent deleteEmptyLines call",'
                '"mechanism_verified":true,'
                '"patch_mechanism":"deleteEmptyLines removes values",'
                '"matched_issue_axes":["concern","flow"],'
                '"entities":[{"kind":"function","name":"deleteEmptyLines"}]}],'
                '"continue_search":false,"missing_evidence":[],"next_queries":[]}'
            )
        },
        issue_text="loadStrings should preserve empty lines",
        issue_sketch=_Sketch(),
        ranked=ranked,
        code_contexts=[
            {
                "path": "src/io/files.js",
                "snippets": [{"text": "function loadStrings() { return lines.filter(Boolean); }"}],
            }
        ],
        flow_traces=[],
        round_no=1,
        repair_attempts=0,
    )
    assert review["status"] == "ok"
    assert review["candidates"][0]["grounded"] is True
    assert review["candidates"][0]["quote_supported"] is False
    assert review["candidates"][0]["entity_supported"] is False
    assert review["candidates"][0]["mechanism_verified"] is False
    assert review["stop_ready"] is False


def test_issue_sketch_prefers_llm_claims_and_filters_heading_words() -> None:
    sample = NormalizedSample(
        instance_id="processing__p5.js-6111",
        repo="processing/p5.js",
        dataset="unit",
        issue_text="Original Issue About Most camera frustum behavior in RendererGL and Camera._resize",
        raw={"problem_statement": "Original Issue About Most camera frustum behavior in RendererGL and Camera._resize"},
    )
    evidence = {
        "evidence_packet": {"code_references": [], "url_inspections": [], "image_inspections": []},
        "evidence_synthesis": {"query_groups": {"symbol": ["RendererGL"]}},
        "tool_observations": [],
        "llm_understanding": {
            "issue_sketch": {
                "workflow": "WebGL camera projection",
                "concern": "frustum clipping and orthographic camera updates",
                "state": "near far camera bounds",
                "expected_effect": "render geometry inside the configured frustum",
                "entities": ["RendererGL", "Camera._resize"],
                "concern_queries": ["camera frustum projection"],
                "evidence_roles": [],
            }
        },
    }
    sketch = build_issue_sketch(sample, evidence)
    assert sketch.workflow[0] == "WebGL camera projection"
    assert "Camera._resize" in sketch.entities
    assert not {"Original", "Issue", "About", "Most"}.intersection(sketch.entities)
    assert any(row["source"] == "llm_understanding" for row in sketch.claim_evidence)


def test_issue_sketch_keeps_url_artifacts_out_of_state_and_entities() -> None:
    issue = (
        "[Original Issue]\nloadStrings() omits empty lines. "
        "Demo: https://codepen.io/Spongman/pen/wXVeYP. "
        "The bug also makes loadShader line numbers incorrect.\n"
        "Attached Images:\n- https://user-images.githubusercontent.com/example.png\n"
        "[Multimodal Context - Compact] around runtime_reproduction browser_behavior"
    )
    sample = NormalizedSample(
        instance_id="processing__p5.js-3068",
        repo="processing/p5.js",
        dataset="unit",
        issue_text=issue,
        raw={"problem_statement": issue},
    )
    evidence = {
        "evidence_packet": {
            "code_references": [],
            "url_inspections": [
                {
                    "url": "https://codepen.io/Spongman/pen/wXVeYP",
                    "role": "reproduction_entry",
                    "path": "/Spongman/pen/wXVeYP",
                }
            ],
            "image_inspections": [],
        },
        "evidence_synthesis": {"query_groups": {"symbol": ["loadStrings", "v0.6.1"]}},
        "tool_observations": [],
        "llm_understanding": {
            "issue_sketch": {
                "workflow": "reproduction_understanding behavior_to_code_layer",
                "concern_queries": ["loadStrings", "empty lines"],
                "state": "https://codepen.io/Spongman/pen/wXVeYP around",
                "expected_effect": "runtime_reproduction browser_behavior",
                "entities": ["loadStrings", "loadShader", "/Spongman/pen/wXVeYP", "v0.6.1"],
            }
        },
    }
    sketch = build_issue_sketch(sample, evidence)
    assert sketch.workflow == ["text file loading and line parsing"]
    assert sketch.states == ["empty_lines"]
    assert "loadStrings" in sketch.entities
    assert "loadShader" in sketch.entities
    assert "around" not in sketch.states
    assert "/Spongman/pen/wXVeYP" not in sketch.entities
    assert "v0.6.1" not in sketch.entities


def test_demo_url_query_does_not_create_route_flow_for_parser_issue() -> None:
    issue = (
        "Marked emphasis parsing fails in this "
        "[playground](https://example.test/demo?baseUrl=null&route=preview)."
    )
    sample = NormalizedSample(
        instance_id="markedjs__marked-2627",
        repo="markedjs/marked",
        dataset="unit",
        issue_text=issue,
        raw={"problem_statement": issue},
    )
    sketch = build_issue_sketch(
        sample,
        {
            "evidence_packet": {"code_references": [], "url_inspections": [], "image_inspections": []},
            "evidence_synthesis": {},
            "tool_observations": [],
        },
    )
    flow_types = {item["flow_type"] for item in sketch.flow_obligations}
    assert "parser_tokenizer_flow" in flow_types
    assert "url_builder_or_route_flow" not in flow_types


def test_natural_language_redirect_still_creates_route_flow() -> None:
    issue = "The account link redirects to the wrong route after saving."
    sample = NormalizedSample(
        instance_id="example__project-1",
        repo="example/project",
        dataset="unit",
        issue_text=issue,
        raw={"problem_statement": issue},
    )
    sketch = build_issue_sketch(
        sample,
        {
            "evidence_packet": {"code_references": [], "url_inspections": [], "image_inspections": []},
            "evidence_synthesis": {},
            "tool_observations": [],
        },
    )
    assert "url_builder_or_route_flow" in {item["flow_type"] for item in sketch.flow_obligations}


def test_early_stop_requires_grounded_review_or_stability() -> None:
    ranked = [
        RankedLocation(
            path="src/io/files.js",
            score=150.0,
            score_components={"symbol_score": 20.0, "llm_candidate_review": 10.0},
        ),
        RankedLocation(path="src/core/main.js", score=20.0),
    ]
    confidence = {
        "confidence": 0.95,
        "axes": {"source": True, "symbol": True, "path": False, "review": True, "read": True, "program": False, "flow": False},
    }
    decision = _stop_decision(
        round_no=1,
        max_rounds=2,
        ranked=ranked,
        previous_top_paths=[],
        next_queries=["loadStrings implementation"],
        confidence=confidence,
        candidate_review={
            "status": "ok",
            "stop_ready": False,
            "continue_search": False,
            "critical_missing_evidence": ["actual source implementation flow"],
        },
    )
    assert decision["stop"] is False
    assert decision["reason"] == "candidate_review_has_critical_evidence_gap"


def test_evidence_plateau_does_not_hide_required_review_evidence() -> None:
    ranked = [RankedLocation(path="src/io/files.js", score=100.0)]
    decision = _stop_decision(
        round_no=3,
        max_rounds=12,
        ranked=ranked,
        previous_top_paths=["src/io/files.js"],
        next_queries=["same unresolved query"],
        candidate_review={
            "status": "ok",
            "continue_search": True,
            "critical_missing_evidence": ["unresolved mechanism"],
        },
        round_progress={"plateau_streak": 2},
    )
    assert decision["stop"] is False
    assert decision["reason"] == "candidate_review_requests_more_evidence"


def test_evidence_plateau_stops_repeated_noncritical_review_after_late_round(monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_PLATEAU_REVIEW_OVERRIDE_ROUND", "10")
    ranked = [RankedLocation(path="src/io/files.js", score=100.0)]
    decision = _stop_decision(
        round_no=10,
        max_rounds=14,
        ranked=ranked,
        previous_top_paths=["src/io/files.js"],
        next_queries=["same optional query"],
        candidate_review={
            "status": "ok",
            "continue_search": True,
            "critical_missing_evidence": [],
        },
        round_progress={
            "plateau_streak": 2,
            "stable_top3": True,
            "strong_evidence_gain_count": 0,
            "new_flow_count": 0,
        },
    )
    assert decision["stop"] is True
    assert decision["reason"] == "evidence_plateau_after_repeated_review"


def test_round_progress_collapses_term_only_flow_churn_into_one_family() -> None:
    previous = RankedLocation(
        path="src/target.js",
        score=100.0,
        belief={"evidence_signature": ["read:verified"], "new_evidence": []},
    )
    current = RankedLocation(
        path="src/target.js",
        score=99.0,
        belief={"evidence_signature": ["read:verified"], "new_evidence": [], "evidence_gain_count": 0},
    )
    progress = _round_progress(
        ranked=[current],
        previous_ranked=[previous],
        flow_traces=[
            {"flow_type": "state_selector_use_chain", "term": "email", "backend": "regex_ast_lightweight"},
            {"flow_type": "state_selector_use_chain", "term": "option", "backend": "regex_ast_lightweight"},
        ],
        previous_flow_traces=[
            {"flow_type": "state_selector_use_chain", "term": "selector", "backend": "regex_ast_lightweight"}
        ],
        next_queries=["src target"],
        active_queries=["target"],
        previous_progress={"plateau_streak": 1},
    )
    assert progress["new_flow_count"] == 0
    assert progress["top5_overlap"] == 1.0
    assert progress["plateau"] is True
    assert progress["plateau_streak"] == 2


def test_final_patch_set_excludes_weak_context_only_support() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative in ("src/io/files.js", "src/core/error_helpers.js"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export function target() {}\n", encoding="utf-8")
        index = RepositoryIndex(repo="processing/p5.js", instance_id="closure", repo_root=root)
        graph = TypedRepositoryGraph(index)
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path="src/io/files.js", score=100.0),
                RankedLocation(path="src/core/error_helpers.js", score=20.0),
            ],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": "src/io/files.js",
                            "role": "patch_target",
                            "confidence": 0.95,
                            "matched_issue_axes": ["concern", "flow"],
                        },
                        {
                            "path": "src/core/error_helpers.js",
                            "role": "supporting_target",
                            "confidence": 0.6,
                            "matched_issue_axes": ["concern"],
                        },
                    ]
                }
            },
            graph=graph,
            issue_sketch=_Sketch(),
        )
        assert closure["files"] == ["src/io/files.js"]


def test_modification_closure_converges_to_one_grounded_source_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text("export function loadStrings() { return lines.filter(Boolean); }\n", encoding="utf-8")
        index = RepositoryIndex(repo="processing/p5.js", instance_id="single-closure", repo_root=root)
        closure = _build_modification_closure(
            ranked=[RankedLocation(path="src/io/files.js", score=100.0)],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": "src/io/files.js",
                            "role": "patch_target",
                            "confidence": 0.96,
                            "grounded": True,
                            "mechanism_verified": True,
                            "patch_mechanism": "filter(Boolean) drops empty lines.",
                        }
                    ]
                }
            },
            graph=TypedRepositoryGraph(index),
            issue_sketch=_Sketch(),
            code_contexts=[{"path": "src/io/files.js", "snippets": [{"text": "return lines.filter(Boolean);"}]}],
            flow_traces=[],
        )
        assert closure["complete"] is True
        assert closure["status"] == "complete"
        assert closure["files"] == ["src/io/files.js"]
        assert closure["coverage"]["required_coverage"] == 1.0
        assert closure["trace"][0]["decision"] == "complete_minimal_set"


def test_modification_closure_prefers_read_grounded_patch_target_over_rank_one_context() -> None:
    class EventSketch(_Sketch):
        flow_obligations = [
            {
                "flow_type": "ui_event_to_handler",
                "state": "legend hover state",
                "behavior": "onLeave callback",
                "required_relation": "CALL",
                "reason": "Trace the event into the legend handler.",
            },
            {
                "flow_type": "url_builder_or_route_flow",
                "state": "spurious route term",
                "behavior": "navigation",
                "required_relation": "CALL+DATA",
                "reason": "An uncorroborated sketch obligation must remain advisory.",
            },
        ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative in ("src/core/controller.js", "src/plugins/legend.js"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export function handleEvent() {}\n", encoding="utf-8")
        index = RepositoryIndex(repo="chartjs/Chart.js", instance_id="review-primary", repo_root=root)
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path="src/core/controller.js", score=100.0),
                RankedLocation(path="src/plugins/legend.js", score=88.0),
            ],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": "src/plugins/legend.js",
                            "role": "patch_target",
                            "confidence": 0.9,
                            "grounded": True,
                            "quote_supported": True,
                            "mechanism_verified": False,
                            "patch_mechanism": "Adjust legend hit testing so onLeave fires.",
                            "matched_issue_axes": ["concern", "call", "flow"],
                        }
                    ]
                }
            },
            graph=TypedRepositoryGraph(index),
            issue_sketch=EventSketch(),
            code_contexts=[
                {"path": "src/core/controller.js", "snippets": [{"text": "handleEvent();"}]},
                {"path": "src/plugins/legend.js", "snippets": [{"text": "handleEvent();"}]},
            ],
            flow_traces=[
                {
                    "flow_type": "ui_event_to_handler",
                    "term": "onLeave",
                    "candidate_target_paths": ["src/plugins/legend.js"],
                }
            ],
        )
        assert closure["files"] == ["src/plugins/legend.js"]
        assert closure["complete"] is True
        unsupported = next(item for item in closure["obligations"] if item["id"] == "flow:url_route")
        assert unsupported["required"] is False
        assert unsupported["support_status"] == "advisory_no_matching_flow_trace"


def test_closure_candidate_review_memory_preserves_earlier_grounded_observation() -> None:
    merged = _merge_closure_candidate_reviews(
        [
            {
                "llm_candidate_review": {
                    "status": "ok",
                    "candidates": [
                        {
                            "path": "src/plugins/legend.js",
                            "role": "patch_target",
                            "confidence": 0.9,
                            "grounded": True,
                            "mechanism_verified": False,
                            "matched_issue_axes": ["concern", "call", "flow"],
                        }
                    ],
                }
            },
            {"llm_candidate_review": {"status": "fallback", "candidates": []}},
        ]
    )
    assert merged["source"] == "cross_round_candidate_review_memory"
    assert merged["candidates"][0]["path"] == "src/plugins/legend.js"
    assert merged["candidates"][0]["grounded"] is True


def test_modification_closure_keeps_two_independent_grounded_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative in ("src/public_api.py", "src/serializer.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def target(value):\n    return value\n", encoding="utf-8")
        index = RepositoryIndex(repo="example/project", instance_id="multi-closure", repo_root=root)
        review_candidates = [
            {
                "path": "src/public_api.py",
                "role": "patch_target",
                "confidence": 0.95,
                "grounded": True,
                "mechanism_verified": True,
                "patch_mechanism": "The public API must accept and forward the option.",
            },
            {
                "path": "src/serializer.py",
                "role": "supporting_target",
                "confidence": 0.9,
                "grounded": True,
                "mechanism_verified": True,
                "patch_mechanism": "The serializer must consume the forwarded option.",
                "matched_issue_axes": ["call", "flow"],
            },
        ]
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path="src/public_api.py", score=100.0),
                RankedLocation(path="src/serializer.py", score=80.0),
            ],
            verifier={"llm_candidate_review": {"candidates": review_candidates}},
            graph=TypedRepositoryGraph(index),
            issue_sketch=_Sketch(),
            code_contexts=[{"path": item["path"], "snippets": [{"text": "def target(value):"}]} for item in review_candidates],
            flow_traces=[],
        )
        assert closure["complete"] is True
        assert closure["files"] == ["src/public_api.py", "src/serializer.py"]
        assert closure["trace"][0]["expanded"][0]["obligation_gain"] == ["review_target:src/serializer.py"]


def test_modification_closure_expands_missing_flow_then_ablates_duplicate_support(monkeypatch) -> None:
    class FlowSketch(_Sketch):
        flow_obligations = [
            {
                "flow_type": "parameter_or_config_flow",
                "state": "line option",
                "behavior": "downstream parser behavior",
                "required_relation": "PARAMETER",
                "reason": "The option must reach its consumer.",
            }
        ]

    monkeypatch.setenv("MYCODE_CLOSURE_EXPAND_PER_ROUND", "2")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative in ("src/public_api.py", "src/parser.py", "src/parser_alias.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def target(value):\n    return value\n", encoding="utf-8")
        index = RepositoryIndex(repo="example/project", instance_id="flow-closure", repo_root=root)
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path="src/public_api.py", score=100.0),
                RankedLocation(path="src/parser.py", score=75.0),
                RankedLocation(path="src/parser_alias.py", score=70.0),
            ],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": "src/public_api.py",
                            "role": "patch_target",
                            "confidence": 0.95,
                            "grounded": True,
                            "mechanism_verified": True,
                            "patch_mechanism": "The API exposes the option but does not forward it.",
                        }
                    ]
                }
            },
            graph=TypedRepositoryGraph(index),
            issue_sketch=FlowSketch(),
            code_contexts=[
                {"path": "src/public_api.py", "snippets": [{"text": "def target(value):"}]},
                {"path": "src/parser.py", "snippets": [{"text": "def target(value):"}]},
                {"path": "src/parser_alias.py", "snippets": [{"text": "def target(value):"}]},
            ],
            flow_traces=[
                {
                    "flow_type": "parameter_or_config_flow",
                    "term": "line option",
                    "candidate_target_paths": ["src/parser.py", "src/parser_alias.py"],
                }
            ],
        )
        assert closure["complete"] is True
        assert closure["files"] == ["src/public_api.py", "src/parser.py"]
        assert [item["path"] for item in closure["trace"][0]["expanded"]] == [
            "src/parser.py",
            "src/parser_alias.py",
        ]
        assert closure["trace"][0]["pruned"] == [
            {
                "path": "src/parser_alias.py",
                "reason": "necessity_ablation_no_required_coverage_loss",
                "previous_role": "supporting_target",
            }
        ]


def test_modification_closure_marks_unread_top_rank_as_incomplete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/guess.py"
        source.parent.mkdir(parents=True)
        source.write_text("def guess():\n    return None\n", encoding="utf-8")
        index = RepositoryIndex(repo="example/project", instance_id="incomplete-closure", repo_root=root)
        closure = _build_modification_closure(
            ranked=[RankedLocation(path="src/guess.py", score=100.0)],
            verifier={},
            graph=TypedRepositoryGraph(index),
            issue_sketch=_Sketch(),
            code_contexts=[],
            flow_traces=[],
        )
        assert closure["files"] == ["src/guess.py"]
        assert closure["status"] == "incomplete"
        assert closure["complete"] is False
        assert "root_patch_mechanism" in closure["coverage"]["missing_required"]


def test_generic_click_flow_does_not_complete_patch_mechanism() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text("export function loadStrings() {}\n", encoding="utf-8")
        index = RepositoryIndex(repo="processing/p5.js", instance_id="generic-flow", repo_root=root)
        closure = _build_modification_closure(
            ranked=[RankedLocation(path="src/io/files.js", score=100.0)],
            verifier={},
            graph=TypedRepositoryGraph(index),
            issue_sketch=_Sketch(),
            code_contexts=[{"path": "src/io/files.js", "snippets": [{"text": "loadStrings"}]}],
            flow_traces=[
                {
                    "flow_type": "state_selector_flow_chain",
                    "term": "click",
                    "candidate_target_paths": ["src/io/files.js"],
                }
            ],
        )
        assert closure["complete"] is False
        assert closure["status"] == "incomplete"
        assert "root_patch_mechanism" in closure["coverage"]["missing_required"]


def test_closure_rerank_promotes_verified_target_without_changing_recall_set() -> None:
    ranked = [
        RankedLocation(path="src/context.js", score=100.0),
        RankedLocation(path="src/target.js", score=90.0),
        RankedLocation(path="src/support.js", score=80.0),
    ]
    reranked, diagnostics = _rerank_with_modification_closure(
        ranked,
        {
            "complete": True,
            "candidates": [
                {
                    "path": "src/target.js",
                    "role": "patch_target",
                    "confidence": 1.0,
                        "read_verified": True,
                        "mechanism_verified": True,
                        "quote_supported": True,
                        "entity_supported": True,
                        "counterevidence": [],
                }
            ],
        },
        top_k=3,
    )
    assert reranked[0].path == "src/target.js"
    assert {item.path for item in reranked} == {item.path for item in ranked}
    assert diagnostics["applied"] is True


def test_partial_closure_promotes_only_verified_root_mechanism() -> None:
    ranked = [
        RankedLocation(path="src/context.js", score=100.0),
        RankedLocation(path="src/root.js", score=90.0),
        RankedLocation(path="src/flow_only.js", score=80.0),
    ]
    reranked, diagnostics = _rerank_with_modification_closure(
        ranked,
        {
            "complete": False,
            "candidates": [
                {
                    "path": "src/root.js",
                    "role": "patch_target",
                    "confidence": 0.9,
                    "read_verified": True,
                    "quote_supported": True,
                    "entity_supported": True,
                    "mechanism_verified": True,
                    "counterevidence": [],
                },
                {
                    "path": "src/flow_only.js",
                    "role": "patch_target",
                    "confidence": 0.99,
                    "read_verified": True,
                    "task_relevant_direct_flow": True,
                    "mechanism_verified": False,
                    "counterevidence": [],
                },
            ],
        },
        top_k=3,
    )
    assert [item.path for item in reranked] == ["src/root.js", "src/context.js", "src/flow_only.js"]
    assert diagnostics["strategy"] == "partial_verified_root_promotion"


def test_candidate_review_exact_entity_improves_function_grounding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text(
            "function helper() { return true; }\nexport function loadStrings(lines) { return lines.filter(Boolean); }\n",
            encoding="utf-8",
        )
        index = RepositoryIndex(repo="processing/p5.js", instance_id="unit", repo_root=root)
        ranked_files = [RankedLocation(path="src/io/files.js", score=100.0)]
        review = {
            "status": "ok",
            "candidates": [
                {
                    "path": "src/io/files.js",
                    "role": "patch_target",
                    "confidence": 0.95,
                    "entities": [{"kind": "function", "name": "loadStrings"}],
                }
            ],
        }
        _modules, functions = _rank_entities(
            index=index,
            ranked_files=ranked_files,
            entity_hits=[],
            flow_traces=[],
            top_k=5,
            candidate_review=review,
        )
        assert functions[0].name == "loadStrings"
        assert any("llm_review_exact_entity" in reason for reason in functions[0].reasons)


def test_structure_index_finds_javascript_prototype_assignment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text(
            "p5.prototype.loadStrings = function(...args) {\n"
            "  return args;\n"
            "};\n",
            encoding="utf-8",
        )
        index = RepositoryIndex(repo="processing/p5.js", instance_id="prototype", repo_root=root)
        matches = [
            entity
            for entity in index.entities
            if entity.path == "src/io/files.js" and entity.name == "loadStrings"
        ]
        assert len(matches) == 1
        assert matches[0].kind == "function"
        assert matches[0].start_line == 1


def test_structure_index_ignores_javascript_functions_inside_comments() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text(
            "/** Example:\n"
            " * function setup() { draw(); }\n"
            " */\n"
            "p5.prototype.loadStrings = function() { return []; };\n",
            encoding="utf-8",
        )
        index = RepositoryIndex(repo="processing/p5.js", instance_id="comments", repo_root=root)
        names = {entity.name for entity in index.entities}
        assert "loadStrings" in names
        assert "setup" not in names


def test_structure_snapshot_wins_over_mismatched_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_root = root / "repo"
        source = repo_root / "src/feature.js"
        source.parent.mkdir(parents=True)
        source.write_text("function staleCheckoutOnly() {}\n", encoding="utf-8")
        structure_path = root / "instance.json"
        structure_path.write_text(
            json.dumps(
                {
                    "base_commit": "benchmark-base",
                    "structure": {
                        "src": {
                            "feature.js": {
                                "text": "function benchmarkTarget() {}\n",
                                "functions": [],
                                "classes": [],
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        index = RepositoryIndex(
            repo="demo/repo",
            instance_id="snapshot-demo",
            repo_root=repo_root,
            structure_path=structure_path,
        )
        assert index.files["src/feature.js"] == "function benchmarkTarget() {}\n"
        names = {entity.name for entity in index.entities if entity.path == "src/feature.js"}
        assert "benchmarkTarget" in names
        assert "staleCheckoutOnly" not in names


def test_structure_snapshot_does_not_rescan_java_when_callables_exist(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        structure_path = root / "assertj.json"
        java_text = (
            "public class AtomicReferenceArrayAssert {\n"
            "  public AtomicReferenceArrayAssert containsExactly(Object... values) { return this; }\n"
            "}\n"
        )
        structure_path.write_text(
            json.dumps(
                {
                    "structure": {
                        "assertj-core": {
                            "src": {
                                "AtomicReferenceArrayAssert.java": {
                                    "text": java_text,
                                    "functions": [
                                        {"name": "containsExactly", "start_line": 2, "end_line": 2}
                                    ],
                                    "classes": [
                                        {
                                            "name": "AtomicReferenceArrayAssert",
                                            "start_line": 1,
                                            "end_line": 3,
                                            "methods": [],
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        def fail_if_called(_path: str, _text: str):
            raise AssertionError("structured Java source must not be reparsed")

        monkeypatch.setattr(structure_index, "_extract_source_entities", fail_if_called)
        index = RepositoryIndex(
            repo="assertj/assertj",
            instance_id="assertj__assertj-3321",
            repo_root=root / "missing-repo",
            structure_path=structure_path,
        )
        assert any(entity.name == "containsExactly" for entity in index.entities)


def test_structure_snapshot_reads_nested_class_methods() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        structure_path = root / "nested-method.json"
        structure_path.write_text(
            json.dumps(
                {
                    "structure": {
                        "src": {
                            "Service.java": {
                                "text": "class Service {\n  void execute() {}\n}\n",
                                "functions": [],
                                "classes": [
                                    {
                                        "name": "Service",
                                        "start_line": 1,
                                        "end_line": 3,
                                        "methods": [
                                            {"name": "execute", "start_line": 2, "end_line": 2}
                                        ],
                                    }
                                ],
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        index = RepositoryIndex(
            repo="demo/java",
            instance_id="nested-method",
            repo_root=root / "missing-repo",
            structure_path=structure_path,
        )
        methods = [entity for entity in index.entities if entity.kind == "method"]
        assert [(entity.name, entity.start_line) for entity in methods] == [("execute", 2)]


def test_java_source_fallback_handles_multiline_methods_without_false_calls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/Service.java"
        source.parent.mkdir(parents=True)
        source.write_text(
            "\n".join(
                [
                    "public class Service {",
                    "  public <T> T transform(",
                    "      T value,",
                    "      java.util.function.Function<T, T> mapper",
                    "  ) throws IllegalArgumentException {",
                    "    return mapper.apply(value);",
                    "  }",
                    "",
                    "  Service() {}",
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        index = RepositoryIndex(
            repo="demo/java",
            instance_id="java-fallback",
            repo_root=root,
            structure_path=root / "missing.json",
        )
        names = {entity.name for entity in index.entities}
        assert "Service" in names
        assert "transform" in names
        assert "apply" not in names
        constructor = next(
            entity
            for entity in index.entities
            if entity.kind == "method" and entity.name == "Service"
        )
        assert constructor.start_line == 9


def test_controller_accepts_wrapped_json_array() -> None:
    parsed = _parse_llm_decisions(
        {
            "content": (
                '[{"tool":"SearchAnchor","mode":"concern","reason":"find implementation",'
                '"queries":["loadStrings"],"preferred_edge_types":[],"confidence":0.8}]'
            )
        }
    )
    assert len(parsed) == 1
    assert parsed[0].tool == "SearchAnchor"
    assert parsed[0].source == "llm"


def test_controller_limits_llm_plan_to_two_actions() -> None:
    payload = [
        {
            "tool": "SearchAnchor",
            "mode": "concern",
            "reason": f"resolve gap {index}",
            "queries": [f"query {index}"],
        }
        for index in range(3)
    ]
    assert len(_parse_llm_decisions(json.dumps(payload))) == 2


def test_react_enforces_four_tool_coverage(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src/io/files.js"
        source.parent.mkdir(parents=True)
        source.write_text("export function loadStrings(lines) { return lines; }\n", encoding="utf-8")
        index = RepositoryIndex(repo="processing/p5.js", instance_id="unit", repo_root=root)
        graph = TypedRepositoryGraph(index)
        sample = NormalizedSample(
            instance_id="processing__p5.js-3068",
            repo="processing/p5.js",
            dataset="unit",
            issue_text="loadStrings should preserve empty lines",
            raw={},
            gold_files=["src/io/files.js"],
        )

        class Sketch(_Sketch):
            workflow = []
            concerns = ["loadStrings empty lines"]
            states = ["empty lines"]
            expected_effects = ["preserve lines"]
            entities = ["loadStrings"]
            seed_policy = []
            flow_obligations = []

        monkeypatch.setenv("MYCODE_REACT_ENFORCE_TOOL_COVERAGE", "1")
        result = run_react_tool_agent(
            sample=sample,
            evidence_result={},
            index=index,
            graph=graph,
            issue_sketch=Sketch(),
            queries=["loadStrings", "empty lines"],
            top_k=5,
            max_steps=4,
            planner_llm=lambda _prompt: {
                "content": '{"thought":"read again","tool":"ReadCode","mode":"read","queries":[],"stop":false}',
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )
        assert [step["tool"] for step in result["steps"]] == [
            "SearchAnchor",
            "NavigateCode",
            "TraceFlow",
            "ReadCode",
        ]
        assert result["usage_summary"]["total_tokens"] == 48
        assert sum(step["token_usage"]["total_tokens"] for step in result["steps"]) == 48


def test_precision_rerank_promotes_grounded_mechanism_over_generic_rank_one() -> None:
    ranked = [
        RankedLocation(path="src/state/selectors.js", score=100.0),
        RankedLocation(path="src/context.js", score=90.0),
        RankedLocation(path="src/navigation.js", score=80.0),
        RankedLocation(path="src/ownership.js", score=70.0),
    ]
    reranked, diagnostics = _precision_rerank_locations(
        ranked=ranked,
        round_rankings=[ranked, ranked],
        issue_text="Fix ownership transfer when an administrator changes the plan owner.",
        review={
            "candidates": [
                {
                    "path": "src/ownership.js",
                    "role": "patch_target",
                    "confidence": 0.96,
                    "grounded": True,
                    "quote_supported": True,
                    "entity_supported": True,
                    "mechanism_verified": True,
                    "patch_mechanism": "The ownership handler does not dispatch the transfer.",
                    "direct_flow_supported": True,
                },
                {
                    "path": "src/state/selectors.js",
                    "role": "navigation_only",
                    "confidence": 0.9,
                    "grounded": False,
                    "mechanism_verified": False,
                },
            ]
        },
        code_contexts=[
            {"path": "src/ownership.js", "snippets": [{"text": "transferOwnership(owner);"}]}
        ],
        top_k=4,
    )
    assert reranked[0].path == "src/ownership.js"
    assert {item.path for item in reranked} == {item.path for item in ranked}
    assert diagnostics["strategy"] == "verified_head_selector_tail_preserving"
    assert diagnostics["top_before"][0] == "src/state/selectors.js"
    assert [item.path for item in reranked[1:]] == [item.path for item in ranked if item.path != "src/ownership.js"]


def test_precision_rerank_does_not_promote_unverified_implementation_sibling() -> None:
    ranked = [
        RankedLocation(
            path="client/state/posts/actions.js",
            score=100.0,
            score_components={"call_score": 3.0},
            belief={"read_verified": True},
        ),
        RankedLocation(
            path="client/state/ui/editor/actions.js",
            score=99.0,
            score_components={"path_score": 20.0, "domain_path_probe": 8.0},
            belief={"read_verified": True},
        ),
    ]
    reranked, diagnostics = _precision_rerank_locations(
        ranked=ranked,
        round_rankings=[ranked, ranked],
        issue_text="Saving a post must update application state through its action.",
        review={
            "candidates": [
                {
                    "path": "client/state/ui/editor/actions.js",
                    "role": "patch_target",
                    "confidence": 0.95,
                    "grounded": False,
                    "quote_supported": False,
                    "entity_supported": False,
                    "mechanism_verified": False,
                }
            ]
        },
        code_contexts=[
            {
                "path": "client/state/ui/editor/actions.js",
                "snippets": [{"text": "export const updateEditor = () => ({ type: UPDATE_EDITOR });"}],
            }
        ],
        top_k=2,
    )

    assert [item.path for item in reranked] == [item.path for item in ranked]
    assert diagnostics["head_replaced"] is False
    assert diagnostics["eligible_heads"] == []


def test_precision_rerank_accepts_quote_entity_and_direct_flow_as_equivalent_mechanism() -> None:
    ranked = [
        RankedLocation(path="src/generic.py", score=100.0),
        RankedLocation(
            path="src/handler.py",
            score=90.0,
            score_components={"call_score": 4.0},
            belief={"read_verified": True},
        ),
    ]
    reranked, diagnostics = _precision_rerank_locations(
        ranked=ranked,
        round_rankings=[ranked, ranked],
        issue_text="The handler fails to dispatch the update.",
        review={
            "candidates": [
                {
                    "path": "src/handler.py",
                    "role": "patch_target",
                    "confidence": 0.9,
                    "grounded": True,
                    "quote_supported": True,
                    "entity_supported": True,
                    "supported_entities": ["dispatch_update"],
                    "direct_flow_supported": True,
                    "mechanism_verified": False,
                    "patch_mechanism": "The handler forwards the old value to dispatch_update instead of the new value.",
                }
            ]
        },
        code_contexts=[
            {"path": "src/handler.py", "snippets": [{"text": "dispatch_update(value)"}]}
        ],
        top_k=2,
    )

    assert reranked[0].path == "src/handler.py"
    assert diagnostics["head_replaced"] is True
    assert diagnostics["selected_head_evidence"]["equivalent_mechanism"] is True


def test_source_first_classifies_common_compiled_bundle_suffixes() -> None:
    assert _path_role("lib/library.umd.js") == "generated_or_lockfile"
    assert _path_role("lib/marked.esm.js") == "generated_or_lockfile"
    assert _path_role("client/lib/runtime.js") == "implementation"
    assert _path_role("public/app.min.css") == "generated_or_lockfile"


def test_cross_round_frontier_preserves_head_and_recovers_read_tail_candidate() -> None:
    selected = [
        RankedLocation(path=f"src/selected_{index}.py", score=100.0 - index)
        for index in range(1, 7)
    ]
    recovered = RankedLocation(
        path="src/ownership.py",
        score=88.0,
        score_components={"path_score": 12.0},
        belief={"read_verified": True, "supporting_axes": ["vertical_program"]},
    )
    later = [selected[0], recovered, *selected[1:5]]

    fused, diagnostics = _cross_round_candidate_frontier(
        selected=selected,
        round_rankings=[selected, later],
        issue_text="Fix the ownership transfer handler.",
        review={
            "candidates": [
                {
                    "path": "src/ownership.py",
                    "role": "patch_target",
                    "confidence": 0.9,
                    "grounded": True,
                    "mechanism_verified": True,
                }
            ]
        },
        code_contexts=[
            {"path": "src/ownership.py", "snippets": [{"text": "transfer_ownership()"}]}
        ],
        top_k=6,
    )

    assert [item.path for item in fused[:5]] == [item.path for item in selected[:5]]
    assert fused[5].path == "src/ownership.py"
    assert diagnostics["replacements"][0]["removed"] == "src/selected_6.py"


def test_cross_round_frontier_keeps_top_six_and_uses_tail_for_verified_recovery(monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_CROSS_ROUND_PROTECTED_PREFIX", "6")
    monkeypatch.setenv("MYCODE_CROSS_ROUND_REPLACEMENT_MARGIN", "0")
    selected = [
        RankedLocation(path=f"src/selected_{index}.py", score=100.0 - index)
        for index in range(1, 16)
    ]
    recovered = RankedLocation(
        path="src/recovered.py",
        score=90.0,
        score_components={"flow_score": 5.0},
        belief={"read_verified": True},
    )
    fused, diagnostics = _cross_round_candidate_frontier(
        selected=selected,
        round_rankings=[selected, [recovered, *selected[:14]]],
        issue_text="Fix the recovered state transition.",
        review={
            "candidates": [
                {
                    "path": "src/recovered.py",
                    "role": "patch_target",
                    "confidence": 0.95,
                    "grounded": True,
                    "quote_supported": True,
                    "entity_supported": True,
                    "supported_entities": ["transition"],
                    "direct_flow_supported": True,
                    "mechanism_verified": True,
                }
            ]
        },
        code_contexts=[
            {"path": "src/recovered.py", "snippets": [{"text": "def transition(): pass"}]}
        ],
        top_k=15,
    )
    assert [item.path for item in fused[:6]] == [item.path for item in selected[:6]]
    assert "src/recovered.py" in [item.path for item in fused[6:]]
    assert diagnostics["protected_prefix"] == 6


def test_cross_round_frontier_locks_only_verified_candidates(monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_CROSS_ROUND_LOCKED_HEAD", "2")
    selected = [
        RankedLocation(path="src/weak_seed.py", score=100.0),
        RankedLocation(path="src/verified_a.py", score=95.0, score_components={"flow_score": 2.0}),
        RankedLocation(path="src/verified_b.py", score=90.0, score_components={"call_score": 2.0}),
        RankedLocation(path="src/tail.py", score=85.0),
    ]
    review_candidates_data = []
    contexts = []
    for path in ("src/verified_a.py", "src/verified_b.py"):
        review_candidates_data.append(
            {
                "path": path,
                "grounded": True,
                "quote_supported": True,
                "entity_supported": True,
                "mechanism_verified": True,
            }
        )
        contexts.append({"path": path, "snippets": [{"text": "def verified(): pass"}]})
    fused, diagnostics = _cross_round_candidate_frontier(
        selected=selected,
        round_rankings=[selected, selected],
        issue_text="Fix the verified behavior.",
        review={"candidates": review_candidates_data},
        code_contexts=contexts,
        top_k=4,
    )
    assert [item["path"] for item in diagnostics["adaptive_locks"]] == [
        "src/verified_a.py",
        "src/verified_b.py",
    ]
    assert "src/weak_seed.py" not in {item["path"] for item in diagnostics["adaptive_locks"]}
    assert [item.path for item in fused] == [item.path for item in selected]


def test_representative_failure_trajectories_recover_target_into_top_six(monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_CROSS_ROUND_LOCKED_HEAD", "2")
    monkeypatch.setenv("MYCODE_CROSS_ROUND_MAX_REPLACEMENTS", "4")
    monkeypatch.setenv("MYCODE_CROSS_ROUND_REPLACEMENT_MARGIN", "0")
    fixture = Path(__file__).parent / "fixtures" / "front_rank_failure_cases.json"
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        selected = [
            RankedLocation(path=path, score=100.0 - rank)
            for rank, path in enumerate(case["selected"])
        ]
        target = RankedLocation(
            path=case["target"],
            score=98.0,
            score_components={"flow_score": 8.0},
            belief={"read_verified": True},
        )
        review = {
            "candidates": [
                {
                    "path": case["target"],
                    "role": "patch_target",
                    "confidence": 0.95,
                    "grounded": True,
                    "quote_supported": True,
                    "entity_supported": True,
                    "direct_flow_supported": True,
                    "mechanism_verified": True,
                }
            ]
        }
        contexts = [{"path": case["target"], "snippets": [{"text": "verified source mechanism"}]}]
        fused, _diagnostics = _cross_round_candidate_frontier(
            selected=selected,
            round_rankings=[selected, [target, *selected[:5]]],
            issue_text=case["issue"],
            review=review,
            code_contexts=contexts,
            top_k=6,
        )
        reranked, _head = _precision_rerank_locations(
            ranked=fused,
            round_rankings=[selected, [target, *selected[:5]]],
            issue_text=case["issue"],
            review=review,
            code_contexts=contexts,
            top_k=6,
        )
        assert case["target"] in [item.path for item in reranked[:6]], case["instance_id"]


def test_closure_inherits_read_verification_from_candidate_belief() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        relative = "src/io/files.py"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def load_strings(): return []\n", encoding="utf-8")
        index = RepositoryIndex(repo="example/io", instance_id="belief-read", repo_root=root)
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(
                    path=relative,
                    score=100.0,
                    score_components={"symbol_score": 20.0},
                    belief={"read_verified": True},
                )
            ],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": relative,
                            "role": "patch_target",
                            "confidence": 0.95,
                            "grounded": True,
                            "quote_supported": True,
                            "mechanism_verified": True,
                        }
                    ]
                }
            },
            graph=TypedRepositoryGraph(index),
            issue_sketch=_Sketch(),
            code_contexts=[],
            flow_traces=[],
        )

        assert closure["complete"] is True
        assert closure["candidates"][0]["read_verified"] is True
        assert closure["candidates"][0]["read_evidence_sources"] == ["candidate_belief"]


def test_partial_closure_keeps_complementary_feature_responsibilities() -> None:
    class FeatureSketch(_Sketch):
        task_type = "feature_request"
        concerns = ["profile editing workflow"]
        expected_effects = ["update the profile through the user interface"]
        architectural_queries = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ui_path = "src/components/profile-editor.jsx"
        state_path = "src/state/profile/actions.js"
        for relative in (ui_path, state_path):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export function updateProfile() {}\n", encoding="utf-8")
        index = RepositoryIndex(repo="example/profile", instance_id="partial-set", repo_root=root)
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path=ui_path, score=100.0),
                RankedLocation(path=state_path, score=90.0),
            ],
            verifier={
                "llm_candidate_review": {
                    "candidates": [
                        {
                            "path": ui_path,
                            "role": "patch_target",
                            "confidence": 0.9,
                            "grounded": True,
                            "mechanism_verified": False,
                        },
                        {
                            "path": state_path,
                            "role": "supporting_target",
                            "confidence": 0.85,
                            "grounded": True,
                            "entity_supported": True,
                            "mechanism_verified": False,
                        },
                    ]
                }
            },
            graph=TypedRepositoryGraph(index),
            issue_sketch=FeatureSketch(),
            code_contexts=[
                {"path": ui_path, "snippets": [{"text": "updateProfile();"}]},
                {"path": state_path, "snippets": [{"text": "dispatch(updateProfile());"}]},
            ],
            flow_traces=[],
        )
        assert closure["complete"] is False
        assert closure["status"] == "partial"
        assert set(closure["files"]) == {ui_path, state_path}
        assert closure["partial_fallback"]["applied"] is True
        assert closure["partial_fallback"]["bounds"]["minimum"] == 2
