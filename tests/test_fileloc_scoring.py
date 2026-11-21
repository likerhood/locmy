import importlib.util
from pathlib import Path
import pytest


def test_snapshot_stage_ranks_with_nested_rounds():
    path = Path(__file__).resolve().parents[1] / "scripts/score_fileloc_pairs.py"
    spec = importlib.util.spec_from_file_location("score_fileloc_pairs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshots = {
        "rounds": {"1": [{"path": "a"}, {"path": "target"}]},
        "head_selector": [{"path": "target"}, {"path": "a"}],
        "modification_closure": [{"path": "a"}],
    }
    assert module.stage_ranks(snapshots, {"target"}) == {
        "rounds/1": 2, "head_selector": 1, "modification_closure": 0,
    }


def test_model_failure_is_not_hidden_by_other_successful_calls():
    path = Path(__file__).resolve().parents[1] / "scripts/score_fileloc_pairs.py"
    spec = importlib.util.spec_from_file_location("score_fileloc_pairs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {"token_usage_summary": {"total_tokens": 100},
              "evidence": {"llm_error": "HTTP 400: Unsupported model"},
              "browser": {"error": "HTTP 403: private page"}}
    assert module.model_configuration_errors(result) == ["/evidence/llm_error"]


def test_omni_singular_gold_keys_are_not_silently_empty():
    path = Path(__file__).resolve().parents[1] / "scripts/score_fileloc_pairs.py"
    spec = importlib.util.spec_from_file_location("score_fileloc_pairs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.canonical_gold({"file": ["a"], "module": ["m"], "function": ["f"]}) == {
        "files": ["a"], "modules": ["m"], "functions": ["f"],
    }


def test_complement_cannot_replace_valid_outcomes_or_mix_code():
    path = Path(__file__).resolve().parents[1] / "scripts/score_fileloc_pairs.py"
    spec = importlib.util.spec_from_file_location("score_fileloc_pairs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    a = {"implementation_hashes": {"a.py": "old"}, "cases": [{"instance_id": "a"}]}
    b = {"implementation_hashes": {"a.py": "old"}, "cases": [{"instance_id": "b"}]}
    assert len(module.merge_complement_manifest(a, b)["cases"]) == 2
    assert len(a["cases"]) == 1
    with pytest.raises(ValueError, match="disjoint"):
        module.merge_complement_manifest(a, a)
    with pytest.raises(ValueError, match="implementation_hashes"):
        module.merge_complement_manifest(a, dict(b, implementation_hashes={"a.py": "new"}))


def test_explicit_new_release_policy_discloses_other_granularity_regression():
    path = Path(__file__).resolve().parents[1] / "scripts/score_fileloc_pairs.py"
    spec = importlib.util.spec_from_file_location("score_fileloc_pairs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keys = ["acc@1", "acc@3", "acc@5", "acc@8", "acc@15", "set_sl@15", "set_rec@15"]
    rows = [{"arm": arm, "metrics": {level: {k: val for k in keys} for level, val in values.items()}}
            for arm, values in [("baseline", {"file": 0, "function": 1}), ("improved", {"file": 1, "function": 0})]]
    release = module.experimental_release_assessment(rows, complete=True)
    assert release["eligible_for_experimental_push"]
    assert "file/acc@1" in release["gain_metrics"]
    assert "function/acc@1" in release["regressed_metrics"]
    assert not module.experimental_release_assessment(rows, complete=False)["eligible_for_experimental_push"]
