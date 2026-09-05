from __future__ import annotations

import tempfile
from pathlib import Path

from mycode.dynamic_retrieval.tools import run_four_tool_agent_round
from mycode.dynamic_retrieval.search_agent import dynamic_localize
from mycode.evidence.issue_sketch import build_issue_sketch
from mycode.evaluation.localization_eval import evaluate_three_level_ranking
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dynamic_agent_tracks_serializer_backend_parameter_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/cryptography/hazmat/primitives/_serialization.py",
            "\n".join(
                [
                    "class BestAvailableEncryption:",
                    "    def __init__(self, password, kdf_rounds=None):",
                    "        self.password = password",
                    "        self.kdf_rounds = kdf_rounds",
                    "",
                    "class _KeySerializationEncryption:",
                    "    def __init__(self, kdf_rounds=None):",
                    "        self.kdf_rounds = kdf_rounds",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/ssh.py",
            "\n".join(
                [
                    "def _serialize_ssh_private_key(key, password, kdf_rounds=None):",
                    "    rounds = kdf_rounds or 16",
                    "    return b'openssh' + str(rounds).encode()",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/backends/openssl/backend.py",
            "\n".join(
                [
                    "from cryptography.hazmat.primitives.serialization import ssh",
                    "",
                    "def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                    "    if format == 'OpenSSH':",
                    "        return ssh._serialize_ssh_private_key(",
                    "            key, encryption_algorithm.password, encryption_algorithm.kdf_rounds",
                    "        )",
                    "    return b''",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/__init__.py",
            "from cryptography.hazmat.primitives._serialization import BestAvailableEncryption\n",
        )
        sample = NormalizedSample(
            instance_id="pyca__cryptography-7520",
            repo="pyca/cryptography",
            dataset="unit",
            issue_text=(
                "OpenSSH private key encryption should support kdf_rounds like ssh-keygen -a. "
                "BestAvailableEncryption must pass kdf_rounds through backend serialization to ssh serializer."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/src/cryptography/hazmat/primitives/_serialization.py b/src/cryptography/hazmat/primitives/_serialization.py",
                        "--- a/src/cryptography/hazmat/primitives/_serialization.py",
                        "+++ b/src/cryptography/hazmat/primitives/_serialization.py",
                        "@@ -1,4 +1,4 @@",
                        " class BestAvailableEncryption:",
                        "-    def __init__(self, password):",
                        "+    def __init__(self, password, kdf_rounds=None):",
                        "         self.password = password",
                        "+        self.kdf_rounds = kdf_rounds",
                        "diff --git a/src/cryptography/hazmat/backends/openssl/backend.py b/src/cryptography/hazmat/backends/openssl/backend.py",
                        "--- a/src/cryptography/hazmat/backends/openssl/backend.py",
                        "+++ b/src/cryptography/hazmat/backends/openssl/backend.py",
                        "@@ -3,4 +3,5 @@",
                        " def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                        "     if format == 'OpenSSH':",
                        "-        return ssh._serialize_ssh_private_key(key, encryption_algorithm.password)",
                        "+        return ssh._serialize_ssh_private_key(",
                        "+            key, encryption_algorithm.password, encryption_algorithm.kdf_rounds)",
                        "diff --git a/src/cryptography/hazmat/primitives/serialization/ssh.py b/src/cryptography/hazmat/primitives/serialization/ssh.py",
                        "--- a/src/cryptography/hazmat/primitives/serialization/ssh.py",
                        "+++ b/src/cryptography/hazmat/primitives/serialization/ssh.py",
                        "@@ -1,3 +1,3 @@",
                        "-def _serialize_ssh_private_key(key, password):",
                        "+def _serialize_ssh_private_key(key, password, kdf_rounds=None):",
                        "     rounds = kdf_rounds or 16",
                    ]
                )
            },
            gold_files=[
                "src/cryptography/hazmat/primitives/_serialization.py",
                "src/cryptography/hazmat/backends/openssl/backend.py",
                "src/cryptography/hazmat/primitives/serialization/ssh.py",
            ],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["BestAvailableEncryption", "kdf_rounds", "_serialize_ssh_private_key"],
                "concern_queries": ["OpenSSH private key encryption serializer backend"],
                "flow_hypotheses": ["kdf_rounds parameter flows from public API to backend to ssh serializer"],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["BestAvailableEncryption", "kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
                    "concerns": ["OpenSSH serialization backend"],
                    "flows": ["parameter_or_config_flow", "serializer_backend_call_chain"],
                }
            },
            "tool_observations": [],
        }
        missing_structure = repo_root / "missing_repo_structure.json"
        localization = dynamic_localize(sample, evidence, repo_root=repo_root, structure_path=missing_structure, top_k=15)
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        ranked_functions = [item["name"] for item in localization["ranked_functions"]]
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in top_files
        assert "_private_key_bytes" in ranked_functions
        assert "_serialize_ssh_private_key" in ranked_functions
        assert any(flow["flow_type"] == "serializer_backend_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("backend") == "issue_guided_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("chain_edges") for flow in localization["flow_traces"])
        assert any(flow["flow_type"] == "serializer_backend_call_chain" for flow in localization["flow_traces"])
        assert any(flow.get("backend") == "regex_ast_lightweight" for flow in localization["flow_traces"])
        assert any(flow.get("edge_summary") for flow in localization["flow_traces"])
        assert any(flow.get("candidate_target_paths") for flow in localization["flow_traces"])
        assert any("backend" in flow.get("role_coverage", {}).get("roles", {}) for flow in localization["flow_traces"])
        summary = localization["agent_reasoning_summary"]
        assert summary["concern_horizontal"]["score_support"]
        assert summary["program_vertical"]["edge_type_counts_near_top_files"]
        assert summary["flow_validation"]["flow_type_counts"]["serializer_backend_flow_chain"] >= 1
        assert summary["multilingual_graph"]["language_counts"]["python"] >= 1
        first_frontier = localization["dynamic_rounds"][0]["frontier_state"]
        assert first_frontier["strategy"] == "dual_frontier_concern_horizontal_program_vertical_flow_validation"
        assert first_frontier["query_groups"]["explicit_entity"]
        assert first_frontier["query_groups"]["concern"]
        assert first_frontier["query_groups"]["flow"]
        assert first_frontier["agent_round_questions"]["believed_files"]
        assert first_frontier["candidate_beliefs"]
        assert first_frontier["agent_plan"]["name"] == "evidence_aware_concern_call_flow_round"
        assert [step["tool"] for step in first_frontier["agent_plan"]["tool_schedule"]] == [
            "SearchAnchor",
            "NavigateCode",
            "TraceFlow",
            "ReadCode",
        ]
        assert first_frontier["agent_plan"]["tool_schedule"][0]["axis"] == "horizontal"
        assert first_frontier["agent_plan"]["tool_schedule"][1]["axis"] == "vertical"
        assert first_frontier["agent_plan"]["tool_schedule"][2]["axis"] == "flow"
        assert first_frontier["program_candidates"] or first_frontier["flow_candidates"]
        assert any(
            "program_graph" in components or "flow_verifier" in components
            for components in first_frontier["score_components"].values()
        )
        assert any(flow.get("local_closure_edges") for flow in localization["flow_traces"])
        bootstrap_trace = next(item for item in localization["search_trace"] if item["step"] == "four_tool_agent_bootstrap")
        assert bootstrap_trace["flow_coverage"]["flow_count"] > 0
        assert bootstrap_trace["pruning"]["strategy"] == "cosil_inspired_signal_family_beam_for_readcode"
        assert summary["implemented_agent_design"]["stage_2_dynamic_localization"]["horizontal_axis"]
        assert "LocAgent_gap" in summary["innovation_against_baselines"]

        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, repo_root=repo_root, structure_path=missing_structure)
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["recall@15"] >= 2 / 3
        assert metrics["function"]["gold_count"] >= 2
        assert metrics["function"]["recall@15"] > 0.0


def test_dynamic_agent_records_multi_round_call_dataflow_navigation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "client/reader/post-actions.jsx",
            "\n".join(
                [
                    "import { getEditURL } from '../../lib/posts/utils';",
                    "",
                    "export function ReaderPostActions({ post, site }) {",
                    "  const href = getEditURL(post, site);",
                    "  return <a className='edit-link' href={href}>Edit</a>;",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "client/lib/posts/utils.js",
            "\n".join(
                [
                    "export function getEditURL(post, site) {",
                    "  const postId = post.ID || post.global_ID;",
                    "  const siteSlug = site.slug || site.URL;",
                    "  return `/post/${siteSlug}/${postId}`;",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "client/reader/reducer.js",
            "export function receivePost(state, action) { return state; }\n",
        )
        sample = NormalizedSample(
            instance_id="Automattic__wp-calypso-23915",
            repo="Automattic/wp-calypso",
            dataset="unit",
            issue_text=(
                "Reader edit link is broken for Jetpack posts. The visible Edit action opens the wrong URL, "
                "so trace the Reader action to the URL builder for post and site identity."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/client/lib/posts/utils.js b/client/lib/posts/utils.js",
                        "--- a/client/lib/posts/utils.js",
                        "+++ b/client/lib/posts/utils.js",
                        "@@ -1,5 +1,5 @@",
                        " export function getEditURL(post, site) {",
                        "-  const postId = post.ID || post.global_ID;",
                        "+  const postId = post.global_ID || post.ID;",
                        "   const siteSlug = site.slug || site.URL;",
                    ]
                )
            },
            gold_files=["client/lib/posts/utils.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["Edit", "ReaderPostActions", "post", "site"],
                "concern_queries": ["Reader edit link wrong URL"],
                "flow_hypotheses": ["user clicks Edit link then href comes from getEditURL URL builder"],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["Edit", "href", "post", "site"],
                    "concerns": ["Reader action URL builder"],
                    "flows": ["ui_event_to_handler", "parameter_or_config_flow"],
                }
            },
            "tool_observations": [
                {
                    "tool": "browser_reproduction_reader",
                    "extracted": {
                        "parsed_reproduction": {
                            "semantic_queries": ["wordpress.com/read post edit link href"],
                            "likely_layers": ["reader action component", "url builder"],
                        }
                    },
                }
            ],
        }
        missing_structure = repo_root / "missing_repo_structure.json"
        localization = dynamic_localize(
            sample,
            evidence,
            repo_root=repo_root,
            structure_path=missing_structure,
            top_k=15,
            max_rounds=3,
        )
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        assert "client/lib/posts/utils.js" in top_files
        assert len(localization["dynamic_rounds"]) >= 2
        assert localization["dynamic_rounds"][0]["next_queries"]
        assert localization["dynamic_rounds"][0]["frontier_state"]["concern_candidates"]
        assert any("getEditURL" in item["id"] for item in localization["ranked_functions"])
        assert any(flow["flow_type"] == "url_builder_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("backend") == "issue_guided_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("chain_edges") for flow in localization["flow_traces"])
        assert any(item["status"] == "ok" for item in localization["round_evaluations"])
        assert any(flow.get("edge_summary") for flow in localization["flow_traces"])
        summary = localization["agent_reasoning_summary"]
        assert summary["rounds"]["round_count"] >= 2
        assert summary["flow_validation"]["flow_type_counts"]["url_builder_flow_chain"] >= 1
        assert summary["program_vertical"]["score_support"]

        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, repo_root=repo_root, structure_path=missing_structure)
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@5"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0


def test_dynamic_agent_does_not_treat_code_url_selector_as_patch_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "client/state/current-user/selectors.js",
            "\n".join(
                [
                    "export const isCurrentUserEmailVerified = createCurrentUserSelector('email_verified', false);",
                    "export function getCurrentUser(state) { return state.currentUser; }",
                ]
            ),
        )
        _write(
            repo_root / "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
            "\n".join(
                [
                    "import { isCurrentUserEmailVerified } from '../../../../state/current-user/selectors';",
                    "",
                    "export function StoreLocationSetupView({ state, redirectToWpAdmin }) {",
                    "  const verified = isCurrentUserEmailVerified(state);",
                    "  if (!verified) {",
                    "    return renderEmailVerificationNotice('WooCommerce store setup');",
                    "  }",
                    "  return redirectToWpAdmin('/wp-admin/admin.php?page=wc-setup');",
                    "}",
                    "export function renderEmailVerificationNotice(message) { return message; }",
                ]
            ),
        )
        _write(
            repo_root / "client/extensions/woocommerce/app/dashboard/index.js",
            "export function Dashboard() { return 'woocommerce dashboard'; }\n",
        )
        _write(
            repo_root / "client/signup/steps/site.js",
            "export function SignupSiteStep() { return 'signup'; }\n",
        )
        sample = NormalizedSample(
            instance_id="Automattic__wp-calypso-21409",
            repo="Automattic/wp-calypso",
            dataset="unit",
            issue_text=(
                "Store signup flow should force email verification before users enter wp-admin. "
                "The issue links isCurrentUserEmailVerified in current-user selectors, but the behavior "
                "belongs to WooCommerce dashboard store setup and address flow."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js b/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "--- a/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "+++ b/client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
                        "@@ -1,4 +1,7 @@",
                        " export function StoreLocationSetupView({ state, redirectToWpAdmin }) {",
                        "+  const verified = isCurrentUserEmailVerified(state);",
                        "+  if (!verified) return renderEmailVerificationNotice('WooCommerce store setup');",
                        "   return redirectToWpAdmin('/wp-admin/admin.php?page=wc-setup');",
                        " }",
                    ]
                )
            },
            gold_files=["client/extensions/woocommerce/app/dashboard/store-location-setup-view.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["isCurrentUserEmailVerified", "email_verified", "wp-admin"],
                "concern_queries": ["WooCommerce dashboard store signup email verification"],
                "flow_hypotheses": ["email verification selector is evidence, downstream WooCommerce dashboard flow is patch target"],
                "code_references": [
                    {
                        "repo": "Automattic/wp-calypso",
                        "path": "client/state/current-user/selectors.js",
                        "line": 157,
                        "role": "code_evidence_seed",
                    }
                ],
                "url_inspections": [
                    {
                        "url": "https://github.com/Automattic/wp-calypso/blob/master/client/state/current-user/selectors.js#L157",
                        "role": "code_evidence_seed",
                        "path": "client/state/current-user/selectors.js",
                        "semantic_terms": ["isCurrentUserEmailVerified", "email_verified", "wp-admin"],
                    }
                ],
                "image_inspections": [
                    {
                        "image_type": "web_ui_screenshot",
                        "visual_queries": ["store signup wp-admin email verification notice"],
                        "likely_layers": ["dashboard flow", "state selector use"],
                    }
                ],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["isCurrentUserEmailVerified", "StoreLocationSetupView", "renderEmailVerificationNotice"],
                    "concerns": ["WooCommerce dashboard email verification"],
                    "flows": ["state_selector_use_chain"],
                }
            },
            "tool_observations": [],
        }
        missing_structure = repo_root / "missing_repo_structure.json"
        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, repo_root=repo_root, structure_path=missing_structure)
        issue_sketch = build_issue_sketch(sample, evidence)
        graph = TypedRepositoryGraph(index)
        agent_round = run_four_tool_agent_round(
            sample=sample,
            evidence_result=evidence,
            index=index,
            graph=graph,
            issue_sketch=issue_sketch,
            queries=["WooCommerce dashboard email verification redirect"],
            top_k=15,
        )
        assert any(role["role"] == "Reference API / code evidence seed" for role in issue_sketch.evidence_roles)
        assert any(policy["navigation"] == "used_by_or_called_by_first" for policy in issue_sketch.seed_policy)
        assert any(item["flow_type"] == "state_selector_use_chain" for item in issue_sketch.flow_obligations)
        assert [item["tool"] for item in agent_round["tool_observations"][:4]] == [
            "SearchAnchor",
            "NavigateCode",
            "NavigateCode",
            "NavigateCode",
        ]
        assert "TraceFlow" in [item["tool"] for item in agent_round["tool_observations"]]
        assert "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js" in agent_round["candidate_paths"]
        assert agent_round["pruning"]["strategy"] == "cosil_inspired_signal_family_beam_for_readcode"
        assert agent_round["summary"]["read_file_count"] <= agent_round["summary"]["read_budget"]
        assert "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js" in agent_round["read_paths"]
        assert agent_round["flow_coverage"]["flow_count"] > 0
        assert "email_verified" in agent_round["flow_coverage"]["covered_states"]
        assert agent_round["flow_coverage"]["candidate_path_count"] > 0
        assert "state_or_selector" in agent_round["flow_coverage"]["roles"]

        localization = dynamic_localize(sample, evidence, repo_root=repo_root, structure_path=missing_structure, top_k=15, max_rounds=3)
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        assert top_files[0] == "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js"
        assert top_files.index("client/extensions/woocommerce/app/dashboard/store-location-setup-view.js") < top_files.index(
            "client/state/current-user/selectors.js"
        )
        first_frontier = localization["dynamic_rounds"][0]["frontier_state"]
        assert first_frontier["query_groups"]["evidence_role"]
        assert first_frontier["agent_round_questions"]["believed_files"]
        target_components = first_frontier["score_components"].get(
            "client/extensions/woocommerce/app/dashboard/store-location-setup-view.js",
            {},
        )
        assert target_components.get("flow_verifier", 0.0) > 0.0
        assert first_frontier["concern_candidates"] or first_frontier["program_candidates"]
        assert any(flow["flow_type"] == "state_selector_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("backend") == "issue_guided_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("chain_edges") for flow in localization["flow_traces"])
        assert any(note["trap"] == "evidence_seed_ranked_as_candidate" for note in localization["verifier"]["trap_notes"])
        summary = localization["agent_reasoning_summary"]
        assert summary["evidence_role_controls"]["trap_notes"]
        assert summary["flow_validation"]["flow_type_counts"]["state_selector_flow_chain"] >= 1
        assert summary["concern_horizontal"]["score_support"]

        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@1"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0


def test_dynamic_agent_moves_from_react_pdf_example_to_stylesheet_pipeline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "packages/examples/src/knobs/index.js",
            "\n".join(
                [
                    "import { Document, Page, View } from '@react-pdf/renderer';",
                    "export const example = { margin: 'auto', width: 100 };",
                    "export function KnobsExample() { return <View style={example} />; }",
                ]
            ),
        )
        _write(
            repo_root / "packages/stylesheet/src/expand.js",
            "\n".join(
                [
                    "export function processBoxModel(key, value) {",
                    "  if (value === 'auto') {",
                    "    return { [key]: 'auto' };",
                    "  }",
                    "  return expandEdges(key, value);",
                    "}",
                    "export function expandEdges(key, value) { return { [key]: value }; }",
                ]
            ),
        )
        _write(
            repo_root / "packages/stylesheet/src/resolve.js",
            "\n".join(
                [
                    "import { processBoxModel } from './expand';",
                    "export function resolveStyles(style) {",
                    "  return processBoxModel('margin', style.margin);",
                    "}",
                ]
            ),
        )
        _write(
            repo_root / "packages/renderer/src/index.js",
            "export function render() { return 'pdf renderer'; }\n",
        )
        sample = NormalizedSample(
            instance_id="diegomura__react-pdf-1178",
            repo="diegomura/react-pdf",
            dataset="unit",
            issue_text=(
                "margin: auto does not work in react-pdf v2. The linked knobs example reproduces the visual layout diff, "
                "but the library should resolve CSS-like stylesheet box model values."
            ),
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/packages/stylesheet/src/expand.js b/packages/stylesheet/src/expand.js",
                        "--- a/packages/stylesheet/src/expand.js",
                        "+++ b/packages/stylesheet/src/expand.js",
                        "@@ -1,3 +1,4 @@",
                        " export function processBoxModel(key, value) {",
                        "+  if (value === 'auto') return { [key]: 'auto' };",
                        "   return expandEdges(key, value);",
                        " }",
                        "diff --git a/packages/stylesheet/src/resolve.js b/packages/stylesheet/src/resolve.js",
                        "--- a/packages/stylesheet/src/resolve.js",
                        "+++ b/packages/stylesheet/src/resolve.js",
                        "@@ -1,3 +1,3 @@",
                        " export function resolveStyles(style) {",
                        "-  return style;",
                        "+  return processBoxModel('margin', style.margin);",
                        " }",
                    ]
                )
            },
            gold_files=["packages/stylesheet/src/expand.js", "packages/stylesheet/src/resolve.js"],
        )
        evidence = {
            "evidence_packet": {
                "symbol_queries": ["margin", "auto", "processBoxModel", "resolveStyles"],
                "concern_queries": ["react-pdf stylesheet margin auto layout"],
                "flow_hypotheses": ["visual layout symptom maps from example reproduction to stylesheet expand resolve pipeline"],
                "code_references": [
                    {
                        "repo": "diegomura/react-pdf",
                        "path": "packages/examples/src/knobs/index.js",
                        "line": 20,
                        "role": "code_evidence_seed",
                    }
                ],
                "url_inspections": [
                    {
                        "url": "https://github.com/diegomura/react-pdf/blob/master/packages/examples/src/knobs/index.js#L20",
                        "role": "code_evidence_seed",
                        "path": "packages/examples/src/knobs/index.js",
                        "semantic_terms": ["margin", "auto", "knobs", "example"],
                    }
                ],
                "image_inspections": [
                    {
                        "image_type": "pdf_layout_diff",
                        "visual_queries": ["margin auto layout diff react-pdf"],
                        "likely_layers": ["stylesheet expansion", "style resolve", "layout engine"],
                    }
                ],
            },
            "deterministic_understanding": {
                "search_queries": {
                    "symbols": ["margin", "auto", "processBoxModel", "resolveStyles"],
                    "concerns": ["stylesheet expand resolve box model"],
                    "flows": ["visual_style_pipeline_flow"],
                }
            },
            "tool_observations": [
                {
                    "tool": "browser_reproduction_reader",
                    "extracted": {
                        "source_files": [
                            {"path": "packages/examples/src/knobs/index.js", "code_preview": "margin: 'auto'"},
                        ],
                        "parsed_reproduction": {
                            "semantic_queries": ["knobs example margin auto"],
                            "likely_layers": ["stylesheet", "layout", "style resolver"],
                        },
                    },
                }
            ],
        }
        missing_structure = repo_root / "missing_repo_structure.json"
        localization = dynamic_localize(sample, evidence, repo_root=repo_root, structure_path=missing_structure, top_k=15, max_rounds=3)
        top_files = [item["path"] for item in localization["ranked_locations"][:5]]
        assert "packages/stylesheet/src/expand.js" in top_files[:3]
        assert "packages/stylesheet/src/resolve.js" in top_files[:3]
        assert top_files.index("packages/stylesheet/src/expand.js") < top_files.index("packages/examples/src/knobs/index.js")
        assert any(flow["flow_type"] == "visual_style_pipeline_flow" for flow in localization["flow_traces"])
        assert any(flow["flow_type"] == "style_pipeline_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("backend") == "issue_guided_flow_chain" for flow in localization["flow_traces"])
        assert any(flow.get("chain_edges") for flow in localization["flow_traces"])
        assert any("style_resolver" in flow.get("role_coverage", {}).get("covered_roles", []) for flow in localization["flow_traces"])
        assert any(
            "flow_verifier" in components
            for components in localization["dynamic_rounds"][0]["frontier_state"]["score_components"].values()
        )
        assert localization["dynamic_rounds"][0]["agent_observation"]["four_tool_agent_flow_coverage"]["flow_count"] > 0
        assert localization["dynamic_rounds"][0]["agent_observation"]["four_tool_agent_pruning"]["kept_for_read"] > 0
        summary = localization["agent_reasoning_summary"]
        assert summary["flow_validation"]["flow_type_counts"]["style_pipeline_flow_chain"] >= 1
        assert summary["program_vertical"]["edge_type_counts_near_top_files"]
        assert any(
            key in summary["multilingual_graph"]["language_counts"]
            for key in ("javascript", "javascript/typescript", "jsx", "tsx")
        )

        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, repo_root=repo_root, structure_path=missing_structure)
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["recall@15"] == 1.0
        assert metrics["function"]["recall@15"] > 0.0


if __name__ == "__main__":
    test_dynamic_agent_tracks_serializer_backend_parameter_flow()
    test_dynamic_agent_records_multi_round_call_dataflow_navigation()
    test_dynamic_agent_does_not_treat_code_url_selector_as_patch_target()
    test_dynamic_agent_moves_from_react_pdf_example_to_stylesheet_pipeline()
