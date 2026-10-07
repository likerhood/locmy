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
                    assess_patch_coverage, changed_line_count, choose_candidate,
                    format_repair_messages, line_evidence, localized_context,
                    failed_request_candidate, make_patch, normalized_patch_key, parse_locations, parse_model_edits,
                    parse_edits_with_format_retry, parse_localization, CandidateOutputError,
                    SEARCH_MARKER, DIVIDER_MARKER, REPLACE_MARKER, validate_path,
                    validate_updated_files)
from preflight import load_env


class RepairTests(unittest.TestCase):
    def test_api_uses_mimo_max_completion_tokens_and_thinking(self):
        class Response:
            def __enter__(self):
                return iter([
                    b'data: {"id":"ok","choices":[{"delta":{"role":"assistant","reasoning_content":"think"},"finish_reason":null,"index":0}]}\n',
                    b'data: {"id":"ok","choices":[{"delta":{"content":"done"},"finish_reason":"stop","index":0}]}\n',
                    b'data: {"id":"ok","choices":[],"usage":{"completion_tokens":3,"prompt_tokens":2,"total_tokens":5}}\n',
                    b'data: [DONE]\n',
                ])

            def __exit__(self, *args):
                return False

        env = {'RQ4_MODEL': 'mimo-v2.5', 'RQ4_BASE_URL': 'https://api.example/v1',
               'RQ4_API_KEY': 'test', 'RQ4_HTTP_RETRIES': '0'}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            with patch('urllib.request.urlopen', return_value=Response()) as request:
                _, response = api_call(Path(tmp) / 'mimo', [], 0.8, 131072, 1.0,
                                       'max_completion_tokens', 'enabled', True)
            sent = json.loads(request.call_args.args[0].data)
            self.assertEqual(sent['max_completion_tokens'], 131072)
            self.assertNotIn('max_tokens', sent)
            self.assertEqual(sent['thinking'], {'type': 'enabled'})
            self.assertTrue(sent['stream'])
            self.assertEqual(sent['stream_options'], {'include_usage': True})
            self.assertEqual(response['choices'][0]['message']['reasoning_content'], 'think')
            self.assertEqual(response['choices'][0]['message']['content'], 'done')
            self.assertEqual(response['usage']['total_tokens'], 5)
            self.assertTrue((Path(tmp) / 'mimo/stream.jsonl').is_file())
            attempt = json.loads((Path(tmp) / 'mimo/attempt.json').read_text())
            self.assertEqual(attempt['token_field'], 'max_completion_tokens')
            self.assertEqual(attempt['thinking_mode'], 'enabled')

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
                result, _ = api_call(Path(tmp) / 'http', [], 0, 10, 0.95)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result['http_attempt_count'], 2)
            attempt = json.loads((Path(tmp) / 'http/attempt.json').read_text())
            self.assertEqual(attempt['http_failures'][0]['http_status'], 500)
            self.assertEqual(attempt['top_p'], 0.95)

            with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('reset')) as request:
                with self.assertRaises(urllib.error.URLError):
                    api_call(Path(tmp) / 'connection', [], 0, 10)
            self.assertEqual(request.call_count, 1)

    def test_uncertain_repair_request_becomes_auditable_failed_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / 'repair_00'
            call_dir.mkdir()
            (call_dir / 'failure.json').write_text(json.dumps({
                'status': 'failed', 'error_type': 'TimeoutError',
                'http_attempt_count': 1,
            }))
            candidate = failed_request_candidate(
                0, 0, 1.0, TimeoutError('read timed out'), call_dir,
            )
        self.assertEqual(candidate['status'], 'failed')
        self.assertEqual(candidate['failure_stage'], 'api_request')
        self.assertEqual(candidate['request_failure']['error_type'], 'TimeoutError')
        self.assertEqual(candidate['model_patch'], '')
        self.assertIn('not_resent', candidate['retry_safety'])

    def test_prompts_are_english_and_state_output_contracts(self):
        self.assertTrue(LOCALIZATION_SYSTEM_PROMPT.isascii())
        self.assertIn('exactly one valid JSON object', LOCALIZATION_SYSTEM_PROMPT)
        self.assertIn('only locations and repair_plan keys', LOCALIZATION_SYSTEM_PROMPT)
        self.assertTrue(REPAIR_SYSTEM_PROMPT.isascii())
        self.assertIn('SEARCH/REPLACE', REPAIR_SYSTEM_PROMPT)
        self.assertIn('reason about the root cause', REPAIR_SYSTEM_PROMPT)
        self.assertIn('NO_VALID_EDIT', REPAIR_SYSTEM_PROMPT)
        self.assertIn('state flow', LOCALIZATION_SYSTEM_PROMPT)
        self.assertIn('repair_plan', REPAIR_SYSTEM_PROMPT)

        protocol = json.loads((ROOT / 'configs/protocol.json').read_text())
        self.assertEqual(protocol['name'], 'cosil_rq3_repair_top15_k10_mimo128k_stream_v8_patch_audit')
        self.assertEqual(
            protocol['uncertain_repair_request_policy'],
            'record_failed_api_candidate_without_resend_then_continue',
        )
        self.assertEqual(protocol['prompt_contract'], 'english_cosil_search_replace_repair_plan_v4')
        self.assertEqual(protocol['localization_temperature'], 0.8)
        self.assertEqual(protocol['localization_top_p'], 1.0)
        self.assertEqual(protocol['greedy_top_p'], 1.0)
        self.assertEqual(protocol['sampling_top_p'], 1.0)
        self.assertIn('official F2P/P2P', protocol['rerank_test_policy'])
        self.assertEqual(protocol['localization_max_output_tokens'], 131072)
        self.assertEqual(protocol['greedy_max_output_tokens'], 131072)
        self.assertEqual(protocol['sampling_max_output_tokens'], 131072)
        self.assertEqual(protocol['completion_token_field'], 'max_completion_tokens')
        self.assertEqual(protocol['thinking_mode'], 'enabled')
        self.assertTrue(protocol['streaming'])
        self.assertEqual(protocol['format_repair_attempts'], 1)
        self.assertIn('plan_file_coverage', protocol['candidate_selection'])

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
            path.write_text('RQ4_BASE_URL=https://token.example.test/v1\nRQ4_API_DIRECT=1\nRQ4_GITHUB_DIRECT=0\n')
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:7890',
                                         'NO_PROXY': 'localhost'}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['NO_PROXY'], 'localhost,token.example.test')
                self.assertEqual(os.environ['no_proxy'], 'token.example.test')
                self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:7890')

    def test_github_direct_disables_mirror_and_reaches_pipeline_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('RQ4_GITHUB_DIRECT=1\nRQ4_GITHUB_MIRROR_PREFIX=https://bad.example\n')
            inherited = {
                'HTTPS_PROXY': 'http://127.0.0.1:7890',
                'NO_PROXY': 'localhost',
                'RQ4_GITHUB_MIRROR_PREFIX': 'https://inherited.example',
            }
            with patch.dict(os.environ, inherited, clear=True):
                load_env(path)
                self.assertEqual(os.environ['RQ4_GITHUB_MIRROR_PREFIX'], '')
                self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:7890')
                for key in ('NO_PROXY', 'no_proxy'):
                    entries = os.environ[key].split(',')
                    self.assertIn('github.com', entries)
                    self.assertIn('raw.githubusercontent.com', entries)
                child = subprocess.check_output(
                    [sys.executable, '-c',
                     'import os; print(os.environ["RQ4_GITHUB_MIRROR_PREFIX"]); print(os.environ["NO_PROXY"])'],
                    text=True,
                ).splitlines()
                self.assertEqual(child[0], '')
                self.assertIn('github.com', child[1].split(','))

    def test_model_edits_accept_cosil_search_replace_and_json_fallback(self):
        self.assertEqual(parse_model_edits('{"edits": []}'), ([], 'json'))
        self.assertEqual(parse_model_edits('NO_VALID_EDIT'), ([], 'no_valid_edit'))
        content = 'Explanation before.\n```json\n{"edits": []}\n```\n'
        self.assertEqual(parse_model_edits(content), ([], 'single_json_fence'))
        content = f'''Reasoning first.\n```python
### src/a.js
{SEARCH_MARKER}
const value = 1;
{DIVIDER_MARKER}
const value = 2;
{REPLACE_MARKER}
```'''
        edits, response_format = parse_model_edits(content)
        self.assertEqual(response_format, 'cosil_search_replace')
        self.assertEqual(edits, [{'path': 'src/a.js', 'search': 'const value = 1;',
                                  'replace': 'const value = 2;'}])
        with self.assertRaises(ValueError):
            parse_model_edits('```json\n{"edits": []}\n```\n```json\n{"edits": []}\n```')
        with self.assertRaises(ValueError):
            parse_model_edits('{"edits": [], "extra": true}')

    def test_model_edits_accept_json_files_and_context_unified_diff(self):
        payload = json.dumps({'files': [{'path': 'src/a.js', 'old': 'const x = 1;\n',
                                         'new': 'const x = 2;\n'}]})
        edits, fmt = parse_model_edits(payload)
        self.assertEqual(fmt, 'json_files')
        self.assertEqual(edits[0]['search'], 'const x = 1;\n')
        diff = '''```diff
diff --git a/src/a.js b/src/a.js
index 1111111..2222222 100644
--- a/src/a.js
+++ b/src/a.js
@@ -1,3 +1,3 @@
 const before = true;
-const x = 1;
+const x = 2;
 const after = true;
```
'''
        edits, fmt = parse_model_edits(diff)
        self.assertEqual(fmt, 'unified_diff')
        original = {'src/a.js': 'const before = true;\nconst x = 1;\nconst after = true;\n'}
        updated = apply_edits(original, edits, visible={'src/a.js': original['src/a.js']})
        self.assertEqual(updated['src/a.js'], 'const before = true;\nconst x = 2;\nconst after = true;\n')
        with self.assertRaises(ValueError):
            parse_model_edits('--- /dev/null\n+++ b/src/new.js\n@@ -0,0 +1 @@\n+new\n')

    def test_fine_localization_plan_and_coverage_diagnostic(self):
        files = {'ui.js': 'function render() { return state.value; }\n',
                 'state.js': 'function reducer(state, action) { return state; }\n'}
        payload = {'locations': [
            {'path': 'ui.js', 'function': 'render', 'start_line': 1, 'end_line': 1},
            {'path': 'state.js', 'function': 'reducer', 'start_line': 1, 'end_line': 1}],
            'repair_plan': {'root_cause': 'state is not updated',
                'entry_points': [{'path': 'ui.js', 'symbol': 'render'}],
                'state_transitions': [{'path': 'state.js', 'symbol': 'reducer', 'from': 'old', 'to': 'new'}],
                'boundary_cases': ['missing state'], 'expected_files': ['ui.js', 'state.js']}}
        locations, _, plan = parse_localization(json.dumps(payload), files)
        self.assertEqual(len(locations), 2)
        partial = assess_patch_coverage(plan, ['ui.js'])
        self.assertEqual(partial['status'], 'coverage_gap')
        self.assertEqual(partial['missing_expected_files'], ['state.js'])
        complete = assess_patch_coverage(plan, ['ui.js', 'state.js'])
        self.assertEqual(complete['status'], 'complete')
        with self.assertRaises(ValueError):
            parse_localization(json.dumps({**payload, 'repair_plan': {
                **payload['repair_plan'], 'expected_files': ['outside.js']}}), files)

    def test_coverage_signal_precedes_vote_but_test_evidence_is_not_required(self):
        candidates = [
            {'candidate_index': 0, 'status': 'generated', 'model_patch': 'partial',
             'normalized_patch_sha256': 'partial', 'changed_lines': 2,
             'validation': {'status': 'passed'},
             'coverage_diagnostic': {'score': 0.5}},
            {'candidate_index': 1, 'status': 'generated', 'model_patch': 'complete',
             'normalized_patch_sha256': 'complete-a', 'changed_lines': 4,
             'validation': {'status': 'passed'}, 'coverage_diagnostic': {'score': 1.0}},
            {'candidate_index': 2, 'status': 'generated', 'model_patch': 'complete-copy',
             'normalized_patch_sha256': 'complete-b', 'changed_lines': 5,
             'validation': {'status': 'passed'}, 'coverage_diagnostic': {'score': 1.0}},
        ]
        self.assertEqual(choose_candidate(candidates)['candidate_index'], 1)

    def test_format_repair_is_explicitly_format_only(self):
        messages = format_repair_messages('some proposed edit', ValueError('unsupported output'))
        self.assertIn('Do not invent or improve a code change', messages[0]['content'])
        request = json.loads(messages[1]['content'])
        self.assertEqual(request['source_response'], 'some proposed edit')
        self.assertEqual(request['parse_error'], 'unsupported output')

    def test_format_retry_recovers_only_a_concrete_format_and_preserves_incomplete_state(self):
        calls = []

        def retry(content, error):
            calls.append((content, str(error)))
            return json.dumps({'files': [{'path': 'src/a.js', 'old': 'before', 'new': 'after'}]})

        edits, fmt, source = parse_edits_with_format_retry('Here is my edit: before -> after', 'stop', retry)
        self.assertEqual(fmt, 'json_files')
        self.assertEqual(source, 'format_repair_response')
        self.assertEqual(edits, [{'path': 'src/a.js', 'search': 'before', 'replace': 'after'}])
        self.assertEqual(len(calls), 1)

        with self.assertRaises(CandidateOutputError) as raised:
            parse_edits_with_format_retry('truncated', 'length', retry)
        self.assertEqual(raised.exception.failure_class, 'INCOMPLETE_RESPONSE')
        self.assertEqual(len(calls), 1)

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
            {'candidate_index': 0, 'status': 'generated', 'model_patch': 'a',
             'normalized_patch_sha256': 'a', 'changed_lines': 4,
             'validation': {'status': 'passed'}},
            {'candidate_index': 1, 'status': 'generated', 'model_patch': 'b',
             'normalized_patch_sha256': 'b', 'changed_lines': 2,
             'validation': {'status': 'passed'}},
            {'candidate_index': 2, 'status': 'generated', 'model_patch': 'c',
             'normalized_patch_sha256': 'b', 'changed_lines': 3,
             'validation': {'status': 'passed'}},
        ]
        self.assertEqual(choose_candidate(candidates)['candidate_index'], 1)

    def test_candidate_validation_normalization_and_localized_scope(self):
        original = {'a.py': 'def f():\n    return 1\n', 'data.json': '{"a": 1}\n'}
        updated = {'a.py': 'def f():\n    return 2\n', 'data.json': '{"a": 1}\n'}
        validation = validate_updated_files(original, updated)
        self.assertEqual(validation['status'], 'passed')
        self.assertEqual(validation['syntax_checked_files'], ['a.py'])
        key = normalized_patch_key(original, updated)
        whitespace = {'a.py': 'def f():  \r\n    return 2\r\n', 'data.json': '{"a": 1}\n'}
        self.assertEqual(key, normalized_patch_key(original, whitespace))
        patch = make_patch(original, updated)
        self.assertEqual(changed_line_count(patch), 2)
        with self.assertRaises(SyntaxError):
            validate_updated_files(original, {'a.py': 'def f(:\n', 'data.json': '{"a": 1}\n'})
        with self.assertRaises(ValueError):
            apply_edits({'a.py': 'x = 1\ny = 2\n'},
                        [{'path': 'a.py', 'search': 'y = 2', 'replace': 'y = 3'}],
                        visible={'a.py': 'x = 1'})

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
