from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.controller import decide_next_actions
from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.evidence.evidence_agent import build_evidence_packet
from mycode.evidence.llm_evidence_agent import normalize_llm_evidence_analysis
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _controller_actions(localization: dict) -> list[dict]:
    actions: list[dict] = []
    for round_info in localization.get("dynamic_rounds", []) or []:
        for action in round_info.get("agent_actions", []) or []:
            decision = action.get("controller_decision")
            if decision:
                actions.append(decision)
    return actions


def test_controller_accepts_llm_json_and_falls_back_to_heuristics() -> None:
    prompts: list[str] = []

    def fake_llm(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "decisions": [
                    {
                        "tool": "TraceFlow",
                        "mode": "serializer_backend_call_chain",
                        "reason": "kdf_rounds must flow from API to backend serializer",
                        "queries": ["BestAvailableEncryption kdf_rounds ssh serializer"],
                        "preferred_edge_types": ["calls", "imports"],
                        "seed_strategy": "parameter_closure",
                        "confidence": 0.91,
                    }
                ]
            }
        )

    decisions = decide_next_actions(
        round_no=1,
        issue_text="OpenSSH serializer should add kdf_rounds to BestAvailableEncryption",
        evidence_result={},
        queries=["kdf_rounds"],
        previous_top_paths=[],
        current_seed_files=[],
        current_flow_traces=[],
        llm_controller=fake_llm,
    )
    assert prompts and "allowed_tools" in prompts[0]
    assert decisions[0].source == "llm"
    assert decisions[0].mode == "serializer_backend_call_chain"

    fallback = decide_next_actions(
        round_no=1,
        issue_text="mypy deleted variable TypeInfo binder",
        evidence_result={},
        queries=[],
        previous_top_paths=[],
        current_seed_files=[],
        current_flow_traces=[],
        llm_controller=lambda _: "not json",
    )
    assert fallback[0].source == "fallback"
    assert any(decision.mode == "python_type_binding_flow" for decision in fallback)


def test_llm_evidence_output_is_normalized_into_issue_sketch() -> None:
    sample = NormalizedSample(
        instance_id="chartjs__Chart.js-10301",
        repo="chartjs/Chart.js",
        dataset="unit",
        issue_text=(
            "Legend onLeave is not triggered when the mouse quickly leaves the chart. "
            "See https://www.chartjs.org/docs/latest/samples/legend/events.html and "
            "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx"
        ),
        raw={
            "problem_statement": (
                "Legend onLeave is not triggered when the mouse quickly leaves the chart. "
                "See https://www.chartjs.org/docs/latest/samples/legend/events.html and "
                "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx"
            ),
        },
    )
    packet = build_evidence_packet(sample)
    normalized = normalize_llm_evidence_analysis(
        {
            "issue_understanding": "Legend hover state is not cleared after onLeave.",
            "evidence_roles": {
                "docs": {
                    "url": "https://www.chartjs.org/docs/latest/samples/legend/events.html",
                    "role": "reproduction_entry",
                    "description": "Official sample for legend events.",
                    "navigation_value": "high",
                },
                "sandbox": {
                    "url": "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0",
                    "role": "wrapper_reproduction",
                    "description": "React wrapper reproduction, useful but not likely patch target.",
                },
            },
            "visual_understanding": {
                "implication": "hover state persists",
                "layers_involved": ["legend plugin", "event dispatcher"],
            },
            "search_queries": ["plugin.legend onLeave"],
            "graph_navigation_plan": {"entry_points": ["src/plugins/plugin.legend.js"]},
            "missing_tools": ["browser interaction"],
        },
        packet,
    )
    sketch = normalized["issue_sketch"]
    assert "Legend" in sketch["concern"]
    assert any(role["modification_prior"] == "low" for role in sketch["evidence_roles"])
    assert "src/plugins/plugin.legend.js" in sketch["concern_queries"]
    assert any("hover state persists" in query for query in sketch["flow_queries"])
    assert "browser interaction" in sketch["missing_tools"]


def test_js_code_url_is_navigation_seed_not_patch_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "client/state/current-user/selectors.js",
            "export const isCurrentUserEmailVerified = createCurrentUserSelector('email_verified', false);\n",
        )
        _write(
            repo_root / "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
            "\n".join(
                [
                    "import { isCurrentUserEmailVerified } from '../../../../state/current-user/selectors';",
                    "import { isCountrySupported } from '../../state/sites/locations/selectors';",
                    "",
                    "export function StoreLocationSetupView({ user, country, redirect }) {",
                    "  function onNext() {",
                    "    if (!isCurrentUserEmailVerified(user)) {",
                    "      redirect('/checkout/email-verification');",
                    "    }",
                    "    if (!isCountrySupported(country)) {",
                    "      redirect('/wp-admin/admin.php?page=wc-setup');",
                    "    }",
                    "  }",
                    "  return <button onClick={onNext}>Continue</button>;",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "client/extensions/woocommerce/state/sites/locations/selectors.js",
            "export function isCountrySupported(country) { return country !== 'AQ'; }\n",
        )
        _write(
            repo_root / "client/signup/steps/site.js",
            "export function SiteSignupStep() { return <div>signup</div>; }\n",
        )
        sample = NormalizedSample(
            instance_id="Automattic__wp-calypso-21409",
            repo="Automattic/wp-calypso",
            dataset="unit",
            issue_text=(
                "Store signup flow should force email verification before wp-admin. "
                "Screenshot shows WooCommerce Store Location setup. "
                "Reference code URL: https://github.com/Automattic/wp-calypso/blob/master/client/state/current-user/selectors.js#L157"
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js b/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "--- a/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "+++ b/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "@@ -4,7 +4,9 @@",
                        " export function StoreLocationSetupView({ user, country, redirect }) {",
                        "   function onNext() {",
                        "+    if (!isCurrentUserEmailVerified(user)) {",
                        "+      redirect('/checkout/email-verification');",
                        "+    }",
                        "     if (!isCountrySupported(country)) {",
                    ]
                )
            },
            gold_files=["client/extensions/woocommerce/app/dashboard/store-location-setup-view.js"],
        )
        evidence = {
            "evidence_packet": {
                "url_inspections": [
                    {
                        "url": "https://github.com/Automattic/wp-calypso/blob/master/client/state/current-user/selectors.js#L157",
                        "url_type": "github_code_file",
                        "role": "code_evidence_seed",
                        "semantic_terms": ["isCurrentUserEmailVerified", "email_verified"],
                    }
                ],
                "image_inspections": [
                    {
                        "image_type": "Web UI页面截图",
                        "role": "workflow_context",
                        "visual_queries": ["WooCommerce Store Location setup", "wp-admin redirect"],
                    }
                ],
                "symbol_queries": ["isCurrentUserEmailVerified", "email_verified"],
                "concern_queries": ["WooCommerce store setup email verification redirect"],
                "flow_hypotheses": ["email_verified selector affects StoreLocationSetupView redirect behavior"],
            }
        }
        index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            repo_root=repo_root,
            structure_path=repo_root / "missing_structure.json",
        )
        localization = dynamic_localize(sample, evidence, index=index, top_k=15, max_rounds=3)
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        actions = _controller_actions(localization)
        metrics = evaluate_three_level_ranking(localization, sample, index)

        assert "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js" in top_files
        assert any(action["mode"] == "used_by" for action in actions)
        assert any("code URL" in action["reason"] for action in actions)
        plan = localization["dynamic_rounds"][0]["frontier_state"]["agent_plan"]
        assert plan["seed_policy"]["policy"].startswith("use explicit code/URL evidence")
        assert "client/state/current-user/selectors.js" in plan["seed_policy"]["evidence_seed_paths"]
        assert any(step["axis"] == "vertical" for step in plan["tool_schedule"])
        assert metrics["file"]["recall@15"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0


def test_python_mypy_type_binding_flow_prioritizes_binder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "mypy/binder.py",
            "\n".join(
                [
                    "class Frame:",
                    "    def __init__(self):",
                    "        self.deleted = set()",
                    "",
                    "def get_declaration(expr):",
                    "    typ = getattr(expr, 'node', None)",
                    "    return typ",
                    "",
                    "def top_frame_context(frame, expr):",
                    "    declaration = get_declaration(expr)",
                    "    if declaration in frame.deleted:",
                    "        return 'deleted variable'",
                    "    return 'ok'",
                    "",
                    "def bind_name(frame, name, value):",
                    "    frame.deleted.discard(name)",
                ]
            ),
        )
        _write(
            repo_root / "mypy/checker.py",
            "def visit_name_expr(expr): return expr\n",
        )
        _write(
            repo_root / "mypy/nodes.py",
            "class TypeInfo: pass\nclass TypeType: pass\n",
        )
        sample = NormalizedSample(
            instance_id="python__mypy-13481",
            repo="python/mypy",
            dataset="unit",
            issue_text=(
                "mypy should report trying to read deleted variable after del Foo; "
                "print(Foo) should use binder declaration state with TypeInfo TypeType."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/mypy/binder.py b/mypy/binder.py",
                        "--- a/mypy/binder.py",
                        "+++ b/mypy/binder.py",
                        "@@ -5,8 +5,10 @@",
                        " def get_declaration(expr):",
                        "     typ = getattr(expr, 'node', None)",
                        "+    if typ.__class__.__name__ in {'TypeInfo', 'TypeType'}:",
                        "+        return typ",
                        "     return typ",
                        "@@ -9,6 +11,7 @@",
                        " def top_frame_context(frame, expr):",
                        "     declaration = get_declaration(expr)",
                        "+    # preserve deleted declaration state",
                    ]
                )
            },
            gold_files=["mypy/binder.py"],
            language="Python",
        )
        evidence = {
            "evidence_packet": {
                "url_inspections": [
                    {
                        "url": "https://mypy-play.net/?mypy=latest&python=3.10&gist=1dea89d07b0f24e562595bf221e4f7d8",
                        "url_type": "playground",
                        "role": "reproduction",
                        "semantic_terms": ["del Foo", "print(Foo)", "deleted variable"],
                    }
                ],
                "symbol_queries": ["TypeInfo", "TypeType", "get_declaration", "top_frame_context"],
                "concern_queries": ["mypy binder deleted variable read"],
                "flow_hypotheses": ["deleted variable state flows through binder declaration lookup"],
            }
        }
        index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            repo_root=repo_root,
            structure_path=repo_root / "missing_structure.json",
        )
        localization = dynamic_localize(sample, evidence, index=index, top_k=15, max_rounds=3)
        top_files = [item["path"] for item in localization["ranked_locations"][:3]]
        functions = [item["name"] for item in localization["ranked_functions"][:8]]
        actions = _controller_actions(localization)
        metrics = evaluate_three_level_ranking(localization, sample, index)

        assert "mypy/binder.py" in top_files
        assert any(action["mode"] == "python_type_binding_flow" for action in actions)
        plan = localization["dynamic_rounds"][0]["frontier_state"]["agent_plan"]
        assert any(item["flow_type"] == "python_type_binding_flow" for item in plan["issue_sketch_used"]["flow_obligations"])
        assert plan["tool_schedule"][2]["flow_type_counts"]
        assert "get_declaration" in functions
        assert "top_frame_context" in functions
        assert any("python_type" in flow.get("flow_type", "") for flow in localization["flow_traces"])
        assert metrics["file"]["recall@15"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0


def test_java_delegate_and_override_case_uses_dispatch_navigation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/main/java/org/assertj/core/api/Assertions.java",
            "\n".join(
                [
                    "package org.assertj.core.api;",
                    "public class Assertions {",
                    "  public static RecursiveComparisonAssert assertThat(Object actual) {",
                    "    return new RecursiveComparisonAssert(actual);",
                    "  }",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "src/main/java/org/assertj/core/api/RecursiveComparisonAssert.java",
            "\n".join(
                [
                    "package org.assertj.core.api;",
                    "public class RecursiveComparisonAssert {",
                    "  private final RecursiveComparisonConfiguration configuration = new RecursiveComparisonConfiguration();",
                    "  public RecursiveComparisonAssert(Object actual) {}",
                    "  public RecursiveComparisonAssert ignoringOverriddenEqualsForTypes(Class<?> type) {",
                    "    configuration.ignoreOverriddenEqualsForTypes(type);",
                    "    return this;",
                    "  }",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java",
            "\n".join(
                [
                    "package org.assertj.core.api;",
                    "public class RecursiveComparisonConfiguration {",
                    "  public void ignoreOverriddenEqualsForTypes(Class<?> type) {",
                    "    registerIgnoredType(type);",
                    "  }",
                    "  private void registerIgnoredType(Class<?> type) {",
                    "    System.out.println(type.getName());",
                    "  }",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "src/test/java/org/assertj/core/api/RecursiveComparisonAssert_Test.java",
            "class RecursiveComparisonAssert_Test { void should_ignore_overridden_equals() {} }\n",
        )
        sample = NormalizedSample(
            instance_id="assertj__assertj-2364",
            repo="assertj/assertj",
            dataset="unit",
            issue_text=(
                "Java AssertJ recursive comparison should ignore overridden equals for a given class. "
                "The public API delegates to internal configuration and override/implements dispatch matters."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java b/src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java",
                        "--- a/src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java",
                        "+++ b/src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java",
                        "@@ -2,7 +2,9 @@",
                        " public class RecursiveComparisonConfiguration {",
                        "   public void ignoreOverriddenEqualsForTypes(Class<?> type) {",
                        "+    if (type == null) { return; }",
                        "     registerIgnoredType(type);",
                    ]
                )
            },
            gold_files=["src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java"],
            language="Java",
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["ignoringOverriddenEqualsForTypes", "ignoreOverriddenEqualsForTypes"],
                "concern_queries": ["AssertJ recursive comparison overridden equals class exclusion"],
                "flow_hypotheses": ["public API delegates type exclusion into RecursiveComparisonConfiguration"],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["RecursiveComparisonConfiguration", "registerIgnoredType"],
                    "concerns": ["java delegate override recursive comparison"],
                    "flows": ["java_dispatch", "parameter_or_config_flow"],
                }
            },
        }
        index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            repo_root=repo_root,
            structure_path=repo_root / "missing_structure.json",
        )
        localization = dynamic_localize(sample, evidence, index=index, top_k=15, max_rounds=3)
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        functions = [item["name"] for item in localization["ranked_functions"][:8]]
        actions = _controller_actions(localization)
        metrics = evaluate_three_level_ranking(localization, sample, index)

        assert "src/main/java/org/assertj/core/api/RecursiveComparisonConfiguration.java" in top_files
        assert any(action["mode"] == "call" and action["seed_strategy"] == "java_dispatch" for action in actions)
        plan = localization["dynamic_rounds"][0]["frontier_state"]["agent_plan"]
        assert "calls" in plan["tool_schedule"][1]["edge_types"]
        assert localization["agent_reasoning_summary"]["multilingual_graph"]["language_counts"]["java"] >= 1
        assert "ignoreOverriddenEqualsForTypes" in functions
        assert metrics["file"]["recall@15"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0
