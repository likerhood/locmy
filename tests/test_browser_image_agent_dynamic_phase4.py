from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

from mycode.agent.pipeline import run_localization_pipeline
from mycode.data.dataset_loader import load_samples
from mycode.evidence import image_batch
from mycode.evidence.tools import browser_reproduction_reader
from mycode.evidence.tools.browser_reproduction_reader import read_browser_reproduction
from mycode.evidence.tools.vlm_image_reader import normalize_vlm_analysis
from mycode.evidence.understanding_agent import run_evidence_understanding
from mycode.schemas.evidence import NormalizedSample


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError("chartjs__Chart.js-10301 not found")


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": {
                "directories": [
                    {"shortid": "srcid", "title": "src", "directory_shortid": None},
                ],
                "modules": [
                    {
                        "id": "app",
                        "title": "App.tsx",
                        "directory_shortid": "srcid",
                        "code": (
                            "import { Chart } from 'react-chartjs-2';\n"
                            "function handleHover(evt, item, legend) { legend.chart.update(); }\n"
                            "function handleLeave(evt, item, legend) { legend.chart.update(); }\n"
                            "export default function App() { return <Chart />; }\n"
                        ),
                    },
                    {
                        "id": "pkg",
                        "title": "package.json",
                        "directory_shortid": None,
                        "code": json.dumps({"dependencies": {"chart.js": "3.7.0", "react-chartjs-2": "4.0.0"}}),
                    },
                ],
            }
        }


def test_codesandbox_reader_merges_api_files_browser_console_and_interactions(monkeypatch):
    def fake_get(*args, **kwargs):
        return _FakeResponse()

    def fake_browser(url, *, cache_dir, timeout_ms, interaction_tasks=None):
        screenshot = Path(cache_dir) / "preview.png"
        after = Path(cache_dir) / "preview_after_interactions.png"
        html = Path(cache_dir) / "page.html"
        screenshot.write_bytes(PNG_1X1)
        after.write_bytes(PNG_1X1)
        html.write_text("<html><body>Legend hover reproduction</body></html>", encoding="utf-8")
        return {
            "status": "ok",
            "page_title": "Sandbox",
            "visible_text_preview": "Legend hover reproduction",
            "console_logs": ["log: enter src/plugins/plugin.legend.js:123 in handleHover", "log: leave"],
            "page_errors": [],
            "screenshot": str(screenshot),
            "after_interaction_screenshot": str(after),
            "html_path": str(html),
            "file_tree_candidates": ["src/App.tsx", "src/index.tsx", "package.json"],
            "dom_source_files": [
                {
                    "path": "src/App.tsx",
                    "source": "browser_monaco_model",
                    "bytes": 156,
                    "code_preview": "function handleHover(evt, item, legend) { legend.chart.update(); }",
                }
            ],
            "full_dom_source_files": [
                {
                    "path": "src/App.tsx",
                    "source": "browser_monaco_model",
                    "bytes": 156,
                    "code": "function handleHover(evt, item, legend) { legend.chart.update(); }\n",
                    "code_preview": "function handleHover(evt, item, legend) { legend.chart.update(); }",
                }
            ],
            "preview_frames": [
                {
                    "url": "https://3kw5p0.csb.app/",
                    "visible_text_preview": "Legend hover reproduction chart",
                }
            ],
            "interaction_trace": [{"action": "mouse_move", "x": 720, "y": 500}],
        }

    monkeypatch.setattr(browser_reproduction_reader.requests, "get", fake_get)
    monkeypatch.setattr(browser_reproduction_reader, "_playwright_available", lambda: True)
    monkeypatch.setattr(browser_reproduction_reader, "_read_with_playwright", fake_browser)

    with tempfile.TemporaryDirectory() as temp_dir:
        result = read_browser_reproduction(
            "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0?file=/src/App.tsx",
            cache_dir=temp_dir,
            allow_network=True,
            allow_browser=True,
        )
        cached = read_browser_reproduction(
            "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0?file=/src/App.tsx",
            cache_dir=temp_dir,
            allow_network=False,
            allow_browser=False,
        )

    assert result["status"] == "ok"
    assert "src/App.tsx" in result["file_tree"]
    assert result["requested_source_file"]["path"] == "src/App.tsx"
    assert "chart.js" in result["semantic_queries"]
    assert result["live_browser"]["console_logs"] == [
        "log: enter src/plugins/plugin.legend.js:123 in handleHover",
        "log: leave",
    ]
    assert "runtime_trace" in result
    assert "src/plugins/plugin.legend.js" in result["runtime_trace"]["trace"]
    assert result["live_browser"]["interaction_trace"]
    assert result["browser_capabilities"]["browser_file_tree"] is True
    assert result["browser_capabilities"]["active_editor_text"] is True
    assert result["browser_capabilities"]["preview_visible_text"] is True
    written_paths = {item["path"] for item in result["source_files_written"]}
    assert "src/App.tsx" in written_paths
    assert cached["cache_hit"] is True


def test_image_batch_caches_problem_statement_images(monkeypatch):
    sample = NormalizedSample(
        instance_id="demo__repo-1",
        repo="demo/repo",
        dataset="unit",
        issue_text="Chart screenshot: https://example.com/screenshot.png",
        raw={},
        gold_files=[],
    )

    def fake_read_image_asset(url, *, cache_dir, download=True, timeout=30):
        local = Path(cache_dir) / "screenshot.png"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(PNG_1X1)
        return {
            "url": url,
            "status": "ok",
            "local_path": str(local),
            "bytes": len(PNG_1X1),
            "format": "png",
            "dimensions": {"width": 1, "height": 1},
            "processable": True,
            "needs_vlm": True,
        }

    monkeypatch.setattr(image_batch, "read_image_asset", fake_read_image_asset)
    with tempfile.TemporaryDirectory() as temp_dir:
        cache = Path(temp_dir) / "images.jsonl"
        summary = image_batch.build_image_understanding_batch(
            [sample],
            cache_path=cache,
            asset_cache_dir=Path(temp_dir) / "assets",
            allow_network=False,
            use_vlm=False,
        )
        reused = image_batch.build_image_understanding_batch(
            [sample],
            cache_path=cache,
            asset_cache_dir=Path(temp_dir) / "assets",
            allow_network=False,
            use_vlm=False,
        )
        validation = image_batch.validate_image_understanding_cache(
            [sample],
            cache_path=cache,
            asset_cache_dir=Path(temp_dir) / "assets",
            require_vlm=False,
        )

    assert summary["expected_images"] == 1
    assert summary["processed"] == 1
    assert summary["failed"] == 0
    assert summary["complete_instances"] == 1
    assert summary["image_type_counts"]["chart_or_canvas_render"] == 1
    assert summary["vlm_status_counts"]["heuristic_only"] == 1
    assert summary["sample_image_records"][0]["search_queries"]
    assert reused["reused"] == 1
    assert validation["expected_images"] == 1
    assert validation["cached_images"] == 1
    assert validation["complete_for_cached_processable_images"] is True


def test_vlm_analysis_normalizer_backfills_queries_and_schema():
    normalized = normalize_vlm_analysis(
        {
            "visible_text": "Legend hover is wrong",
            "symptom": "hover interaction does not reset legend colors",
            "likely_code_layers": ["legend plugin", "event handler"],
        },
        image_format="png",
        issue_summary="Chart.js legend hover/onLeave color reset regression",
        repo="chartjs/Chart.js",
        local_path="/tmp/chart.png",
    )

    assert normalized["schema_version"] == "vlm_image_understanding.v2"
    assert normalized["image_format"] == "png"
    assert normalized["local_path"] == "/tmp/chart.png"
    assert normalized["search_queries"]
    assert "search_queries_backfilled_from_heuristic" in normalized["cautions"]


def test_evidence_agent_records_round_stop_decisions_without_llm():
    sample = _load_chartjs_10301()
    with tempfile.TemporaryDirectory() as temp_dir:
        result = run_evidence_understanding(
            sample,
            use_llm=False,
            execute_tools=True,
            cache_dir=temp_dir,
            allow_network=False,
            allow_browser=False,
            download_images=False,
            max_tool_rounds=3,
        )

    assert result["agent_rounds"]
    assert "reasoning_trace" in result["agent_rounds"][0]
    assert "stop_decision" in result["agent_rounds"][0]
    assert result["problem_statement_only"] is True


def test_dynamic_localization_outputs_trace_flow_and_verifier():
    sample = _load_chartjs_10301()
    with tempfile.TemporaryDirectory() as temp_dir:
        result = run_localization_pipeline(
            sample,
            use_llm=False,
            execute_tools=True,
            cache_dir=temp_dir,
            download_images=False,
            top_k=15,
        )

    localization = result["localization"]
    ranked = [item["path"] for item in localization["ranked_locations"]]
    assert "src/plugins/plugin.legend.js" in ranked[:5]
    assert localization["search_trace"]
    assert localization["code_contexts"]
    assert localization["flow_traces"]
    assert localization["verifier"]["decisions"]
    assert result["evaluation"]["acc@5"] == 1.0


class _SimpleMonkeyPatch:
    def setattr(self, target, name, value):
        setattr(target, name, value)


if __name__ == "__main__":
    monkeypatch = _SimpleMonkeyPatch()
    test_codesandbox_reader_merges_api_files_browser_console_and_interactions(monkeypatch)
    test_image_batch_caches_problem_statement_images(monkeypatch)
    test_vlm_analysis_normalizer_backfills_queries_and_schema()
    test_evidence_agent_records_round_stop_decisions_without_llm()
    test_dynamic_localization_outputs_trace_flow_and_verifier()
    print("PASS browser/image/agent/dynamic phase4")
