from types import SimpleNamespace

from mycode.dynamic_retrieval.react_agent import _requested_read_paths, _run_tool
from mycode.dynamic_retrieval.search_agent import _verified_frontier_order
from mycode.dynamic_retrieval.tools import DynamicToolObservation


def test_explicit_paths_exact_scoped_and_safe():
    index = SimpleNamespace(files={"src/a.ts": {}, "src/b.ts": {}})
    paths, rejected = _requested_read_paths(
        ["read `./src/a.ts`, src/a.ts and src\\b.ts", "../secret.ts /src/a.ts missing.ts"], index
    )
    assert paths == ["src/a.ts", "src/b.ts"]
    assert {item["reason"] for item in rejected} == {"outside_repository", "not_in_tool_index"}


def test_read_priority_switch_and_budget(monkeypatch):
    from mycode.dynamic_retrieval import react_agent
    class Reader:
        def __init__(self, index):
            pass
        def run(self, paths, queries, limit):
            return DynamicToolObservation(tool="ReadCode", action="read", status="ok", seed_files=paths)
    monkeypatch.setattr(react_agent, "ReadCodeTool", Reader)
    candidates = [f"src/{i}.ts" for i in range(25)]
    arguments = dict(move={"tool": "ReadCode", "queries": ["src/owner.ts"]},
                     sample=None, evidence_result={}, index=SimpleNamespace(files={"src/owner.ts": {}}),
                     graph=None, issue_sketch=SimpleNamespace(), queries=[], candidate_paths=candidates, top_k=15)
    monkeypatch.setattr(react_agent, "sketch_query_terms", lambda _: [])
    old = _run_tool(**arguments)
    assert old.seed_files == candidates[:18]
    monkeypatch.setenv("MYCODE_READ_EXPLICIT_PATHS", "1")
    new = _run_tool(**arguments)
    assert new.seed_files == ["src/owner.ts"] + candidates[:17]
    assert len(new.seed_files) == 18
    assert new.metadata["read_execution"]["requested_index_paths"] == ["src/owner.ts"]


def row(score, rank, eligible=False):
    return (score, rank, None, {"head_eligible": eligible})


def test_frontier_verified_promotion_and_stability():
    rows = [row(2, 1), row(3, 2), row(10, 3, True), row(99, 4)]
    result = _verified_frontier_order(rows, 3)
    assert [r[1] for r in result] == [3, 1, 2, 4]
    assert sorted(r[1] for r in result) == [1, 2, 3, 4]


def test_frontier_ties_and_insufficient_margin_do_not_move():
    rows = [row(10, 1), row(10, 2, True), row(12, 3, True)]
    assert _verified_frontier_order(rows, 3) == rows


def test_frontier_does_not_promote_beyond_fifteen():
    rows = [row(0, i) for i in range(1, 16)] + [row(100, 16, True)]
    assert _verified_frontier_order(rows, 3) == rows
