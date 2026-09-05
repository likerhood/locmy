from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.dynamic_retrieval.tools import NavigateCodeTool, TraceFlowTool
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.evaluation.localization_eval import evaluate_three_level_ranking, file_module_id
from mycode.flow_analysis.statement_flow import trace_statement_flows
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_statement_flow_does_not_connect_files_only_for_same_term() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/a.py", "def first():\n    kdf_rounds = 10\n    return kdf_rounds\n")
        _write(root / "src/b.py", "def second():\n    kdf_rounds = 20\n    return kdf_rounds\n")
        index = RepositoryIndex(repo="example/python", instance_id="same-term", repo_root=root)
        flows = trace_statement_flows(index, issue_text="support kdf_rounds", queries=["kdf_rounds"])
        assert flows
        edges = [edge for flow in flows for edge in flow.get("statement_edges", [])]
        assert all(edge.get("relation") != "same_term_statement_flow" for edge in edges)
        assert all(
            edge.get("source") == edge.get("target")
            for edge in edges
            if edge.get("relation") == "local_def_use"
        )


def _sample(
    *,
    instance_id: str,
    repo: str,
    issue_text: str,
    gold_files: list[str],
    patch: str = "",
) -> NormalizedSample:
    return NormalizedSample(
        instance_id=instance_id,
        repo=repo,
        dataset="unit",
        issue_text=issue_text,
        gold_files=gold_files,
        raw={"patch": patch} if patch else {},
    )


def test_statement_flow_tracks_python_serializer_parameter_chain() -> None:
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
                    "",
                    "class _KeySerializationEncryption:",
                    "    def __init__(self, kdf_rounds=None):",
                    "        self.kdf_rounds = kdf_rounds",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/backends/openssl/backend.py",
            "\n".join(
                [
                    "from cryptography.hazmat.primitives.serialization import ssh",
                    "",
                    "def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                    "    if format == 'OpenSSH':",
                    "        return ssh._serialize_ssh_private_key(",
                    "            key, encryption_algorithm.password, encryption_algorithm.kdf_rounds",
                    "        )",
                    "    return b''",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/ssh.py",
            "\n".join(
                [
                    "def _serialize_ssh_private_key(key, password, kdf_rounds=None):",
                    "    rounds = kdf_rounds or 16",
                    "    return b'openssh' + str(rounds).encode()",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="pyca/cryptography",
            instance_id="pyca__cryptography-7520",
            dataset="unit",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )

        flows = trace_statement_flows(
            index,
            issue_text=(
                "OpenSSH private key encryption should support kdf_rounds like ssh-keygen -a. "
                "BestAvailableEncryption must pass kdf_rounds through backend to ssh serializer."
            ),
            tool_observations=[],
            queries=["BestAvailableEncryption", "kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
            limit=10,
        )

        assert flows
        assert any(flow["backend"] == "statement_static_flow" for flow in flows)
        candidate_paths = {path for flow in flows for path in flow.get("candidate_target_paths", [])}
        assert "src/cryptography/hazmat/primitives/_serialization.py" in candidate_paths
        assert "src/cryptography/hazmat/backends/openssl/backend.py" in candidate_paths
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in candidate_paths
        statement_entities = {
            stmt.get("entity", {}).get("name")
            for flow in flows
            for loc in flow.get("locations", [])
            for stmt in loc.get("statements", [])
        }
        assert "_private_key_bytes" in statement_entities
        assert "_serialize_ssh_private_key" in statement_entities


def test_typed_graph_has_js_hook_event_style_and_java_override_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/ChartView.tsx",
            "\n".join(
                [
                    "import './legend.css';",
                    "import { useLegendState } from './useLegendState';",
                    "import { handleLeave } from './legendEvents';",
                    "import LegendPanel from './LegendPanel';",
                    "",
                    "export default function ChartView() {",
                    "  const legend = useLegendState();",
                    "  return <LegendPanel onMouseLeave={handleLeave} legend={legend} />;",
                    "}",
                ]
            ),
        )
        _write(repo_root / "src/useLegendState.ts", "export function useLegendState() { return {}; }\n")
        _write(repo_root / "src/legendEvents.ts", "export function handleLeave(evt) { return evt.type; }\n")
        _write(repo_root / "src/LegendPanel.tsx", "export default function LegendPanel(props) { return <div />; }\n")
        _write(repo_root / "src/legend.css", ".legend { display: flex; }\n")
        _write(
            repo_root / "src/main/java/example/Formatter.java",
            "package example;\npublic interface Formatter {\n  String format(String value);\n}\n",
        )
        _write(
            repo_root / "src/main/java/example/JsonFormatter.java",
            "\n".join(
                [
                    "package example;",
                    "public class JsonFormatter implements Formatter {",
                    "  @Override",
                    "  public String format(String value) {",
                    "    return value.trim();",
                    "  }",
                    "}",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="example/mixed",
            instance_id="example__mixed-1",
            dataset="unit",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        graph = TypedRepositoryGraph(index)
        counts = graph.edge_summary()["edge_type_counts"]

        assert counts.get("renders", 0) >= 1
        assert counts.get("styles", 0) >= 1
        assert counts.get("uses_hook", 0) >= 1
        assert counts.get("binds_ui_event", 0) >= 1
        assert counts.get("inherits_or_implements", 0) >= 1
        assert counts.get("overrides", 0) >= 1

        concern_hits = NavigateCodeTool(graph).run(["src/ChartView.tsx"], ["legend", "mouseleave"], mode="concern")
        assert concern_hits.status == "ok"
        assert any(item["path"] == "src/legendEvents.ts" for item in concern_hits.candidates)


def test_trace_flow_tool_exposes_statement_flows_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/plugin.legend.js",
            "\n".join(
                [
                    "export function handleLeave(chart, item) {",
                    "  chart.legend.options.onLeave(item);",
                    "  chart.update();",
                    "}",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="chartjs/Chart.js",
            instance_id="chartjs__Chart.js-10301",
            dataset="unit",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        sample = _sample(
            instance_id="chartjs__Chart.js-10301",
            repo="chartjs/Chart.js",
            issue_text="Legend onLeave hover behavior does not restore colors. Handler should call chart update.",
            gold_files=["src/plugin.legend.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["onLeave", "handleLeave", "legend"],
                "concern_queries": ["legend hover color restore"],
                "flow_hypotheses": ["ui event onLeave changes chart state then chart.update"],
            },
            "tool_observations": [],
        }
        sketch = build_issue_sketch(sample, evidence)
        observation = TraceFlowTool(index).run(
            sample,
            evidence,
            sketch,
            ["onLeave", "legend", "chart.update"],
            ["src/plugin.legend.js"],
        )

        assert observation.status == "ok"
        assert observation.metadata["statement_flows"]
        assert any(item.get("source") == "flow_candidate_target" for item in observation.candidates)


def test_dynamic_localize_and_three_level_eval_handle_non_function_css_gold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/App.jsx",
            "\n".join(
                [
                    "import './style.css';",
                    "export default function App() {",
                    "  return <button className=\"primary\">Submit</button>;",
                    "}",
                ]
            ),
        )
        _write(repo_root / "src/style.css", ".primary { color: red; }\n")
        patch = "\n".join(
            [
                "diff --git a/src/style.css b/src/style.css",
                "--- a/src/style.css",
                "+++ b/src/style.css",
                "@@ -1,1 +1,1 @@",
                "-.primary { color: red; }",
                "+.primary { color: blue; }",
            ]
        )
        sample = _sample(
            instance_id="ui__style-1",
            repo="ui/project",
            issue_text="The primary button style should use the expected blue color.",
            gold_files=["src/style.css"],
            patch=patch,
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["primary", "style", "blue"],
                "concern_queries": ["button style css color"],
                "flow_hypotheses": ["style rule affects component rendering"],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["primary"],
                    "concerns": ["style css color"],
                    "flows": ["style_pipeline"],
                }
            },
            "tool_observations": [],
        }
        index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            dataset=sample.dataset,
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        localization = dynamic_localize(sample, evidence, index=index, top_k=15, max_rounds=2)
        metrics = evaluate_three_level_ranking(localization, sample, index)
        ranked_modules = [item["id"] for item in localization.get("ranked_modules", [])]

        assert metrics["file"]["acc@15"] == 1.0
        assert metrics["module"]["acc@15"] == 1.0
        assert metrics["function"]["gold_count"] == 0.0
        assert file_module_id("src/style.css") in ranked_modules


if __name__ == "__main__":
    test_statement_flow_tracks_python_serializer_parameter_chain()
    test_typed_graph_has_js_hook_event_style_and_java_override_edges()
    test_trace_flow_tool_exposes_statement_flows_metadata()
    test_dynamic_localize_and_three_level_eval_handle_non_function_css_gold()
    print("ok")
