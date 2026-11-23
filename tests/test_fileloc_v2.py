from types import SimpleNamespace

from mycode.dynamic_retrieval.fileloc_policy import (
    resolve_reads, quote_alignment, operation_quote, challenge_paths,
)
from mycode.dynamic_retrieval.candidate_reviewer import _validate
from mycode.utils.model_profile import ModelProtocolProfile


def test_resolve_unique_same_directory_stem():
    paths, events = resolve_reads(["Read src/elements/arc.js"], {"src/elements/arc.ts": "source"})
    assert paths == ["src/elements/arc.ts"]
    assert events[0]["reason"] == "unique_same_directory_stem"


def test_ambiguity_scope_and_url():
    files = {"src/arc.ts": "", "src/arc.jsx": "", "other/arc.js": ""}
    paths, events = resolve_reads(["src/arc.js ../src/arc.ts /src/arc.ts https://a/src/arc.ts"], files)
    assert paths == []
    assert [e["reason"] for e in events] == ["ambiguous", "outside_repository", "outside_repository"]


def test_exact_path_precedes_aliases():
    assert resolve_reads(["src/arc.ts"], {"src/arc.ts": "", "src/arc.js": ""})[0] == ["src/arc.ts"]


def test_quote_cannot_prove_sibling_texture_function():
    quote = "488: p5.prototype.textureMode = function(mode) { 489: if (mode !== constants.IMAGE) {"
    snippets = [{"text": quote, "start_line": 488, "end_line": 490}]
    entities = [{"name": "texture", "start_line": 400, "end_line": 430}]
    assert not quote_alignment(quote, entities, snippets, entities)


def test_quote_can_prove_body_without_declaration():
    quote = "410: this._renderer.bindTexture(image);"
    snippets = [{"text": quote, "start_line": 410, "end_line": 410}]
    entities = [{"name": "texture", "start_line": 400, "end_line": 430}]
    assert quote_alignment(quote, entities, snippets, entities)


def test_import_is_not_operation():
    assert not operation_quote("19: import { settings } from './settings';")
    assert operation_quote("410: this._renderer.bindTexture(image);")
    assert operation_quote("18: .container { margin-bottom: 16px; }")


def validate_example(monkeypatch, enabled, *, file_only=False):
    monkeypatch.setenv("MYCODE_FILELOC_OWNER_CHALLENGE", str(int(enabled)))
    quote = "18: .container { margin-bottom: 16px; }" if file_only else "488: p5.prototype.textureMode = function(mode) {"
    item = dict(path="src/file", verdict="verified", snippet_id="S1", entity_id=None if file_only else "E1",
                flow_id="F1", confidence=.95, causal_chain="state -> operation -> failing effect",
                file_anchor=".container" if file_only else None)
    return _validate(dict(selected_head="src/file", reviews=[item], continue_search=False),
        allowed_paths={"src/file"}, grounded_paths={"src/file"}, context_text_by_path={"src/file":quote},
        direct_flow_paths={"src/file"}, snippets_by_path={"src/file":{"S1":{"text":quote,"start_line":488,"end_line":489}}},
        entities_by_path={"src/file":{"E1":{"name":"texture","kind":"function","start_line":400,"end_line":430}}},
        flow_paths_by_id={"F1":{"src/file"}}, profile=ModelProtocolProfile(name="mimo"))


def test_reproduces_legacy_false_verification_and_blocks_it(monkeypatch):
    legacy = validate_example(monkeypatch, False)
    assert legacy["candidates"][0]["mechanism_verified"]
    strict = validate_example(monkeypatch, True)
    assert not strict["candidates"][0]["mechanism_verified"]
    assert strict["selected_head"] is None
    assert strict["continue_search"] and not strict["stop_ready"]
    assert "Read actual implementation src/file texture" in strict["next_queries"]


def test_style_owner_does_not_require_function(monkeypatch):
    result = validate_example(monkeypatch, True, file_only=True)
    assert result["candidates"][0]["file_anchor_verified"]
    assert result["candidates"][0]["mechanism_verified"]


def test_challenge_only_uses_indexed_observed_delegate():
    index = SimpleNamespace(files={"src/caller.js":"import owner from './owner';", "src/rival.js":"", "src/owner.ts":""})
    ranked = [SimpleNamespace(path=p) for p in ["src/caller.js", "src/rival.js"]]
    paths, events = challenge_paths(index, ranked)
    assert paths == ["src/caller.js", "src/rival.js", "src/owner.ts"]
    assert events[0]["source_path"] == "src/caller.js"


def test_delegate_ambiguity_does_not_invent_path():
    index = SimpleNamespace(files={"caller.js":"import owner from './owner';", "owner.ts":"", "owner.js":""})
    assert challenge_paths(index, [SimpleNamespace(path="caller.js")])[0] == ["caller.js"]


def test_read_reconcile_independently_executes_actual_request(monkeypatch):
    from mycode.dynamic_retrieval import react_agent
    from mycode.dynamic_retrieval.tools import DynamicToolObservation
    class Reader:
        def __init__(self, index):
            pass
        def run(self, paths, queries, limit):
            return DynamicToolObservation(tool="ReadCode", action="read", status="ok", seed_files=paths,
                candidates=[{"path":p,"snippets":[{"text":"body"}]} for p in paths])
    monkeypatch.setattr(react_agent, "ReadCodeTool", Reader)
    monkeypatch.setattr(react_agent, "sketch_query_terms", lambda _: [])
    monkeypatch.setenv("MYCODE_READ_EXPLICIT_PATHS", "0")
    monkeypatch.setenv("MYCODE_FILELOC_READ_RECONCILE", "1")
    result = react_agent._run_tool(move={"tool":"ReadCode","queries":["src/owner.js"]},
        sample=None,evidence_result={},index=SimpleNamespace(files={"src/owner.ts":"body"}),
        graph=None,issue_sketch=None,queries=[],candidate_paths=[f"other/{i}.js" for i in range(25)],top_k=15)
    assert result.seed_files[0] == "src/owner.ts"
    assert len(result.seed_files) == 18
    assert result.metadata["read_execution"]["path_resolution"][0]["reason"] == "unique_same_directory_stem"
    assert "src/owner.ts" in result.metadata["read_execution"]["snippet_paths"]


def test_actual_gap_blocks_high_confidence_stop(monkeypatch):
    from mycode.dynamic_retrieval.search_agent import _stop_decision, RankedLocation
    review = validate_example(monkeypatch, True)
    result = _stop_decision(round_no=2,max_rounds=4,ranked=[RankedLocation(path="src/file",score=100)],
        previous_top_paths=["src/file"],next_queries=review["next_queries"],candidate_review=review,
        confidence={"confidence":.99,"axes":{"source":True,"read":True,"program":True}})
    assert not result["stop"]


def test_new_harness_whitelist_and_omni_scoring():
    import importlib.util
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    def load(name):
        import sys
        sys.path.insert(0,str(root/"scripts"))
        spec=importlib.util.spec_from_file_location(name,root/"scripts"/(name+".py"))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    runner=load("fileloc_v2_smoke")
    assert runner.task_input({"problem_statement":"bug","patch":"secret","gold":["secret"],"difficulty":"hard"}) == {"problem_statement":"bug"}
    scorer=load("score_fileloc_v2")
    metrics=scorer.score({"ranked_locations":[{"path":"a.py"}],"ranked_functions":["a.py::function:run"]},
        {"files":["a.py"],"modules":[],"functions":["a.py::run"]},"Omni296")
    assert metrics["function"]["acc@1"] == 1
