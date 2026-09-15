from types import SimpleNamespace

import pytest

from mycode.dynamic_retrieval.search_agent import (
    RankedLocation, _backfill_review_context, _path_role, _source_first_multiplier,
    _flow_entity_support, _entity_issue_semantic_bonus, _rank_entities,
)
from mycode.dynamic_retrieval.package_navigation import navigation_only_file
from mycode.dynamic_retrieval.candidate_reviewer import _validate


@pytest.mark.parametrize("path", ["website/blog/2021-05-09.md", "CONTRIBUTING.md", "notes/release.rst"])
def test_documents_are_navigation_not_implementation(path):
    assert _path_role(path) == "reproduction_or_example"
    assert navigation_only_file(path, "Formatting a function fails")
    assert not navigation_only_file(path, "Fix the documentation typo")
    assert _source_first_multiplier(path, "Formatting a function fails")[0] < 1


def test_actual_lib_source_is_not_documentation():
    assert _path_role("lib/NormalModuleFactory.js") == "implementation"


def test_missing_context_gets_bounded_entity_read_without_mutation():
    rows = [RankedLocation(path=f"src/{i}.py", score=1, entities=[{"name": "target"}]) for i in range(3)]
    index = SimpleNamespace(files={r.path: "\n" * 50 + "def target():\n    return 1" for r in rows})
    old = [{"path": r.path, "snippets": []} for r in rows]
    new, diag = _backfill_review_context(index, rows, old, candidate_limit=3, context_limit=3)
    assert len(diag["filled"]) == 2
    assert "def target" in new[0]["snippets"][0]["text"]
    assert not new[2]["snippets"]
    assert all(not x["snippets"] for x in old)
    off, diag = _backfill_review_context(index, rows, old, candidate_limit=3, context_limit=3, limit=0)
    assert off == old and not diag["attempted"]


def test_backfill_never_exceeds_context_capacity_or_fabricates_source():
    row = RankedLocation(path="src/missing.py", score=1, entities=[{"name": "target"}])
    old = [{"path": "src/existing.py", "snippets": [{"text": "source"}]}]
    out, diag = _backfill_review_context(SimpleNamespace(files={}), [row], old,
                                        candidate_limit=1, context_limit=1)
    assert out == old and not diag["attempted"]


@pytest.mark.parametrize("codes,expected", [
    (["no_source"], "insufficient_evidence"),
    (["no_source", "no_entity"], "insufficient_evidence"),
    (["no_source", "wrong operation"], "unlikely"),
    (["artifact"], "unlikely"),
])
def test_missing_evidence_is_distinct_from_rejection(codes, expected):
    path = "src/a.py"
    result = _validate({"candidates": [{"path": path, "role": "unlikely", "counterevidence": codes}]},
                       allowed_paths={path}, grounded_paths=set(), context_text_by_path={},
                       direct_flow_paths=set(), snippets_by_path={}, entities_by_path={}, flow_paths_by_id={})
    decision = result["candidates"][0]
    assert decision["role"] == expected
    assert not decision["mechanism_verified"]


def test_duplicate_flow_observations_do_not_inflate_entity_support():
    flow = {"confidence": 0.8, "locations": [
        {"path": "src/a.py", "kind": "function", "name": "transform"}
    ]}
    assert _flow_entity_support([flow] * 20) == _flow_entity_support([flow])
    assert _flow_entity_support([dict(flow, reason="new query"), flow]) == _flow_entity_support([flow])
    weaker = dict(flow, confidence=0.2)
    assert _flow_entity_support([weaker, flow]) == _flow_entity_support([flow, weaker])
    other = {"confidence": 0.8, "locations": [
        {"path": "src/b.py", "kind": "function", "name": "parse"}
    ]}
    assert len(_flow_entity_support([flow, other])) == 2


def test_language_boilerplate_is_not_semantic_mechanism_evidence():
    entity = SimpleNamespace(name="helper", text="export default function helper() { return transform(); }")
    assert _entity_issue_semantic_bonus(entity, ["export default function return should"])[0] == 0
    assert _entity_issue_semantic_bonus(entity, ["transform"])[0] > 0


def test_review_boost_requires_source_quote_and_supported_entity():
    entity = SimpleNamespace(path="src/a.py", kind="function", name="transform", text="",
                             start_line=1, end_line=2)
    kwargs = dict(index=SimpleNamespace(entities=[entity]),
                  ranked_files=[RankedLocation(path=entity.path, score=100)],
                  entity_hits=[], flow_traces=[], top_k=15)
    hint = {"kind": "function", "name": "transform"}
    review = dict(path=entity.path, role="patch_target", confidence=1, entities=[hint])
    def score(candidate):
        return _rank_entities(**kwargs, candidate_review={"candidates": [candidate]})[1][0].score
    baseline = score(review)
    assert score(dict(review, quote_supported=True)) == baseline
    assert score(dict(review, supported_entities=[hint])) == baseline
    assert score(dict(review, quote_supported=True, supported_entities=[hint])) > baseline
