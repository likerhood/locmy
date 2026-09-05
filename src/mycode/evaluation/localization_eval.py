from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Sequence

from mycode.repo_index.structure_index import CodeEntity, RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./")


DEFAULT_KS = tuple(range(1, 16))
DEFAULT_SET_CUTOFFS = ("8", "10", "15", "all")


def _unique_normalized(items: Iterable[str]) -> list[str]:
    preds: list[str] = []
    seen = set()
    for item in items:
        norm = _normalize(item)
        if norm and norm not in seen:
            seen.add(norm)
            preds.append(norm)
    return preds


def set_metrics_at_cutoff(
    predicted_items: Iterable[str],
    gold_items: Iterable[str],
    cutoff: int | Literal["all"],
) -> Dict[str, float]:
    """Compute set-level localization metrics.

    ``sl`` follows the LocAgent eval script's set-success semantics: all gold
    items must be contained in the predicted set at the cutoff. ``rec``,
    ``pre`` and ``f1`` are ordinary set recall, precision and F1.
    """

    preds = _unique_normalized(predicted_items)
    gold = {_normalize(item) for item in gold_items if _normalize(item)}
    pred_set = set(preds if cutoff == "all" else preds[: int(cutoff)])
    if not gold:
        return {"sl": 0.0, "rec": 0.0, "pre": 0.0, "f1": 0.0}
    hits = len(pred_set & gold)
    rec = hits / len(gold)
    pre = hits / len(pred_set) if pred_set else 0.0
    f1 = 2 * pre * rec / (pre + rec) if pre + rec else 0.0
    return {
        "sl": 1.0 if gold <= pred_set else 0.0,
        "rec": rec,
        "pre": pre,
        "f1": f1,
    }


def _evaluate_ranking(
    predicted_items: Iterable[str],
    gold_items: Iterable[str],
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, float]:
    preds = _unique_normalized(predicted_items)
    gold = {_normalize(item) for item in gold_items if _normalize(item)}
    metrics: Dict[str, float] = {"gold_count": float(len(gold)), "pred_count": float(len(preds))}
    if not gold:
        for k in ks:
            metrics[f"acc@{k}"] = 0.0
            metrics[f"strict_acc@{k}"] = 0.0
            metrics[f"recall@{k}"] = 0.0
        for cutoff in DEFAULT_SET_CUTOFFS:
            for name in ("sl", "rec", "pre", "f1"):
                metrics[f"set_{name}@{cutoff}"] = 0.0
        metrics["mrr@15"] = 0.0
        metrics["map@15"] = 0.0
        return metrics

    for k in ks:
        top = set(preds[:k])
        hits = len(top & gold)
        metrics[f"acc@{k}"] = 1.0 if hits else 0.0
        metrics[f"strict_acc@{k}"] = 1.0 if gold <= top else 0.0
        metrics[f"recall@{k}"] = hits / len(gold)

    for cutoff in DEFAULT_SET_CUTOFFS:
        set_metrics = set_metrics_at_cutoff(preds, gold, "all" if cutoff == "all" else int(cutoff))
        for name, value in set_metrics.items():
            metrics[f"set_{name}@{cutoff}"] = value

    rr = 0.0
    precisions = []
    hit_count = 0
    for rank, path in enumerate(preds[:15], start=1):
        if path in gold:
            if rr == 0.0:
                rr = 1.0 / rank
            hit_count += 1
            precisions.append(hit_count / rank)
    metrics["mrr@15"] = rr
    metrics["map@15"] = sum(precisions) / len(gold) if precisions else 0.0
    return metrics


def evaluate_file_ranking(
    predicted_files: Iterable[str],
    gold_files: Iterable[str],
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, float]:
    return _evaluate_ranking(predicted_files, gold_files, ks)


def evaluate_entity_ranking(
    predicted_entities: Iterable[str],
    gold_entities: Iterable[str],
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, float]:
    return _evaluate_ranking(predicted_entities, gold_entities, ks)


@dataclass
class GoldEntitySets:
    files: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    changed_lines: dict[str, list[int]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "modules": self.modules,
            "functions": self.functions,
            "changed_lines": self.changed_lines,
            "diagnostics": self.diagnostics,
        }


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
DIFF_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$")


def file_module_id(path: str) -> str:
    return f"{_normalize(path)}::module:__file__"


def entity_id(path: str, kind: str, name: str) -> str:
    return f"{_normalize(path)}::{kind}:{name}".strip()


def entity_to_id(entity: CodeEntity | Dict[str, Any]) -> str:
    if isinstance(entity, CodeEntity):
        return entity_id(entity.path, entity.kind, entity.name)
    return entity_id(
        str(entity.get("path") or ""),
        str(entity.get("kind") or "entity"),
        str(entity.get("name") or ""),
    )


def _gold_patch_from_sample(sample: NormalizedSample) -> str:
    raw = sample.raw or {}
    for key in ("patch", "gold_patch", "solution_patch", "model_patch"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def extract_changed_lines_from_patch(patch_text: str) -> dict[str, set[int]]:
    """Parse unified diff and return changed target-side lines per file.

    Added lines are exact target lines. Deleted lines have no target-side line,
    so we approximate them to the current target cursor. This is sufficient for
    localization-level entity overlap and mirrors how hunk-based gold mapping is
    usually approximated when only a patch is available.
    """

    changed: dict[str, set[int]] = {}
    current_path = ""
    new_line = 0
    in_hunk = False
    for raw_line in patch_text.splitlines():
        diff_match = DIFF_RE.match(raw_line)
        if diff_match:
            current_path = _normalize(diff_match.group(2))
            changed.setdefault(current_path, set())
            in_hunk = False
            continue
        if raw_line.startswith("+++ b/"):
            current_path = _normalize(raw_line[len("+++ b/"):])
            changed.setdefault(current_path, set())
            continue
        hunk_match = HUNK_RE.match(raw_line)
        if hunk_match:
            new_line = int(hunk_match.group("start"))
            in_hunk = True
            continue
        if not current_path or not in_hunk:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed[current_path].add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            changed[current_path].add(max(new_line, 1))
        elif raw_line.startswith("\\"):
            continue
        else:
            new_line += 1
    return {path: lines for path, lines in changed.items() if lines}


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        norm = _normalize(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _entities_for_path(index: RepositoryIndex, path: str) -> list[CodeEntity]:
    norm = _normalize(path)
    return [entity for entity in index.entities if _normalize(entity.path) == norm]


def _entities_covering_line(index: RepositoryIndex, path: str, line: int, kinds: Sequence[str]) -> list[CodeEntity]:
    wanted = set(kinds)
    return [
        entity
        for entity in _entities_for_path(index, path)
        if entity.kind in wanted and int(entity.start_line or 0) <= line <= int(entity.end_line or 0)
    ]


def build_gold_entity_sets(sample: NormalizedSample, index: RepositoryIndex) -> GoldEntitySets:
    files = _unique(sample.gold_files)
    patch_text = _gold_patch_from_sample(sample)
    changed = extract_changed_lines_from_patch(patch_text)
    modules: list[str] = []
    functions: list[str] = []
    diagnostics: list[str] = []

    raw = sample.raw or {}
    explicit_modules = raw.get("gold_modules") or raw.get("modules")
    explicit_functions = raw.get("gold_functions") or raw.get("functions")
    if isinstance(explicit_modules, list):
        modules.extend(str(item) for item in explicit_modules if item)
    if isinstance(explicit_functions, list):
        functions.extend(str(item) for item in explicit_functions if item)

    for path in files:
        lines = changed.get(path) or changed.get(_normalize(path)) or set()
        if not lines:
            modules.append(file_module_id(path))
            diagnostics.append(f"{path}: no changed lines from patch; use file module fallback")
            continue
        path_modules: set[str] = set()
        path_functions: set[str] = set()
        for line in sorted(lines):
            class_hits = _entities_covering_line(index, path, line, ("class", "module"))
            function_hits = _entities_covering_line(index, path, line, ("function", "method"))
            for entity in class_hits:
                path_modules.add(entity_to_id(entity))
            for entity in function_hits:
                path_functions.add(entity_to_id(entity))
        if not path_modules:
            path_modules.add(file_module_id(path))
        if not path_functions:
            diagnostics.append(f"{path}: changed lines do not overlap any function/method entity")
        modules.extend(sorted(path_modules))
        functions.extend(sorted(path_functions))

    return GoldEntitySets(
        files=files,
        modules=_unique(modules),
        functions=_unique(functions),
        changed_lines={path: sorted(lines) for path, lines in changed.items()},
        diagnostics=diagnostics,
    )


def _predicted_entity_ids(items: Iterable[Any]) -> list[str]:
    ids: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            if item.get("id"):
                ids.append(str(item["id"]))
            else:
                ids.append(entity_id(str(item.get("path") or ""), str(item.get("kind") or "entity"), str(item.get("name") or "")))
    return ids


def evaluate_three_level_ranking(
    localization: Dict[str, Any],
    sample: NormalizedSample,
    index: RepositoryIndex,
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    gold = build_gold_entity_sets(sample, index)
    predicted_files = [str(item.get("path") or "") for item in localization.get("ranked_locations", []) or []]
    predicted_modules = _predicted_entity_ids(localization.get("ranked_modules", []) or [])
    predicted_functions = _predicted_entity_ids(localization.get("ranked_functions", []) or [])
    return {
        "gold": gold.to_dict(),
        "file": evaluate_file_ranking(predicted_files, gold.files, ks),
        "module": evaluate_entity_ranking(predicted_modules, gold.modules, ks),
        "function": evaluate_entity_ranking(predicted_functions, gold.functions, ks),
    }


def evaluate_three_level_from_gold_sets(
    localization: Dict[str, Any],
    gold_sets: Dict[str, Any],
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    """Evaluate stored predictions against stored gold sets.

    This is useful for re-scoring old ``localization_results.jsonl`` files
    after the metric implementation changes. It does not need the original
    samples or repository structures as long as the result file already stores
    ``evaluation_3level.gold``.
    """

    predicted_files = [str(item.get("path") or "") for item in localization.get("ranked_locations", []) or []]
    predicted_modules = _predicted_entity_ids(localization.get("ranked_modules", []) or [])
    predicted_functions = _predicted_entity_ids(localization.get("ranked_functions", []) or [])
    return {
        "gold": {
            "files": list(gold_sets.get("files") or []),
            "modules": list(gold_sets.get("modules") or []),
            "functions": list(gold_sets.get("functions") or []),
            "changed_lines": dict(gold_sets.get("changed_lines") or {}),
            "diagnostics": list(gold_sets.get("diagnostics") or []),
        },
        "file": evaluate_file_ranking(predicted_files, gold_sets.get("files") or [], ks),
        "module": evaluate_entity_ranking(predicted_modules, gold_sets.get("modules") or [], ks),
        "function": evaluate_entity_ranking(predicted_functions, gold_sets.get("functions") or [], ks),
    }


def gold_applicability_report(gold: GoldEntitySets) -> Dict[str, Any]:
    """Explain whether module/function metrics are meaningful for this sample.

    Some benchmark patches touch CSS/JSON/docs/imports/top-level constants or
    newly added code outside the repository structure snapshot. File-level gold
    still exists in those cases, but function-level gold may be empty. This
    report keeps the metric honest without changing the benchmark sample count.
    """

    file_module_fallbacks: list[str] = []
    non_function_files: list[str] = []
    no_changed_line_files: list[str] = []
    for diagnostic in gold.diagnostics:
        path = diagnostic.split(":", 1)[0]
        if "use file module fallback" in diagnostic:
            file_module_fallbacks.append(path)
        if "do not overlap any function/method entity" in diagnostic:
            non_function_files.append(path)
        if "no changed lines from patch" in diagnostic:
            no_changed_line_files.append(path)

    return {
        "file_gold_count": len(gold.files),
        "module_gold_count": len(gold.modules),
        "function_gold_count": len(gold.functions),
        "module_applicable": bool(gold.modules),
        "function_applicable": bool(gold.functions),
        "function_empty_reason": (
            "changed_lines_do_not_overlap_function_or_structure_missing"
            if gold.files and not gold.functions
            else ""
        ),
        "non_function_or_unmapped_files": _unique(non_function_files),
        "file_module_fallback_files": _unique(file_module_fallbacks),
        "no_changed_line_files": _unique(no_changed_line_files),
        "diagnostics": list(gold.diagnostics),
    }


def evaluate_three_level_ranking_with_applicability(
    localization: Dict[str, Any],
    sample: NormalizedSample,
    index: RepositoryIndex,
    ks: Iterable[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    """Evaluate file/module/function ranking and attach applicability notes."""

    result = evaluate_three_level_ranking(localization, sample, index, ks)
    gold = build_gold_entity_sets(sample, index)
    result["applicability"] = gold_applicability_report(gold)
    return result
