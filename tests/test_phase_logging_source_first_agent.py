from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_phase_logging_source_first_and_light_flow(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo_root = tmp_path / "repo"
        events_path = tmp_path / "phase_events.jsonl"
        state_path = tmp_path / "state.json"

        _write(
            repo_root / "src/io/files.js",
            "\n".join(
                [
                    "export function loadStrings(path, callback) {",
                    "  return fetch(path)",
                    "    .then(response => response.text())",
                    "    .then(text => text.split(/\\r?\\n/));",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "test/unit/io/files_input.js",
            "describe('loadStrings', () => { it('loads files', () => loadStrings('x')); });\n",
        )
        _write(
            repo_root / "test/manual-test-examples/async/loadStrings_callback/sketch.js",
            "function setup() { loadStrings('assets/empty-lines.txt', console.log); }\n",
        )
        _write(
            repo_root / "docs/reference/loadStrings.md",
            "# loadStrings\nExample documentation for loadStrings and empty lines.\n",
        )
        _write(
            repo_root / "lib/addons/p5.sound.min.js",
            "function a(){return loadStrings('x')}\n",
        )

        sample = NormalizedSample(
            instance_id="processing__p5.js-3068",
            repo="processing/p5.js",
            dataset="unit",
            issue_text=(
                "loadStrings should preserve empty lines in text files. The current behavior filters out "
                "empty strings. Please fix the implementation of loadStrings, not the examples or docs."
            ),
            raw={},
            gold_files=["src/io/files.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["loadStrings"],
                "concern_queries": ["loadStrings empty lines text split"],
                "flow_hypotheses": ["loaded text flows through split into returned lines"],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["loadStrings"],
                    "concerns": ["empty lines", "text file loading"],
                    "flows": ["text split return lines"],
                }
            },
            "tool_observations": [],
        }

        monkeypatch.setenv("MYCODE_PHASE_LOG", "1")
        monkeypatch.setenv("MYCODE_PHASE_STDOUT", "0")
        monkeypatch.setenv("MYCODE_PHASE_EVENTS_JSONL", str(events_path))
        monkeypatch.setenv("MYCODE_PHASE_STATE_JSON", str(state_path))
        monkeypatch.setenv("MYCODE_PHASE_INSTANCE_ID", sample.instance_id)
        monkeypatch.setenv("MYCODE_FLOW_MODE", "light")
        monkeypatch.setenv("MYCODE_SOURCE_FIRST_FILTER", "1")
        monkeypatch.setenv("MYCODE_EARLY_STOP_HIGH_CONFIDENCE", "1")
        monkeypatch.setenv("MYCODE_HIGH_CONFIDENCE_THRESHOLD", "0.60")
        monkeypatch.setenv("MYCODE_ROUND_POOL_LIMIT", "30")
        monkeypatch.setenv("MYCODE_ROUND_READ_BUDGET", "10")
        monkeypatch.setenv("MYCODE_FLOW_POOL_LIMIT", "12")
        monkeypatch.setenv("MYCODE_CODE_CONTEXT_LIMIT", "4")

        result = dynamic_localize(
            sample,
            evidence,
            repo_root=repo_root,
            structure_path=repo_root / "missing_structure.json",
            top_k=5,
            max_rounds=2,
            max_react_steps=1,
            controller_llm=None,
        )

        top_paths = [item["path"] for item in result["ranked_locations"]]
        assert top_paths[0] == "src/io/files.js"
        scores = {item["path"]: item["score"] for item in result["ranked_locations"]}
        source_score = scores["src/io/files.js"]
        assert source_score > scores["test/manual-test-examples/async/loadStrings_callback/sketch.js"]
        assert source_score > scores["lib/addons/p5.sound.min.js"]
        assert scores["lib/addons/p5.sound.min.js"] < 0

        first_round = result["dynamic_rounds"][0]
        pool = first_round["frontier_state"]["candidate_pool"]
        assert pool["source_first_policy"]["enabled"] is True
        assert pool["flow_execution"]["mode"] == "light"
        assert pool["flow_execution"]["deep_flow_enabled"] is False
        assert first_round["stop_decision"]["reason"] in {
            "high_confidence_source_candidate",
            "max_rounds_reached",
            "top3_stable_and_no_new_frontier",
            "no_new_queries",
            "continue_with_new_frontier",
        }

        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        phases = {event["phase"] for event in events}
        assert "dynamic.scope_prefilter" in phases
        assert "dynamic.cheap_recall" in phases
        assert "dynamic.TraceFlow" in phases
        assert any(event.get("backend") == "trace_program_flows" for event in events)
        assert state_path.exists()
