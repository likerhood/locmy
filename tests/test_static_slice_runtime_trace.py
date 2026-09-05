from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.dynamic_retrieval.tools import TraceFlowTool
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.flow_analysis.runtime_trace import verify_runtime_traces
from mycode.flow_analysis.static_slice import trace_static_slices
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_arise_inspired_static_slice_tracks_crypto_kdf_rounds_chain() -> None:
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
                    "from cryptography.hazmat.primitives.serialization import ssh",
                    "",
                    "def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                    "    if format == 'OpenSSH':",
                    "        return ssh._serialize_ssh_private_key(",
                    "            key, encryption_algorithm.password, encryption_algorithm.kdf_rounds",
                    "        )",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/ssh.py",
            "\n".join(
                [
                    "def _serialize_ssh_private_key(key, password, kdf_rounds=None):",
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

        flows = trace_static_slices(
            index,
            issue_text="OpenSSH BestAvailableEncryption should pass kdf_rounds through backend to ssh serializer.",
            queries=["BestAvailableEncryption", "kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
            limit=10,
        )

        assert flows
        assert any(flow["backend"] == "arise_inspired_static_slice" for flow in flows)
        paths = {path for flow in flows for path in flow.get("candidate_target_paths", [])}
        assert "src/cryptography/hazmat/primitives/_serialization.py" in paths
        assert "src/cryptography/hazmat/backends/openssl/backend.py" in paths
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in paths
        assert any(flow.get("def_use_edges") for flow in flows)
        assert any(flow.get("call_boundary_edges") for flow in flows)


def test_daira_style_runtime_trace_maps_stack_to_repo_entity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "mypy/binder.py",
            "\n".join(
                [
                    "class ConditionalTypeBinder:",
                    "    def get_declaration(self, expr):",
                    "        return self.top_frame_context.get(expr)",
                    "",
                    "    def top_frame_context(self):",
                    "        return {}",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="python/mypy",
            instance_id="python__mypy-13481",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        tool_observations = [
            {
                "tool": "runtime_reproducer",
                "extracted": {
                    "runtime_trace": (
                        "Traceback: mypy/binder.py:2 in get_declaration\n"
                        "Trying to read deleted variable after del Foo; top_frame_context was consulted."
                    )
                },
            }
        ]

        flows = verify_runtime_traces(
            index,
            issue_text="del Foo should produce a deleted variable error in mypy binder.",
            tool_observations=tool_observations,
            queries=["deleted variable", "get_declaration", "top_frame_context"],
        )

        assert flows
        assert flows[0]["backend"] == "runtime_trace_observation_parser"
        assert "mypy/binder.py" in flows[0]["candidate_target_paths"]
        assert any(loc.get("name") == "get_declaration" for loc in flows[0]["locations"])


def test_traceflow_and_dynamic_agent_use_static_slice_and_runtime_trace_for_chartjs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/plugins/plugin.legend.js",
            "\n".join(
                [
                    "export function handleHover(evt, item, legend) {",
                    "  legend.chart.update();",
                    "}",
                    "",
                    "export function handleLeave(evt, item, legend) {",
                    "  legend.chart.update();",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "docs/sandbox/App.tsx",
            "const options = { plugins: { legend: { onHover: handleHover, onLeave: handleLeave } } };\n",
        )
        sample = NormalizedSample(
            instance_id="chartjs__Chart.js-10301",
            repo="chartjs/Chart.js",
            dataset="unit",
            issue_text=(
                "Legend onHover changes pie chart colors but onLeave does not restore the chart. "
                "The CodeSandbox reproduction uses legend onHover and onLeave callbacks."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/src/plugins/plugin.legend.js b/src/plugins/plugin.legend.js",
                        "--- a/src/plugins/plugin.legend.js",
                        "+++ b/src/plugins/plugin.legend.js",
                        "@@ -1,4 +1,4 @@",
                        " export function handleLeave(evt, item, legend) {",
                        "+  legend.chart.update();",
                        " }",
                    ]
                )
            },
            gold_files=["src/plugins/plugin.legend.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["legend", "onHover", "onLeave", "handleLeave", "chart.update"],
                "concern_queries": ["legend hover leave color restore"],
                "flow_hypotheses": ["browser hover and leave event should update chart"],
            },
            "tool_observations": [
                {
                    "tool": "browser_reproduction_reader",
                    "extracted": {
                        "parsed_reproduction": {"semantic_queries": ["legend onHover onLeave chart update"]},
                        "runtime_trace": "console trace: src/plugins/plugin.legend.js:5 in handleLeave after mouseleave",
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
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        sketch = build_issue_sketch(sample, evidence)
        observation = TraceFlowTool(index).run(
            sample,
            evidence,
            sketch,
            ["legend", "onLeave", "chart.update"],
            ["docs/sandbox/App.tsx", "src/plugins/plugin.legend.js"],
        )

        assert observation.metadata["static_slices"]
        assert observation.metadata["runtime_trace_verifications"]
        localization = dynamic_localize(sample, evidence, repo_root=repo_root, structure_path=repo_root / "missing.json", top_k=10)
        assert localization["ranked_locations"][0]["path"] == "src/plugins/plugin.legend.js"
        assert any(flow["backend"] == "arise_inspired_static_slice" for flow in localization["flow_traces"])
        assert any(flow["backend"] == "runtime_trace_observation_parser" for flow in localization["flow_traces"])
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@1"] == 1.0
        assert metrics["function"]["gold_count"] >= 1
