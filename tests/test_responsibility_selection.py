from copy import deepcopy
from types import SimpleNamespace

import pytest

from mycode.dynamic_retrieval.responsibility import responsibility_evidence
from mycode.dynamic_retrieval.search_agent import RankedLocation, _precision_rerank_locations
from mycode.dynamic_retrieval.seed_frontier import restore_seed_frontier


def owner(path="src/owner.js", **updates):
    return dict(
        {"path": path, "role": "patch_target", "grounded": True,
         "quote_supported": True, "entity_supported": True,
         "direct_flow_supported": True, "mechanism_verified": True,
         "patch_mechanism": "The assignment writes the old value into the saved state."},
        **updates,
    )


@pytest.mark.parametrize("field", [
    "grounded", "quote_supported", "entity_supported", "direct_flow_supported", "patch_mechanism",
])
def test_responsibility_requires_each_grounded_evidence(field):
    decision = owner(**{field: None})
    result = responsibility_evidence(decision)
    assert not result["supported"]
    assert field in result["missing"]


@pytest.mark.parametrize("role", ["navigation_only", "supporting_target", "unlikely"])
def test_related_code_does_not_establish_ownership(role):
    assert not responsibility_evidence(owner(role=role))["supported"]


def test_counterevidence_and_missing_analysis_are_different():
    assert responsibility_evidence(owner(counterevidence=["no_flow"]))["supported"]
    assert not responsibility_evidence(owner(counterevidence=["wrong state writer"]))["supported"]


def select(review, monkeypatch, *, strict=True):
    monkeypatch.setenv("MYCODE_HEAD_REQUIRE_RESPONSIBILITY", "1" if strict else "0")
    ranked = [RankedLocation(path=p, score=10-i, belief={"read_verified": True},
                             score_components={"call_score": 50.0})
              for i, p in enumerate(["src/seed.js", "src/context.js", "src/owner.js", "src/tail.js"])]
    original = deepcopy(ranked)
    result, report = _precision_rerank_locations(
        ranked=ranked, round_rankings=[ranked], issue_text="The saved state uses the old value.",
        review={"candidates": review}, code_contexts=[], top_k=4,
    )
    assert ranked == original
    assert {x.path for x in result} == {x.path for x in ranked}
    assert [x.path for x in result[1:]] == [x.path for x in ranked if x.path != result[0].path]
    return result, report


def test_graph_score_and_quote_cannot_replace_head_without_causal_account(monkeypatch):
    decision = owner(mechanism_verified=False, patch_mechanism="")
    result, report = select([decision], monkeypatch)
    assert result[0].path == "src/seed.js"
    assert report["responsibility_required"]
    assert "patch_mechanism" in report["responsibility_checks"][2]["missing"]
    legacy, _ = select([decision], monkeypatch, strict=False)
    assert legacy[0].path == "src/owner.js"


def test_real_owner_can_replace_weak_seed_without_reordering_tail(monkeypatch):
    result, report = select([owner()], monkeypatch)
    assert result[0].path == "src/owner.js"
    assert report["head_comparison"]["reason"] == "responsibility_challenger"


def test_verified_incumbent_respects_replacement_margin(monkeypatch):
    monkeypatch.setenv("MYCODE_HEAD_REPLACEMENT_MARGIN", "1000")
    result, _ = select([owner("src/seed.js"), owner()], monkeypatch)
    assert result[0].path == "src/seed.js"


def test_seed_guard_preserves_verified_top6_owner_and_recall(monkeypatch):
    monkeypatch.setenv("MYCODE_SEED_RESPONSIBILITY_GUARD", "1")
    paths = ["src/context.js", "src/owner.js", "src/tail.js"]
    rows = [RankedLocation(path=p, score=10-i) for i, p in enumerate(paths)]
    original = deepcopy(rows)
    seeds = [RankedLocation(path="src/seed.js", score=20)]
    index = SimpleNamespace(files={p: "source" for p in paths + ["src/seed.js"]})
    result, report = restore_seed_frontier(rows, seeds, index=index,
                                           review={"candidates": [owner()]}, top_k=4)
    assert [x.path for x in result] == ["src/owner.js", "src/seed.js", "src/context.js", "src/tail.js"]
    assert report["protected_responsibility_paths"] == ["src/owner.js"]
    assert rows == original
    monkeypatch.setenv("MYCODE_SEED_RESPONSIBILITY_GUARD", "0")
    legacy, _ = restore_seed_frontier(rows, seeds, index=index,
                                      review={"candidates": [owner()]}, top_k=4)
    assert legacy[0].path == "src/seed.js"


def test_navigation_seed_is_not_removed_without_counterevidence(monkeypatch):
    monkeypatch.setenv("MYCODE_SEED_RESPONSIBILITY_GUARD", "1")
    rows = [RankedLocation(path="src/owner.js", score=1)]
    seeds = [RankedLocation(path="src/seed.js", score=2)]
    index = SimpleNamespace(files={p.path: "source" for p in rows + seeds})
    result, report = restore_seed_frontier(rows, seeds, index=index,
        review={"candidates": [owner(role="navigation_only")]}, top_k=2)
    assert result[0].path == "src/seed.js"
    assert report["protected_responsibility_paths"] == []


@pytest.mark.parametrize("issue,expected", [
    ("Fix the documentation typo.", "docs/usage.md"),
    ("The saved state uses an old value.", "src/owner.js"),
])
def test_source_fallback_respects_explicit_documentation_task(monkeypatch, issue, expected):
    monkeypatch.setenv("MYCODE_HEAD_REQUIRE_RESPONSIBILITY", "1")
    ranked = [
        RankedLocation(path="docs/usage.md", score=10),
        RankedLocation(path="src/owner.js", score=9, belief={"read_verified": True},
                       score_components={"call_score": 50.0}),
    ]
    result, report = _precision_rerank_locations(
        ranked=ranked, round_rankings=[ranked], issue_text=issue,
        review={"candidates": [owner(mechanism_verified=False, patch_mechanism="")]},
        code_contexts=[], top_k=2,
    )
    assert result[0].path == expected
    assert report["incumbent_blocked"] == (expected == "src/owner.js")
