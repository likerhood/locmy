from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mycode.ablation import ARMS, ablation, flow_backend
from mycode.dynamic_retrieval import react_agent, search_agent
from mycode.dynamic_retrieval.tools import DynamicToolObservation, TraceFlowTool
from mycode.evidence.evidence_builder import build_evidence_sketch
from mycode.evidence.issue_sketch import IssueSketch
from mycode.evidence.runtime.tool_executor import execute_tool_request
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample, ToolRequest


@pytest.fixture(autouse=True)
def clean_arm(monkeypatch):
    monkeypatch.delenv("MAGNET_ABLATION", raising=False)


def test_unknown_arm_rejected(monkeypatch):
    monkeypatch.setenv("MAGNET_ABLATION", "no_folw")
    with pytest.raises(ValueError):
        ablation()


def test_full_preserves_backend_result():
    sentinel = object()
    assert flow_backend(lambda: sentinel)() is sentinel


@pytest.mark.parametrize("module,name", [
    ("program_flow", "trace_program_flows"), ("parameter_closure", "trace_parameter_closures"),
    ("statement_flow", "trace_statement_flows"), ("static_slice", "trace_static_slices"),
    ("interprocedural_flow", "trace_interprocedural_flows"), ("flow_chain", "trace_flow_chains"),
    ("runtime_trace", "verify_runtime_traces"),
])
def test_all_flow_backends_short_circuit(monkeypatch, module, name):
    monkeypatch.setenv("MAGNET_ABLATION", "no_flow")
    backend = getattr(importlib.import_module("mycode.flow_analysis." + module), name)
    # Missing required arguments would fail if any backend body were entered.
    assert backend() == []


def test_flow_tool_never_enters_backend(monkeypatch):
    monkeypatch.setenv("MAGNET_ABLATION", "no_flow")
    assert TraceFlowTool(None).run(None, {}, None, [], []).status == "disabled_by_ablation"


@pytest.mark.parametrize("arm", ["no_graph", "no_graph_flow"])
def test_graph_has_no_edges_but_keeps_source_scope(monkeypatch, arm):
    monkeypatch.setenv("MAGNET_ABLATION", arm)
    monkeypatch.setattr(TypedRepositoryGraph, "_build", lambda self: pytest.fail("graph built"))
    graph = TypedRepositoryGraph(SimpleNamespace(files={"src/a.py": "x = 1"}), ["src/a.py"])
    assert graph.graph_files == ["src/a.py"]
    assert not graph.edges_by_source
    assert graph.expand(["src/a.py"], ["x"]) == []


def test_no_closure_does_not_access_graph(monkeypatch):
    monkeypatch.setenv("MAGNET_ABLATION", "no_closure")
    closure = search_agent._build_modification_closure(
        ranked=[], verifier={}, graph=None, issue_sketch=None)
    assert closure["rounds_run"] == 0
    assert closure["patch_set_applicable"] is False


def test_no_visual_blocks_image_discovery_and_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGNET_ABLATION", "no_visual")
    sample = NormalizedSample("a", "a/b", "test", "Bug ![screen](https://example.org/screen.png)", {})
    assert build_evidence_sketch(sample).images == []
    request = ToolRequest("vlm_image_inspector", "https://example.org/screen.png", "image", "symptom", "high", "test")
    result = execute_tool_request(request, cache_dir=tmp_path / "cache", allow_network=True, use_vlm=True)
    assert result.status == "disabled_by_ablation"
    assert not (tmp_path / "cache").exists()


def test_fixed_react_never_calls_planner(monkeypatch):
    monkeypatch.setenv("MAGNET_ABLATION", "fixed_react")
    monkeypatch.setattr(react_agent, "_run_tool", lambda **kwargs: DynamicToolObservation(
        tool=kwargs["move"]["tool"], action="test", status="ok"))
    sample = NormalizedSample("a", "a/b", "test", "wrong value", {})
    result = react_agent.run_react_tool_agent(
        sample=sample, evidence_result={}, index=None, graph=None,
        issue_sketch=IssueSketch("a", "a/b", "test"), queries=["value"], max_steps=4,
        planner_llm=lambda *_args: pytest.fail("planner called"))
    assert not result["planner_used"]
    assert [step["tool"] for step in result["steps"]] == ablation().tools()


def test_launcher_dry_run_has_no_side_effects(tmp_path):
    root = Path(__file__).resolve().parents[1]
    samples = tmp_path / "samples.jsonl"
    samples.write_text("".join(json.dumps({"instance_id": str(i)}) + "\n" for i in range(92)))
    profile = tmp_path / "qwen.env"
    profile.write_text("BASE_URL=https://example.org/v1\nAPI_KEY=not-a-real-secret\nMODEL_NAME=Qwen\nMODEL_API_NAME=qwen-test\n")
    env = {**os.environ, "PYTHON_BIN": sys.executable, "SAMPLES": str(samples)}
    result = subprocess.run(["bash", str(root / "scripts/run_ablation_qwen.sh"),
                             "--env-file", str(profile), "--arm", "no_flow", "--tag", tmp_path.name,
                             "--dry-run"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "not-a-real-secret" not in result.stdout
    record = json.loads(result.stdout)
    assert record["ablation"]["flow_evidence"] is False
    assert record["dataset_count"] == 92
    assert not Path(record["output"]).exists()


@pytest.mark.parametrize("arm", ARMS)
def test_offline_dynamic_pipeline_for_each_arm(monkeypatch, tmp_path, arm):
    monkeypatch.setenv("MAGNET_ABLATION", arm)
    (tmp_path / "app.py").write_text("from store import save\ndef submit(value):\n    return save(value)\n")
    (tmp_path / "store.py").write_text("def save(value):\n    return value.strip()\n")
    sample = NormalizedSample("ablation-fixture", "fixture/project", "test", "`app.py` calls `save` in `store.py`: submit should preserve value whitespace", {})
    index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id,
                            dataset=sample.dataset, repo_root=tmp_path, structure_path=tmp_path / "missing.json")
    assert index.ready
    result = search_agent.dynamic_localize(sample, {}, index=index, max_rounds=1,
                                           max_react_steps=4, top_k=2, controller_llm=None)
    assert result["ranked_locations"]
    if not ablation().flow:
        assert not result["flow_traces"]
    if arm == "no_closure":
        assert result["modification_closure"]["status"] == "disabled_by_ablation"
        assert not result["final_patch_set"]
    if arm == "no_head":
        assert not result["best_round_selection"]["precision_rerank"]["enabled"]
