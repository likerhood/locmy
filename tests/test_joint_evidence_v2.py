from types import SimpleNamespace

from mycode.dynamic_retrieval.source_windows import focused_windows
from mycode.dynamic_retrieval.search_agent import _joint_round_consensus, _closure_context_map, _reread_function_ledger
from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex


def test_windows_prefer_implementation_over_imports():
    text = "import { placeholder } from 'ui';\n" + "// filler\n" * 20 + "const result = decodeEntities(placeholder);\n"
    windows = focused_windows(text, ["read placeholder decodeEntities implementation"], limit=1)
    assert windows[0]["start_line"] > 10
    assert "decodeEntities" in windows[0]["text"]


def test_context_union_preserves_distinct_windows_and_entities(monkeypatch):
    monkeypatch.setenv("MYCODE_JOINT_EVIDENCE_V2", "1")
    a = {"path": "a", "snippets": [{"start_line": 1}], "entities": [{"name": "one"}]}
    b = {"path": "a", "snippets": [{"start_line": 20}], "entities": [{"name": "two"}]}
    merged = _closure_context_map([a, b, a])["a"]
    assert len(merged["snippets"]) == len(merged["entities"]) == 2
    assert len(a["snippets"]) == 1


def test_recurrent_source_observed_candidate_recovers_without_gold():
    a, b, c = [SimpleNamespace(path=p) for p in "abc"]
    result, log = _joint_round_consensus([a, b], [[a, b], [c, a], [c, a]],
        key=lambda i: i.path, contexts={"c": {"snippets": [{"text": "real source"}]}}, top_k=2)
    assert [i.path for i in result] == ["a", "c"]
    assert log["recovered"] == ["c"]


def test_unobserved_challenger_is_not_added():
    a, c = [SimpleNamespace(path=p) for p in "ac"]
    result, _ = _joint_round_consensus([a], [[c], [c]], key=lambda i: i.path, contexts={}, top_k=1)
    assert result == [a]


def test_function_source_range_and_owning_path_are_required():
    a = SimpleNamespace(id="a::change", path="a", start_line=20, end_line=30)
    b = SimpleNamespace(id="b::change", path="b", start_line=20, end_line=30)
    result, _ = _joint_round_consensus([a], [[b], [b]], key=lambda i: i.id,
        contexts={"b": {"snippets": [{"start_line": 1, "end_line": 8}]}}, top_k=1)
    assert result == [a]


def test_verified_file_head_survives_frequent_grounded_counterevidence():
    owner = SimpleNamespace(path="owner", belief={"precision_rerank": {
        "grounded": True, "mechanism_verified": True, "head_eligible": True}})
    noise = SimpleNamespace(path="noise", belief={"precision_rerank": {"counterevidence_grounded": True}})
    result, log = _joint_round_consensus([owner, noise], [[noise, owner]] * 3,
        key=lambda i: i.path, contexts={}, top_k=2)
    assert result == [owner, noise]
    assert log["evidence_pinned"] == ["owner"]


def test_function_consensus_does_not_inherit_file_checkpoint_vote():
    target = SimpleNamespace(id="a::target", path="a", start_line=1, end_line=5)
    generic = SimpleNamespace(id="a::generic", path="a", start_line=10, end_line=20)
    filler = [SimpleNamespace(id=f"a::{i}", path="a", start_line=30, end_line=40) for i in range(4)]
    selected = [generic] + filler + [target]
    rounds = [[target, filler[0], generic], [target, filler[0], generic], selected]
    result, log = _joint_round_consensus(selected, rounds, key=lambda i: i.id, contexts={}, top_k=6)
    assert result[0] == target
    assert log["selected_extra_vote"] is False


def test_ledger_reads_actual_range_and_requires_independent_axes():
    item = SimpleNamespace(id="src/a.py::function:update", path="src/a.py", start_line=1, end_line=2,
                           reasons=["program_flow_support"])
    entity = CodeEntity("src/a.py", "function", "update", 1, 2, "not trusted cached text")
    index = SimpleNamespace(files={"src/a.py": "def update(sender):\n    return placeholder(sender)\n"}, entities=[entity])
    record = _reread_function_ledger(index, [[item]], "placeholder sender broken")[item.id]
    assert record["source_read"] and record["strong_singleton"]
    assert "placeholder(sender)" in record["snippets"][0]["text"]
    assert "not trusted" not in record["snippets"][0]["text"]
    item.reasons = []
    assert not _reread_function_ledger(index, [[item]], "placeholder sender broken")[item.id]["strong_singleton"]
    assert _reread_function_ledger(index, [[item]], "placeholder sender", limit=0)[item.id]["reason"] == "read_budget_exhausted"


def test_ledger_rejects_range_not_present_in_index():
    item = SimpleNamespace(id="a::function:f", path="a", start_line=10, end_line=20, reasons=["program_flow_support"])
    record = _reread_function_ledger(SimpleNamespace(files={"a": "one line"}, entities=[]), [[item]], "sender placeholder")[item.id]
    assert not record["source_read"] and not record["strong_singleton"]


def test_one_round_grounded_discovery_can_use_tail_reservoir():
    items = [SimpleNamespace(id=f"a::{i}", path="a", start_line=i+1, end_line=i+1) for i in range(6)]
    selected = items[:5]
    target = items[5]
    rankings = [items[:4] + [target], selected, selected]
    evidence = {target.id: {"source_read": True, "strong_singleton": True, "best_rank": 5}}
    result, log = _joint_round_consensus(selected, rankings, key=lambda i: i.id, contexts={}, top_k=5, entity_evidence=evidence)
    assert result[:3] == selected[:3]
    assert target in result and len(result) == 5
    assert log["source_reservoir"] == [{"id": target.id, "replaced": items[4].id}]
    assert selected == items[:5]


def test_joint_final_file_order_equals_existing_closure(monkeypatch, tmp_path):
    from mycode.dynamic_retrieval.search_agent import dynamic_localize
    from mycode.schemas.evidence import NormalizedSample
    monkeypatch.setenv("MYCODE_JOINT_EVIDENCE_V2", "1")
    monkeypatch.setenv("MYCODE_FAST_SEED_LLM", "0")
    monkeypatch.setenv("MYCODE_LLM_CANDIDATE_REVIEW", "0")
    (tmp_path / "handler.py").write_text("def update(sender):\n    return placeholder(sender)\n")
    index = RepositoryIndex(repo="test/repo", instance_id="joint-v3", repo_root=tmp_path)
    sample = NormalizedSample("joint-v3", "test/repo", "test", "sender placeholder is broken", {})
    result = dynamic_localize(sample, {}, index=index, max_rounds=1, max_react_steps=1)
    files = result["best_round_selection"]["joint_evidence_v2"]["files"]
    assert files["before"] == files["after"] == [i["path"] for i in result["ranked_locations"]]
    assert files["strategy"] == "preserve_precision_closure_order_v3"
