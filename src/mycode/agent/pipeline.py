from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from mycode.dynamic_retrieval.search_agent import (
    clear_dynamic_localization_checkpoint,
    dynamic_localize,
    take_dynamic_localization_checkpoint,
)
from mycode.evaluation.localization_eval import (
    evaluate_file_ranking,
    evaluate_three_level_ranking_with_applicability,
)
from mycode.evidence.tools.llm_client import (
    LLMClientError,
    chat_completion,
    first_text,
    model_for_stage,
)
from mycode.evidence.understanding_agent import run_evidence_understanding
from mycode.repo_index.repo_locator import find_repo_structure
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample
from mycode.utils.phase_logger import phase_context, phase_event


def _make_controller_llm(enabled: bool):
    if not enabled:
        return None

    def _controller(prompt: str) -> Dict[str, Any]:
        try:
            response = chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "You control a repository localization agent. "
                            "Return compact JSON only, following the schema requested by the user prompt."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=model_for_stage("controller"),
                temperature=0,
                max_tokens=900,
                timeout=120,
            )
        except LLMClientError as exc:
            raise RuntimeError(str(exc)) from exc
        return {
            "content": first_text(response),
            "usage": response.get("usage") or {},
            "raw_response": response,
        }

    return _controller


def run_localization_pipeline(
    sample: NormalizedSample,
    *,
    allow_network: bool = False,
    allow_browser: bool = False,
    use_llm: bool = False,
    use_llm_planning: bool | None = None,
    use_llm_controller: bool | None = None,
    execute_tools: bool = True,
    cache_dir: str = "outputs/tool_cache",
    download_images: bool = False,
    use_vlm: bool = False,
    max_tool_rounds: int = 2,
    top_k: int = 15,
    dynamic_rounds: int = 3,
    max_react_steps: int | None = None,
    structure_only: bool = False,
    lightweight: bool = False,
) -> Dict[str, Any]:
    phase_event(
        "start",
        "pipeline",
        use_llm=use_llm,
        allow_network=allow_network,
        allow_browser=allow_browser,
        use_vlm=use_vlm,
        structure_only=structure_only,
        lightweight=lightweight,
    )
    with phase_context(
        "evidence_agent",
        use_llm=use_llm,
        use_llm_planning=use_llm_planning,
        allow_network=allow_network,
        allow_browser=allow_browser,
        download_images=download_images,
        use_vlm=use_vlm,
        max_tool_rounds=max_tool_rounds,
    ):
        evidence = run_evidence_understanding(
            sample,
            allow_network=allow_network,
            allow_browser=allow_browser,
            use_llm=use_llm,
            use_llm_planning=use_llm_planning,
            execute_tools=execute_tools,
            cache_dir=cache_dir,
            download_images=download_images,
            use_vlm=use_vlm,
            max_tool_rounds=max_tool_rounds,
        )
    structure_path = find_repo_structure(sample.instance_id, sample.dataset) if structure_only else None
    repo_root = Path("/__mycode_no_repo_root__") if structure_only else None
    with phase_context("repository_index", structure_only=structure_only, structure_path=str(structure_path or "")):
        repo_index = RepositoryIndex(
            repo=sample.repo,
            instance_id=sample.instance_id,
            dataset=sample.dataset,
            repo_root=repo_root,
            structure_path=structure_path,
        )
        phase_event(
            "progress",
            "repository_index",
            ready=repo_index.ready,
            file_count=len(repo_index.files),
            entity_count=len(repo_index.entities),
        )
    if use_llm_controller is None:
        use_llm_controller = use_llm
    controller_llm = _make_controller_llm(bool(use_llm_controller))
    clear_dynamic_localization_checkpoint(sample.instance_id)
    try:
        with phase_context(
            "dynamic_localization_agent",
            top_k=top_k,
            dynamic_rounds=dynamic_rounds,
            max_react_steps=max_react_steps or "auto",
            lightweight=lightweight,
        ):
            localization = dynamic_localize(
                sample,
                evidence,
                top_k=top_k,
                max_rounds=dynamic_rounds,
                max_react_steps=max_react_steps,
                index=repo_index,
                controller_llm=controller_llm,
                lightweight=lightweight,
            )
    except TimeoutError as exc:
        localization = take_dynamic_localization_checkpoint(sample.instance_id)
        if localization is None:
            raise
        localization["termination"] = {
            **(localization.get("termination") or {}),
            "reason": "sample_timeout_checkpoint_recovery",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        phase_event(
            "progress",
            "dynamic.timeout_recovery",
            status="partial",
            completed_rounds=len(localization.get("dynamic_rounds") or []),
            selected_round=(localization.get("best_round_selection") or {}).get("selected_round"),
            candidate_count=len(localization.get("ranked_locations") or []),
        )
    else:
        clear_dynamic_localization_checkpoint(sample.instance_id)
    predicted_files = [item["path"] for item in localization.get("ranked_locations", [])]
    final_patch_files = [str(path) for path in localization.get("final_patch_set", []) if path]
    with phase_context("evaluation", predicted_file_count=len(predicted_files), gold_file_count=len(sample.gold_files or [])):
        evaluation = evaluate_file_ranking(predicted_files, sample.gold_files)
        final_patch_evaluation = evaluate_file_ranking(final_patch_files, sample.gold_files)
        evaluation_3level = (
            evaluate_three_level_ranking_with_applicability(localization, sample, repo_index)
            if repo_index.ready
            else {"status": "missing_repo_index", "file": evaluation}
        )
        phase_event(
            "progress",
            "evaluation",
            file_acc_at_1=evaluation.get("acc@1"),
            file_recall_at_15=evaluation.get("recall@15"),
        )
    phase_event("end", "pipeline", top1=predicted_files[0] if predicted_files else "")
    return {
        "instance_id": sample.instance_id,
        "repo": sample.repo,
        "dataset": sample.dataset,
        "status": localization.get("status", "ok"),
        "problem_statement_only": evidence.get("problem_statement_only") is True,
        "llm_controller_used": controller_llm is not None,
        "lightweight_localization": lightweight,
        "evidence": evidence,
        "localization": localization,
        "evaluation": evaluation,
        "final_patch_set_evaluation": final_patch_evaluation,
        "evaluation_3level": evaluation_3level,
        "gold_files": sample.gold_files,
    }
