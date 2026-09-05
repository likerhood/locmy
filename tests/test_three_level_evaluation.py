from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mycode.evaluation.localization_eval import (
    build_gold_entity_sets,
    entity_id,
    evaluate_three_level_ranking,
    evaluate_three_level_ranking_with_applicability,
    file_module_id,
)
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def test_patch_lines_map_to_module_and_function_gold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        structure_path = Path(tmp) / "sample.json"
        text = "\n".join(
            [
                "import os",
                "class Bar:",
                "    def foo(self):",
                "        old_value = 1",
                "        return old_value",
                "",
            ]
        )
        structure_path.write_text(
            json.dumps(
                {
                    "structure": {
                        "src/a.py": {
                            "text": text,
                            "classes": [{"name": "Bar", "start_line": 2, "end_line": 5}],
                            "functions": [{"name": "foo", "start_line": 3, "end_line": 5}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        sample = NormalizedSample(
            instance_id="repo__sample-1",
            repo="repo/sample",
            dataset="unit",
            issue_text="foo should use new value",
            raw={
                "patch": "\n".join(
                    [
                        "diff --git a/src/a.py b/src/a.py",
                        "--- a/src/a.py",
                        "+++ b/src/a.py",
                        "@@ -1,5 +1,5 @@",
                        " import os",
                        " class Bar:",
                        "     def foo(self):",
                        "-        old_value = 1",
                        "+        old_value = 2",
                        "         return old_value",
                    ]
                )
            },
            gold_files=["src/a.py"],
        )
        index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id, structure_path=structure_path)
        gold = build_gold_entity_sets(sample, index)
        assert gold.files == ["src/a.py"]
        assert entity_id("src/a.py", "class", "Bar") in gold.modules
        assert entity_id("src/a.py", "function", "foo") in gold.functions

        localization = {
            "ranked_locations": [{"path": "src/a.py"}],
            "ranked_modules": [{"id": entity_id("src/a.py", "class", "Bar")}],
            "ranked_functions": [{"id": entity_id("src/a.py", "function", "foo")}],
        }
        metrics = evaluate_three_level_ranking(localization, sample, index)
        assert metrics["file"]["acc@1"] == 1.0
        assert metrics["file"]["strict_acc@1"] == 1.0
        assert metrics["file"]["set_sl@all"] == 1.0
        assert metrics["file"]["set_rec@all"] == 1.0
        assert metrics["file"]["set_pre@all"] == 1.0
        assert metrics["file"]["set_f1@all"] == 1.0
        assert metrics["module"]["acc@1"] == 1.0
        assert metrics["module"]["strict_acc@1"] == 1.0
        assert metrics["function"]["acc@1"] == 1.0
        assert metrics["function"]["strict_acc@1"] == 1.0


def test_non_function_file_gets_file_module_but_empty_function_gold() -> None:
    sample = NormalizedSample(
        instance_id="repo__sample-2",
        repo="repo/sample",
        dataset="unit",
        issue_text="style color should change",
        raw={
            "patch": "\n".join(
                [
                    "diff --git a/src/style.css b/src/style.css",
                    "--- a/src/style.css",
                    "+++ b/src/style.css",
                    "@@ -1,3 +1,3 @@",
                    "-.box { color: red; }",
                    "+.box { color: blue; }",
                ]
            )
        },
        gold_files=["src/style.css"],
    )
    index = RepositoryIndex(repo=sample.repo, instance_id=sample.instance_id)
    gold = build_gold_entity_sets(sample, index)
    assert gold.modules == [file_module_id("src/style.css")]
    assert gold.functions == []
    metrics = evaluate_three_level_ranking_with_applicability(
        {"ranked_locations": [{"path": "src/style.css"}], "ranked_modules": [], "ranked_functions": []},
        sample,
        index,
    )
    assert metrics["file"]["acc@1"] == 1.0
    assert metrics["file"]["strict_acc@1"] == 1.0
    assert metrics["function"]["set_sl@all"] == 0.0
    assert metrics["function"]["set_rec@all"] == 0.0
    assert metrics["function"]["set_pre@all"] == 0.0
    assert metrics["function"]["set_f1@all"] == 0.0
    assert metrics["applicability"]["module_applicable"] is True
    assert metrics["applicability"]["function_applicable"] is False
    assert metrics["applicability"]["function_empty_reason"] == "changed_lines_do_not_overlap_function_or_structure_missing"
    assert metrics["applicability"]["non_function_or_unmapped_files"] == ["src/style.css"]


if __name__ == "__main__":
    test_patch_lines_map_to_module_and_function_gold()
    test_non_function_file_gets_file_module_but_empty_function_gold()
