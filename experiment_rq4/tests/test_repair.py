import json
import io
import subprocess
import sys
import tempfile
import unittest
import os
import urllib.error
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from repair import (LOCALIZATION_SYSTEM_PROMPT, REPAIR_SYSTEM_PROMPT, api_call, apply_edits,
                    choose_candidate, line_evidence, localized_context, make_patch,
                    parse_locations, parse_model_edits, validate_path)
from preflight import load_env


class RepairTests(unittest.TestCase):
    def test_api_retries_explicit_http_500_but_not_connection_uncertainty(self):
        class Response:
            def __enter__(self):
                return io.StringIO('{"id":"ok","choices":[],"usage":{}}')

            def __exit__(self, *args):
                return False

        env = {'RQ4_MODEL': 'model', 'RQ4_BASE_URL': 'https://api.example/v1',
               'RQ4_API_KEY': 'test', 'RQ4_HTTP_RETRIES': '2',
               'RQ4_HTTP_RETRY_SLEEPS': '0'}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            error = urllib.error.HTTPError('https://api.example', 500, 'Internal', {}, None)
            with patch('urllib.request.urlopen', side_effect=[error, Response()]) as request:
                result, _ = api_call(Path(tmp) / 'http', [], 0, 10)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result['http_attempt_count'], 2)
            attempt = json.loads((Path(tmp) / 'http/attempt.json').read_text())
            self.assertEqual(attempt['http_failures'][0]['http_status'], 500)

            with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('reset')) as request:
                with self.assertRaises(urllib.error.URLError):
                    api_call(Path(tmp) / 'connection', [], 0, 10)
            self.assertEqual(request.call_count, 1)

    def test_prompts_are_english_and_state_strict_output_contracts(self):
        for prompt, key in [(LOCALIZATION_SYSTEM_PROMPT, 'locations'),
                            (REPAIR_SYSTEM_PROMPT, 'edits')]:
            self.assertTrue(prompt.isascii())
            self.assertIn('exactly one valid JSON object', prompt)
            self.assertIn(f'only the {key} key', prompt)
            self.assertIn('Never return an empty response', prompt)

        protocol = json.loads((ROOT / 'configs/protocol.json').read_text())
        self.assertEqual(protocol['prompt_contract'], 'strict_english_json_v2')
        self.assertGreaterEqual(protocol['localization_max_output_tokens'], 8192)
        self.assertGreaterEqual(protocol['sampling_max_output_tokens'], 8192)

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

    def test_api_direct_adds_only_configured_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('RQ4_BASE_URL=https://token.example.test/v1\nRQ4_API_DIRECT=1\n')
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:7890',
                                         'NO_PROXY': 'localhost'}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['NO_PROXY'], 'localhost,token.example.test')
                self.assertEqual(os.environ['no_proxy'], 'token.example.test')
                self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:7890')

    def test_model_edits_accept_strict_or_single_fenced_json(self):
        self.assertEqual(parse_model_edits('{"edits": []}'), ([], 'json'))
        content = 'Explanation before.\n```json\n{"edits": []}\n```\n'
        self.assertEqual(parse_model_edits(content), ([], 'single_json_fence'))
        with self.assertRaises(json.JSONDecodeError):
            parse_model_edits('```json\n{"edits": []}\n```\n```json\n{"edits": []}\n```')
        with self.assertRaises(ValueError):
            parse_model_edits('{"edits": [], "extra": true}')

    def test_function_line_localization_contract_and_context(self):
        files = {'a.js': 'const one = 1;\nfunction target() {\n  return one;\n}\n'}
        evidence = line_evidence(files, ['a.js::function:target'], radius=1)
        self.assertEqual(evidence['a.js']['upstream_functions'], ['target'])
        locations, fmt = parse_locations(
            '```json\n{"locations":[{"path":"a.js","function":"target","start_line":2,"end_line":4}]}\n```', files)
        self.assertEqual(fmt, 'single_json_fence')
        self.assertIn('function target()', localized_context(files, locations, 1)['a.js'])
        with self.assertRaises(ValueError):
            parse_locations('{"locations":[{"path":"a.js","function":"x","start_line":0,"end_line":4}]}', files)
        with self.assertRaises(json.JSONDecodeError):
            parse_locations('', files)

    def test_candidate_vote_deduplicates_and_is_deterministic(self):
        candidates = [
            {'candidate_index': 0, 'status': 'generated', 'model_patch': 'a'},
            {'candidate_index': 1, 'status': 'generated', 'model_patch': 'b'},
            {'candidate_index': 2, 'status': 'generated', 'model_patch': 'b'},
        ]
        self.assertEqual(choose_candidate(candidates)['candidate_index'], 1)

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
                self.assertEqual(set(row), {'instance_id', 'found_files', 'found_functions', 'status'})


if __name__ == '__main__':
    unittest.main()
