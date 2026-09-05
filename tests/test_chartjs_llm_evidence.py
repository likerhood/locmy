from __future__ import annotations

import json
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_agent import build_evidence_packet
from mycode.evidence.llm_evidence_agent import analyze_evidence_with_llm


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
OUT = ROOT / "outputs" / "tests" / "chartjs_10301_llm_analysis.json"


def load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError(f"chartjs__Chart.js-10301 not found in {SWE_CLEAN15}")


def test_chartjs_10301_llm_evidence_analysis():
    packet = build_evidence_packet(load_chartjs_10301())
    analysis = analyze_evidence_with_llm(packet)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    text = json.dumps(analysis, ensure_ascii=False).lower()
    assert "legend" in text
    assert "onleave" in text or "leave" in text
    assert "codesandbox" in text
    assert "plugin.legend" in text or "legend_plugin" in text


if __name__ == "__main__":
    test_chartjs_10301_llm_evidence_analysis()
    print(f"PASS chartjs__Chart.js-10301 LLM evidence analysis -> {OUT}")
