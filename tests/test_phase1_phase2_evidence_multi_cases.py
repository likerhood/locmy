from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.data.dataset_loader import load_samples
from mycode.evidence.understanding_agent import run_evidence_understanding


ROOT = Path(__file__).resolve().parents[1]
SWE_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "swebench_multimodal-full-dev.clean15.samples.jsonl"
OMNI_CLEAN15 = ROOT.parent.parent / "clean_subsets_new" / "omnigirl-full-candidates.clean15.v458.samples.jsonl"


def _sample(path: Path, dataset: str, instance_id: str):
    for item in load_samples(path, dataset=dataset):
        if item.instance_id == instance_id:
            return item
    raise AssertionError(f"{instance_id} not found in {path}")


def _understand(path: Path, dataset: str, instance_id: str):
    sample = _sample(path, dataset, instance_id)
    with tempfile.TemporaryDirectory() as temp_dir:
        return run_evidence_understanding(
            sample,
            use_llm=False,
            execute_tools=True,
            cache_dir=temp_dir,
            allow_network=False,
            allow_browser=False,
            download_images=False,
        )


def test_automattic_21409_keeps_code_url_as_evidence_seed_not_gold_target():
    result = _understand(SWE_CLEAN15, "swe_clean15", "Automattic__wp-calypso-21409")
    synthesis = result["evidence_synthesis"]

    assert synthesis["problem_statement_only"] is True
    assert synthesis["url_role_counts"]["code_evidence_seed"] == 1
    assert "client/state/current-user/selectors.js" in synthesis["query_groups"]["url_seed"]
    assert any(hint["kind"] == "code_url_seed" for hint in synthesis["navigation_hints"])
    assert "gold_files" not in str(result["evidence_packet"]).lower()


def test_automattic_23915_extracts_product_routes_as_reproduction_evidence():
    result = _understand(SWE_CLEAN15, "swe_clean15", "Automattic__wp-calypso-23915")
    synthesis = result["evidence_synthesis"]

    assert synthesis["url_role_counts"]["reproduction_entry"] >= 2
    route_terms = set(synthesis["query_groups"]["concern"] + synthesis["query_groups"]["docs"])
    assert {"read", "feeds", "posts"} & route_terms
    assert "url_builder_or_route_flow" in synthesis["query_groups"]["flow"]


def test_chartjs_10301_plans_browser_reproduction_and_visual_evidence():
    result = _understand(SWE_CLEAN15, "swe_clean15", "chartjs__Chart.js-10301")
    synthesis = result["evidence_synthesis"]

    assert synthesis["url_role_counts"]["reproduction_entry"] == 2
    assert "browser_reproduction_reader:parsed_needs_browser_or_network" in synthesis["tool_status_counts"]
    assert any(hint["kind"] == "reproduction_to_program" for hint in synthesis["navigation_hints"])
    assert "chart_or_canvas_render" in synthesis["image_type_counts"]


def test_react_pdf_1178_marks_example_code_url_and_layout_visual_symptom():
    result = _understand(SWE_CLEAN15, "swe_clean15", "diegomura__react-pdf-1178")
    synthesis = result["evidence_synthesis"]

    assert synthesis["url_role_counts"]["code_evidence_seed"] >= 1
    assert "packages/examples/src/knobs/index.js" in synthesis["query_groups"]["url_seed"]
    assert "layout_or_pdf_render" in synthesis["image_type_counts"]
    assert "render_style_pipeline_flow" in synthesis["query_groups"]["flow"]


def test_mypy_playground_and_cryptography_issue_discussion_are_distinct_url_roles():
    mypy = _understand(OMNI_CLEAN15, "omni_clean15", "python__mypy-13481")
    crypto = _understand(OMNI_CLEAN15, "omni_clean15", "pyca__cryptography-7520")

    assert mypy["evidence_synthesis"]["url_role_counts"]["reproduction_entry"] == 1
    assert "mypy_play" in " ".join(mypy["evidence_synthesis"]["query_groups"]["reproduction"])
    assert "binder" in mypy["evidence_synthesis"]["query_groups"]["flow"]

    assert crypto["evidence_synthesis"]["url_role_counts"]["historical_discussion"] == 1
    assert "parameter_or_config_flow" in crypto["evidence_synthesis"]["query_groups"]["flow"]


if __name__ == "__main__":
    test_automattic_21409_keeps_code_url_as_evidence_seed_not_gold_target()
    test_automattic_23915_extracts_product_routes_as_reproduction_evidence()
    test_chartjs_10301_plans_browser_reproduction_and_visual_evidence()
    test_react_pdf_1178_marks_example_code_url_and_layout_visual_symptom()
    test_mypy_playground_and_cryptography_issue_discussion_are_distinct_url_roles()
    print("PASS phase1/phase2 multi-case evidence tests")
