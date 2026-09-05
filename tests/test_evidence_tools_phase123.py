from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.evidence.tools import browser_reproduction_reader
from mycode.evidence.tools.vlm_image_reader import heuristic_image_understanding
from mycode.evidence.understanding_agent import run_evidence_understanding
from mycode.schemas.evidence import NormalizedSample


CODESANDBOX_URL = "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0?file=/src/App.tsx"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_codesandbox_api_reader_extracts_files_dependencies_and_code_features() -> None:
    payload = {
        "data": {
            "directories": [
                {"shortid": "srcdir", "title": "src"},
            ],
            "modules": [
                {
                    "id": "app",
                    "title": "App.tsx",
                    "directory_shortid": "srcdir",
                    "code": (
                        "import { Chart, registerables } from 'chart.js';\n"
                        "import { Chart as ReactCharts } from 'react-chartjs-2';\n"
                        "function handleHover(evt, item, legend) { legend.chart.update(); }\n"
                        "const options = { plugins: { legend: { onHover: handleHover } } };\n"
                    ),
                },
                {
                    "id": "pkg",
                    "title": "package.json",
                    "code": '{"dependencies":{"chart.js":"3.7.0","react-chartjs-2":"4.0.0"}}',
                },
            ],
        }
    }

    old_get = browser_reproduction_reader.requests.get
    browser_reproduction_reader.requests.get = lambda *args, **kwargs: _FakeResponse(payload)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = browser_reproduction_reader.read_browser_reproduction(
                CODESANDBOX_URL,
                cache_dir=temp_dir,
                allow_network=True,
                allow_browser=False,
            )
            source_root = Path(temp_dir) / "source_files"
            app_written = (source_root / "src" / "App.tsx").exists()
    finally:
        browser_reproduction_reader.requests.get = old_get

    assert result["status"] == "ok"
    assert result["requested_source_file"]["path"] == "src/App.tsx"
    assert "chart.js" in result["code_features"]["imports"]
    assert "handleHover" in result["code_features"]["event_terms"]
    assert {"name": "chart.js", "version": "3.7.0", "section": "dependencies"} in result["package_dependencies"]
    assert app_written


def test_heuristic_image_understanding_is_explicitly_not_pixel_reading() -> None:
    result = heuristic_image_understanding(
        image_format="png",
        issue_summary="Chart legend onLeave does not restore the pie slice color.",
        repo="chartjs/Chart.js",
    )

    assert result["vlm_status"] == "heuristic_only"
    assert "chart_plugin" in result["likely_code_layers"]
    assert "plugin.legend" in result["search_queries"]
    assert "heuristic_image_understanding_does_not_read_pixels" in result["cautions"]


def test_evidence_understanding_runs_second_round_llm_followup_tool() -> None:
    sample = NormalizedSample(
        instance_id="toy__docs-1",
        repo="toy/docs",
        dataset="toy",
        issue_text="The API option `fooMode` is documented at https://example.com/docs/api/foo but behaves differently.",
        raw={"problem_statement": "The API option `fooMode` is documented at https://example.com/docs/api/foo but behaves differently."},
    )

    from mycode.evidence import understanding_agent

    def fake_chat_completion(messages, **kwargs):
        text = messages[-1]["content"]
        if "tool_observations" in text:
            return {
                "id": "followup",
                "model": "fake",
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"additional_tool_requests":[{'
                                '"tool":"web_snapshot_fetcher",'
                                '"source":"https://example.com/docs/api/foo",'
                                '"evidence_type":"docs_url",'
                                '"role_hypothesis":"spec_or_api_semantics",'
                                '"priority":"medium",'
                                '"reason":"read page snapshot after docs planning"}],'
                                '"reasoning":"need snapshot","stop_reason":"one_followup"}'
                            )
                        }
                    }
                ],
            }
        return {
            "id": "planning",
            "model": "fake",
            "choices": [{"message": {"content": '{"issue_summary":"toy"}'}}],
        }

    old_chat = understanding_agent.chat_completion
    understanding_agent.chat_completion = fake_chat_completion
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_evidence_understanding(
                sample,
                use_llm=True,
                use_llm_planning=False,
                execute_tools=True,
                cache_dir=temp_dir,
                allow_network=False,
                allow_browser=False,
                download_images=False,
                max_tool_rounds=2,
            )
    finally:
        understanding_agent.chat_completion = old_chat

    assert len(result["agent_rounds"]) == 2
    assert any(obs["tool"] == "web_snapshot_fetcher" for obs in result["tool_observations"])
    assert result["evidence_synthesis"]["problem_statement_only"] is True


if __name__ == "__main__":
    test_codesandbox_api_reader_extracts_files_dependencies_and_code_features()
    test_heuristic_image_understanding_is_explicitly_not_pixel_reading()
    test_evidence_understanding_runs_second_round_llm_followup_tool()
    print("PASS evidence tools phase 1/2/3")
