import json
import subprocess
import sys
import tempfile
import unittest
import os
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from repair import apply_edits, make_patch, validate_path
from preflight import load_env


class RepairTests(unittest.TestCase):
    def test_mycode_env_aliases_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('BASE_URL=https://example.invalid/v1\nAPI_KEY=test-only\nMODEL_NAME=label\nMODEL_API_NAME=api-id\n')
            with patch.dict(os.environ, {}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['RQ4_MODEL'], 'api-id')
                self.assertEqual(os.environ['RQ4_API_KEY'], 'test-only')
            with patch.dict(os.environ, {'RQ4_MODEL': 'override'}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['RQ4_MODEL'], 'override')

    def test_path_boundary(self):
        for path in ['../x', '/tmp/x', '.git/config', 'a/../../x', 'a\\b']:
            with self.assertRaises(ValueError):
                validate_path(path)

    def test_ambiguous_and_outside_edits(self):
        for edit in [{'path': 'a.js', 'search': 'x', 'replace': 'y'},
                     {'path': 'b.js', 'search': 'x', 'replace': 'y'}]:
            with self.assertRaises(ValueError):
                apply_edits({'a.js': 'xx'}, [edit])

    def test_multilanguage_patch_applies_preserving_newlines(self):
        old = {'a.js': 'let value = 1;', 'b.java': 'class B { int value = 1; }\n'}
        edits = [{'path': p, 'search': 'value = 1', 'replace': 'value = 2'} for p in old]
        new = apply_edits(old, edits)
        patch = make_patch(old, new)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            for p, text in old.items():
                (root / p).write_text(text)
            result = subprocess.run(['git', '-C', tmp, 'apply', '-'], input=patch, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for p, text in new.items():
                self.assertEqual((root / p).read_text(), text)

    def test_isolated_inputs_and_predictions(self):
        for tag in ['swe', 'omni']:
            path = ROOT / 'data/inputs' / f'{tag}50.jsonl'
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len({r['instance_id'] for r in rows}), 50)
            for row in rows:
                self.assertEqual(set(row), {'instance_id', 'repo', 'base_commit', 'problem_statement'})
        for path in (ROOT / 'normalized').glob('*/*.jsonl'):
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(rows), 50)
            for row in rows:
                self.assertEqual(set(row), {'instance_id', 'found_files', 'status'})


if __name__ == '__main__':
    unittest.main()
