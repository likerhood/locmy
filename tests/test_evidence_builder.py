from pathlib import Path

from mycode.data.dataset_loader import NormalizedSample
from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_builder import build_evidence_sketch
from mycode.evidence.evidence_agent import build_evidence_packet


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"


def test_build_evidence_sketch_classifies_code_and_image_urls():
    sample = NormalizedSample(
        instance_id="repo__case-1",
        repo="owner/repo",
        dataset="unit",
        issue_text=(
            "Click submit and redirect via `getEditURL`. "
            "See https://github.com/owner/repo/blob/main/src/url.js#L10 "
            "and screenshot https://user-images.githubusercontent.com/a/b/c.png"
        ),
        raw={"image_urls": ["https://user-images.githubusercontent.com/a/b/c.png"]},
        gold_files=["src/url.js"],
    )

    sketch = build_evidence_sketch(sample)

    assert sketch.instance_id == "repo__case-1"
    assert sketch.urls[0].url_type == "github_code"
    assert sketch.images[0].image_type in {"web_ui_screenshot", "generic_visual"}
    assert "url_builder_or_route_flow" in sketch.flow_hypotheses
    assert "getEditURL" in sketch.symbol_queries


def test_build_evidence_sketch_uses_problem_statement_only():
    sample = NormalizedSample(
        instance_id="repo__case-2",
        repo="owner/repo",
        dataset="unit",
        issue_text="Plain issue body without links.",
        raw={
            "web_urls": ["https://github.com/owner/repo/pull/123"],
            "image_urls": ["https://user-images.githubusercontent.com/a/b/leak.png"],
            "hints_text": "https://github.com/owner/repo/commit/abc",
        },
        gold_files=["src/url.js"],
    )

    sketch = build_evidence_sketch(sample)

    assert sketch.urls == []
    assert sketch.images == []


def test_evidence_packet_parses_reproduction_and_code_seed():
    sample = NormalizedSample(
        instance_id="repo__case-3",
        repo="python/mypy",
        dataset="unit",
        issue_text=(
            "The repro is at "
            "https://mypy-play.net/?mypy=latest&python=3.10&flags=strict&code=print%28Foo%29 "
            "and related code is "
            "https://github.com/python/mypy/blob/master/mypy/binder.py#L100. "
            "Screenshot: https://user-images.githubusercontent.com/a/b/repro.png"
        ),
        raw={
            "hints_text": "https://github.com/python/mypy/pull/999",
            "web_urls": ["https://github.com/python/mypy/commit/leak"],
        },
        gold_files=["mypy/binder.py"],
    )

    packet = build_evidence_packet(sample)

    assert packet.modality == "image_and_url"
    assert len(packet.reproduction_cases) == 1
    assert packet.reproduction_cases[0]["platform"] == "mypy_play"
    assert packet.reproduction_cases[0]["config"]["python"] == "3.10"
    assert packet.code_references[0]["path"] == "mypy/binder.py"
    assert packet.code_references[0]["localization_use"] == "use_as_seed_not_target"
    assert packet.leakage == []
    assert any(step["stage"] == "reproduction_understanding" for step in packet.search_plan)


def _load_chartjs_10301_sample() -> NormalizedSample:
    if SWE_CLEAN15.exists():
        for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
            if sample.instance_id == "chartjs__Chart.js-10301":
                return sample

    return NormalizedSample(
        instance_id="chartjs__Chart.js-10301",
        repo="chartjs/Chart.js",
        dataset="swe_clean15",
        issue_text=(
            "Legend event onLeave. In the example at "
            "https://www.chartjs.org/docs/latest/samples/legend/events.html "
            "you can hover over a legend. If you quickly place the mouse outside "
            "the chart, content sometimes remains highlighted because onLeave is "
            "not called. "
            "![image](https://user-images.githubusercontent.com/58777964/157239796-95ccabbb-7ac1-4e58-89ca-c902b1df0dfe.png) "
            "I added console.log to onHover and onLeave. "
            "![image](https://user-images.githubusercontent.com/58777964/157240018-395c6e62-d8e3-431f-8926-7644d5441078.png) "
            "Reproducible sample: "
            "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx "
            "Drag the mouse between one of the legends and then up to the next. "
            "![image](https://user-images.githubusercontent.com/58777964/157241538-f55bf466-916f-4763-b0ea-ef78ef847127.png)"
        ),
        raw={},
        gold_files=["src/plugins/plugin.legend.js"],
    )


def test_chartjs_10301_clean15_evidence_packet_parses_docs_repro_and_images():
    sample = _load_chartjs_10301_sample()

    packet = build_evidence_packet(sample)

    assert packet.instance_id == "chartjs__Chart.js-10301"
    assert packet.repo == "chartjs/Chart.js"
    assert packet.metadata["problem_statement_only"] is True
    assert "gold_files" not in packet.metadata
    assert packet.modality == "image_and_url"

    urls = {item["url"]: item for item in packet.url_inspections}
    docs_url = "https://www.chartjs.org/docs/latest/samples/legend/events.html"
    sandbox_url = "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx"
    assert docs_url in urls
    assert sandbox_url in urls
    assert urls[docs_url]["kind"] == "docs_sample"
    assert urls[docs_url]["role"] == "reproduction_entry"
    assert urls[docs_url]["localization_use"] == "extract_docs_sample_api_behavior_and_event_config"
    assert urls[sandbox_url]["kind"] == "playground"
    assert urls[sandbox_url]["role"] == "reproduction_entry"

    platforms = {case["platform"]: case for case in packet.reproduction_cases}
    assert "chartjs_docs_sample" in platforms
    assert "codesandbox" in platforms
    assert "legend_plugin" in platforms["chartjs_docs_sample"]["likely_layers"]
    assert "event_handler" in platforms["chartjs_docs_sample"]["likely_layers"]
    assert platforms["codesandbox"]["query_fields"] == ["file"]
    assert platforms["codesandbox"]["semantic_queries"][0] == "codesandbox file /src/App.tsx"
    assert "codesandbox file /src/App.tsx" in platforms["codesandbox"]["semantic_queries"]

    assert len(packet.image_inspections) == 3
    assert all(image["needs_vlm"] for image in packet.image_inspections)
    assert any("legend" in image["visual_queries"] for image in packet.image_inspections)

    assert "render_style_pipeline_flow" not in packet.flow_hypotheses
    assert "ui_event_flow" not in packet.flow_hypotheses
    assert any(step["stage"] == "reproduction_understanding" for step in packet.search_plan)
    assert any(step["stage"] == "behavior_to_code_layer" for step in packet.search_plan)
    assert any(step["stage"] == "visual_to_program_layer" for step in packet.search_plan)
    assert packet.leakage == []
