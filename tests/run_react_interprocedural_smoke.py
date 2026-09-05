from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mycode.dynamic_retrieval.react_agent import run_react_tool_agent
from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.evidence.tools.llm_client import LLMClientError, chat_completion, first_text
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.flow_analysis.interprocedural_flow import trace_interprocedural_flows
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample
from mycode.utils.trace_recorder import collect_token_usage, compact_agent_trace


OUT_DIR = ROOT / "tests" / "_smoke_outputs"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _step(no: int, total: int, message: str) -> float:
    print(f"[{no}/{total}] {message}", flush=True)
    return time.time()


def _done(start: float, message: str) -> None:
    print(f"  ok elapsed={time.time() - start:.3f}s {message}", flush=True)


def _make_chartjs_case() -> tuple[tempfile.TemporaryDirectory[str], NormalizedSample, dict[str, Any], RepositoryIndex]:
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
                "import { Chart, registerables } from 'chart.js';",
                "Chart.register(...registerables);",
                "function handleHover(evt, item, legend) {",
                "  legend.chart.data.datasets[0].backgroundColor[0] = '#CB4335DD';",
                "  legend.chart.update();",
                "}",
                "function handleLeave(evt, item, legend) {",
                "  legend.chart.data.datasets[0].backgroundColor[0] = '#CB4335';",
                "  legend.chart.update();",
                "}",
                "const options = { plugins: { legend: { onHover: handleHover, onLeave: handleLeave } } };",
                "export default function App() { return <Pie options={options} />; }",
            ]
        ),
    )
    sample = NormalizedSample(
        instance_id="chartjs__Chart.js-10301",
        repo="chartjs/Chart.js",
        dataset="swe_clean15_smoke",
        issue_text=(
            "CodeSandbox reproduction and screenshot show that a pie chart legend onHover changes colors, "
            "but onLeave/mouseout does not restore them. The wrapper demo is evidence; the patch should be "
            "in chart.js legend plugin event handling."
        ),
        raw={
            "problem_statement": (
                "Reproduction URL: https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0. "
                "Expected the onLeave callback to restore legend colors after hover."
            ),
            "patch": "\n".join(
                [
                    "diff --git a/src/plugins/plugin.legend.js b/src/plugins/plugin.legend.js",
                    "--- a/src/plugins/plugin.legend.js",
                    "+++ b/src/plugins/plugin.legend.js",
                    "@@ -8,6 +8,7 @@ export function handleEvent(e, legend) {",
                    "+    opts.onLeave(e, item, legend);",
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
            "effect_queries": ["restore color after mouseout"],
            "flow_hypotheses": ["mouse hover and leave event should flow into legend plugin update"],
            "url_inspections": [
                {
                    "url": "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0",
                    "url_type": "playground",
                    "role": "reproduction evidence",
                    "semantic_terms": ["onHover", "onLeave", "react-chartjs-2", "legend", "mouseout"],
                    "modification_prior": "low",
                    "navigation_value": "high",
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


def _run_python_interprocedural_case() -> None:
    start = _step(1, 4, "ARISE-inspired interprocedural parameter flow: pyca__cryptography-7520")
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
            issue_text=(
                "OpenSSH private key encryption should pass kdf_rounds from "
                "BestAvailableEncryption to backend and ssh serializer."
            ),
            queries=["BestAvailableEncryption", "kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
            limit=10,
        )
        paths = {path for flow in flows for path in flow.get("candidate_target_paths", [])}
        assert "src/cryptography/hazmat/primitives/_serialization.py" in paths
        assert "src/cryptography/hazmat/backends/openssl/backend.py" in paths
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in paths
        assert any(edge.get("relation") == "passes_term_to_callee" for flow in flows for edge in flow.get("edges", []))
        _done(start, f"flows={len(flows)} paths={sorted(paths)}")


def _run_chartjs_react_case() -> dict[str, Any]:
    start = _step(2, 4, "ReAct tools over image+URL evidence: chartjs__Chart.js-10301")
    tmp, sample, evidence, index = _make_chartjs_case()
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
        assert tools[:4] == ["SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"], tools
        assert "src/plugins/plugin.legend.js" in result["candidate_paths"]
        flow_backends = sorted({flow.get("backend", "") for flow in result.get("flow_traces", []) if flow.get("backend")})
        _done(start, f"tools={tools} flow_backends={flow_backends}")
        return {"tmp": tmp, "sample": sample, "evidence": evidence, "index": index, "react": result}
    except Exception:
        tmp.cleanup()
        raise


def _run_dynamic_eval_case(bundle: dict[str, Any]) -> dict[str, Any]:
    start = _step(3, 4, "Dynamic localization + file/module/function eval")
    sample = bundle["sample"]
    evidence = bundle["evidence"]
    index = bundle["index"]
    localization = dynamic_localize(sample, evidence, index=index, top_k=10, max_rounds=1)
    metrics = evaluate_three_level_ranking(localization, sample, index)
    assert localization["ranked_locations"][0]["path"] == "src/plugins/plugin.legend.js"
    assert metrics["file"]["acc@1"] == 1.0
    assert metrics["module"]["pred_count"] >= 1
    assert metrics["function"]["pred_count"] >= 1
    wrapped = {
        "instance_id": sample.instance_id,
        "repo": sample.repo,
        "dataset": sample.dataset,
        "status": "ok",
        "elapsed_seconds": round(time.time() - start, 3),
        "evidence": evidence,
        "localization": localization,
        "evaluation_3level": metrics,
    }
    trace = compact_agent_trace(wrapped)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "react_interprocedural_trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _done(
        start,
        "top1=src/plugins/plugin.legend.js "
        f"file_acc@1={metrics['file']['acc@1']} module_pred={metrics['module']['pred_count']} "
        f"function_pred={metrics['function']['pred_count']}",
    )
    return wrapped


def _run_optional_live_llm_case() -> None:
    if not os.environ.get("MYCODE_RUN_LIVE_LLM"):
        print("[4/4] skip live LLM planner smoke: set MYCODE_RUN_LIVE_LLM=1 to enable", flush=True)
        return
    start = _step(4, 4, "Live LLM ReAct planner integrated with tools + token usage")

    def planner(prompt: str) -> dict[str, Any]:
        try:
            response = chat_completion(
                [
                    {"role": "system", "content": "Return compact JSON only. Do not use markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=300,
                timeout=60,
            )
        except LLMClientError as exc:
            raise AssertionError(f"live LLM smoke failed: {exc}") from exc
        return {"content": first_text(response), "usage": response.get("usage") or {}, "raw_response": response}

    tmp, sample, evidence, index = _make_chartjs_case()
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
            max_steps=2,
            planner_llm=planner,
        )
        assert result["planner_used"] is True
        assert result["steps"], result
        usage = result.get("usage_summary") or {}
        assert int(usage.get("total_tokens") or 0) > 0, usage
        tools = [step["tool"] for step in result["steps"]]
        _done(start, f"tools={tools} usage={usage} first_llm_raw={result['steps'][0].get('llm_raw', '')[:120]!r}")
    finally:
        tmp.cleanup()


def main() -> None:
    all_start = time.time()
    print("mycode ReAct/interprocedural smoke test", flush=True)
    print(f"Root: {ROOT}", flush=True)
    _run_python_interprocedural_case()
    bundle = _run_chartjs_react_case()
    try:
        wrapped = _run_dynamic_eval_case(bundle)
        token_usage = collect_token_usage(wrapped)
        print(f"  token_usage_collected={token_usage}", flush=True)
    finally:
        bundle["tmp"].cleanup()
    _run_optional_live_llm_case()
    print(f"Done. elapsed={time.time() - all_start:.3f}s trace={OUT_DIR / 'react_interprocedural_trace.json'}", flush=True)


if __name__ == "__main__":
    main()
