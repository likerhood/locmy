from __future__ import annotations

from argparse import Namespace

from newtest.run_clean15_dataset import KS, _flatten_metrics, _summarize, _write_summary_md


def test_clean15_report_recomputes_complete_three_level_metrics(tmp_path):
    result = {
        "instance_id": "repo__case-1",
        "repo": "repo/case",
        "dataset": "unit-clean15",
        "gold_files": ["src/fix.py"],
        "evaluation_3level": {
            "gold": {
                "files": ["src/fix.py"],
                "modules": ["src/fix.py::module:__file__"],
                "functions": ["src/fix.py::function:target"],
            },
            # Simulate an older result that did not persist every k value.
            "file": {"acc@1": 0.0, "gold_count": 1.0, "pred_count": 2.0},
            "module": {"gold_count": 1.0, "pred_count": 1.0},
            "function": {"gold_count": 1.0, "pred_count": 1.0},
        },
        "localization": {
            "ranked_locations": [{"path": "src/other.py"}, {"path": "src/fix.py"}],
            "ranked_modules": [{"id": "src/fix.py::module:__file__"}],
            "ranked_functions": [{"id": "src/fix.py::function:target"}],
        },
        "evidence": {},
    }

    row = _flatten_metrics(result)
    assert KS == tuple(range(1, 16))
    assert row["file_acc@1"] == 0.0
    assert row["file_acc@2"] == 1.0
    assert row["file_strict_acc@2"] == 1.0
    assert row["module_acc@1"] == 1.0
    assert row["function_acc@1"] == 1.0

    summary = _summarize([row], [], elapsed=1.23)
    output = tmp_path / "metrics_3level.md"
    _write_summary_md(
        output,
        summary,
        Namespace(
            dataset="unit-clean15",
            samples="samples.jsonl",
            output_dir=str(tmp_path),
            top_k=15,
            dynamic_rounds=1,
            max_tool_rounds=1,
            max_react_steps=2,
            use_llm=True,
            use_llm_planning=True,
            use_llm_controller=True,
            allow_network=False,
            allow_browser=False,
            use_vlm=False,
            sample_timeout_seconds=900,
            structure_only=False,
            lightweight=False,
        ),
    )
    text = output.read_text(encoding="utf-8")
    assert "# Three-Level Localization Metrics" in text
    assert "Acc@2" in text
    assert "Strict Acc@15" in text
    assert "Recall@15" in text
    assert "## Set Metrics @All" in text
    assert "| File | 0.00 | 100.00" in text
