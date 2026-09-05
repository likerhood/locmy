from __future__ import annotations

import re
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mycode.evidence.issue_sketch import IssueSketch, sketch_query_terms
from mycode.flow_analysis.flow_chain import trace_flow_chains
from mycode.flow_analysis.interprocedural_flow import trace_interprocedural_flows
from mycode.flow_analysis.parameter_closure import trace_parameter_closures
from mycode.flow_analysis.program_flow import trace_program_flows
from mycode.flow_analysis.runtime_trace import verify_runtime_traces
from mycode.flow_analysis.static_slice import trace_static_slices
from mycode.flow_analysis.statement_flow import trace_statement_flows
from mycode.repo_index.structure_index import RepositoryIndex, tokenize
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample
from mycode.utils.phase_logger import phase_context, phase_event


@dataclass
class DynamicToolObservation:
    tool: str
    action: str
    status: str
    queries: list[str] = field(default_factory=list)
    seed_files: list[str] = field(default_factory=list)
    seed_entities: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dedupe(values: Iterable[str], *, limit: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _path_role(path: str) -> str:
    lower = _norm(path).lower()
    filename = lower.rsplit("/", 1)[-1]
    first_part = lower.split("/", 1)[0]
    if filename.endswith((".min.js", ".bundle.js", ".map")) or any(
        name in lower for name in ("/dist/", "/build/", "/vendor/", "/generated/")
    ) or first_part in {"dist", "build", "vendor", "generated"}:
        return "generated_or_bundle"
    if lower.startswith("lib/addons/"):
        return "addon_bundle"
    if any(part in lower for part in ("/examples/", "/example/", "/demo/", "/demos/", "/docs/", "/playground/", "/sandbox/", "/manual-test-examples/")) or first_part in {"examples", "example", "demo", "demos", "docs", "playground", "sandbox", "developer_docs"}:
        return "reproduction_or_example"
    if any(part in lower for part in ("/test/", "/tests/", "__tests__", "/fixture/", "/fixtures/")) or first_part in {"test", "tests", "__tests__", "fixture", "fixtures"} or filename.startswith("test_") or ".test." in filename or ".spec." in filename:
        return "test_or_fixture"
    if any(part in lower for part in ("/src/", "/lib/", "/client/", "/packages/", "/java/", "/main/")):
        return "source"
    return "implementation"


def _source_first_multiplier(path: str, issue_text: str) -> tuple[float, str]:
    role = _path_role(path)
    issue = issue_text.lower()
    issue_mentions_test = any(token in issue for token in ("test", "spec", "fixture", "docs", "documentation", "example"))
    if role == "source":
        return 1.18, "source_first:source_boost"
    if role == "addon_bundle":
        return 0.24, "source_first:addon_bundle_downweight"
    if role in {"test_or_fixture", "reproduction_or_example"} and not issue_mentions_test:
        return 0.22, f"source_first:noise_downweight:{role}"
    if role == "generated_or_bundle":
        return 0.08, "source_first:generated_bundle_downweight"
    return 1.0, "source_first:neutral"


def _score_path_for_terms(path: str, text: str, queries: Iterable[str]) -> float:
    haystack = f"{path}\n{text}".lower()
    score = 0.0
    for token in tokenize(" ".join(queries)):
        if len(token) < 3:
            continue
        if token in path.lower():
            score += 5.0
        count = haystack.count(token)
        if count:
            score += min(count, 6) * 0.8
    return score


def _tokens_from_values(values: Iterable[Any]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in tokenize(str(value or "")):
            if len(token) >= 3:
                tokens.add(token)
    return tokens


def _flow_candidate_paths(flow: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    paths.extend(str(path or "") for path in flow.get("candidate_target_paths", []) or [])
    for key in ("path", "source", "target"):
        value = flow.get(key)
        if isinstance(value, str) and "/" in value:
            paths.append(value)
    for location in flow.get("locations", []) or []:
        if isinstance(location, dict):
            paths.append(str(location.get("path") or ""))
    for step_key in ("source_steps", "sink_steps", "steps", "statement_nodes"):
        for step in flow.get(step_key, []) or []:
            if isinstance(step, dict):
                paths.append(str(step.get("path") or ""))
    for edge_key in ("edges", "chain_edges", "statement_edges", "trace_edges", "def_use_edges", "call_boundary_edges"):
        for edge in flow.get(edge_key, []) or []:
            if not isinstance(edge, dict):
                continue
            paths.extend([str(edge.get("source") or ""), str(edge.get("target") or "")])
    return _dedupe([_norm(path) for path in paths if "/" in str(path)], limit=80)


def _flow_roles(flow: dict[str, Any]) -> list[str]:
    roles: list[str] = []
    role_coverage = flow.get("role_coverage", {}) or {}
    if isinstance(role_coverage, dict):
        roles.extend(str(role or "") for role in role_coverage.get("covered_roles", []) or [])
        roles.extend(str(role or "") for role in (role_coverage.get("roles", {}) or {}).keys())
    roles.extend(str(role or "") for role in (flow.get("path_roles", {}) or {}).values())
    for location in flow.get("locations", []) or []:
        if isinstance(location, dict):
            roles.append(str(location.get("role") or ""))
    for step_key in ("source_steps", "sink_steps", "steps", "statement_nodes"):
        for step in flow.get(step_key, []) or []:
            if isinstance(step, dict):
                roles.append(str(step.get("role") or ""))
    return _dedupe(roles, limit=40)


def _flow_coverage_summary(
    flows: list[dict[str, Any]],
    issue_sketch: IssueSketch,
    candidate_paths: list[str],
) -> dict[str, Any]:
    """Summarize whether flow evidence covers the issue's state/effect contract."""

    flow_types = _dedupe([str(flow.get("flow_type") or "") for flow in flows], limit=40)
    flow_backends = _dedupe([str(flow.get("backend") or "") for flow in flows], limit=40)
    flow_terms = _dedupe([str(flow.get("term") or "") for flow in flows], limit=80)
    roles: list[str] = []
    paths: list[str] = []
    confidence_values: list[float] = []
    text_fragments: list[Any] = []
    for flow in flows:
        roles.extend(_flow_roles(flow))
        paths.extend(_flow_candidate_paths(flow))
        text_fragments.extend(
            [
                flow.get("flow_type"),
                flow.get("term"),
                flow.get("reason"),
                flow.get("edge_summary"),
                flow.get("role_coverage"),
                flow.get("path_roles"),
            ]
        )
        try:
            confidence_values.append(float(flow.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass

    candidate_path_set = {_norm(path) for path in candidate_paths if _norm(path)}
    path_set = {_norm(path) for path in paths if _norm(path)}
    evidence_tokens = _tokens_from_values(text_fragments + flow_terms + flow_types + list(path_set))

    def covered_terms(terms: Iterable[str]) -> tuple[list[str], list[str]]:
        covered: list[str] = []
        missing: list[str] = []
        for term in terms:
            term_text = str(term or "").strip()
            if not term_text:
                continue
            term_tokens = _tokens_from_values([term_text])
            if term_tokens and (term_tokens & evidence_tokens):
                covered.append(term_text)
            else:
                missing.append(term_text)
        return _dedupe(covered, limit=40), _dedupe(missing, limit=40)

    covered_states, missing_states = covered_terms(issue_sketch.states)
    covered_effects, missing_effects = covered_terms(issue_sketch.expected_effects)
    return {
        "flow_count": len(flows),
        "flow_types": flow_types,
        "flow_backends": flow_backends,
        "flow_terms": flow_terms[:30],
        "covered_states": covered_states,
        "missing_states": missing_states,
        "covered_effects": covered_effects,
        "missing_effects": missing_effects,
        "roles": _dedupe(roles, limit=40),
        "candidate_paths_from_flow": sorted(path_set)[:60],
        "candidate_path_count": len(path_set),
        "overlap_with_dynamic_frontier": sorted(path_set & candidate_path_set)[:60],
        "confidence_max": round(max(confidence_values), 3) if confidence_values else 0.0,
        "confidence_avg": round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else 0.0,
        "precision_note": (
            "Flow coverage is a lightweight static/runtime-evidence summary; "
            "it is not a complete statement-level def-use closure."
        ),
    }


class SearchAnchorTool:
    """Find the first anchors from entities, concerns and effects."""

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index

    def run(self, issue_sketch: IssueSketch, queries: list[str], *, limit: int = 12) -> DynamicToolObservation:
        with phase_context("four_tool.SearchAnchor", tool="SearchAnchor", limit=limit):
            sketch_terms = sketch_query_terms(issue_sketch)
            active_queries = _dedupe(issue_sketch.entities + issue_sketch.states + issue_sketch.concerns + queries + sketch_terms)
            file_hits = self.index.search_files(active_queries, limit=max(limit, 20))
            entity_hits = self.index.search_entities(active_queries, limit=max(limit, 30))
            phase_event(
                "progress",
                "four_tool.SearchAnchor",
                tool="SearchAnchor",
                query_count=len(active_queries),
                file_hit_count=len(file_hits),
                entity_hit_count=len(entity_hits),
            )

        candidates: list[dict[str, Any]] = []
        for hit in file_hits[:limit]:
            candidates.append(
                {
                    "path": hit.path,
                    "score": round(hit.score, 3),
                    "source": "file_search",
                    "reasons": hit.reasons[:6],
                }
            )
        for hit in entity_hits[:limit]:
            candidates.append(
                {
                    "path": hit.path,
                    "kind": hit.kind,
                    "name": hit.name,
                    "score": round(hit.score, 3),
                    "source": "entity_search",
                    "reasons": hit.reasons[:6],
                }
            )

        seed_files = _dedupe(
            [hit.path for hit in file_hits[:limit]]
            + [hit.path for hit in entity_hits[:limit]]
            + [str(item.get("path") or "") for item in issue_sketch.seed_policy],
            limit=limit * 2,
        )
        seed_entities = _dedupe([f"{hit.kind}:{hit.path}:{hit.name}" for hit in entity_hits[:limit]], limit=limit)
        observation = DynamicToolObservation(
            tool="SearchAnchor",
            action="search entity/concern/effect anchors",
            status="ok" if candidates else "empty",
            queries=active_queries[:40],
            seed_files=seed_files,
            seed_entities=seed_entities,
            candidates=candidates[: limit * 2],
            notes=[
                "Anchor search uses issue sketch fields, not gold files.",
                "Evidence seeds are included as navigation anchors and handled by verifier before ranking.",
            ],
            metadata={"file_hit_count": len(file_hits), "entity_hit_count": len(entity_hits)},
        )
        phase_event("progress", "four_tool.SearchAnchor", tool="SearchAnchor", candidate_count=len(observation.candidates))
        return observation


class NavigateCodeTool:
    """Navigate typed repository graph along concern/call/used-by relations."""

    EDGE_GROUPS = {
        "concern": [
            "renders",
            "reverse_renders",
            "incoming_renders",
            "selects_state",
            "reverse_selects_state",
            "incoming_selects_state",
            "dispatches_action",
            "reverse_dispatches_action",
            "incoming_dispatches_action",
            "uses_hook",
            "reverse_uses_hook",
            "incoming_uses_hook",
            "binds_ui_event",
            "reverse_binds_ui_event",
            "incoming_binds_ui_event",
            "hook_flow",
            "reverse_hook_flow",
            "incoming_hook_flow",
            "styles",
            "reverse_styles",
            "incoming_styles",
            "configures",
            "reverse_configures",
            "incoming_configures",
            "documents",
            "reverse_documents",
            "incoming_documents",
            "same_directory",
        ],
        "call": [
            "calls",
            "reverse_calls",
            "incoming_calls",
            "imports",
            "reverse_imports",
            "incoming_imports",
            "inherits_or_implements",
            "reverse_inherits_or_implements",
            "incoming_inherits_or_implements",
            "overrides",
            "reverse_overrides",
            "incoming_overrides",
            "uses_hook",
            "reverse_uses_hook",
            "incoming_uses_hook",
            "hook_flow",
            "reverse_hook_flow",
            "incoming_hook_flow",
            "type_flow",
            "reverse_type_flow",
            "incoming_type_flow",
        ],
        "used_by": [
            "reverse_calls",
            "incoming_calls",
            "reverse_imports",
            "incoming_imports",
            "reverse_renders",
            "incoming_renders",
            "reverse_selects_state",
            "incoming_selects_state",
            "reverse_dispatches_action",
            "incoming_dispatches_action",
            "reverse_uses_hook",
            "incoming_uses_hook",
            "reverse_binds_ui_event",
            "incoming_binds_ui_event",
            "reverse_hook_flow",
            "incoming_hook_flow",
            "reverse_overrides",
            "incoming_overrides",
        ],
    }

    def __init__(self, graph: TypedRepositoryGraph) -> None:
        self.graph = graph

    def run(self, seed_files: list[str], queries: list[str], *, mode: str, limit: int = 20) -> DynamicToolObservation:
        with phase_context("four_tool.NavigateCode", tool="NavigateCode", mode=mode, seed_count=len(seed_files), limit=limit):
            allowed_edges = self.EDGE_GROUPS.get(mode, self.EDGE_GROUPS["concern"] + self.EDGE_GROUPS["call"])
            hits = self.graph.expand(seed_files, queries, limit=limit, allowed_edge_types=allowed_edges)
            if not hits and allowed_edges:
                hits = self.graph.expand(seed_files, queries, limit=limit)
            phase_event(
                "progress",
                "four_tool.NavigateCode",
                tool="NavigateCode",
                mode=mode,
                hit_count=len(hits),
                edge_type_count=len(allowed_edges),
            )
        return DynamicToolObservation(
            tool="NavigateCode",
            action=f"navigate {mode} edges",
            status="ok" if hits else "empty",
            queries=queries[:30],
            seed_files=seed_files[:20],
            candidates=[
                {
                    "path": path,
                    "score": round(score, 3),
                    "mode": mode,
                    "edge_reasons": reasons[:6],
                }
                for path, score, reasons in hits
            ],
            notes=[
                "Typed edges are navigation/semantic edges, not full statement-level dataflow.",
                "Concern mode is horizontal; call/used_by mode is vertical.",
            ],
            metadata={"allowed_edge_types": allowed_edges, "hit_count": len(hits)},
        )


class TraceFlowTool:
    """Validate whether candidate code can carry issue state into expected behavior."""

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index

    def run(
        self,
        sample: NormalizedSample,
        evidence_result: dict[str, Any],
        issue_sketch: IssueSketch,
        queries: list[str],
        candidate_paths: list[str],
        *,
        limit: int = 10,
    ) -> DynamicToolObservation:
        flow_queries = _dedupe(queries + sketch_query_terms(issue_sketch), limit=120)
        mode = os.environ.get("MYCODE_FLOW_MODE", "auto").strip().lower()
        obligation_types = {
            str(item.get("flow_type") or "")
            for item in issue_sketch.flow_obligations
            if isinstance(item, dict)
        }
        high_value_obligation = bool(
            obligation_types.intersection(
                {
                    "serializer_backend_call_chain",
                    "python_type_binding_flow",
                    "ui_event_to_handler",
                    "url_builder_or_route_flow",
                }
            )
        )
        run_deep = mode == "deep" or (
            mode == "auto"
            and (_env_bool("MYCODE_FOUR_TOOL_DEEP_FLOW", False) or high_value_obligation)
            and len(candidate_paths) <= _env_int("MYCODE_FOUR_TOOL_DEEP_FLOW_CANDIDATES", 24, minimum=1)
        )
        run_statement = run_deep or (
            _env_bool("MYCODE_LIGHT_STATEMENT_FLOW", True)
            and len(candidate_paths) <= _env_int("MYCODE_LIGHT_STATEMENT_FLOW_CANDIDATES", 20, minimum=1)
        )
        timings: list[dict[str, Any]] = []

        def run_backend(name: str, fn, **kwargs: Any) -> list[dict[str, Any]]:
            start = time.perf_counter()
            phase_event("start", "four_tool.TraceFlow.backend", tool="TraceFlow", backend=name, candidate_count=len(candidate_paths))
            result = fn(
                self.index,
                issue_text=sample.issue_text,
                tool_observations=evidence_result.get("tool_observations", []) or [],
                queries=flow_queries,
                limit=limit,
                **kwargs,
            )
            elapsed = round(time.perf_counter() - start, 3)
            timings.append({"backend": name, "elapsed_seconds": elapsed, "count": len(result)})
            phase_event("end", "four_tool.TraceFlow.backend", tool="TraceFlow", backend=name, elapsed_seconds=elapsed, flow_count=len(result))
            return result

        with phase_context(
            "four_tool.TraceFlow",
            tool="TraceFlow",
            mode=mode,
            run_deep=run_deep,
            candidate_count=len(candidate_paths),
            limit=limit,
        ):
            program_flows = run_backend("trace_program_flows", trace_program_flows)
            closures = run_backend("trace_parameter_closures", trace_parameter_closures)
            statement_flows: list[dict[str, Any]] = []
            static_slices: list[dict[str, Any]] = []
            interprocedural_flows: list[dict[str, Any]] = []
            flow_chains: list[dict[str, Any]] = []
            runtime_traces: list[dict[str, Any]] = []
            if run_statement:
                statement_flows = run_backend("trace_statement_flows", trace_statement_flows)
            if run_deep:
                static_slices = run_backend("trace_static_slices", trace_static_slices)
                interprocedural_flows = run_backend(
                    "trace_interprocedural_flows",
                    trace_interprocedural_flows,
                    candidate_paths=candidate_paths,
                    max_hops=2,
                )
                flow_chains = run_backend("trace_flow_chains", trace_flow_chains)
                runtime_traces = run_backend("verify_runtime_traces", verify_runtime_traces)
        obligation_terms = [
            f"{item.get('flow_type', '')} {item.get('state', '')} {item.get('behavior', '')}"
            for item in issue_sketch.flow_obligations
        ]
        validation_checks: list[dict[str, Any]] = []
        for path in candidate_paths[:30]:
            text = self.index.files.get(path, "")
            score = _score_path_for_terms(path, text, obligation_terms + issue_sketch.states + issue_sketch.expected_effects)
            if score <= 0:
                continue
            matched = [
                item.get("flow_type")
                for item in issue_sketch.flow_obligations
                if any(token in f"{path}\n{text}".lower() for token in tokenize(str(item)))
            ]
            validation_checks.append(
                {
                    "path": path,
                    "score": round(score, 3),
                    "matched_obligations": _dedupe(matched, limit=10),
                }
            )

        candidates = []
        for flow in (
            program_flows
            + closures
            + statement_flows
            + static_slices
            + interprocedural_flows
            + flow_chains
            + runtime_traces
        ):
            for path in flow.get("candidate_target_paths", []) or []:
                candidates.append(
                    {
                        "path": path,
                        "source": "flow_candidate_target",
                        "flow_type": flow.get("flow_type"),
                        "term": flow.get("term"),
                        "confidence": flow.get("confidence"),
                    }
                )
        for item in validation_checks:
            candidates.append({"path": item["path"], "source": "issue_obligation_check", "score": item["score"]})

        all_flows = (
            program_flows
            + closures
            + statement_flows
            + static_slices
            + interprocedural_flows
            + flow_chains
            + runtime_traces
        )
        flow_coverage = _flow_coverage_summary(all_flows, issue_sketch, candidate_paths)
        coverage_note = "Flow evidence covers the current issue state/effect contract."
        if flow_coverage["missing_states"] or flow_coverage["missing_effects"]:
            coverage_note = (
                "Flow evidence is partial; missing states/effects should stay in the next search queries "
                "instead of being treated as resolved."
            )

        return DynamicToolObservation(
            tool="TraceFlow",
            action="check state/effect obligations against candidate code",
            status="ok" if candidates or all_flows else "empty",
            queries=flow_queries[:40],
            seed_files=candidate_paths[:20],
            candidates=candidates[:40],
            notes=[
                "This is the ARISE-inspired verification layer: state/parameter/effect flow is checked after candidate discovery.",
                "StaticSlice adds ARISE-inspired statement def-use/call-boundary evidence, while RuntimeTraceVerifier adds DAIRA-style observed execution evidence when available.",
                "InterproceduralFlow adds a best-effort cross-function parameter/state closure inspired by ARISE's def-use graph.",
                "Current implementation records missing precision backends for future SCIP/CodeQL/tree-sitter upgrades.",
                coverage_note,
            ],
            metadata={
                "flow_obligations": issue_sketch.flow_obligations,
                "program_flows": program_flows,
                "parameter_closures": closures,
                "statement_flows": statement_flows,
                "static_slices": static_slices,
                "interprocedural_flows": interprocedural_flows,
                "flow_chains": flow_chains,
                "runtime_trace_verifications": runtime_traces,
                "validation_checks": validation_checks,
                "flow_coverage": flow_coverage,
                "flow_mode": mode,
                "deep_flow_enabled": run_deep,
                "backend_timings": timings,
            },
        )


class ReadCodeTool:
    """Read short snippets from top candidate files for agent-side judgment."""

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index

    def run(self, paths: list[str], queries: list[str], *, limit: int = 8) -> DynamicToolObservation:
        with phase_context("four_tool.ReadCode", tool="ReadCode", path_count=len(paths), limit=limit):
            terms = [term for term in tokenize(" ".join(queries)) if len(term) >= 3][:80]
            candidates: list[dict[str, Any]] = []
            for path in _dedupe(paths, limit=limit):
                multiplier, reason = _source_first_multiplier(path, " ".join(queries))
                if multiplier < 0.1:
                    phase_event("progress", "four_tool.ReadCode", tool="ReadCode", skipped_path=path, skip_reason=reason)
                    continue
                text = self.index.files.get(path, "")
                if not text:
                    continue
                lines = text.splitlines()
                snippets = []
                for idx, line in enumerate(lines):
                    lower = line.lower()
                    if not any(term in lower for term in terms):
                        continue
                    start = max(0, idx - 2)
                    end = min(len(lines), idx + 5)
                    snippets.append(
                        {
                            "start_line": start + 1,
                            "end_line": end,
                            "text": "\n".join(f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)),
                        }
                    )
                    if len(snippets) >= 2:
                        break
                candidates.append(
                    {
                        "path": path,
                        "snippets": snippets,
                        "entities": [
                            {"kind": entity.kind, "name": entity.name, "start_line": entity.start_line, "end_line": entity.end_line}
                            for entity in self.index.entities
                            if _norm(entity.path) == _norm(path)
                        ][:8],
                    }
                )
            phase_event("progress", "four_tool.ReadCode", tool="ReadCode", candidate_count=len(candidates))
            return DynamicToolObservation(
            tool="ReadCode",
            action="read concise snippets from candidate files",
            status="ok" if candidates else "empty",
            queries=queries[:30],
            seed_files=paths[:20],
            candidates=candidates,
            notes=["ReadCode is deliberately late: it reads candidates after evidence role, navigation and flow checks."],
        )


def _candidate_paths_from_observations(observations: Iterable[DynamicToolObservation]) -> list[str]:
    paths: list[str] = []
    for observation in observations:
        paths.extend(observation.seed_files)
        for candidate in observation.candidates:
            paths.append(str(candidate.get("path") or ""))
    return _dedupe(paths, limit=80)


def _low_modification_seed_paths(issue_sketch: IssueSketch) -> set[str]:
    paths: set[str] = set()
    for policy in issue_sketch.seed_policy:
        if str(policy.get("modification_prior") or "").lower() != "low":
            continue
        path = _norm(str(policy.get("path") or ""))
        if path:
            paths.add(path)
    return paths


def _signal_score(tool: str, candidate: dict[str, Any]) -> tuple[str, float, str]:
    source = str(candidate.get("source") or "")
    mode = str(candidate.get("mode") or "")
    flow_type = str(candidate.get("flow_type") or "")
    if tool == "TraceFlow":
        if source == "issue_obligation_check":
            return "flow_validation", 4.2, "state/effect obligation matched candidate code"
        if flow_type:
            return "flow_validation", 4.8, f"flow candidate from {flow_type}"
        return "flow_validation", 3.2, "flow verifier returned candidate"
    if tool == "NavigateCode":
        if mode == "concern":
            return "concern_navigation", 2.7, "kept by horizontal concern navigation"
        if mode == "used_by":
            return "used_by_navigation", 3.2, "kept by used-by navigation from evidence seed"
        return "call_navigation", 3.0, "kept by vertical call/import navigation"
    if tool == "SearchAnchor":
        if source == "entity_search":
            return "entity_anchor", 2.2, "matched code entity anchor"
        return "concern_anchor", 1.8, "matched issue concern/file anchor"
    return "other", 1.0, "kept by tool observation"


def _prune_paths_for_read(
    *,
    index: RepositoryIndex,
    candidate_paths: list[str],
    observations: list[DynamicToolObservation],
    issue_sketch: IssueSketch,
    queries: list[str],
    top_k: int,
    read_budget: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """CoSIL-inspired frontier pruning before expensive code reading.

    The point is not to throw away graph evidence. It is to keep a small,
    diverse beam that preserves each evidence family: anchor, concern
    navigation, call/used-by navigation, and flow validation.
    """

    budget = max(8, read_budget or max(top_k, 15))
    scores: dict[str, float] = {}
    signals: dict[str, set[str]] = {}
    reasons: dict[str, list[str]] = {}
    low_seed_paths = _low_modification_seed_paths(issue_sketch)

    def add(path: str, amount: float, signal: str, reason: str) -> None:
        norm = _norm(path)
        if not norm:
            return
        scores[norm] = scores.get(norm, 0.0) + amount
        signals.setdefault(norm, set()).add(signal)
        reasons.setdefault(norm, [])
        if reason and reason not in reasons[norm]:
            reasons[norm].append(reason)

    for rank, path in enumerate(candidate_paths):
        add(path, max(0.15, 2.2 - 0.025 * rank), "frontier_order", "appeared in merged dynamic frontier")

    for observation in observations:
        for seed_file in observation.seed_files:
            add(seed_file, 0.6, "seed_context", f"seed from {observation.tool}")
        for rank, candidate in enumerate(observation.candidates):
            path = str(candidate.get("path") or "")
            signal, amount, reason = _signal_score(observation.tool, candidate)
            add(path, max(0.2, amount - 0.05 * rank), signal, reason)

    semantic_queries = _dedupe(
        queries
        + issue_sketch.workflow
        + issue_sketch.concerns
        + issue_sketch.states
        + issue_sketch.expected_effects
        + issue_sketch.entities,
        limit=120,
    )
    for path in list(scores):
        text = index.files.get(path, "")
        lexical = _score_path_for_terms(path, text[:20_000], semantic_queries)
        if lexical:
            add(path, min(3.5, lexical * 0.12), "semantic_match", "path/content matches issue sketch terms")
        if path in low_seed_paths:
            add(path, -3.0, "evidence_seed_penalty", "explicit code URL seed has low modification prior")

    ranked = sorted(scores, key=lambda path: (-scores[path], path))
    selected: dict[str, str] = {}

    def keep(paths: Iterable[str], why: str, limit: int) -> None:
        count = 0
        for path in paths:
            if path in selected:
                continue
            selected[path] = why
            count += 1
            if len(selected) >= budget or count >= limit:
                break

    keep(ranked, "overall_beam", max(4, min(top_k, budget)))
    per_signal_limits = {
        "flow_validation": 6,
        "call_navigation": 4,
        "used_by_navigation": 4,
        "concern_navigation": 4,
        "entity_anchor": 3,
        "concern_anchor": 3,
    }
    for signal, limit in per_signal_limits.items():
        signal_ranked = [path for path in ranked if signal in signals.get(path, set())]
        keep(signal_ranked, f"signal_family:{signal}", limit)
        if len(selected) >= budget:
            break
    if len(selected) < min(budget, len(ranked)):
        keep(ranked, "fill_by_score", budget)

    read_paths = list(selected.keys())[:budget]
    diagnostics = {
        "strategy": "cosil_inspired_signal_family_beam_for_readcode",
        "input_candidates": len(_dedupe(candidate_paths, limit=500)),
        "kept_for_read": len(read_paths),
        "read_budget": budget,
        "per_signal_limits": per_signal_limits,
        "low_modification_seed_paths": sorted(low_seed_paths),
        "top_reasons": [
            {
                "path": path,
                "score": round(scores.get(path, 0.0), 3),
                "signals": sorted(signals.get(path, set())),
                "keep_reason": selected.get(path),
                "reasons": reasons.get(path, [])[:6],
            }
            for path in read_paths[:20]
        ],
    }
    return read_paths, diagnostics


def run_four_tool_agent_round(
    *,
    sample: NormalizedSample,
    evidence_result: dict[str, Any],
    index: RepositoryIndex,
    graph: TypedRepositoryGraph,
    issue_sketch: IssueSketch,
    queries: list[str],
    previous_candidates: list[str] | None = None,
    top_k: int = 15,
) -> dict[str, Any]:
    """A compact CoSIL-style dynamic tool cycle.

    The cycle is intentionally deterministic in this module: an outer LLM agent
    can call the same four tools, while tests and offline runs remain stable.
    """

    previous_candidates = previous_candidates or []
    base_queries = _dedupe(queries + sketch_query_terms(issue_sketch), limit=140)

    search = SearchAnchorTool(index).run(issue_sketch, base_queries, limit=max(10, top_k))
    seed_files = _dedupe(previous_candidates + search.seed_files, limit=40)
    navigation_modes = ["concern", "call"]
    if any("used_by" in str(policy.get("navigation") or "") for policy in issue_sketch.seed_policy):
        navigation_modes.insert(0, "used_by")

    navigator = NavigateCodeTool(graph)
    nav_observations = [
        navigator.run(seed_files, base_queries, mode=mode, limit=max(20, top_k * 2))
        for mode in navigation_modes
    ]
    candidate_paths = _candidate_paths_from_observations([search] + nav_observations)
    trace = TraceFlowTool(index).run(
        sample,
        evidence_result,
        issue_sketch,
        base_queries,
        candidate_paths,
        limit=max(8, min(14, top_k)),
    )
    flow_candidates = [str(item.get("path") or "") for item in trace.candidates]
    candidate_paths = _dedupe(candidate_paths + flow_candidates, limit=80)
    pre_read_observations = [search] + nav_observations + [trace]
    read_paths, pruning = _prune_paths_for_read(
        index=index,
        candidate_paths=candidate_paths,
        observations=pre_read_observations,
        issue_sketch=issue_sketch,
        queries=base_queries,
        top_k=top_k,
        read_budget=max(12, min(24, top_k * 2)),
    )
    read = ReadCodeTool(index).run(read_paths, base_queries, limit=max(8, min(16, len(read_paths))))
    observations = [search] + nav_observations + [trace, read]

    next_queries = _dedupe(
        issue_sketch.concerns
        + issue_sketch.states
        + issue_sketch.expected_effects
        + [str(item.get("flow_type") or "") for item in issue_sketch.flow_obligations]
        + [Path(path).stem for path in candidate_paths[:20]],
        limit=60,
    )
    flow_traces = (
        (trace.metadata.get("program_flows", []) or [])
        + (trace.metadata.get("parameter_closures", []) or [])
        + (trace.metadata.get("statement_flows", []) or [])
        + (trace.metadata.get("static_slices", []) or [])
        + (trace.metadata.get("interprocedural_flows", []) or [])
        + (trace.metadata.get("flow_chains", []) or [])
        + (trace.metadata.get("runtime_trace_verifications", []) or [])
    )
    flow_coverage = trace.metadata.get("flow_coverage", {}) or {}

    return {
        "status": "ok",
        "strategy": "SearchAnchor -> NavigateCode(concern/call/used_by) -> TraceFlow -> ReadCode",
        "tool_observations": [observation.to_dict() for observation in observations],
        "seed_files": seed_files,
        "candidate_paths": candidate_paths[:80],
        "read_paths": read_paths,
        "next_queries": next_queries,
        "flow_traces": flow_traces,
        "flow_coverage": flow_coverage,
        "pruning": pruning,
        "summary": {
            "anchor_count": len(search.candidates),
            "navigation_modes": navigation_modes,
            "candidate_count": len(candidate_paths),
            "flow_obligation_count": len(issue_sketch.flow_obligations),
            "flow_coverage_roles": flow_coverage.get("roles", []),
            "missing_flow_states": flow_coverage.get("missing_states", []),
            "missing_flow_effects": flow_coverage.get("missing_effects", []),
            "read_file_count": len(read.candidates),
            "pre_read_candidate_count": pruning["input_candidates"],
            "read_budget": pruning["read_budget"],
            "note": "Concern edges expand horizontally, call/used_by edges navigate vertically, TraceFlow validates state/effect obligations.",
        },
    }
