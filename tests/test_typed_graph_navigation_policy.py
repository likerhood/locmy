from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ambiguous_javascript_call_names_do_not_create_all_to_all_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/a.js", "export function filter() { return 1; }\n")
        _write(root / "src/b.js", "export function filter() { return 2; }\n")
        _write(root / "src/c.js", "export function filter() { return 3; }\n")
        _write(root / "app/main.js", "export function run() { return filter(); }\n")
        index = RepositoryIndex(repo="example/js", instance_id="ambiguous", repo_root=root)
        graph = TypedRepositoryGraph(index)
        call_targets = {
            edge.target
            for edge in graph.edges_by_source.get("app/main.js", [])
            if edge.edge_type == "calls" and "filter" in edge.evidence
        }
        assert call_targets == set()


def test_imported_javascript_call_keeps_resolved_call_edge() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "src/filter.js", "export function filter() { return 1; }\n")
        _write(
            root / "app/main.js",
            "import { filter } from '../src/filter';\nexport function run() { return filter(); }\n",
        )
        index = RepositoryIndex(repo="example/js", instance_id="resolved", repo_root=root)
        graph = TypedRepositoryGraph(index)
        assert any(
            edge.target == "src/filter.js" and edge.edge_type == "calls" and "resolved imported" in edge.evidence
            for edge in graph.edges_by_source.get("app/main.js", [])
        )


def test_typed_graph_connects_component_state_and_style_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/components/ChartView.jsx",
            "\n".join(
                [
                    "import './ChartView.css';",
                    "import { LegendPanel } from './LegendPanel';",
                    "import { selectLegendItems } from '../state/selectors';",
                    "",
                    "export function ChartView({ state }) {",
                    "  const items = selectLegendItems(state);",
                    "  return <LegendPanel className='legend-item' items={items} />;",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "src/components/LegendPanel.jsx",
            "\n".join(
                [
                    "export function LegendPanel({ items }) {",
                    "  return <ul>{items.map(item => <li className='legend-item'>{item.text}</li>)}</ul>;",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "src/components/ChartView.css",
            ".legend-item { cursor: pointer; }\n",
        )
        _write(
            repo_root / "src/state/selectors.js",
            "\n".join(
                [
                    "export function selectLegendItems(state) {",
                    "  return state.legend.items;",
                    "}",
                ]
            ),
        )

        missing_structure = repo_root / "missing_repo_structure.json"
        index = RepositoryIndex(repo="chartjs/Chart.js", instance_id="chartjs__Chart.js-10301", repo_root=repo_root, structure_path=missing_structure)
        graph = TypedRepositoryGraph(index)
        expanded = graph.expand(["src/components/ChartView.jsx"], ["legend selector style"], limit=10)
        by_path = {path: reasons for path, _, reasons in expanded}

        assert "src/components/LegendPanel.jsx" in by_path
        assert "src/state/selectors.js" in by_path
        assert "src/components/ChartView.css" in by_path
        assert any("renders" in reason for reason in by_path["src/components/LegendPanel.jsx"])
        assert any("selects_state" in reason or "imports" in reason for reason in by_path["src/state/selectors.js"])
        assert any("styles" in reason for reason in by_path["src/components/ChartView.css"])


def test_typed_graph_expand_uses_budgeted_beam_without_losing_relevant_neighbor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        imports = ["import { target } from './target';"]
        for idx in range(80):
            _write(repo_root / f"src/noise/noise_{idx}.js", f"export function noise{idx}() {{ return {idx}; }}\n")
            imports.append(f"import {{ noise{idx} }} from './noise/noise_{idx}';")
        _write(
            repo_root / "src/entry.js",
            "\n".join(imports + ["export function runLegendHover() { return target(); }"]),
        )
        _write(
            repo_root / "src/target.js",
            "export function target() { return 'legend hover onLeave restore color'; }\n",
        )

        index = RepositoryIndex(
            repo="chartjs/Chart.js",
            instance_id="chartjs__Chart.js-budget",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        graph = TypedRepositoryGraph(index)
        expanded = graph.expand(
            ["src/entry.js"],
            ["legend hover onLeave restore color"],
            limit=5,
            max_seed_nodes=2,
            max_edges_per_seed=120,
            beam_width=12,
        )

        assert len(expanded) <= 5
        assert expanded[0][0] == "src/target.js"


def test_dynamic_agent_records_typed_navigation_and_entity_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/plugins/plugin.legend.js",
            "\n".join(
                [
                    "export function handleEvent(chart, e) {",
                    "  const item = getLegendItemAt(e.x, e.y);",
                    "  if (e.type === 'mousemove') {",
                    "    chart.options.plugins.legend.onHover(e, item, chart.legend);",
                    "  }",
                    "  if (e.type === 'mouseout') {",
                    "    chart.options.plugins.legend.onLeave(e, item, chart.legend);",
                    "  }",
                    "}",
                    "export function getLegendItemAt(x, y) { return { index: 0 }; }",
                ]
            ),
        )
        _write(
            repo_root / "src/controllers/controller.pie.js",
            "export function updateElements() { return true; }\n",
        )
        _write(
            repo_root / "docs/samples/legend/events.md",
            "The CodeSandbox reproduction shows legend onHover and onLeave behavior.\n",
        )
        _write(
            repo_root / "src/helpers/helpers.canvas.js",
            "export function clipArea() { return null; }\n",
        )
        sample = NormalizedSample(
            instance_id="chartjs__Chart.js-10301",
            repo="chartjs/Chart.js",
            dataset="unit",
            issue_text=(
                "Legend onHover makes pie slice transparent but onLeave does not restore the color. "
                "The issue includes a CodeSandbox reproduction and a screenshot of the chart legend."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/src/plugins/plugin.legend.js b/src/plugins/plugin.legend.js",
                        "--- a/src/plugins/plugin.legend.js",
                        "+++ b/src/plugins/plugin.legend.js",
                        "@@ -1,7 +1,8 @@",
                        " export function handleEvent(chart, e) {",
                        "   const item = getLegendItemAt(e.x, e.y);",
                        "+  const previous = chart.legend._hoveredItem;",
                        "   if (e.type === 'mouseout') {",
                        "-    chart.options.plugins.legend.onLeave(e, item, chart.legend);",
                        "+    chart.options.plugins.legend.onLeave(e, previous, chart.legend);",
                        "   }",
                    ]
                )
            },
            gold_files=["src/plugins/plugin.legend.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["onHover", "onLeave", "legend", "hoveredItem"],
                "concern_queries": ["Chart.js legend hover leave event restores color"],
                "flow_hypotheses": ["mousemove changes legend item then mouseout calls onLeave with previous item"],
                "url_inspections": [
                    {
                        "url": "https://codesandbox.io/s/react-chartjs-2-chartjs-issue-template-3kw5p0",
                        "role": "reproduction_playground",
                        "semantic_terms": ["CodeSandbox", "legend", "onHover", "onLeave", "pie chart"],
                    }
                ],
                "image_inspections": [
                    {
                        "image_type": "chart_canvas_screenshot",
                        "visual_queries": ["pie chart legend red blue hover transparency"],
                        "likely_layers": ["legend plugin", "event handler"],
                    }
                ],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["onHover", "onLeave", "handleEvent", "legend"],
                    "concerns": ["legend plugin event handling"],
                    "flows": ["ui_event_to_handler", "visual_style_semantic"],
                }
            },
            "tool_observations": [
                {
                    "tool": "browser_reproduction_reader",
                    "extracted": {
                        "parsed_reproduction": {
                            "semantic_queries": ["CodeSandbox App.tsx legend onHover onLeave"],
                            "likely_layers": ["legend plugin event handler"],
                            "browser_observation_plan": {"requested_file": "src/App.tsx"},
                        }
                    },
                }
            ],
        }
        missing_structure = repo_root / "missing_repo_structure.json"
        localization = dynamic_localize(sample, evidence, repo_root=repo_root, structure_path=missing_structure, top_k=15, max_rounds=3)

        assert localization["status"] == "ok"
        assert localization["ranked_locations"][0]["path"] == "src/plugins/plugin.legend.js"
        assert localization["agent_trace"]
        first_round = localization["dynamic_rounds"][0]
        action_names = {item["action"] for item in first_round["agent_actions"]}
        assert "evidence_role_check" in action_names
        assert "visual_style_semantic_expand" in action_names
        assert first_round["graph_summary"]["graph_type"] == "typed_heterogeneous_repository_graph"
        assert "handleEvent" in {item["name"] for item in localization["ranked_functions"]}

        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, repo_root=repo_root, structure_path=missing_structure)
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@1"] == 1.0
        assert metrics["function"]["gold_count"] >= 1
        assert metrics["function"]["recall@15"] > 0.0
