from __future__ import annotations

from mycode.dynamic_retrieval.search_agent import (
    DynamicSearchRound,
    RankedEntity,
    RankedLocation,
    _save_dynamic_localization_checkpoint,
    clear_dynamic_localization_checkpoint,
    take_dynamic_localization_checkpoint,
)
from mycode.schemas.evidence import NormalizedSample


class _IndexStub:
    def metadata(self):
        return {"ready": True, "file_count": 1}


class _IssueSketchStub:
    def to_dict(self):
        return {"concerns": ["timeout checkpoint"]}


def test_completed_round_checkpoint_is_evaluable_and_consumed_once() -> None:
    sample = NormalizedSample(
        instance_id="repo__issue-1",
        repo="owner/repo",
        dataset="test",
        issue_text="find the implementation",
        raw={},
        gold_files=["src/target.py"],
    )
    ranked_file = RankedLocation(
        path="src/target.py",
        score=9.0,
        belief={"read_verified": True, "supporting_axes": ["vertical_program"]},
    )
    search_round = DynamicSearchRound(
        round_no=1,
        input_queries=["target"],
        agent_actions=[],
        frontier_state={},
        seed_files=["src/target.py"],
        file_hits=[],
        entity_hits=[],
        graph_hits=[],
        graph_summary={},
        code_contexts=[{"path": "src/target.py", "snippet": "def target(): pass"}],
        flow_traces=[],
        verifier={},
        agent_observation={},
        ranked_locations=[ranked_file],
        ranked_modules=[RankedEntity("src/target.py", "src/target.py", "module", "target", 9.0)],
        ranked_functions=[RankedEntity("src/target.py::target", "src/target.py", "function", "target", 8.0)],
        next_queries=[],
        evaluation={"file": {"acc@1": 1.0}},
        stop_decision={"confidence": {"confidence": 0.8}, "stop": False},
    )

    clear_dynamic_localization_checkpoint(sample.instance_id)
    _save_dynamic_localization_checkpoint(
        sample=sample,
        index=_IndexStub(),
        issue_sketch=_IssueSketchStub(),
        queries=["target"],
        search_trace=[{"step": "round_1"}],
        max_rounds=3,
        react_max_steps=4,
        graph_scope={"selected": 1},
        phase_timings=[{"phase": "round_1", "elapsed_seconds": 1.0}],
        react_agent={},
        rounds=[search_round],
        best_round=search_round,
        checkpoint_updates=[{"round_no": 1, "selected": True}],
    )

    checkpoint = take_dynamic_localization_checkpoint(sample.instance_id)
    assert checkpoint is not None
    assert checkpoint["status"] == "partial"
    assert checkpoint["termination"]["completed_rounds"] == 1
    assert checkpoint["ranked_locations"][0]["path"] == "src/target.py"
    assert checkpoint["ranked_modules"][0]["id"] == "src/target.py"
    assert checkpoint["ranked_functions"][0]["id"] == "src/target.py::target"
    assert checkpoint["final_patch_set"] == []
    assert take_dynamic_localization_checkpoint(sample.instance_id) is None


def test_pipeline_evaluates_checkpoint_after_timeout(monkeypatch) -> None:
    from mycode.agent import pipeline

    sample = NormalizedSample(
        instance_id="repo__issue-2",
        repo="owner/repo",
        dataset="test",
        issue_text="find the implementation",
        raw={},
        gold_files=["src/target.py"],
    )
    checkpoint = {
        "status": "partial",
        "dynamic_rounds": [{"round_no": 1}],
        "best_round_selection": {"selected_round": 1},
        "ranked_locations": [{"path": "src/target.py", "score": 9.0}],
        "ranked_modules": [],
        "ranked_functions": [],
        "final_patch_set": [],
    }

    class _ReadyIndex:
        ready = True
        files = {"src/target.py": object()}
        entities = []

        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(pipeline, "run_evidence_understanding", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pipeline, "find_repo_structure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "RepositoryIndex", _ReadyIndex)
    monkeypatch.setattr(pipeline, "clear_dynamic_localization_checkpoint", lambda _instance_id: None)
    monkeypatch.setattr(
        pipeline,
        "dynamic_localize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("budget exhausted")),
    )
    monkeypatch.setattr(
        pipeline,
        "take_dynamic_localization_checkpoint",
        lambda _instance_id: checkpoint.copy(),
    )
    monkeypatch.setattr(
        pipeline,
        "evaluate_three_level_ranking_with_applicability",
        lambda localization, *_args: {
            "file": {"acc@1": float(localization["ranked_locations"][0]["path"] == "src/target.py")},
            "module": {},
            "function": {},
        },
    )

    result = pipeline.run_localization_pipeline(sample, structure_only=True)

    assert result["status"] == "partial"
    assert result["evaluation"]["acc@1"] == 1.0
    assert result["evaluation_3level"]["file"]["acc@1"] == 1.0
    assert result["localization"]["termination"]["reason"] == "sample_timeout_checkpoint_recovery"
