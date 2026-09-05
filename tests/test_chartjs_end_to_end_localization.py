from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.agent.pipeline import run_localization_pipeline
from mycode.data.dataset_loader import load_samples


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"


def _load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError("chartjs__Chart.js-10301 not found")


def test_chartjs_10301_end_to_end_localizes_legend_plugin_without_llm():
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

    ranked = [item["path"] for item in result["localization"]["ranked_locations"]]
    assert "src/plugins/plugin.legend.js" in ranked[:5]
    assert result["evaluation"]["acc@5"] == 1.0
    assert result["evaluation"]["recall@15"] == 1.0


if __name__ == "__main__":
    test_chartjs_10301_end_to_end_localizes_legend_plugin_without_llm()
    print("PASS chartjs end-to-end localization")
