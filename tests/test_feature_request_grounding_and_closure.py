from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import (
    RankedLocation,
    _build_modification_closure,
    _domain_path_probes,
    _grounded_evidence_queries,
    _path_probe_hits,
    _rank_entities,
)
from mycode.evidence.image_extractor import classify_image
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


OWNERSHIP_ISSUE = """Plans: Allow changing owner of a plan
We should make it so that customers can transfer ownership of a purchased plan
to any other Administrator on their site. On Me > Manage Purchases, make the
Owner box clickable when another Administrator exists, show a warning and a
list of Administrators, then transfer ownership to the selected person.

![purchases_manage_purchase_wordpress_com](https://example.test/purchase.png)

Attached Images:
- https://example.test/purchase.png

[Multimodal Context - Compact]
The visual likely uses a <div> or <span> with class .owner-field and code such
as if (has_other_admins). It may involve chart_config and canvas_renderer.
"""


def _sample() -> NormalizedSample:
    return NormalizedSample(
        instance_id="Automattic__wp-calypso-25725",
        repo="Automattic/wp-calypso",
        dataset="unit",
        issue_text=OWNERSHIP_ISSUE,
        raw={"problem_statement": OWNERSHIP_ISSUE},
    )


def _evidence() -> dict:
    return {
        "evidence_packet": {
            "code_references": [],
            "url_inspections": [],
            "image_inspections": [
                {
                    "raw_url": "https://example.test/purchase.png",
                    "image_type": "web_ui_screenshot",
                    "visual_queries": ["<div>", ".owner-field", "transfer ownership"],
                    "likely_layers": ["React component", "state action handler"],
                }
            ],
        },
        "evidence_synthesis": {"query_groups": {"symbol": [], "concern": []}},
        "tool_observations": [],
        "llm_understanding": {
            "issue_sketch": {
                "workflow": ["manage purchase ownership"],
                "concern": ["transfer ownership", "chart_config scale layout plugin canvas_renderer"],
                "state": ["current owner"],
                "expected_effect": [
                    "transfer ownership to another Administrator",
                    "diagnostic/error reporting changes",
                ],
                "entities": ["WordPress.com", "purchases_manage_purchase_wordpress_com"],
            }
        },
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_purchase_image_ignores_generic_adapter_render_words() -> None:
    image = classify_image(
        "https://example.test/purchase.png",
        "image_urls",
        OWNERSHIP_ISSUE + "\n[Adapter Note]\nvisual render graph evidence",
    )
    assert image.image_type == "web_ui_screenshot"


def test_feature_sketch_rejects_unrelated_llm_axes_and_marks_visual_guesses_unverified() -> None:
    sketch = build_issue_sketch(_sample(), _evidence())

    assert sketch.task_type == "feature_request"
    assert "site and plan ownership management" in sketch.workflow
    assert "current_owner" in sketch.states
    assert "selected_administrator" in sketch.states
    assert "transfer ownership to a selected administrator" in sketch.expected_effects
    assert not any("chart_config" in value for value in sketch.concerns)
    assert not any("diagnostic" in value for value in sketch.expected_effects)
    assert "WordPress.com" not in sketch.entities
    assert any("data-layer" in value for value in sketch.architectural_queries)
    assert all(item["status"] == "unverified" for item in sketch.hypotheses)


def test_visual_implementation_guesses_require_issue_grounding() -> None:
    queries = _grounded_evidence_queries(
        ["<div>", "<span>", ".owner-field", "if (has_other_admins)", "transfer ownership"],
        OWNERSHIP_ISSUE,
    )
    assert queries == ["transfer ownership"]


def test_ownership_path_probes_are_generic_architecture_queries() -> None:
    sketch = build_issue_sketch(_sample(), _evidence())
    probes = _domain_path_probes(_sample(), _evidence(), sketch)
    joined = "\n".join(probes)
    assert "state data-layer sites plan transfer" in probes
    assert "client/my-sites/site-settings/manage-connection/site-ownership" not in joined
    assert "client/state/data-layer/wpcom/sites/plan-transfer" not in joined


def test_generic_architecture_queries_recall_path_neighborhoods_without_gold_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ui_path = "client/site-settings/manage-connection/site-ownership.jsx"
        state_path = "client/state/data-layer/sites/plan-transfer/index.js"
        _write(root / ui_path, "export const Ownership = () => null;\n")
        _write(root / state_path, "export const transfer = request => request;\n")
        _write(root / "client/me/purchases/manage-purchase/index.jsx", "export const Purchase = () => null;\n")
        index = RepositoryIndex(repo="example/ownership", instance_id="path-probe", repo_root=root)
        hits = _path_probe_hits(
            index,
            ["site settings manage connection ownership", "state data-layer sites plan transfer"],
            limit=20,
        )
        paths = {path for path, _score, _reason in hits}
        assert {ui_path, state_path} <= paths


def test_redux_action_constant_builds_cross_file_action_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "client/state/action-types.js",
            "export const PLAN_OWNERSHIP_TRANSFER = 'PLAN_OWNERSHIP_TRANSFER';\n",
        )
        _write(
            root / "client/state/sites/plans/actions.js",
            "import { PLAN_OWNERSHIP_TRANSFER } from '../../action-types';\n"
            "export const transfer = payload => ({ type: PLAN_OWNERSHIP_TRANSFER, payload });\n",
        )
        _write(
            root / "client/state/data-layer/sites/plan-transfer/index.js",
            "import { PLAN_OWNERSHIP_TRANSFER } from '../../../action-types';\n"
            "export const handler = { [PLAN_OWNERSHIP_TRANSFER]: request => request };\n",
        )
        index = RepositoryIndex(repo="example/ownership", instance_id="action-flow", repo_root=root)
        graph = TypedRepositoryGraph(index)
        assert any(
            edge.edge_type == "handles_action" and "PLAN_OWNERSHIP_TRANSFER" in edge.evidence
            for edges in graph.edges_by_source.values()
            for edge in edges
        )


def test_feature_modification_closure_keeps_ui_and_state_integration_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ui_path = "client/site-settings/manage-connection/ownership.jsx"
        state_path = "client/state/data-layer/sites/ownership/index.js"
        _write(root / ui_path, "export function Ownership() { return <button>Transfer</button>; }\n")
        _write(
            root / state_path,
            "export function transferOwnership(owner) { return request(owner); }\n",
        )
        index = RepositoryIndex(repo="example/ownership", instance_id="closure", repo_root=root)
        graph = TypedRepositoryGraph(index)
        sketch = build_issue_sketch(_sample(), _evidence())
        closure = _build_modification_closure(
            ranked=[
                RankedLocation(path=ui_path, score=100.0),
                RankedLocation(path=state_path, score=90.0),
            ],
            verifier={"llm_candidate_review": {"candidates": []}},
            graph=graph,
            issue_sketch=sketch,
            code_contexts=[
                {"path": ui_path, "snippets": [{"text": "Ownership Transfer button"}]},
                {"path": state_path, "snippets": [{"text": "transferOwnership request"}]},
            ],
            flow_traces=[
                {
                    "flow_type": "ownership_transfer_action_flow",
                    "candidate_target_paths": [state_path],
                }
            ],
        )
        assert closure["complete"] is True
        assert set(closure["files"]) == {ui_path, state_path}
        assert closure["coverage"]["required_coverage"] == 1.0


def test_function_grounding_uses_concrete_behavior_terms_inside_ranked_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = "src/plugins/plugin.legend.js"
        _write(
            root / path,
            "const itemsEqual = (a, b) => a.index === b.index;\n"
            "function isListened(type, opts) {\n"
            "  return type === 'mousemove' && (opts.onHover || opts.onLeave);\n"
            "}\n",
        )
        index = RepositoryIndex(repo="example/chart", instance_id="functions", repo_root=root)
        distracting_hits = index.search_entities(["itemsEqual"], limit=10)
        _modules, functions = _rank_entities(
            index=index,
            ranked_files=[RankedLocation(path=path, score=100.0)],
            entity_hits=distracting_hits,
            flow_traces=[],
            top_k=10,
            entity_queries=["legend onHover onLeave mousemove mouseout"],
        )
        listened = next(item for item in functions if item.name == "isListened")
        assert any(reason.startswith("entity_issue_semantics:") for reason in listened.reasons)
        assert [item.name for item in functions[:2]].count("isListened") == 1
