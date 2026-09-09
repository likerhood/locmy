from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mycode.dynamic_retrieval.controller import LLMController
from mycode.dynamic_retrieval.tools import (
    DynamicToolObservation,
    NavigateCodeTool,
    ReadCodeTool,
    SearchAnchorTool,
    TraceFlowTool,
)
from mycode.evidence.issue_sketch import IssueSketch, sketch_query_terms
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.repo_index.typed_graph import TypedRepositoryGraph
from mycode.schemas.evidence import NormalizedSample


@dataclass
class ReActStep:
    round_no: int
    thought: str
    tool: str
    action: str
    tool_input: dict[str, Any]
    observation: dict[str, Any]
    candidate_paths: list[str] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    llm_raw: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

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


def _path_candidates(observation: DynamicToolObservation | dict[str, Any]) -> list[str]:
    if isinstance(observation, DynamicToolObservation):
        data = observation.to_dict()
    else:
        data = observation
    paths: list[str] = []
    for candidate in data.get("candidates", []) or []:
        path = str(candidate.get("path") or "")
        if path:
            paths.append(path)
    paths.extend(data.get("seed_files", []) or [])
    return _dedupe(paths, limit=80)


def _merge_frontier(previous: Iterable[str], observed: Iterable[str], *, limit: int = 100) -> list[str]:
    """Put fresh tool observations first so stale anchors cannot pin the beam."""

    return _dedupe(list(observed) + list(previous), limit=limit)


def _required_tool_order(issue_sketch: IssueSketch) -> list[str]:
    if str(getattr(issue_sketch, "task_type", "unknown")) == "feature_request":
        return ["SearchAnchor", "ReadCode", "NavigateCode", "TraceFlow"]
    return ["SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode"]


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _observation_flow_signatures(observation: DynamicToolObservation | None) -> set[str]:
    if observation is None:
        return set()
    signatures: set[str] = set()
    metadata = observation.metadata or {}
    for key in (
        "program_flows",
        "parameter_closures",
        "statement_flows",
        "static_slices",
        "interprocedural_flows",
        "flow_chains",
        "runtime_trace_verifications",
    ):
        for flow in metadata.get(key, []) or []:
            if not isinstance(flow, dict):
                continue
            signatures.add(
                "|".join(
                    [
                        str(flow.get("flow_type") or key),
                        str(flow.get("term") or ""),
                        str(flow.get("backend") or ""),
                    ]
                ).lower()
            )
    return signatures


def _token_usage_from_response(response: Any) -> dict[str, int]:
    if not isinstance(response, dict):
        return {}
    usage = response.get("usage") or response.get("token_usage") or {}
    if not usage and isinstance(response.get("raw_response"), dict):
        usage = response["raw_response"].get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _planner_prompt(
    *,
    sample: NormalizedSample,
    issue_sketch: IssueSketch,
    queries: list[str],
    previous_candidates: list[str],
    step_no: int,
    history: list[dict[str, Any]],
) -> str:
    completed_signatures = [str(item.get("action_signature") or "") for item in history if item.get("action_signature")]
    return (
        "You are the evidence-gap controller for repository issue localization. Choose exactly one action "
        "that resolves the highest-priority missing evidence, or stop when no required gap remains.\n"
        "Return compact JSON with keys: tool, mode, queries, resolves_gap, decision_basis, stop, stop_reason.\n"
        "Available tools: SearchAnchor, NavigateCode, TraceFlow, ReadCode.\n"
        "SearchAnchor finds concern/entity/effect anchors.\n"
        "NavigateCode modes: concern, call, used_by.\n"
        "TraceFlow validates state/parameter/effect propagation.\n"
        "ReadCode reads pruned candidate files.\n\n"
        "Visual implementation details are hypotheses until ReadCode confirms them; do not search invented CSS selectors, HTML tags, or conditions.\n"
        "For feature requests, an execution path may not exist yet. Search analogous existing capabilities, ownership/configuration components, action types, reducers, and data-layer handlers.\n"
        "Use ReadCode when the leading candidate lacks source verification. Use TraceFlow only when both endpoints "
        "are grounded in observed source entities. Do not expand from navigation-only, generated, test, documentation, "
        "visual-only, or external-reproduction candidates.\n"
        "Do not repeat a completed action signature after a no-gain result. Do not run every tool merely for coverage. "
        "Stop when a source-read candidate has direct entity and program-flow evidence and no required responsibility "
        "obligation remains unresolved. Keep queries concrete and repository-oriented.\n\n"
        f"Instance: {sample.instance_id}\n"
        f"Repo: {sample.repo}\n"
        f"Task type: {getattr(issue_sketch, 'task_type', 'unknown')}\n"
        f"Workflow: {issue_sketch.workflow[:6]}\n"
        f"Concerns: {issue_sketch.concerns[:10]}\n"
        f"States: {issue_sketch.states[:10]}\n"
        f"Expected effects: {issue_sketch.expected_effects[:10]}\n"
        f"Entities: {issue_sketch.entities[:10]}\n"
        f"Architecture queries: {list(getattr(issue_sketch, 'architectural_queries', []) or [])[:10]}\n"
        f"Seed policy: {issue_sketch.seed_policy[:6]}\n"
        f"Queries: {queries[:18]}\n"
        f"Previous candidates: {previous_candidates[:12]}\n"
        f"Recent evidence state: {history[-6:]}\n"
        f"Completed action signatures: {completed_signatures[-12:]}\n"
        f"Step: {step_no}\n"
    )


def _message_text_from_openai_response(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning.strip()
    return ""


def _parse_planner_response(response: str | dict[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(response, dict):
        if response.get("tool"):
            return response, json.dumps(response, ensure_ascii=False)
        content = response.get("content") or response.get("text")
        if not content and isinstance(response.get("raw_response"), dict):
            content = _message_text_from_openai_response(response["raw_response"])
        text = str(content or "").strip()
        if not text:
            return {}, json.dumps(response, ensure_ascii=False)
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1]), text
                except json.JSONDecodeError:
                    pass
        return {"thought": text[:500]}, text
    text = str(response or "").strip()
    if not text:
        return {}, ""
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1]), text
            except json.JSONDecodeError:
                pass
    return {"thought": text[:500]}, text


def _heuristic_move(
    *,
    step_no: int,
    issue_sketch: IssueSketch,
    queries: list[str],
    candidate_paths: list[str],
) -> dict[str, Any]:
    evidence_text = " ".join(
        str(policy.get("role") or "") + " " + str(policy.get("navigation") or "") for policy in issue_sketch.seed_policy
    ).lower()
    architecture_queries = list(getattr(issue_sketch, "architectural_queries", []) or [])
    task_type = str(getattr(issue_sketch, "task_type", "unknown") or "unknown")
    sketch_text = " ".join(issue_sketch.concerns + issue_sketch.states + issue_sketch.expected_effects + architecture_queries + queries).lower()
    if step_no == 1:
        return {
            "thought": "Start from issue sketch anchors so URL/image evidence is not treated as a gold target too early.",
            "tool": "SearchAnchor",
            "mode": "concern",
            "queries": queries,
            "stop": False,
        }
    if step_no == 2 and ("used_by" in evidence_text or "reference" in evidence_text):
        return {
            "thought": "Explicit code URL looks like evidence. Walk used_by/called_by before ranking the linked file.",
            "tool": "NavigateCode",
            "mode": "used_by",
            "queries": queries + ["downstream users", "caller consumer component"],
            "stop": False,
        }
    if step_no == 2 and task_type == "feature_request":
        return {
            "thought": "Read the strongest lexical and architecture-analogy candidates before assuming a future call path already exists.",
            "tool": "ReadCode",
            "mode": "read",
            "queries": queries + architecture_queries,
            "stop": False,
        }
    if step_no == 2:
        mode = "call" if any(token in sketch_text for token in ("api", "parameter", "serializer", "backend", "call")) else "concern"
        return {
            "thought": f"Expand candidate frontier through {mode} edges after anchor search.",
            "tool": "NavigateCode",
            "mode": mode,
            "queries": queries,
            "stop": False,
        }
    if step_no == 3 and task_type == "feature_request":
        return {
            "thought": "Navigate horizontally from verified analogous implementations and vertically through action/data-layer conventions.",
            "tool": "NavigateCode",
            "mode": "concern",
            "queries": queries + architecture_queries,
            "stop": False,
        }
    if step_no == 3:
        return {
            "thought": "Validate whether candidate files carry the issue state/parameter into the expected behavior.",
            "tool": "TraceFlow",
            "mode": "flow",
            "queries": queries,
            "stop": False,
        }
    if step_no == 4 and task_type == "feature_request":
        return {
            "thought": "Validate state/action/handler obligations only after analogous implementation code has been read.",
            "tool": "TraceFlow",
            "mode": "flow",
            "queries": queries,
            "stop": False,
        }
    if step_no == 4:
        return {
            "thought": "Read a small pruned set of candidate files before final ranking.",
            "tool": "ReadCode",
            "mode": "read",
            "queries": queries,
            "stop": False,
        }
    return {
        "thought": "Stop after one complete SearchAnchor/NavigateCode/TraceFlow/ReadCode cycle.",
        "tool": "Stop",
        "mode": "stop",
        "queries": [],
        "stop": True,
    }


def _run_tool(
    *,
    move: dict[str, Any],
    sample: NormalizedSample,
    evidence_result: dict[str, Any],
    index: RepositoryIndex,
    graph: TypedRepositoryGraph,
    issue_sketch: IssueSketch,
    queries: list[str],
    candidate_paths: list[str],
    top_k: int,
) -> DynamicToolObservation | None:
    tool = str(move.get("tool") or "")
    mode = str(move.get("mode") or "concern")
    tool_queries = _dedupe(queries + list(move.get("queries") or []) + sketch_query_terms(issue_sketch), limit=140)
    if tool == "SearchAnchor":
        return SearchAnchorTool(index).run(issue_sketch, tool_queries, limit=max(10, top_k))
    if tool == "NavigateCode":
        seeds = _dedupe(candidate_paths + [str(item.get("path") or "") for item in issue_sketch.seed_policy], limit=50)
        return NavigateCodeTool(graph).run(seeds, tool_queries, mode=mode if mode else "concern", limit=max(20, top_k * 2))
    if tool == "TraceFlow":
        return TraceFlowTool(index).run(
            sample,
            evidence_result,
            issue_sketch,
            tool_queries,
            candidate_paths,
            limit=max(8, min(14, top_k)),
        )
    if tool == "ReadCode":
        read_paths = _dedupe(candidate_paths, limit=max(8, min(18, top_k * 2)))
        return ReadCodeTool(index).run(read_paths, tool_queries, limit=max(8, min(18, len(read_paths))))
    return None


def run_react_tool_agent(
    *,
    sample: NormalizedSample,
    evidence_result: dict[str, Any],
    index: RepositoryIndex,
    graph: TypedRepositoryGraph,
    issue_sketch: IssueSketch,
    queries: list[str],
    previous_candidates: list[str] | None = None,
    top_k: int = 15,
    max_steps: int = 5,
    planner_llm: LLMController | None = None,
) -> dict[str, Any]:
    """Run an auditable ReAct-style localization controller.

    The controller can use an LLM to propose moves, but every move is executed
    through the same deterministic tools. If the LLM is absent, malformed, or
    asks for an unknown tool, the controller falls back to the heuristic policy.
    """

    steps: list[ReActStep] = []
    observations: list[DynamicToolObservation] = []
    candidate_paths = _dedupe(previous_candidates or [], limit=80)
    active_queries = _dedupe(queries + sketch_query_terms(issue_sketch), limit=160)
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    used_planner = False
    planner_history: list[dict[str, Any]] = []
    seen_action_signatures: set[str] = set()
    seen_flow_signatures: set[str] = set()
    seen_read_paths: set[str] = set()
    consecutive_no_gain = 0
    stop_reason = "max_steps_reached"

    for step_no in range(1, max(1, max_steps) + 1):
        start = time.time()
        required_order = _required_tool_order(issue_sketch)
        used_tools = [str(item.get("tool") or "") for item in planner_history]
        enforce_coverage = os.environ.get("MYCODE_REACT_ENFORCE_TOOL_COVERAGE", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
        coverage_complete = not enforce_coverage or all(tool in used_tools for tool in required_order)
        no_gain_limit = _env_int("MYCODE_REACT_NO_GAIN_STEPS", 2, minimum=1)
        if coverage_complete and consecutive_no_gain >= no_gain_limit:
            stop_reason = "evidence_plateau_after_tool_coverage"
            steps.append(
                ReActStep(
                    round_no=step_no,
                    thought="All required tools were covered and recent observations added no new candidates, code reads, or flow evidence.",
                    tool="Stop",
                    action="stop",
                    tool_input={"candidate_count": len(candidate_paths), "consecutive_no_gain": consecutive_no_gain},
                    observation={"status": "stopped", "reason": stop_reason},
                    candidate_paths=candidate_paths[:40],
                    next_queries=[],
                    elapsed_seconds=round(time.time() - start, 3),
                )
            )
            break
        llm_raw = ""
        move: dict[str, Any] = {}
        step_usage: dict[str, int] = {}
        if planner_llm is not None:
            try:
                planner_response = planner_llm(
                    _planner_prompt(
                        sample=sample,
                        issue_sketch=issue_sketch,
                        queries=active_queries,
                        previous_candidates=candidate_paths,
                        step_no=step_no,
                        history=planner_history,
                    )
                )
                move, llm_raw = _parse_planner_response(planner_response)
                step_usage = _token_usage_from_response(planner_response if isinstance(planner_response, dict) else {})
                for key, value in step_usage.items():
                    token_usage[key] = token_usage.get(key, 0) + int(value or 0)
                used_planner = True
            except Exception as exc:
                move = {
                    "thought": f"Planner failed with {type(exc).__name__}; fallback to deterministic policy.",
                    "tool": "",
                    "queries": [],
                    "stop": False,
                }
                llm_raw = repr(exc)

        if str(move.get("tool") or "") not in {"SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode", "Stop"}:
            fallback = _heuristic_move(
                step_no=step_no,
                issue_sketch=issue_sketch,
                queries=active_queries,
                candidate_paths=candidate_paths,
            )
            fallback["planner_fallback_reason"] = "missing_or_unknown_tool"
            move = {**fallback, **{k: v for k, v in move.items() if k == "thought" and v}}

        if enforce_coverage:
            used_tools = [str(item.get("tool") or "") for item in planner_history]
            missing_tools = [tool for tool in required_order if tool not in used_tools]
            selected_tool = str(move.get("tool") or "")
            repeated_without_empty_observation = bool(
                selected_tool in used_tools
                and planner_history
                and planner_history[-1].get("status") not in {"empty", "error", "unknown_tool"}
            )
            skipped_foundation = bool(missing_tools and selected_tool != missing_tools[0])
            if missing_tools and (repeated_without_empty_observation or skipped_foundation or selected_tool == "Stop"):
                original_tool = selected_tool or "missing"
                forced_tool = missing_tools[0]
                move["tool"] = forced_tool
                move["mode"] = {
                    "SearchAnchor": "concern",
                    "NavigateCode": (
                        "concern"
                        if str(getattr(issue_sketch, "task_type", "unknown")) == "feature_request"
                        else "used_by" if any("used_by" in str(item) for item in issue_sketch.seed_policy) else "call"
                    ),
                    "TraceFlow": "flow",
                    "ReadCode": "read",
                }[forced_tool]
                move["stop"] = False
                move["thought"] = (
                    str(move.get("thought") or "")
                    + f" Controller discipline changed {original_tool} to {forced_tool} to cover missing evidence."
                ).strip()
                move["planner_adjustment"] = f"tool_coverage:{original_tool}->{forced_tool}"

        action_query_keys = sorted(
            {" ".join(str(query).lower().split()) for query in move.get("queries", []) or [] if str(query).strip()}
        )
        action_signature = "|".join(
            [str(move.get("tool") or ""), str(move.get("mode") or ""), *action_query_keys]
        )
        if (
            action_signature in seen_action_signatures
            and planner_history
            and bool(planner_history[-1].get("no_evidence_gain"))
        ):
            stop_reason = "duplicate_tool_action_without_evidence_gain"
            steps.append(
                ReActStep(
                    round_no=step_no,
                    thought=(str(move.get("thought") or move.get("decision_basis") or "") + " Repeated action suppressed after a no-gain observation.").strip(),
                    tool="Stop",
                    action="stop",
                    tool_input={"candidate_count": len(candidate_paths), "duplicate_action": action_signature},
                    observation={"status": "stopped", "reason": stop_reason},
                    candidate_paths=candidate_paths[:40],
                    next_queries=[],
                    llm_raw=llm_raw,
                    token_usage=step_usage,
                    elapsed_seconds=round(time.time() - start, 3),
                )
            )
            break
        seen_action_signatures.add(action_signature)

        if move.get("stop") or move.get("tool") == "Stop":
            stop_reason = "controller_requested_stop"
            steps.append(
                ReActStep(
                    round_no=step_no,
                    thought=str(move.get("thought") or move.get("decision_basis") or "Stop requested by controller."),
                    tool="Stop",
                    action="stop",
                    tool_input={"candidate_count": len(candidate_paths)},
                    observation={"status": "stopped"},
                    candidate_paths=candidate_paths[:40],
                    next_queries=[],
                    llm_raw=llm_raw,
                    token_usage=step_usage,
                    elapsed_seconds=round(time.time() - start, 3),
                )
            )
            break

        observation = _run_tool(
            move=move,
            sample=sample,
            evidence_result=evidence_result,
            index=index,
            graph=graph,
            issue_sketch=issue_sketch,
            queries=active_queries,
            candidate_paths=candidate_paths,
            top_k=top_k,
        )
        new_candidate_count = 0
        new_read_count = 0
        new_flow_count = 0
        new_paths: list[str] = []
        if observation is None:
            observation_dict = {"status": "unknown_tool", "tool": move.get("tool")}
        else:
            observations.append(observation)
            observation_dict = observation.to_dict()
            new_paths = _path_candidates(observation)
            prior_paths = set(candidate_paths)
            new_candidate_count = len([path for path in new_paths if path not in prior_paths])
            if observation.tool == "ReadCode":
                observed_read_paths = {_path for _path in new_paths if _path}
                new_read_count = len(observed_read_paths - seen_read_paths)
                seen_read_paths.update(observed_read_paths)
            observed_flow_signatures = _observation_flow_signatures(observation)
            new_flow_count = len(observed_flow_signatures - seen_flow_signatures)
            seen_flow_signatures.update(observed_flow_signatures)
            candidate_paths = _merge_frontier(candidate_paths, new_paths, limit=100)
            active_queries = _dedupe(
                active_queries
                + list(observation.queries or [])
                + [Path(path).stem for path in new_paths[:20]]
                + [
                    str(flow.get("term") or "")
                    for flow in (observation.metadata or {}).get("interprocedural_flows", []) or []
                ],
                limit=180,
            )
        evidence_gain_count = new_candidate_count + new_read_count + new_flow_count
        no_evidence_gain = evidence_gain_count == 0
        consecutive_no_gain = consecutive_no_gain + 1 if no_evidence_gain else 0
        steps.append(
            ReActStep(
                round_no=step_no,
                thought=str(move.get("thought") or move.get("decision_basis") or ""),
                tool=str(move.get("tool") or ""),
                action=str(observation_dict.get("action") or move.get("mode") or ""),
                tool_input={
                    "mode": move.get("mode"),
                    "queries": list(move.get("queries") or [])[:30],
                    "candidate_count_before": len(candidate_paths) - len(new_paths),
                },
                observation={
                    "status": observation_dict.get("status"),
                    "candidate_count": len(observation_dict.get("candidates", []) or []),
                    "seed_files": observation_dict.get("seed_files", [])[:12],
                    "notes": observation_dict.get("notes", [])[:4],
                    "metadata_keys": sorted((observation_dict.get("metadata") or {}).keys())[:12],
                    "evidence_gain_count": evidence_gain_count,
                    "new_candidate_count": new_candidate_count,
                    "new_read_count": new_read_count,
                    "new_flow_count": new_flow_count,
                    "consecutive_no_gain": consecutive_no_gain,
                },
                candidate_paths=candidate_paths[:40],
                next_queries=active_queries[:50],
                llm_raw=llm_raw,
                token_usage=step_usage,
                elapsed_seconds=round(time.time() - start, 3),
            )
        )
        planner_history.append(
            {
                "tool": str(move.get("tool") or ""),
                "mode": str(move.get("mode") or ""),
                "status": str(observation_dict.get("status") or ""),
                "candidate_count": len(observation_dict.get("candidates", []) or []),
                "top_candidates": new_paths[:5],
                "new_candidate_count": new_candidate_count,
                "new_read_count": new_read_count,
                "new_flow_count": new_flow_count,
                "evidence_gain_count": evidence_gain_count,
                "no_evidence_gain": no_evidence_gain,
                "action_signature": action_signature,
                "adjustment": str(move.get("planner_adjustment") or ""),
            }
        )

    flow_traces: list[dict[str, Any]] = []
    for observation in observations:
        metadata = observation.metadata or {}
        for key in (
            "program_flows",
            "parameter_closures",
            "statement_flows",
            "static_slices",
            "interprocedural_flows",
            "flow_chains",
            "runtime_trace_verifications",
        ):
            flow_traces.extend(metadata.get(key, []) or [])

    return {
        "status": "ok",
        "strategy": "ReAct planner/controller over SearchAnchor, NavigateCode, TraceFlow and ReadCode",
        "planner_used": used_planner,
        "steps": [step.to_dict() for step in steps],
        "tool_observations": [observation.to_dict() for observation in observations],
        "candidate_paths": candidate_paths[:100],
        "read_paths": [
            str(candidate.get("path") or "")
            for observation in observations
            if observation.tool == "ReadCode"
            for candidate in observation.candidates
        ][:40],
        "next_queries": active_queries[:80],
        "flow_traces": flow_traces[:60],
        "flow_coverage": next(
            (
                observation.metadata.get("flow_coverage", {})
                for observation in observations
                if observation.tool == "TraceFlow" and observation.metadata
            ),
            {},
        ),
        "usage_summary": token_usage,
        "stop_reason": stop_reason,
        "summary": {
            "step_count": len(steps),
            "candidate_count": len(candidate_paths),
            "flow_trace_count": len(flow_traces),
            "read_file_count": sum(len(observation.candidates) for observation in observations if observation.tool == "ReadCode"),
            "planner_used": used_planner,
            "stop_reason": stop_reason,
            "consecutive_no_gain": consecutive_no_gain,
        },
    }
