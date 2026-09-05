from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.agents.planning_agent import plan_evidence_collection
from mycode.evidence.runtime.tool_executor import execute_collection_plan
from mycode.evidence.understanding_agent import run_evidence_understanding


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"


def _load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError(f"chartjs__Chart.js-10301 not found in {SWE_CLEAN15}")


def test_tool_execution_layer_runs_offline_and_preserves_reproduction_plan():
    sample = _load_chartjs_10301()
    plan = plan_evidence_collection(sample, use_llm=False)
    with tempfile.TemporaryDirectory() as temp_dir:
        observations = execute_collection_plan(
            plan,
            cache_dir=temp_dir,
            allow_network=False,
            allow_browser=False,
            download_images=False,
        )

    assert observations
    by_tool = {}
    for observation in observations:
        by_tool.setdefault(observation.tool, []).append(observation)

    assert "browser_reproduction_reader" in by_tool
    browser_obs = by_tool["browser_reproduction_reader"][0]
    parsed = browser_obs.extracted["parsed_reproduction"]
    assert parsed["platform"] == "codesandbox"
    assert parsed["browser_observation_plan"]["requested_file"] == "/src/App.tsx"
    assert parsed["browser_observation_plan"]["preview_url"] == "https://3kw5p0.csb.app/"
    assert browser_obs.status == "parsed_needs_browser_or_network"

    assert "playground_decoder" in by_tool
    assert by_tool["playground_decoder"][0].extracted["platform"] == "chartjs_docs_sample"

    assert "vlm_image_inspector" in by_tool
    assert by_tool["vlm_image_inspector"][0].status == "missing"


def test_understanding_agent_can_include_tool_observations_without_llm():
    sample = _load_chartjs_10301()
    with tempfile.TemporaryDirectory() as temp_dir:
        result = run_evidence_understanding(
            sample,
            use_llm=False,
            execute_tools=True,
            cache_dir=temp_dir,
            allow_network=False,
            download_images=False,
        )

    assert result["tools_executed"] is True
    assert result["llm_used"] is False
    assert result["tool_observations"]
    assert any(item["tool"] == "browser_reproduction_reader" for item in result["tool_observations"])
    assert "gold_files" not in str(result["collection_plan"]["tool_requests"]).lower()


if __name__ == "__main__":
    test_tool_execution_layer_runs_offline_and_preserves_reproduction_plan()
    test_understanding_agent_can_include_tool_observations_without_llm()
    print("PASS tool execution layer")
