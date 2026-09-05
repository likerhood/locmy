from __future__ import annotations

import json
from pathlib import Path

from mycode.agent.pipeline import run_localization_pipeline
from mycode.data.dataset_loader import load_samples
from mycode.utils.env import llm_config_from_env


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
OUT_DIR = ROOT / "outputs" / "tests" / "real_llm_dynamic"


def _require_real_api_config() -> dict[str, str]:
    config = llm_config_from_env()
    missing = [key for key in ("base_url", "api_key", "model_api_name") if not config.get(key)]
    if missing:
        raise RuntimeError(
            "Real LLM dynamic localization test requires BASE_URL, API_KEY, MODEL_NAME or MODEL_API_NAME. "
            "Set them in the shell or in .env.local. Missing: " + ", ".join(missing)
        )
    return config


def _load_sample(instance_id: str):
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == instance_id:
            return sample
    raise AssertionError(f"{instance_id} not found in {SWE_CLEAN15}")


def test_real_llm_chartjs_10301_dynamic_localization_pipeline():
    config = _require_real_api_config()
    sample = _load_sample("chartjs__Chart.js-10301")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    result = run_localization_pipeline(
        sample,
        use_llm=True,
        use_llm_planning=True,
        use_llm_controller=True,
        execute_tools=True,
        allow_network=False,
        allow_browser=False,
        download_images=False,
        use_vlm=False,
        max_tool_rounds=3,
        dynamic_rounds=3,
        cache_dir=str(OUT_DIR / "tool_cache"),
        top_k=15,
    )

    output = OUT_DIR / "chartjs_10301_qwen_real_llm_dynamic_pipeline.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    top_files = [item["path"] for item in result["localization"].get("ranked_locations", [])[:5]]
    top_functions = result["localization"].get("ranked_functions", [])[:15]
    evidence = result["evidence"]
    rounds = result["localization"].get("dynamic_rounds", [])
    three = result.get("evaluation_3level", {})
    controller_sources = [
        action.get("controller_decision", {}).get("source")
        for round_item in rounds
        for action in round_item.get("agent_actions", [])
        if action.get("action") == "controller_decision"
    ]

    assert evidence["llm_used"] is True
    assert result["llm_controller_used"] is True
    assert evidence["problem_statement_only"] is True
    assert evidence["agent_rounds"]
    assert rounds
    assert any(source in {"llm", "fallback", "heuristic_support"} for source in controller_sources)
    assert "src/plugins/plugin.legend.js" in top_files
    assert any(item.get("path") == "src/plugins/plugin.legend.js" for item in top_functions)
    assert result["evaluation"]["acc@5"] == 1.0
    assert three.get("file", {}).get("acc@5") == 1.0
    assert three.get("function", {}).get("recall@15", 0) > 0
    assert config["model_api_name"]


if __name__ == "__main__":
    test_real_llm_chartjs_10301_dynamic_localization_pipeline()
    print(f"PASS real LLM dynamic localization pipeline -> {OUT_DIR}")
