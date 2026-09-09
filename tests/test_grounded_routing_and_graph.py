from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import (
    RankedLocation,
    _build_localization_query_groups,
    _precision_rerank_locations,
)
from mycode.evidence import understanding_agent
from mycode.evidence.issue_sketch import _grounded_llm_terms
from mycode.evidence.synthesis import synthesize_evidence
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import (
    EvidenceCollectionPlan,
    EvidencePacket,
    NormalizedSample,
    ToolObservation,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_llm_local_source_request_is_rerouted_to_repository_search(monkeypatch) -> None:
    plan = EvidenceCollectionPlan(
        instance_id="assertj__assertj-1332",
        repo="assertj/assertj",
        dataset="unit",
        issue_summary="A character assertion delegates to internal string comparison.",
    )
    observation = ToolObservation(
        tool="web_snapshot_fetcher",
        source="https://example.test/issue",
        success=True,
        status="planned",
    )

    def fake_chat_completion(*_args, **_kwargs):
        return {
            "id": "local-path",
            "model": "fake",
            "choices": [{"message": {"content": (
                '{"additional_tool_requests":[{'
                '"tool":"browser_reproduction_reader",'
                '"source":"src/main/java/org/assertj/core/internal/Strings.java"}],'
                '"reasoning":"inspect implementation","stop_reason":""}'
            )}}],
        }

    monkeypatch.setattr(understanding_agent, "chat_completion", fake_chat_completion)
    result = understanding_agent._llm_followup_requests(
        plan=plan,
        observations=[observation],
        existing=set(),
    )

    assert result["_parsed_tool_requests"] == []
    assert plan.metadata["local_code_hints"] == [
        "src/main/java/org/assertj/core/internal/Strings.java"
    ]
    assert result["_rejected_tool_requests"][0]["reason"].startswith("local_code_reference")
    assert understanding_agent._local_code_hint(
        {
            "tool": "browser_reproduction_reader",
            "source": "client/me/purchases/manage-purchase",
        }
    ) == "client/me/purchases/manage-purchase"

    packet = EvidencePacket(
        instance_id=plan.instance_id,
        repo=plan.repo,
        dataset=plan.dataset,
        issue_summary=plan.issue_summary,
        modality="text_only",
    )
    synthesis = synthesize_evidence(packet=packet, plan=plan, observations=[])
    assert synthesis["query_groups"]["local_code"] == plan.metadata["local_code_hints"]
    assert any(item["kind"] == "local_code_navigation" for item in synthesis["navigation_hints"])


def test_local_code_synthesis_group_becomes_explicit_entity_query() -> None:
    sample = NormalizedSample(
        instance_id="unit",
        repo="assertj/assertj",
        dataset="unit",
        issue_text="Character sequence assertions should compare internal strings correctly.",
        raw={},
    )

    class Sketch:
        entities = []
        concerns = []
        architectural_queries = []
        expected_effects = []
        states = []
        flow_obligations = []

    groups = _build_localization_query_groups(
        sample,
        {
            "evidence_synthesis": {
                "query_groups": {
                    "local_code": ["src/main/java/org/assertj/core/internal/Strings.java"]
                }
            }
        },
        Sketch(),
        [],
    )
    assert "src/main/java/org/assertj/core/internal/Strings.java" in groups["explicit_entity"]


def test_github_local_path_is_preserved_in_evidence_synthesis() -> None:
    plan = EvidenceCollectionPlan(
        instance_id="processing__p5.js-6111",
        repo="processing/p5.js",
        dataset="unit",
        issue_summary="Camera clipping behavior should use the referenced implementation.",
    )
    packet = EvidencePacket(
        instance_id=plan.instance_id,
        repo=plan.repo,
        dataset=plan.dataset,
        issue_summary=plan.issue_summary,
        modality="text_only",
    )
    observation = ToolObservation(
        tool="github_url_parser",
        source="https://github.com/processing/p5.js/blob/main/src/webgl/p5.Camera.js",
        success=True,
        status="ok",
        extracted={
            "path": "src/webgl/p5.Camera.js",
            "local_path": "src/webgl/p5.Camera.js",
            "symbol_hint": "Camera",
        },
    )

    synthesis = synthesize_evidence(
        packet=packet,
        plan=plan,
        observations=[observation],
    )

    assert synthesis["query_groups"]["local_code"] == ["src/webgl/p5.Camera.js"]
    assert any(item["kind"] == "local_code_navigation" for item in synthesis["navigation_hints"])


def test_llm_issue_terms_require_a_non_generic_issue_anchor() -> None:
    issue = "Allow transferring plan ownership to another administrator."
    terms = _grounded_llm_terms(
        [
            "plugin state callback behavior",
            "plan ownership transfer handler",
        ],
        text=issue,
        kind="concern",
        limit=8,
        max_chars=120,
    )
    assert "plugin state callback behavior" not in terms
    assert "plan ownership transfer handler" in terms


def test_java_receiver_call_connects_api_to_internal_implementation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "src/main/java/org/assertj/core/api/AbstractCharSequenceAssert.java",
            """
package org.assertj.core.api;
import org.assertj.core.internal.Strings;
public class AbstractCharSequenceAssert {
  private final Strings strings;
  public void contains(String actual, String expected) {
    strings.assertContains(actual, expected);
  }
}
""",
        )
        _write(
            root / "src/main/java/org/assertj/core/internal/Strings.java",
            """
package org.assertj.core.internal;
public class Strings {
  public void assertContains(String actual, String expected) { }
}
""",
        )
        index = RepositoryIndex(repo="assertj/assertj", instance_id="unit", repo_root=root)
        graph = TypedRepositoryGraph(index)
        edges = graph.edges_by_source[
            "src/main/java/org/assertj/core/api/AbstractCharSequenceAssert.java"
        ]
        assert any(
            edge.target == "src/main/java/org/assertj/core/internal/Strings.java"
            and edge.edge_type == "calls"
            and "receiver-resolved" in edge.evidence
            for edge in edges
        )


def test_javascript_namespace_and_commonjs_member_calls_resolve_to_module() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/parser.js", "export function parse(input) { return input; }\n")
        _write(
            root / "src/esm.js",
            "import * as parser from './parser';\nexport const run = input => parser.parse(input);\n",
        )
        _write(
            root / "src/cjs.js",
            "const parser = require('./parser');\nexports.run = input => parser.parse(input);\n",
        )
        index = RepositoryIndex(repo="example/js", instance_id="unit", repo_root=root)
        graph = TypedRepositoryGraph(index)
        for source in ("src/esm.js", "src/cjs.js"):
            assert any(
                edge.target == "src/parser.js"
                and edge.edge_type == "calls"
                and "member call parser.parse" in edge.evidence
                for edge in graph.edges_by_source[source]
            )


def test_read_implementation_can_displace_unverified_declaration_top_one() -> None:
    ranked = [
        RankedLocation(path="src/widget.d.ts", score=100.0),
        RankedLocation(
            path="src/widget_runtime.ts",
            score=90.0,
            score_components={"call_score": 4.0},
        ),
    ]
    reranked, diagnostics = _precision_rerank_locations(
        ranked=ranked,
        round_rankings=[ranked],
        issue_text="Widget runtime should call the configured handler.",
        review={"candidates": []},
        code_contexts=[
            {
                "path": "src/widget_runtime.ts",
                "snippets": [{"text": "export function runWidget() { handler(); }"}],
            }
        ],
        top_k=2,
    )
    assert reranked[0].path == "src/widget_runtime.ts"
    assert diagnostics["top_before"][0] == "src/widget.d.ts"
