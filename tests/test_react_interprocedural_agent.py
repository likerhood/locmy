from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mycode.dynamic_retrieval.react_agent import run_react_tool_agent
from mycode.dynamic_retrieval.tools import DynamicToolObservation
from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.evidence.tools.llm_client import chat_completion, first_text
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.flow_analysis.interprocedural_flow import trace_interprocedural_flows
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample
from mycode.utils.trace_recorder import collect_token_usage, compact_agent_trace


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _chartjs_sample() -> tuple[tempfile.TemporaryDirectory[str], NormalizedSample, dict[str, Any], RepositoryIndex]:
    tmp = tempfile.TemporaryDirectory()
    repo_root = Path(tmp.name)
    _write(
        repo_root / "src/plugins/plugin.legend.js",
        "\n".join(
            [
                "function getLegendItemAt(chart, e) {",
                "  return chart.legend && chart.legend.getItemAt(e.x, e.y);",
                "}",
                "",
                "export function handleEvent(e, legend) {",
                "  const opts = legend.options;",
                "  const item = getLegendItemAt(legend.chart, e);",
                "  if (e.type === 'mousemove' && item) {",
                "    opts.onHover(e, item, legend);",
                "  }",
                "  if (e.type === 'mouseout') {",
                "    opts.onLeave(e, item, legend);",
                "  }",
                "  legend.chart.update();",
                "}",
            ]
        ),
    )
    _write(
        repo_root / "docs/sandbox/App.tsx",
        "\n".join(
            [
                "import { Pie } from 'react-chartjs-2';",
                "const options = {",
                "  plugins: { legend: { onHover: handleHover, onLeave: handleLeave } }",
                "};",
            ]
        ),
    )
    sample = NormalizedSample(
        instance_id="chartjs__Chart.js-10301",
        repo="chartjs/Chart.js",
        dataset="swe_clean15_unit",
        issue_text=(
            "CodeSandbox reproduction shows a pie chart legend onHover changes slice colors, "
            "but onLeave/mouseout does not restore colors. The fix should be in chart.js "
            "legend plugin event handling, not in react-chartjs wrapper demo code."
        ),
        raw={
            "problem_statement": (
                "CodeSandbox reproduction with react-chartjs-2 wrapper. Expected onLeave to restore "
                "legend colors after hover."
            ),
            "patch": "\n".join(
                [
                    "diff --git a/src/plugins/plugin.legend.js b/src/plugins/plugin.legend.js",
                    "--- a/src/plugins/plugin.legend.js",
                    "+++ b/src/plugins/plugin.legend.js",
                    "@@ -8,6 +8,7 @@ export function handleEvent(e, legend) {",
                    "   if (e.type === 'mouseout') {",
                    "+    opts.onLeave(e, item, legend);",
                    "   }",
                ]
            ),
        },
        gold_files=["src/plugins/plugin.legend.js"],
    )
    evidence = {
        "problem_statement_only": True,
        "evidence_packet": {
            "modality": "image_and_url",
            "symbol_queries": ["legend", "onHover", "onLeave", "mouseout", "handleEvent"],
            "concern_queries": ["chart legend hover leave color restore"],
            "flow_hypotheses": ["mouse hover and leave event should flow into legend plugin update"],
            "url_inspections": [
                {
                    "url": "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0",
                    "url_type": "playground",
                    "role": "reproduction evidence",
                    "semantic_terms": ["onHover", "onLeave", "react-chartjs-2", "legend"],
                }
            ],
            "image_inspections": [
                {
                    "image_type": "chart_canvas_rendering",
                    "role": "visual symptom",
                    "visual_queries": ["pie chart legend color hover restore"],
                }
            ],
        },
        "tool_observations": [
            {
                "tool": "browser_reproduction_reader",
                "extracted": {
                    "parsed_reproduction": {
                        "source_files": ["src/App.tsx"],
                        "semantic_queries": ["legend onHover onLeave chart update mouseout"],
                    },
                    "runtime_trace": "console trace: src/plugins/plugin.legend.js:5 in handleEvent after mouseout",
                    "source_files": [
                        {
                            "path": "docs/sandbox/App.tsx",
                            "code_preview": "legend: { onHover: handleHover, onLeave: handleLeave }",
                        }
                    ],
                },
            }
        ],
    }
    index = RepositoryIndex(
        repo=sample.repo,
        instance_id=sample.instance_id,
        dataset=sample.dataset,
        repo_root=repo_root,
        structure_path=repo_root / "missing.json",
    )
    return tmp, sample, evidence, index


def test_interprocedural_flow_tracks_python_parameter_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/cryptography/hazmat/primitives/_serialization.py",
            "\n".join(
                [
                    "class BestAvailableEncryption:",
                    "    def __init__(self, password, kdf_rounds=None):",
                    "        self.password = password",
                    "        self.kdf_rounds = kdf_rounds",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/backends/openssl/backend.py",
            "\n".join(
                [
                    "from cryptography.hazmat.primitives.serialization.ssh import _serialize_ssh_private_key",
                    "",
                    "def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                    "    return _serialize_ssh_private_key(key, encryption_algorithm.kdf_rounds)",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/ssh.py",
            "\n".join(
                [
                    "def _serialize_ssh_private_key(key, kdf_rounds=None):",
                    "    rounds = kdf_rounds or 16",
                    "    return str(rounds).encode()",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="pyca/cryptography",
            instance_id="pyca__cryptography-7520",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )

        flows = trace_interprocedural_flows(
            index,
            issue_text="OpenSSH private key encryption should pass kdf_rounds from BestAvailableEncryption to backend and ssh serializer.",
            queries=["BestAvailableEncryption", "kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
            limit=10,
        )

        assert flows
        assert any(flow["backend"] == "arise_inspired_interprocedural_flow" for flow in flows)
        paths = {path for flow in flows for path in flow.get("candidate_target_paths", [])}
        assert "src/cryptography/hazmat/primitives/_serialization.py" in paths
        assert "src/cryptography/hazmat/backends/openssl/backend.py" in paths
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in paths
        assert any(edge.get("relation") == "passes_term_to_callee" for flow in flows for edge in flow.get("edges", []))


def test_react_agent_runs_auditable_tools_for_chartjs_event_flow() -> None:
    tmp, sample, evidence, index = _chartjs_sample()
    try:
        graph = TypedRepositoryGraph(index)
        sketch = build_issue_sketch(sample, evidence)
        result = run_react_tool_agent(
            sample=sample,
            evidence_result=evidence,
            index=index,
            graph=graph,
            issue_sketch=sketch,
            queries=["legend", "onHover", "onLeave", "mouseout"],
            top_k=10,
            max_steps=5,
        )

        tools = [step["tool"] for step in result["steps"]]
        assert tools[:4] == ["SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"]
        assert "src/plugins/plugin.legend.js" in result["candidate_paths"]
        assert any(flow["backend"] == "arise_inspired_interprocedural_flow" for flow in result["flow_traces"])
        assert result["summary"]["flow_trace_count"] > 0
    finally:
        tmp.cleanup()


def test_react_agent_stops_after_covered_tools_stop_adding_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    tmp, sample, evidence, index = _chartjs_sample()
    try:
        graph = TypedRepositoryGraph(index)
        sketch = build_issue_sketch(sample, evidence)

        def no_gain_tool(**kwargs: Any) -> DynamicToolObservation:
            tool = str((kwargs.get("move") or {}).get("tool") or "SearchAnchor")
            return DynamicToolObservation(
                tool=tool,
                action="unit-no-gain",
                status="ok",
                candidates=[{"path": "src/plugins/plugin.legend.js"}],
            )

        planner_calls = {"count": 0}

        def planner(_prompt: str) -> dict[str, Any]:
            planner_calls["count"] += 1
            return {
                "thought": "Probe a distinct query while preserving the same deterministic result.",
                "tool": "SearchAnchor",
                "mode": "concern",
                "queries": [f"probe-{planner_calls['count']}"],
                "stop": False,
            }

        monkeypatch.setattr("mycode.dynamic_retrieval.react_agent._run_tool", no_gain_tool)
        result = run_react_tool_agent(
            sample=sample,
            evidence_result=evidence,
            index=index,
            graph=graph,
            issue_sketch=sketch,
            queries=["legend onLeave"],
            previous_candidates=["src/plugins/plugin.legend.js"],
            top_k=10,
            max_steps=8,
            planner_llm=planner,
        )
        assert result["stop_reason"] == "evidence_plateau_after_tool_coverage"
        assert result["steps"][-1]["tool"] == "Stop"
        assert len(result["steps"]) < 8
    finally:
        tmp.cleanup()


def test_dynamic_localize_records_react_trace_and_three_level_metrics() -> None:
    tmp, sample, evidence, index = _chartjs_sample()
    try:
        localization = dynamic_localize(
            sample,
            evidence,
            index=index,
            top_k=10,
            max_rounds=1,
        )
        assert localization["status"] == "ok"
        assert localization["react_agent_trace"]["summary"]["step_count"] >= 4
        assert localization["ranked_locations"][0]["path"] == "src/plugins/plugin.legend.js"
        assert any(flow["backend"] == "arise_inspired_interprocedural_flow" for flow in localization["flow_traces"])

        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@1"] == 1.0
        assert metrics["module"]["pred_count"] >= 1
        assert metrics["function"]["pred_count"] >= 1

        wrapped = {
            "instance_id": sample.instance_id,
            "repo": sample.repo,
            "dataset": sample.dataset,
            "status": "ok",
            "elapsed_seconds": 1.0,
            "evidence": evidence,
            "localization": localization,
            "evaluation_3level": metrics,
        }
        trace = compact_agent_trace(wrapped)
        assert trace["react_agent"]["steps"]
        assert trace["flow_summary"]["backend_counts"]["arise_inspired_interprocedural_flow"] >= 1
    finally:
        tmp.cleanup()


def test_collect_token_usage_keeps_runner_cost_auditable() -> None:
    result = {
        "evidence": {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
        "localization": {
            "react_agent_trace": {
                "steps": [
                    {"token_usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}},
                    {"observation": {"usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}},
                ]
            }
        },
    }
    assert collect_token_usage(result)["total_tokens"] == 29


@pytest.mark.skipif(
    not (os.environ.get("BASE_URL") and os.environ.get("API_KEY") and os.environ.get("MODEL_API_NAME", os.environ.get("MODEL_NAME"))),
    reason="Set BASE_URL, API_KEY and MODEL_API_NAME/MODEL_NAME to run live LLM controller smoke test.",
)
def test_live_llm_controller_can_emit_react_json() -> None:
    response = chat_completion(
        [
            {"role": "system", "content": "Return JSON only."},
            {
                "role": "user",
                "content": (
                    "For a code localization agent, choose one tool from SearchAnchor, NavigateCode, "
                    "TraceFlow, ReadCode. Return JSON keys thought, tool, mode, queries, stop."
                ),
            },
        ],
        temperature=0,
        max_tokens=300,
        timeout=60,
    )
    text = first_text(response)
    assert text
    start = text.find("{")
    end = text.rfind("}")
    assert start >= 0 and end > start
    parsed = json.loads(text[start : end + 1])
    assert parsed.get("tool") in {"SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"}
