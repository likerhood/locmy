from __future__ import annotations

import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.agents.planning_agent import plan_evidence_collection
from mycode.evidence.understanding_agent import run_evidence_understanding
from mycode.evidence.tools.llm_client import LLMClientError


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
OUT = ROOT / "outputs" / "tests" / "chartjs_10301_first_step_understanding.json"


def _load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError(f"chartjs__Chart.js-10301 not found in {SWE_CLEAN15}")


def test_chartjs_first_step_without_llm_builds_actionable_tool_plan():
    result = run_evidence_understanding(_load_chartjs_10301(), use_llm=False)

    assert result["stage"] == "evidence_understanding"
    assert result["problem_statement_only"] is True
    assert result["llm_used"] is False
    assert result["collection_plan"]["metadata"]["problem_statement_only"] is True

    packet = result["evidence_packet"]
    cases = {case["platform"]: case for case in packet["reproduction_cases"]}
    assert "codesandbox" in cases
    assert cases["codesandbox"]["browser_observation_plan"]["requested_file"] == "/src/App.tsx"
    assert cases["codesandbox"]["browser_observation_plan"]["preview_url"] == "https://3kw5p0.csb.app/"

    pending = result["pending_tool_plan"]
    assert any(item["tool"] == "browser_reproduction_reader" for item in pending)
    assert any(item["tool"] == "vlm_image_inspector" for item in pending)


def test_chartjs_planning_agent_selects_tools_before_tool_execution():
    plan = plan_evidence_collection(_load_chartjs_10301(), use_llm=False)
    data = plan.to_dict()

    assert data["metadata"]["problem_statement_only"] is True
    tools = {request["tool"] for request in data["tool_requests"]}
    assert "browser_reproduction_reader" in tools
    assert "playground_decoder" in tools or "browser_reproduction_reader" in tools
    assert "vlm_image_inspector" in tools

    browser_requests = [
        request for request in data["tool_requests"]
        if request["tool"] == "browser_reproduction_reader"
    ]
    assert browser_requests
    assert browser_requests[0]["parameters"]["browser_observation_plan"]["requested_file"] == "/src/App.tsx"
    assert browser_requests[0]["parameters"]["browser_observation_plan"]["preview_url"] == "https://3kw5p0.csb.app/"
    assert "reproduction_code_is_not_target_repository_code" in browser_requests[0]["caution"]


def test_chartjs_first_step_with_llm_generates_issue_understanding():
    result = run_evidence_understanding(_load_chartjs_10301(), use_llm=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    assert result["llm_used"] is True
    assert "gold_files" not in result["evidence_packet"]["metadata"]

    text = json.dumps(result["llm_understanding"], ensure_ascii=False).lower()
    assert "legend" in text
    assert "onleave" in text or "leave" in text
    assert "codesandbox" in text
    assert "gold_files" not in text


def test_evidence_understanding_falls_back_when_final_llm_call_fails(monkeypatch):
    def fail_understanding(*args, **kwargs):
        raise LLMClientError("HTTP 500: temporary provider failure")

    monkeypatch.setattr(
        "mycode.evidence.understanding_agent.analyze_evidence_with_llm",
        fail_understanding,
    )
    result = run_evidence_understanding(
        _load_chartjs_10301(),
        use_llm=True,
        use_llm_planning=False,
    )

    assert result["llm_used"] is True
    assert result["llm_status"] == "fallback"
    assert result["llm_understanding"]["_fallback"] == "deterministic_evidence_analysis"
    assert result["llm_understanding"]["issue_sketch"]["concern"]
    assert "HTTP 500" in result["llm_error"]


if __name__ == "__main__":
    test_chartjs_first_step_without_llm_builds_actionable_tool_plan()
    test_chartjs_planning_agent_selects_tools_before_tool_execution()
    test_chartjs_first_step_with_llm_generates_issue_understanding()
    print(f"PASS chartjs first-step evidence understanding -> {OUT}")
