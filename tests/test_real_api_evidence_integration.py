from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.evidence_agent import build_evidence_packet
from mycode.evidence.llm_evidence_agent import analyze_evidence_with_llm
from mycode.evidence.tools.vlm_image_reader import try_analyze_image_with_vlm
from mycode.evidence.understanding_agent import run_evidence_understanding
from mycode.utils.env import llm_config_from_env


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
OUT_DIR = ROOT / "outputs" / "tests" / "real_api"


def _require_real_api_config() -> dict[str, str]:
    config = llm_config_from_env()
    missing = [key for key in ("base_url", "api_key", "model_api_name") if not config.get(key)]
    if missing:
        raise RuntimeError(
            "Real API integration test requires BASE_URL, API_KEY, MODEL_NAME or MODEL_API_NAME. "
            "Set them in the shell or in .env.local. Missing: " + ", ".join(missing)
        )
    return config


def _load_chartjs_10301():
    for sample in load_samples(SWE_CLEAN15, dataset="swe_clean15"):
        if sample.instance_id == "chartjs__Chart.js-10301":
            return sample
    raise AssertionError(f"chartjs__Chart.js-10301 not found in {SWE_CLEAN15}")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_chart_like_png(path: Path) -> None:
    width, height = 160, 100
    rows = []
    colors = [
        (255, 255, 255),
        (214, 67, 54),
        (38, 112, 150),
        (247, 200, 30),
        (45, 175, 100),
    ]
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if y < 18:
                rgb = (245, 245, 245)
            elif 35 < y < 85 and 20 < x < 145:
                idx = min(4, (x - 20) // 28 + 1)
                rgb = colors[idx]
            else:
                rgb = colors[0]
            row.extend(rgb)
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def test_real_llm_evidence_and_vlm_image_analysis():
    config = _require_real_api_config()
    sample = _load_chartjs_10301()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    packet = build_evidence_packet(sample)
    llm_analysis = analyze_evidence_with_llm(packet)
    llm_path = OUT_DIR / "chartjs_10301_llm_evidence.json"
    llm_path.write_text(json.dumps(llm_analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    text = json.dumps(llm_analysis, ensure_ascii=False).lower()
    assert "legend" in text
    assert "chart" in text
    assert llm_analysis.get("_model") or config["model_api_name"]

    image_path = OUT_DIR / "chart_like.png"
    _write_chart_like_png(image_path)
    vlm_analysis = try_analyze_image_with_vlm(
        image_path=image_path,
        image_format="png",
        issue_summary="Chart.js legend hover/onLeave visual behavior is wrong in this reproduction.",
        repo="chartjs/Chart.js",
        max_tokens=600,
    )
    vlm_path = OUT_DIR / "chart_like_vlm_analysis.json"
    vlm_path.write_text(json.dumps(vlm_analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    assert vlm_analysis.get("vlm_status") == "ok", vlm_analysis
    assert json.dumps(vlm_analysis, ensure_ascii=False).strip()


def test_real_llm_evidence_agent_tool_planning_trace():
    _require_real_api_config()
    sample = _load_chartjs_10301()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result = run_evidence_understanding(
        sample,
        use_llm=True,
        execute_tools=True,
        cache_dir=str(OUT_DIR / "tool_cache"),
        allow_network=False,
        allow_browser=False,
        download_images=False,
        use_vlm=False,
        max_tool_rounds=3,
    )
    output = OUT_DIR / "chartjs_10301_real_agent_trace.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    assert result["llm_used"] is True
    assert result["problem_statement_only"] is True
    assert result["agent_rounds"]
    assert result["agent_rounds"][0].get("stop_decision")


if __name__ == "__main__":
    test_real_llm_evidence_and_vlm_image_analysis()
    test_real_llm_evidence_agent_tool_planning_trace()
    print(f"PASS real API evidence integration -> {OUT_DIR}")
