from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
NEWTEST = ROOT / "newtest"
for path in (SRC, NEWTEST):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mycode.evaluation.localization_eval import evaluate_three_level_from_gold_sets
from run_clean15_dataset import KS, LEVELS, SET_CUTOFFS, SET_FIELDS
from run_clean15_dataset import _flatten_metrics, _summarize, _write_csv, _write_summary_md


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-evaluate an existing mycode localization_results.jsonl with the latest three-level metrics."
    )
    parser.add_argument("--result-dir", required=True, help="Directory containing localization_results.jsonl.")
    parser.add_argument("--results-jsonl", default="", help="Optional explicit localization_results.jsonl path.")
    parser.add_argument("--output-dir", default="", help="Defaults to <result-dir>/eval_latest.")
    parser.add_argument("--dataset", default="", help="Dataset label for the summary.")
    parser.add_argument("--model-name", default="", help="Displayed in the Set Metrics @All table.")
    parser.add_argument("--write-augmented", action="store_true", help="Also write augmented_predictions.jsonl.")
    parser.add_argument(
        "--sync-result-summary",
        action="store_true",
        help="Also promote the latest three-level JSON/Markdown/CSV to the result directory.",
    )
    return parser.parse_args()


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("_") or "existing"


def _read_results(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc
    return rows


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _refresh_metrics(result: dict[str, Any]) -> dict[str, Any]:
    three = result.get("evaluation_3level") or {}
    gold = three.get("gold") or {}
    if not gold:
        raise ValueError(f"{result.get('instance_id', '<unknown>')}: missing evaluation_3level.gold")
    refreshed = dict(result)
    latest = evaluate_three_level_from_gold_sets(result.get("localization") or {}, gold)
    if "applicability" in three:
        latest["applicability"] = three["applicability"]
    refreshed["evaluation_3level"] = latest
    return refreshed


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_metric_explainer(path: Path) -> None:
    lines = [
        "# mycode 三层定位评估口径说明",
        "",
        "## 1. 宽松排名指标",
        "",
        "`Acc@k` 表示 top-k 中只要命中任意一个 gold 就算该样本成功，适合观察定位入口有没有找到。",
        "",
        "`Recall@k` 表示 top-k 命中的 gold 数量除以 gold 总数，适合多文件、多函数样本。",
        "",
        "`MRR@15` 看第一个命中 gold 的排名倒数；`MAP@15` 看所有命中 gold 的平均精度。",
        "",
        "## 2. 严格排名指标",
        "",
        "`Strict Acc@k` 表示 top-k 必须覆盖该层全部 gold 才算成功。多文件 patch 中，这个指标会明显低于宽松 Acc。",
        "",
        "## 3. 集合指标",
        "",
        "`Set Metrics @8/@10/@15/@All` 把预测列表截成集合后与 gold 集合比较：",
        "",
        "- `SL`: gold 集合是否完全包含在预测集合里。",
        "- `REC`: 命中数 / gold 数。",
        "- `PRE`: 命中数 / 预测数。",
        "- `F1`: PRE 和 REC 的调和平均。",
        "",
        "`@All` 使用该结果文件里保留的全部预测，不强制截断到 15；它适合回答“这个 baseline 最终有没有把所有真实目标包含进候选集合”。",
        "",
        "## 4. 三层 gold 的注意事项",
        "",
        "file gold 来自 patch 文件；module/function gold 来自 patch 修改行与 repo_structures 里实体范围的重叠。CSS、JSON、Markdown、顶层常量、新增代码或结构抽取不到的区域可能导致 function gold 为空，所以 function 指标需要结合 applicability 字段解释。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    result_dir = Path(args.result_dir).expanduser().resolve()
    results_jsonl = Path(args.results_jsonl).expanduser().resolve() if args.results_jsonl else result_dir / "localization_results.jsonl"
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else result_dir / "eval_latest"
    if not results_jsonl.exists():
        raise SystemExit(f"Missing localization results: {results_jsonl}")

    raw_results = _read_results(results_jsonl)
    source_summary = _read_json_object(result_dir / "metrics_summary.json")
    source_settings = source_summary.get("settings") or {}
    refreshed_results = []
    skipped = []
    for result in raw_results:
        try:
            refreshed_results.append(_refresh_metrics(result))
        except Exception as exc:
            skipped.append({"instance_id": result.get("instance_id", ""), "error": str(exc)})

    rows = [_flatten_metrics(result) for result in refreshed_results]
    summary = _summarize(rows, skipped, elapsed=float(source_summary.get("elapsed_seconds", 0.0) or 0.0))
    summary["session_elapsed_seconds"] = 0.0
    summary["dataset"] = args.dataset or source_summary.get("dataset") or (
        refreshed_results[0].get("dataset") if refreshed_results else ""
    )
    summary["samples_path"] = source_summary.get("samples_path") or str(results_jsonl)
    summary["output_dir"] = str(output_dir)
    summary["model_name"] = args.model_name or source_summary.get("model_name") or _safe_label(result_dir.name)
    summary["settings"] = {
        **source_settings,
        "source_result_dir": str(result_dir),
        "source_results_jsonl": str(results_jsonl),
        "re_evaluated_only": True,
        "metric_ks": list(KS),
        "set_cutoffs": list(SET_CUTOFFS),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "per_instance_metrics_3level.csv", rows)
    (output_dir / "metrics_3level.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    display_args = argparse.Namespace(
        dataset=summary["dataset"] or result_dir.name,
        samples=summary["samples_path"],
        output_dir=str(output_dir),
        top_k=source_settings.get("top_k", 15),
        dynamic_rounds=source_settings.get("dynamic_rounds", "existing"),
        max_tool_rounds=source_settings.get("max_tool_rounds", "existing"),
        max_react_steps=source_settings.get("max_react_steps", "existing"),
        use_llm=source_settings.get("use_llm", "existing"),
        use_llm_planning=source_settings.get("use_llm_planning", "existing"),
        use_llm_controller=source_settings.get("use_llm_controller", "existing"),
        allow_network=source_settings.get("allow_network", "existing"),
        allow_browser=source_settings.get("allow_browser", "existing"),
        use_vlm=source_settings.get("use_vlm", "existing"),
        sample_timeout_seconds=source_settings.get("sample_timeout_seconds", "existing"),
        structure_only=source_settings.get("structure_only", "existing"),
        lightweight=source_settings.get("lightweight", "existing"),
    )
    _write_summary_md(output_dir / "metrics_3level.md", summary, display_args)
    _write_metric_explainer(output_dir / "metric_definitions.md")
    if skipped:
        _write_jsonl(output_dir / "skipped_results.jsonl", skipped)
    if args.write_augmented:
        _write_jsonl(output_dir / "augmented_predictions.jsonl", refreshed_results)
    if args.sync_result_summary:
        root_display_args = argparse.Namespace(**vars(display_args))
        root_display_args.output_dir = str(result_dir)
        summary["output_dir"] = str(result_dir)
        summary_json = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (result_dir / "metrics_3level.json").write_text(summary_json, encoding="utf-8")
        (result_dir / "metrics_summary.json").write_text(summary_json, encoding="utf-8")
        _write_summary_md(result_dir / "metrics_3level.md", summary, root_display_args)
        _write_summary_md(result_dir / "metrics_summary.md", summary, root_display_args)
        _write_csv(result_dir / "per_instance_metrics_3level.csv", rows)
        _write_metric_explainer(result_dir / "metric_definitions.md")

    print(f"Re-evaluated: {len(refreshed_results)}")
    print(f"Skipped: {len(skipped)}")
    print(f"Metrics: {output_dir / 'metrics_3level.md'}")
    print(f"CSV: {output_dir / 'per_instance_metrics_3level.csv'}")
    if args.sync_result_summary:
        print(f"Promoted summary: {result_dir / 'metrics_summary.md'}")
    print("")
    print("Set Metrics @All")
    header = ["Baseline模型"]
    values = [str(summary["model_name"])]
    for level in LEVELS:
        pretty = {"file": "File", "module": "Module", "function": "Function"}[level]
        for field in SET_FIELDS:
            header.append(f"{pretty} {field.upper()}")
            values.append(f"{summary['metrics'].get(level, {}).get(f'set_{field}@all', 0.0):.4f}")
    print(" | ".join(header))
    print(" | ".join(values))


if __name__ == "__main__":
    main()
