from types import SimpleNamespace

import pytest

from mycode.evidence.issue_sketch import _flow_obligations
from mycode.flow_analysis.language_context import python_binding_context
from mycode.flow_analysis.flow_chain import _classify_flow
from mycode.flow_analysis.parameter_closure import _classify_closure
from mycode.dynamic_retrieval.package_navigation import package_entry_candidates, navigation_only_file
from mycode.dynamic_retrieval.seed_frontier import demote_generated_outputs
from mycode.dynamic_retrieval.fast_seed_planner import plan_fast_seeds


def test_javascript_declaration_does_not_require_python_type_flow():
    issue = "for await variable declaration shadowing fails"
    obligations = _flow_obligations(issue, [], [], [], repo="babel/babel")
    assert not any(x["flow_type"] == "python_type_binding_flow" for x in obligations)
    assert any(x["state"] == "AST/scope/transformation" for x in obligations)
    assert not _flow_obligations("Build fails on Linux", [], [], [], repo="babel/babel")
    assert not python_binding_context(issue, paths=["packages/transform/src/index.ts"])
    assert _classify_flow(issue, [{"path": "src/parser.ts", "code": "declaration"}], []) != "python_type_binding_flow_chain"
    assert _classify_closure(issue, [{"path": "src/parser.ts"}], []) != "python_type_binding_flow"


def test_python_repo_and_source_preserve_binding_navigation():
    assert python_binding_context("declaration narrowing", repo="python/mypy")
    assert python_binding_context("binder declaration", paths=["mypy/binder.py"])
    assert not python_binding_context("Python HTTP response parsing")
    assert any(x["flow_type"] == "python_type_binding_flow" for x in
               _flow_obligations("declaration narrowing", [], [], [], repo="python/mypy"))


def test_package_hints_require_specific_issue_support_and_real_source():
    files = {
        "Makefile.js": "build", "CHANGELOG.md": "history",
        "packages/plugin-async-generator/src/index.ts": "implementation",
        "packages/plugin-async-generator/src/util.ts": "helper",
        "packages/plugin-async-generator/src/index.d.ts": "declaration",
        "packages/plugin-unrelated/src/index.ts": "other",
    }
    assert package_entry_candidates(files, "async generator fails") == [
        "packages/plugin-async-generator/src/index.ts", "packages/plugin-async-generator/src/util.ts",
    ]
    assert package_entry_candidates(files, "generic parser failure") == []


def test_public_file_demotions_preserve_recall_and_task_exceptions():
    paths = ["Makefile.js", "CHANGELOG.md", "packages/parser/src/index.ts"]
    items = [SimpleNamespace(path=p, reasons=[]) for p in paths]
    ranked, _ = demote_generated_outputs(items, files={p: "source" for p in paths},
                                         issue_text="Parsing empty destructuring fails")
    assert [r.path for r in ranked] == [paths[2], paths[0], paths[1]]
    assert not navigation_only_file("Makefile.js", "Build fails on Linux")
    assert not navigation_only_file("CHANGELOG.md", "Update release notes")


def test_package_hint_enters_planner_without_global_hit(monkeypatch):
    monkeypatch.setenv("MYCODE_FAST_SEED_PLANNER", "1")
    monkeypatch.setenv("MYCODE_FAST_SEED_LLM", "0")
    path = "packages/plugin-async-generator/src/index.ts"
    index = SimpleNamespace(files={path: "implementation"}, entities=[], search_files=lambda *a, **kw: [])
    result = plan_fast_seeds(index=index, issue_text="async generator fails",
                             query_groups={}, evidence_result={}, controller_llm=None)
    assert result["seed_files"] == [path]
    assert result["evidence"][path]["channels"] == ["issue_package"]


@pytest.mark.parametrize("path", ["src/parser.ts", "lib/parser.js", "codec/src/main/java/Parser.java"])
def test_all_slice_backends_reject_python_labels_for_non_python_source(path):
    from mycode.flow_analysis.statement_flow import _classify_flow as classify_statement, _role as statement_role
    from mycode.flow_analysis.static_slice import _flow_type, StatementNode, _role as slice_role
    text = "Python compatibility: declaration binding fails"
    node = StatementNode("n", path, 1, 1, text, "statement")
    assert not python_binding_context(text, paths=[path])
    assert "python_type" not in classify_statement(text, [node.to_dict()], [])
    assert "python_type" not in _flow_type(text, [node], [])
    assert statement_role(path, [node.to_dict()]) != "type_binder"
    assert slice_role(path, text) != "type_binder"


def test_slice_backends_preserve_python_binding_and_reject_unknown_language():
    from mycode.flow_analysis.statement_flow import _classify_flow as classify_statement
    from mycode.flow_analysis.static_slice import _flow_type, StatementNode
    node = StatementNode("n", "mypy/binder.py", 1, 1, "declaration binding", "statement")
    assert "python_type" in classify_statement("binding", [node.to_dict()], [])
    assert "python_type" in _flow_type("binding", [node], [])
    assert "python_type" not in _flow_type("declaration", [], [])


def test_java_python_and_lib_entries_are_bounded_and_source_only():
    java = "codec-http2/src/main/java/io/netty/DefaultHttp2ConnectionDecoder.java"
    python = "mypy/typeanal.py"
    js = "lib/NormalModuleFactory.js"
    files = {java: "source", python: "source", "mypy/__init__.py": "source", js: "source",
             "lib/NormalModuleFactory.test.js": "test", "lib/generated/NormalModuleFactory.js": "generated"}
    assert package_entry_candidates(files, "DefaultHttp2ConnectionDecoder fails") == [java]
    assert package_entry_candidates(files, "typeanal fails") == [python]
    assert package_entry_candidates(files, "NormalModuleFactory fails") == [js]
    assert package_entry_candidates(files, "generic lib module failure") == []
    assert package_entry_candidates(files, "mypy declaration issue", limit=1) == ["mypy/__init__.py"]
    assert package_entry_candidates(files, "mypy", limit=0) == []


def test_source_context_prefers_late_entity_over_early_generic_matches():
    from mycode.dynamic_retrieval.search_agent import _read_code_context, RankedLocation
    path = "lib/NormalModuleFactory.js"
    lines = ["const config = {};" for _ in range(40)] + ["function parseResourceWithoutFragment(value) {", "return value;", "}"]
    index = SimpleNamespace(files={path: "\n".join(lines)})
    ranked = [RankedLocation(path=path, score=1, entities=[{"name": "parseResourceWithoutFragment"}])]
    result = _read_code_context(index, ranked, ["config"], limit=1)
    snippets = result[0]["snippets"]
    assert "function parseResourceWithoutFragment" in snippets[0]["text"]
    assert len(snippets) <= 3
    for i, a in enumerate(snippets):
        for b in snippets[i + 1:]:
            assert a["end_line"] < b["start_line"] or b["end_line"] < a["start_line"]
    assert not _read_code_context(index, ranked, ["config"], limit=0)


def test_source_context_does_not_invent_snippet_for_missing_anchor():
    from mycode.dynamic_retrieval.search_agent import _read_code_context, RankedLocation
    index = SimpleNamespace(files={"src/a.py": "pass"})
    result = _read_code_context(index, [RankedLocation(path="src/a.py", score=1)], ["absent"])
    assert result[0]["snippets"] == []
