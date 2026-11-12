import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "repair_v9.py"
SPEC = importlib.util.spec_from_file_location("repair_v9", MODULE)
repair_v9 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_v9)


class MagnetV9Tests(unittest.TestCase):
    def test_expected_file_gets_complete_context_within_budget(self):
        files = {"src/Tokenizer.js": "line1\nline2\nline3\n", "src/Parser.js": "parse\n"}
        plan = {"expected_files": ["src/Tokenizer.js"]}
        contexts, audit = repair_v9.aligned_context(
            files,
            [{"path": "src/Parser.js", "function": "parse", "start_line": 1, "end_line": 1}],
            plan,
            window=1,
            max_bytes=1024,
        )
        self.assertEqual(contexts["src/Tokenizer.js"], "[complete file]\n" + files["src/Tokenizer.js"])
        self.assertIn("src/Tokenizer.js", audit["complete_files"])
        self.assertIn("src/Parser.js", contexts)

    def test_coverage_uses_edit_targets_not_inspection_entry_points(self):
        plan = {
            "expected_files": ["src/Tokenizer.js"],
            "entry_points": [{"path": "src/Parser.js", "symbol": "parse"}],
            "state_transitions": [{"path": "src/Lexer.js", "symbol": "lex"}],
            "boundary_cases": [],
        }
        coverage = repair_v9.assess_patch_coverage(plan, ["src/Tokenizer.js"])
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
