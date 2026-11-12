#!/usr/bin/env python3
"""CoSIL-RQ3-aligned fine localization and multi-candidate repair runner.

Each sample/method gets one function/line localization call, followed by one
greedy and K-1 stochastic repair calls. Every paid call has an independent
durable attempt record. Explicit transient HTTP responses and proven
pre-connect DNS failures receive bounded retries; uncertain transport failures
are never retried automatically.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VARIANT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
from preflight import load_env

DEFAULT_REQUEST_TIMEOUT = 900
SEARCH_MARKER = '<' * 7 + ' SEARCH'
DIVIDER_MARKER = '=' * 7
REPLACE_MARKER = '>' * 7 + ' REPLACE'


class PaidCallUnavailable(RuntimeError):
    """A durable paid-call directory exists without a reusable response."""


class IncompleteStreamingResponse(RuntimeError):
    """A streaming response ended before the provider's DONE marker."""


class CandidateOutputError(ValueError):
    """A completed model response cannot be used as a patch candidate."""

    def __init__(self, message, failure_class, failure_stage):
        super().__init__(message)
        self.failure_class = failure_class
        self.failure_stage = failure_stage


def is_preconnect_dns_failure(exc):
    """Return true only when no connection could have reached the provider."""
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, socket.gaierror)


def archive_recoverable_paid_call(call_dir):
    """Archive a proven unaccepted request so an explicit resume may retry it."""
    failure_path = call_dir/'failure.json'
    if not failure_path.exists() or any((call_dir/name).exists()
                                        for name in ('stream.jsonl', 'response.json', 'result.json')):
        return None
    try:
        failure = json.loads(failure_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    preconnect_dns = (failure.get('error_type') == 'URLError'
                      and failure.get('error_reason_type') == 'gaierror')
    provider_rejected = (failure.get('error_type') == 'HTTPError'
                         and failure.get('http_status') == 429)
    if not (preconnect_dns or provider_rejected):
        return None
    history = (call_dir.parent/'retry_history'/call_dir.name/
               f'{time.strftime("%Y%m%d-%H%M%S")}-{time.time_ns()}')
    history.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(call_dir), str(history))
    return history

LOCALIZATION_SYSTEM_PROMPT = """You are the fine-grained localization stage of a software repair system.
Identify the smallest defensible function and line intervals that must be inspected or edited to solve the issue.

Return exactly one valid JSON object with this schema:
{"locations":[{"path":"relative/path.ext","function":"symbol or scope name","start_line":1,"end_line":2}],"repair_plan":{"root_cause":"...","entry_points":[{"path":"...","symbol":"..."}],"state_transitions":[{"path":"...","symbol":"...","from":"...","to":"..."}],"boundary_cases":["..."],"expected_files":["..."]}}

Rules:
- Use only file paths supplied in file_localization.
- Line numbers are 1-based, inclusive, and must exist in the supplied evidence.
- Prefer narrow intervals that contain the likely fault and enough surrounding context to edit it.
- Include connected functions/files needed for a complete behavior change: callers/callees, action/reducer/selector or equivalent state flow, and relevant fallback/error branches. Return a location for each required code region.
- In repair_plan, list only paths and symbols supported by the supplied evidence. expected_files means files that must actually be edited, not files that are useful only for inspection. Keep it to the smallest set that could complete the fix; use an empty list when evidence does not support an edit target.
- Every expected file must have at least one returned location covering the concrete function or scope that should be edited. Do not use a constructor or class header as a substitute for a different method named in the root-cause analysis.
- Use upstream function evidence when it is relevant, but do not invent symbols or paths.
- Do not return prose, analysis, comments, Markdown, or code fences.
- Use double-quoted JSON strings and escape embedded newlines and quotes correctly.
- Never return an empty response. If no defensible location exists, return {"locations":[]}.
- Before answering, verify that the output is syntactically valid JSON and contains only locations and repair_plan keys."""

REPAIR_SYSTEM_PROMPT = f"""You are the patch-generation stage of a software repair system.
Fix the reported issue using only the supplied localized code from existing files.

First reason about the root cause, the intended behavior, and the smallest complete fix. Then return one or more SEARCH/REPLACE edits in this exact format:

```python
### relative/path.ext
{SEARCH_MARKER}
exact existing text
{DIVIDER_MARKER}
replacement text
{REPLACE_MARKER}
```

Rules:
- Use only paths present in localized_code. Do not create, rename, or delete files.
- Every SEARCH block must be copied exactly from localized_code and must match exactly once in the complete original file.
- Include enough unchanged surrounding text in SEARCH to make the match unique.
- Preserve indentation, syntax, imports, types, and project conventions in REPLACE.
- Make the smallest complete change that addresses the issue; avoid unrelated cleanup.
- Fix the fault at the earliest responsible state transition. Do not compensate in a later parser, renderer, or caller when the supplied source includes the earlier tokenizer, lexer, reducer, or producer that introduced the wrong state.
- Multiple edits and multiple files are allowed when the supplied localized code supports them.
- Implement the supplied repair_plan across all listed expected_files and state-transition targets when the localized code supports the change. Do not omit a listed layer without explaining why in a short `COVERAGE_GAP:` line before the edits.
- Brief reasoning may precede the edits, but do not place commentary inside an edit block.
- Finish with the edit blocks. Do not append prose after the final block.
- If no defensible edit can be made, return exactly NO_VALID_EDIT.
- Before answering, verify every path, SEARCH block, and replacement for exact applicability."""

SELECTOR_SYSTEM_PROMPT = """You are the candidate-selection stage of a software repair system.
Choose the single patch most likely to solve the reported issue without regressions.
Use only the issue, repair plan, static validation, and candidate diffs supplied here.
Do not assume access to hidden or official benchmark tests.
Prefer a fix at the earliest responsible state transition over a downstream compensation.
Return exactly one JSON object: {"selected_candidate":0,"reason":"short explanation"}.
The selected_candidate must be one of the supplied candidate indices."""

FORMAT_REPAIR_SYSTEM_PROMPT = """Convert an existing proposed code edit into the required JSON format.
Do not invent or improve a code change. Preserve the exact paths and old/new code stated in the source response.
Return exactly {\"edits\":[{\"path\":\"...\",\"search\":\"...\",\"replace\":\"...\"}]}.
If the source contains no concrete code edit, return exactly {\"edits\":[]}.
Do not include commentary or Markdown."""


def format_repair_messages(content, parse_error):
    return [
        {'role': 'system', 'content': FORMAT_REPAIR_SYSTEM_PROMPT},
        {'role': 'user', 'content': json.dumps({
            'source_response': content,
            'parse_error': str(parse_error)[:300],
        }, ensure_ascii=False)},
    ]


def save(path, value, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    text = json.dumps(value, ensure_ascii=False) + '\n' if jsonl else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temp.write_text(text)
    temp.replace(path)


def validate_path(path):
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or '..' in path.split('/'):
        raise ValueError('Invalid repository path')
    if '.git' in PurePosixPath(path).parts:
        raise ValueError('Git metadata cannot be edited')
    return path


def apply_edits(original, edits, visible=None):
    if not isinstance(edits, list):
        raise ValueError('edits must be a list')
    updated = dict(original)
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'path', 'search', 'replace'}:
            raise ValueError('Each edit must contain only path/search/replace')
        path = validate_path(edit['path'])
        if path not in original:
            raise ValueError('Only localized existing files may be edited')
        old, new = edit['search'], edit['replace']
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError('Invalid replacement')
        if updated[path].count(old) != 1:
            raise ValueError('Search text must match exactly once')
        if visible is not None and (path not in visible or old not in visible[path]):
            raise ValueError('Search text must be present in localized code')
        updated[path] = updated[path].replace(old, new, 1)
    return updated


def make_patch(original, updated):
    parts = []
    for path, old in original.items():
        new = updated[path]
        if old == new:
            continue
        for line in difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                         fromfile=f'a/{path}', tofile=f'b/{path}'):
            parts.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    return ''.join(parts)


def parse_json_object(content):
    """Accept a JSON object or exactly one fenced JSON object."""
    if not isinstance(content, str):
        raise TypeError('Model content must be a string')

    def load_json(text):
        try:
            return json.loads(text), 'json'
        except json.JSONDecodeError as exc:
            stripped = text.rstrip()
            if exc.pos < len(stripped) - 1:
                raise
            stack = []
            in_string = False
            escaped = False
            pairs = {'{': '}', '[': ']'}
            for char in stripped:
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char in pairs:
                    stack.append(pairs[char])
                elif char in '}]':
                    if not stack or stack.pop() != char:
                        raise exc
            if in_string or not stack:
                raise
            completed = stripped + ''.join(reversed(stack))
            return json.loads(completed), 'json_completed_closing_suffix'

    response_format = 'json'
    try:
        payload, response_format = load_json(content)
    except json.JSONDecodeError:
        blocks = re.findall(r'```(?:json)?[ \t]*\r?\n?(.*?)```', content,
                            flags=re.DOTALL | re.IGNORECASE)
        if len(blocks) != 1:
            raise
        payload, block_format = load_json(blocks[0].strip())
        response_format = ('single_json_fence' if block_format == 'json' else
                           'single_json_fence_completed_closing_suffix')
    if not isinstance(payload, dict):
        raise ValueError('Model response must be a JSON object')
    return payload, response_format


def parse_model_edits(content):
    """Normalize supported model edit formats to exact-search replacements."""
    if not isinstance(content, str):
        raise TypeError('Model content must be a string')
    if content.strip() == 'NO_VALID_EDIT':
        return [], 'no_valid_edit'
    try:
        payload, response_format = parse_json_object(content)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if payload is not None:
        if set(payload) == {'edits'} and isinstance(payload['edits'], list):
            return payload['edits'], response_format
        if set(payload) == {'files'} and isinstance(payload['files'], list):
            edits = []
            for item in payload['files']:
                if not isinstance(item, dict) or set(item) != {'path', 'old', 'new'}:
                    raise ValueError('Each JSON files item must contain path/old/new')
                edits.append({'path': item['path'], 'search': item['old'], 'replace': item['new']})
            return edits, 'json_files'
        if set(payload) == {'patch'} and isinstance(payload['patch'], str):
            return parse_model_edits(payload['patch'])
        raise ValueError('Model JSON must contain edits, files, or a unified diff patch')

    blocks = re.findall(r'```(?:python|diff|text)?[ \t]*\r?\n(.*?)```', content,
                        flags=re.DOTALL | re.IGNORECASE)
    sources = blocks or [content]
    pattern = re.compile(
        r'(?:^|\n)###\s+([^\r\n]+)\r?\n'
        + re.escape(SEARCH_MARKER) + r'\r?\n(.*?)\r?\n'
        + re.escape(DIVIDER_MARKER) + r'\r?\n(.*?)\r?\n'
        + re.escape(REPLACE_MARKER) + r'(?=\r?\n|$)',
        flags=re.DOTALL,
    )
    edits = []
    for source in sources:
        for match in pattern.finditer(source):
            path = match.group(1).strip().strip('`')
            edits.append({'path': path, 'search': match.group(2), 'replace': match.group(3)})
    if not edits:
        diff_edits = parse_unified_diff_edits(content)
        if diff_edits:
            return diff_edits, 'unified_diff'
        raise ValueError('No supported patch format found')
    marker_count = content.count(SEARCH_MARKER)
    if marker_count != len(edits):
        raise ValueError('One or more SEARCH/REPLACE blocks are malformed')
    return edits, 'cosil_search_replace'


def parse_edits_with_format_retry(content, finish_reason, retry=None):
    """Parse once, optionally invoking one caller-supplied format-only retry."""
    if finish_reason == 'length':
        raise CandidateOutputError('Provider stopped at completion length limit',
                                   'INCOMPLETE_RESPONSE', 'incomplete_response')
    if finish_reason == 'content_filter':
        raise CandidateOutputError('Provider filtered the completion',
                                   'MODEL_BLOCKED', 'model_blocked')
    try:
        edits, response_format = parse_model_edits(content)
        return edits, response_format, 'original_response'
    except (TypeError, ValueError, json.JSONDecodeError) as parse_exc:
        if retry is None:
            raise
        retry_content = retry(content, parse_exc)
        edits, response_format = parse_model_edits(retry_content)
        return edits, response_format, 'format_repair_response'


def parse_unified_diff_edits(content):
    """Parse context-bearing unified diffs into exact SEARCH/REPLACE edits.

    Hunk context is retained so apply_edits can verify unique, localized matches.
    Zero-context insertion hunks are rejected instead of guessed.
    """
    if not isinstance(content, str):
        raise TypeError('Model content must be a string')
    fenced = re.findall(r'```diff[ \t]*\r?\n(.*?)```', content,
                        flags=re.DOTALL | re.IGNORECASE)
    source = fenced[0] if len(fenced) == 1 else content
    lines = source.splitlines(keepends=True)
    edits = []
    i = 0
    hunk_header = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    while i < len(lines):
        if not lines[i].startswith('--- '):
            i += 1
            continue
        old_path = lines[i][4:].strip().split('\t', 1)[0]
        i += 1
        if i >= len(lines) or not lines[i].startswith('+++ '):
            raise ValueError('Unified diff is missing its new-file header')
        new_path = lines[i][4:].strip().split('\t', 1)[0]
        i += 1
        if old_path.startswith('a/'):
            old_path = old_path[2:]
        if new_path.startswith('b/'):
            new_path = new_path[2:]
        if old_path == '/dev/null' or new_path == '/dev/null' or old_path != new_path:
            raise ValueError('Unified diff may not add, delete, or rename files')
        path = validate_path(old_path)
        while i < len(lines) and lines[i].startswith('@@ '):
            if not hunk_header.match(lines[i]):
                raise ValueError('Malformed unified diff hunk header')
            i += 1
            old_chunk, new_chunk = [], []
            while i < len(lines) and not lines[i].startswith(('@@ ', '--- ', 'diff --git ')):
                line = lines[i]
                if line.startswith('\\ No newline at end of file'):
                    i += 1
                    continue
                if not line or line[0] not in ' +-':
                    break
                if line[0] in ' -':
                    old_chunk.append(line[1:])
                if line[0] in ' +':
                    new_chunk.append(line[1:])
                i += 1
            search, replacement = ''.join(old_chunk), ''.join(new_chunk)
            if not search:
                raise ValueError('Zero-context unified diff hunk is not safely applicable')
            if search == replacement:
                raise ValueError('Unified diff hunk contains no change')
            edits.append({'path': path, 'search': search, 'replace': replacement})
        if not edits:
            raise ValueError('Unified diff contains no hunks')
    return edits


def validate_updated_files(original, updated):
    """Run deterministic, language-aware checks that require no benchmark tests."""
    changed = [path for path in original if original[path] != updated[path]]
    if not changed:
        return {'status': 'empty', 'changed_files': [], 'syntax_checked_files': []}
    syntax_checked = []
    for path in changed:
        text = updated[path]
        if '\x00' in text:
            raise ValueError('Edited file contains a NUL byte')
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == '.py':
            ast.parse(text, filename=path)
            syntax_checked.append(path)
        elif suffix == '.json':
            json.loads(text)
            syntax_checked.append(path)
    return {'status': 'passed', 'changed_files': changed,
            'syntax_checked_files': syntax_checked}


def normalized_patch_key(original, updated):
    """Canonicalize changed content for stable voting across equivalent diff context."""
    changed = {}
    for path in sorted(original):
        if original[path] == updated[path]:
            continue
        text = updated[path].replace('\r\n', '\n').replace('\r', '\n')
        changed[path] = '\n'.join(line.rstrip() for line in text.split('\n'))
    encoded = json.dumps(changed, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def changed_line_count(patch):
    return sum(1 for line in patch.splitlines()
               if line.startswith(('+', '-')) and not line.startswith(('+++', '---')))


def assess_patch_coverage(repair_plan, changed_files):
    """Compare edited paths with an independently produced localization plan.

    This is a warning/ranking signal only; it does not prove semantic correctness.
    """
    if not isinstance(repair_plan, dict):
        return {'status': 'unavailable', 'score': None, 'missing_expected_files': [],
                'changed_files': sorted(set(changed_files))}
    # v9 separates inspection evidence from files that the plan says must be
    # edited. Entry points and state transitions remain useful context but no
    # longer force unrelated files into a patch merely to improve its score.
    expected = set(repair_plan.get('expected_files', []))
    changed = set(changed_files)
    if not expected:
        return {'status': 'no_expected_files', 'score': None,
                'missing_expected_files': [], 'changed_files': sorted(changed)}
    covered = expected & changed
    missing = sorted(expected - changed)
    return {'status': 'complete' if not missing else 'coverage_gap',
            'score': len(covered) / len(expected),
            'expected_files': sorted(expected), 'covered_files': sorted(covered),
            'missing_expected_files': missing, 'changed_files': sorted(changed),
            'boundary_cases_declared': len(repair_plan.get('boundary_cases', []))}


def parse_locations(content, files):
    locations, response_format, _ = parse_localization(content, files)
    return locations, response_format


def parse_localization(content, files):
    payload, response_format = parse_json_object(content)
    if not {'locations'} <= set(payload) or set(payload) - {'locations', 'repair_plan'}:
        raise ValueError('Localization JSON must contain locations and optional repair_plan')
    if not isinstance(payload['locations'], list):
        raise ValueError('Localization locations must be a list')
    result = []
    for item in payload['locations']:
        if not isinstance(item, dict) or set(item) != {'path', 'function', 'start_line', 'end_line'}:
            raise ValueError('Each location must contain path/function/start_line/end_line')
        path = validate_path(item['path'])
        if path not in files or not isinstance(item['function'], str):
            raise ValueError('Location references an unavailable file/function')
        start, end = item['start_line'], item['end_line']
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(files[path].splitlines()):
            raise ValueError('Invalid localization line interval')
        normalized = {'path': path, 'function': item['function'], 'start_line': start, 'end_line': end}
        if normalized not in result:
            result.append(normalized)
    plan = payload.get('repair_plan')
    if plan is not None:
        allowed = {'root_cause', 'entry_points', 'state_transitions', 'boundary_cases', 'expected_files'}
        if not isinstance(plan, dict) or set(plan) != allowed:
            raise ValueError('repair_plan must contain root_cause/entry_points/state_transitions/boundary_cases/expected_files')
        if not isinstance(plan['root_cause'], str):
            raise ValueError('repair_plan.root_cause must be a string')
        for field in ('entry_points', 'state_transitions', 'boundary_cases', 'expected_files'):
            if not isinstance(plan[field], list):
                raise ValueError(f'repair_plan.{field} must be a list')
        for field in ('entry_points', 'state_transitions'):
            for item in plan[field]:
                if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('symbol'), str):
                    raise ValueError(f'repair_plan.{field} items require path and symbol')
                item['path'] = validate_path(item['path'])
                if item['path'] not in files:
                    raise ValueError(f'repair_plan references unavailable file: {item["path"]}')
        for path in plan['expected_files']:
            validate_path(path)
            if path not in files:
                raise ValueError(f'repair_plan references unavailable expected file: {path}')
        if any(not isinstance(item, str) for item in plan['boundary_cases']):
            raise ValueError('repair_plan.boundary_cases items must be strings')
        plan['expected_files'] = list(dict.fromkeys(plan['expected_files']))
    return result, response_format, plan


def symbol_seed(value):
    if not isinstance(value, str) or '::' not in value:
        return None
    path, name = value.split('::', 1)
    name = re.sub(r'^(?:function|class|method):', '', name).split('.')[-1]
    return (path, name) if path and name and name != '__file__' else None


def line_evidence(files, found_functions, radius=18, max_per_file=12):
    """Create bounded line-numbered evidence around upstream function hits."""
    by_path = {path: [] for path in files}
    for value in found_functions:
        seed = symbol_seed(value)
        if seed and seed[0] in by_path and seed[1] not in by_path[seed[0]]:
            by_path[seed[0]].append(seed[1])
    evidence = {}
    definition = re.compile(r'^\s*(?:async\s+)?(?:def|class|function)\s+([A-Za-z_$][\w$]*)|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=|^\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{')
    for path, text in files.items():
        lines = text.splitlines()
        names = by_path[path]
        hits = []
        for name in names[:max_per_file]:
            pattern = re.compile(r'\b' + re.escape(name) + r'\b')
            indices = [i for i, line in enumerate(lines) if pattern.search(line)]
            if indices:
                i = indices[0]
                hits.append((max(0, i-radius), min(len(lines), i+radius+1), name))
        if not hits:
            for i, line in enumerate(lines):
                match = definition.search(line)
                if match:
                    name = next(x for x in match.groups() if x)
                    hits.append((max(0, i-2), min(len(lines), i+5), name))
                if len(hits) >= max_per_file:
                    break
        chunks = []
        for start, end, name in hits:
            body = '\n'.join(f'{i+1}: {lines[i]}' for i in range(start, end))
            chunks.append({'function': name, 'start_line': start+1, 'end_line': end, 'code': body})
        evidence[path] = {'upstream_functions': names, 'excerpts': chunks}
    return evidence


def localized_context(files, locations, window):
    grouped = {}
    for loc in locations:
        grouped.setdefault(loc['path'], []).append(loc)
    contexts = {}
    for path, locs in grouped.items():
        lines = files[path].splitlines()
        intervals = sorted((max(1, x['start_line']-window), min(len(lines), x['end_line']+window)) for x in locs)
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        contexts[path] = '\n\n'.join(
            f'[lines {start}-{end}]\n' + '\n'.join(lines[start-1:end]) for start, end in merged)
    return contexts


def aligned_context(files, locations, repair_plan, window, max_bytes):
    """Prefer complete edit-target files, then add bounded localized evidence."""
    localized = localized_context(files, locations, window)
    expected = []
    if isinstance(repair_plan, dict):
        for path in repair_plan.get('expected_files', []):
            if path in files and path not in expected:
                expected.append(path)

    contexts = {}
    used = 0
    complete_files = []
    omitted_files = []
    for path in expected:
        complete = '[complete file]\n' + files[path]
        size = len(complete.encode())
        if used + size <= max_bytes:
            contexts[path] = complete
            complete_files.append(path)
            used += size
        elif path in localized:
            snippet = localized[path]
            size = len(snippet.encode())
            if used + size <= max_bytes:
                contexts[path] = snippet
                used += size
            else:
                omitted_files.append(path)
        else:
            omitted_files.append(path)

    for path, snippet in localized.items():
        if path in contexts:
            continue
        size = len(snippet.encode())
        if used + size <= max_bytes:
            contexts[path] = snippet
            used += size
        else:
            omitted_files.append(path)

    return contexts, {
        'complete_files': complete_files,
        'omitted_files': list(dict.fromkeys(omitted_files)),
        'context_bytes': used,
        'expected_files': expected,
    }


def read_streaming_response(response, stream_path):
    """Collect OpenAI-compatible SSE chunks into one chat completion response."""
    payload = {'choices': [{'index': 0, 'finish_reason': None,
                            'message': {'role': 'assistant', 'content': '',
                                        'reasoning_content': '', 'tool_calls': None}}],
               'usage': {}}
    message = payload['choices'][0]['message']
    saw_done = False
    with stream_path.open('w', buffering=1) as stream:
        for raw_line in response:
            line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
            line = line.strip()
            if not line or line.startswith(':') or not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                saw_done = True
                break
            chunk = json.loads(data)
            stream.write(json.dumps(chunk, ensure_ascii=False) + '\n')
            for key in ('id', 'created', 'model', 'object'):
                if chunk.get(key) is not None:
                    payload[key] = chunk[key]
            if chunk.get('usage'):
                payload['usage'] = chunk['usage']
            choices = chunk.get('choices') or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get('finish_reason') is not None:
                payload['choices'][0]['finish_reason'] = choice['finish_reason']
            delta = choice.get('delta') or {}
            for key in ('content', 'reasoning_content'):
                if delta.get(key):
                    message[key] += delta[key]
            if delta.get('role'):
                message['role'] = delta['role']
            if delta.get('tool_calls') is not None:
                message['tool_calls'] = delta['tool_calls']
    if not saw_done:
        raise IncompleteStreamingResponse('Streaming response ended without [DONE]')
    if payload['choices'][0]['finish_reason'] is None:
        raise IncompleteStreamingResponse('Streaming response ended without finish_reason')
    return payload


def api_call(call_dir, messages, temperature, max_tokens, top_p=1.0,
             token_field='max_tokens', thinking_mode=None, streaming=False):
    """Execute a durable request, retrying only explicit transient HTTP failures."""
    result_path, response_path = call_dir/'result.json', call_dir/'response.json'
    if result_path.exists() and response_path.exists():
        return json.loads(result_path.read_text()), json.loads(response_path.read_text())
    if call_dir.exists():
        history = archive_recoverable_paid_call(call_dir)
        if history is None:
            raise PaidCallUnavailable(
                f'Uncertain or failed paid request at {call_dir}; the original request is not resent'
            )
    if token_field not in {'max_tokens', 'max_completion_tokens'}:
        raise ValueError('Unsupported completion token field')
    if thinking_mode not in {None, 'enabled', 'disabled'}:
        raise ValueError('Unsupported thinking mode')
    call_dir.mkdir(parents=True)
    model = os.environ['RQ4_MODEL']
    omit_sampling = model.lower().startswith('mimo-v2.6-') and thinking_mode == 'enabled'
    omission_reason = ('MiMo 2.6 thinking mode does not support custom temperature or top_p'
                       if omit_sampling else None)
    body = {'model': model, 'messages': messages, token_field: max_tokens}
    if not omit_sampling:
        body['temperature'] = temperature
        body['top_p'] = top_p
    if thinking_mode is not None:
        body['thinking'] = {'type': thinking_mode}
    if streaming:
        body['stream'] = True
        body['stream_options'] = {'include_usage': True}
    try:
        http_retries = max(0, int(os.getenv('RQ4_HTTP_RETRIES', '2')))
    except ValueError as exc:
        raise ValueError('RQ4_HTTP_RETRIES must be an integer') from exc
    raw_sleeps = os.getenv('RQ4_HTTP_RETRY_SLEEPS', '10,30')
    try:
        retry_sleeps = [max(0.0, float(x.strip())) for x in raw_sleeps.split(',') if x.strip()]
    except ValueError as exc:
        raise ValueError('RQ4_HTTP_RETRY_SLEEPS must be comma-separated numbers') from exc
    attempt = {'status': 'started',
        'request_sha256': hashlib.sha256(json.dumps(body, ensure_ascii=False).encode()).hexdigest(),
        'temperature': temperature, 'top_p': top_p,
        'requested_temperature': temperature, 'requested_top_p': top_p,
        'effective_temperature': None if omit_sampling else temperature,
        'effective_top_p': None if omit_sampling else top_p,
        'sampling_parameters_omitted': omit_sampling,
        'sampling_parameters_omission_reason': omission_reason,
        'max_tokens': max_tokens, 'token_field': token_field,
        'thinking_mode': thinking_mode, 'streaming': streaming,
        'started_at': time.time(),
        'http_retry_limit': http_retries, 'http_failures': [], 'transport_failures': []}
    save(call_dir/'attempt.json', attempt)
    start = time.monotonic()
    retryable_statuses = {429, 500, 502, 503, 504}
    for request_index in range(http_retries + 1):
        request = urllib.request.Request(os.environ['RQ4_BASE_URL'].rstrip('/') + '/chat/completions',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + os.environ['RQ4_API_KEY']})
        try:
            with urllib.request.urlopen(
                    request,
                    timeout=int(os.getenv('RQ4_REQUEST_TIMEOUT', str(DEFAULT_REQUEST_TIMEOUT))),
            ) as response:
                data = (read_streaming_response(response, call_dir/'stream.jsonl')
                        if streaming else json.load(response))
            save(response_path, data)
            result = {'status': 'completed', 'elapsed_seconds': time.monotonic()-start,
                      'usage': data.get('usage') or {}, 'provider_request_id': data.get('id'),
                      'http_attempt_count': request_index + 1}
            save(result_path, result)
            return result, data
        except urllib.error.HTTPError as exc:
            event = {'attempt_number': request_index + 1, 'http_status': exc.code,
                     'http_reason': str(exc.reason), 'elapsed_seconds': time.monotonic()-start}
            attempt['http_failures'].append(event)
            save(call_dir/'attempt.json', attempt)
            if exc.code in retryable_statuses and request_index < http_retries:
                delay = retry_sleeps[min(request_index, len(retry_sleeps)-1)] if retry_sleeps else 0
                event['retry_after_seconds'] = delay
                save(call_dir/'attempt.json', attempt)
                if delay:
                    time.sleep(delay)
                continue
            save(call_dir/'failure.json', {'status': 'failed', 'error_type': type(exc).__name__,
                'http_status': exc.code, 'http_reason': str(exc.reason),
                'http_attempt_count': request_index + 1, 'elapsed_seconds': time.monotonic()-start})
            raise
        except urllib.error.URLError as exc:
            event = {'attempt_number': request_index + 1,
                     'error_type': type(exc).__name__,
                     'error_reason_type': type(exc.reason).__name__,
                     'elapsed_seconds': time.monotonic()-start}
            attempt['transport_failures'].append(event)
            save(call_dir/'attempt.json', attempt)
            if is_preconnect_dns_failure(exc) and request_index < http_retries:
                delay = retry_sleeps[min(request_index, len(retry_sleeps)-1)] if retry_sleeps else 0
                event['retry_after_seconds'] = delay
                save(call_dir/'attempt.json', attempt)
                if delay:
                    time.sleep(delay)
                continue
            save(call_dir/'failure.json', {'status': 'failed',
                'error_type': type(exc).__name__,
                'http_attempt_count': request_index + 1,
                'elapsed_seconds': time.monotonic()-start,
                'error_reason_type': type(exc.reason).__name__})
            raise
        except Exception as exc:
            failure = {'status': 'failed', 'error_type': type(exc).__name__,
                       'http_attempt_count': request_index + 1, 'elapsed_seconds': time.monotonic()-start}
            if isinstance(exc, urllib.error.URLError):
                failure['error_reason_type'] = type(exc.reason).__name__
            save(call_dir/'failure.json', failure)
            raise


def failed_request_candidate(index, temperature, top_p, exc, call_dir):
    """Represent a lost repair response without retrying or aborting the batch."""
    failure_path = call_dir/'failure.json'
    request_failure = {}
    if failure_path.exists():
        try:
            request_failure = json.loads(failure_path.read_text())
        except (OSError, json.JSONDecodeError):
            request_failure = {'status': 'unreadable_failure_record'}
    return {
        'candidate_index': index,
        'temperature': temperature,
        'top_p': top_p,
        'status': 'failed',
        'failure_stage': 'api_request',
        'model_patch': '',
        'usage': {},
        'provider_request_id': None,
        'finish_reason': None,
        'response_content_chars': 0,
        'response_received': False,
        'response_file': None,
        'response_parse_ok': False,
        'patch_nonempty': False,
        'patch_apply_ok': False,
        'scope_ok': None,
        'failure_class': 'API_REQUEST_FAILED',
        'format_repair_attempted': False,
        'error_type': type(exc).__name__,
        'error_detail': str(exc)[:500],
        'request_failure': request_failure,
        'retry_safety': 'not_resent_because_provider_completion_or_billing_may_be_uncertain',
    }


def load_files(repo, sample, found_files, top_k):
    original, rejected = {}, []
    for raw_path in found_files[:top_k]:
        path = validate_path(raw_path)
        result = subprocess.run(['git', '-C', str(repo), 'show', f'{sample["base_commit"]}:{path}'], capture_output=True)
        if result.returncode:
            rejected.append(path)
            continue
        try:
            original[path] = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            rejected.append(path)
    return original, rejected


def choose_candidate(candidates):
    """Prefer plan-complete patches, then vote, then minimize change deterministically."""
    nonempty = [x for x in candidates if x['status'] == 'generated' and x['model_patch']
                and x.get('validation', {}).get('status') == 'passed']
    if not nonempty:
        return None
    groups = {}
    for candidate in nonempty:
        digest = candidate.get('normalized_patch_sha256') or hashlib.sha256(
            candidate['model_patch'].encode()).hexdigest()
        groups.setdefault(digest, []).append(candidate)
    def coverage_score(group):
        scores = [x.get('coverage_diagnostic', {}).get('score') for x in group]
        scores = [score for score in scores if isinstance(score, (int, float))]
        return max(scores) if scores else -1.0
    winning = min(groups.values(), key=lambda group: (
        -coverage_score(group), -len(group),
        min(x.get('changed_lines', float('inf')) for x in group),
        min(x['candidate_index'] for x in group)))
    return min(winning, key=lambda x: (x.get('changed_lines', float('inf')),
                                       x['candidate_index']))


def review_candidate_selection(protocol, run_dir, issue, repair_plan, candidates,
                               token_field, thinking_mode, streaming):
    """Use one independent, auditable review call without benchmark tests."""
    valid = [candidate for candidate in candidates
             if candidate.get('status') == 'generated'
             and candidate.get('model_patch')
             and candidate.get('validation', {}).get('status') == 'passed']
    fallback = choose_candidate(candidates)
    if len(valid) < 2 or not protocol.get('selector_review', False):
        return fallback, None

    payload = {
        'issue': issue,
        'repair_plan': repair_plan,
        'candidates': [{
            'candidate_index': item['candidate_index'],
            'model_patch': item['model_patch'],
            'changed_lines': item.get('changed_lines'),
            'coverage_diagnostic': item.get('coverage_diagnostic'),
            'validation': item.get('validation'),
        } for item in valid],
    }
    messages = [
        {'role': 'system', 'content': SELECTOR_SYSTEM_PROMPT},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]
    call_dir = run_dir/'paid_calls'/'candidate_selector'
    review = {'status': 'failed', 'fallback_candidate': (
        fallback.get('candidate_index') if fallback else None)}
    try:
        call_result, response = api_call(
            call_dir, messages, 0.0, protocol.get('selector_max_output_tokens', 32768),
            1.0, token_field, thinking_mode, streaming,
        )
        content = ((response.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        payload, response_format = parse_json_object(content)
        selected_index = payload.get('selected_candidate')
        selected = next((item for item in valid
                         if item['candidate_index'] == selected_index), None)
        if selected is None:
            raise ValueError('Selector chose an unavailable candidate')
        review.update({
            'status': 'completed',
            'selected_candidate': selected_index,
            'reason': str(payload.get('reason', ''))[:1000],
            'response_format': response_format,
            'usage': call_result.get('usage') or {},
            'provider_request_id': call_result.get('provider_request_id'),
        })
        save(run_dir/'selection_review.json', review)
        return selected, review
    except Exception as exc:
        review.update({'error_type': type(exc).__name__, 'error_detail': str(exc)[:500]})
        save(run_dir/'selection_review.json', review)
        return fallback, review


def run_candidate_tasks(count, workers, callback, on_complete=None):
    """Run independent candidate jobs with a bounded, deterministic result order."""
    if not 1 <= workers <= 2:
        raise ValueError('candidate workers must be 1 or 2')
    if workers == 1:
        results = []
        for index in range(count):
            candidate = callback(index)
            results.append(candidate)
            if on_complete:
                on_complete(candidate)
        return results
    results = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='rq4-candidate') as pool:
        futures = {pool.submit(callback, index): index for index in range(count)}
        for future in as_completed(futures):
            candidate = future.result()
            results.append(candidate)
            if on_complete:
                on_complete(candidate)
    return sorted(results, key=lambda item: item['candidate_index'])


def generate_candidate(index, protocol, run_dir, repair_messages, token_field,
                       thinking_mode, streaming, editable, contexts, repair_plan,
                       method):
    """Generate, parse, validate, and durably save one independent candidate."""
    temperature = protocol['greedy_temperature'] if index == 0 else protocol['sampling_temperature']
    top_p = protocol['greedy_top_p'] if index == 0 else protocol['sampling_top_p']
    max_tokens = protocol['greedy_max_output_tokens'] if index == 0 else protocol['sampling_max_output_tokens']
    call_dir = run_dir/'paid_calls'/f'repair_{index:02d}'
    try:
        call_result, response = api_call(
            call_dir, repair_messages, temperature, max_tokens, top_p,
            token_field, thinking_mode, streaming,
        )
    except (PaidCallUnavailable, IncompleteStreamingResponse,
            urllib.error.URLError, http.client.IncompleteRead,
            TimeoutError, ConnectionError,
            json.JSONDecodeError) as exc:
        candidate = failed_request_candidate(index, temperature, top_p, exc, call_dir)
        save(run_dir/'candidates'/f'{index:02d}.json', candidate)
        print(f'[repair-candidate] method={method} '
              f'candidate={index+1}/{protocol["candidates_per_instance"]} '
              f'temperature={temperature} top_p={top_p} '
              f'status=failed failure_stage=api_request '
              f'error_type={type(exc).__name__} patch_bytes=0', flush=True)
        return candidate
    response_file = call_dir/'response.json'
    candidate = {'candidate_index': index, 'temperature': temperature, 'top_p': top_p,
        'status': 'failed', 'failure_stage': 'response_parse',
        'model_patch': '', 'usage': call_result['usage'],
        'response_received': True, 'response_file': str(response_file.relative_to(run_dir)),
        'response_parse_ok': False, 'patch_nonempty': False,
        'patch_apply_ok': False, 'scope_ok': None,
        'format_repair_attempted': False,
        'provider_request_id': call_result.get('provider_request_id'),
        'finish_reason': ((response.get('choices') or [{}])[0].get('finish_reason')),
        'response_content_chars': len(str(
            ((response.get('choices') or [{}])[0].get('message') or {}).get('content') or ''))}
    try:
        content = ((response.get('choices') or [{}])[0].get('message') or {}).get('content') or ''

        def request_format_repair(source_content, parse_exc):
            if int(protocol.get('format_repair_attempts', 0)) < 1:
                raise parse_exc
            format_call_dir = run_dir/'paid_calls'/f'repair_{index:02d}_format_01'
            try:
                format_result, format_response = api_call(
                    format_call_dir,
                    format_repair_messages(source_content, parse_exc),
                    0.0, protocol['format_repair_max_output_tokens'], 1.0,
                    token_field, thinking_mode, streaming,
                )
            except (PaidCallUnavailable, IncompleteStreamingResponse,
                    urllib.error.URLError, http.client.IncompleteRead,
                    TimeoutError, ConnectionError, json.JSONDecodeError) as retry_exc:
                candidate.update({'failure_stage': 'format_repair_api',
                    'failure_class': 'FORMAT_REPAIR_API_FAILURE',
                    'format_repair_attempted': True,
                    'format_repair_error': type(retry_exc).__name__})
                raise CandidateOutputError('Bounded format repair request failed',
                    'FORMAT_REPAIR_API_FAILURE', 'format_repair_api') from retry_exc
            retry_message = ((format_response.get('choices') or [{}])[0].get('message') or {})
            retry_content = retry_message.get('content') or ''
            candidate.update({'format_repair_attempted': True,
                'format_repair_response_file': str((format_call_dir/'response.json').relative_to(run_dir)),
                'format_repair_usage': format_result.get('usage') or {},
                'format_repair_finish_reason': (format_response.get('choices') or [{}])[0].get('finish_reason')})
            for usage_key, usage_value in (format_result.get('usage') or {}).items():
                if isinstance(usage_value, (int, float)):
                    candidate['usage'][usage_key] = candidate['usage'].get(usage_key, 0) + usage_value
            if candidate['format_repair_finish_reason'] in {'length', 'content_filter'}:
                raise CandidateOutputError('Format repair completion was incomplete or filtered',
                    'FORMAT_REPAIR_INCOMPLETE', 'format_repair_incomplete')
            return retry_content

        edits, response_format, parsed_from = parse_edits_with_format_retry(
            content, candidate['finish_reason'], request_format_repair)
        candidate['format_repair_attempted'] = parsed_from == 'format_repair_response'
        candidate['parsed_from'] = parsed_from
        candidate['response_parse_ok'] = True
        candidate['response_format'] = response_format
        candidate['failure_stage'] = 'edit_application'
        updated = apply_edits(editable, edits, visible=contexts)
        candidate['patch_apply_ok'] = True
        candidate['scope_ok'] = True
        candidate['failure_stage'] = 'static_validation'
        candidate['validation'] = validate_updated_files(editable, updated)
        candidate['model_patch'] = make_patch(editable, updated)
        candidate['patch_nonempty'] = bool(candidate['model_patch'])
        candidate['status'] = 'generated' if candidate['model_patch'] else 'empty_patch'
        candidate['failure_class'] = None if candidate['model_patch'] else 'NO_VALID_PATCH'
        candidate['coverage_diagnostic'] = assess_patch_coverage(
            repair_plan, candidate['validation']['changed_files'])
        if candidate['model_patch']:
            candidate['normalized_patch_sha256'] = normalized_patch_key(editable, updated)
            candidate['changed_lines'] = changed_line_count(candidate['model_patch'])
        candidate.pop('failure_stage', None)
    except Exception as exc:
        candidate['error_type'] = type(exc).__name__
        candidate['error_detail'] = str(exc)[:500]
        if isinstance(exc, CandidateOutputError):
            candidate['failure_stage'] = exc.failure_stage
            candidate['failure_class'] = exc.failure_class
        elif not candidate.get('failure_class'):
            if candidate.get('failure_stage') == 'response_parse':
                candidate['failure_class'] = 'RESPONSE_PARSE_FAILED'
            elif candidate.get('failure_stage') == 'edit_application':
                candidate['failure_class'] = 'PATCH_NOT_APPLIED'
                candidate['scope_ok'] = False
            elif candidate.get('failure_stage') == 'static_validation':
                candidate['failure_class'] = 'STATIC_VALIDATION_FAILED'
        if isinstance(exc, json.JSONDecodeError):
            candidate['error_location'] = {'line': exc.lineno, 'column': exc.colno}
    save(run_dir/'candidates'/f'{index:02d}.json', candidate)
    print(f'[repair-candidate] method={method} candidate={index+1}/{protocol["candidates_per_instance"]} '
          f'temperature={temperature} top_p={top_p} status={candidate["status"]} '
          f'patch_bytes={len(candidate["model_patch"].encode())}', flush=True)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=['swe', 'omni'], required=True)
    parser.add_argument('--method', choices=['locagent', 'cosil', 'gala', 'graphlocator', 'magnet'], required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--env-file', type=Path, default=ROOT/'.env.local')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--candidate-count', type=int,
                        help='Smoke-only override for paid repair candidates (1..10).')
    parser.add_argument('--candidate-workers', type=int, choices=[1, 2], default=1,
                        help='Maximum concurrent repair candidate calls; fine localization remains serial.')
    args = parser.parse_args()
    load_env(args.env_file)
    protocol = json.loads((VARIANT_ROOT/'protocol_v9.json').read_text())
    if args.candidate_count is not None:
        if not 1 <= args.candidate_count <= protocol['candidates_per_instance']:
            raise SystemExit(f'--candidate-count must be 1..{protocol["candidates_per_instance"]}')
        protocol['candidates_per_instance'] = args.candidate_count
        protocol['smoke_candidate_override'] = args.candidate_count
    token_field = protocol.get('completion_token_field', 'max_tokens')
    thinking_mode = protocol.get('thinking_mode')
    streaming = bool(protocol.get('streaming', False))
    sample = next(r for r in map(json.loads, (ROOT/'data/inputs'/f'{args.dataset}50.jsonl').open())
                  if r['instance_id'] == args.instance_id)
    loc_path = ROOT/'normalized'/args.dataset/f'{args.method}.jsonl'
    loc = next(r for r in map(json.loads, loc_path.open()) if r['instance_id'] == args.instance_id)
    if loc['status'] != 'available' or not loc['found_files']:
        raise SystemExit('Missing or empty file localization prediction')
    repo = args.repo.resolve()
    subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', sample['base_commit']+'^{commit}'], check=True, capture_output=True)
    original, rejected = load_files(repo, sample, loc['found_files'], protocol['file_top_k'])
    if not original:
        raise SystemExit('No readable candidate files')
    run_dir = args.output_dir or ROOT/'runs'/args.dataset/args.method/args.instance_id/str(time.time_ns())
    run_dir.mkdir(parents=True, exist_ok=True)
    evidence = line_evidence(original, loc.get('found_functions', []),
                             protocol['localization_excerpt_radius'], protocol['max_symbol_excerpts_per_file'])
    localization_payload = {'issue': sample['problem_statement'], 'file_localization': list(original),
        'upstream_function_localization': loc.get('found_functions', []), 'evidence': evidence}
    localization_messages = [
        {'role': 'system', 'content': LOCALIZATION_SYSTEM_PROMPT},
        {'role': 'user', 'content': json.dumps(localization_payload, ensure_ascii=False)}]
    save(run_dir/'request.json', {'protocol': protocol, 'base_commit': sample['base_commit'],
        'candidate_workers': args.candidate_workers,
        'candidate_files': list(original), 'upstream_functions': loc.get('found_functions', []),
        'rejected_paths': rejected, 'localization_messages': localization_messages,
        'localization_prompt_sha256': hashlib.sha256(json.dumps(localization_messages, ensure_ascii=False).encode()).hexdigest()})
    if not args.execute:
        selector_calls = 1 if protocol.get('selector_review', False) else 0
        print(f'OFFLINE dry-run passed; planned calls<={1 + protocol["candidates_per_instance"] + selector_calls}; '
              f'candidate_workers={args.candidate_workers}; request={run_dir / "request.json"}')
        return
    if not all(os.getenv(k) for k in ['RQ4_BASE_URL', 'RQ4_API_KEY', 'RQ4_MODEL']):
        raise SystemExit('Configure RQ4_BASE_URL/RQ4_API_KEY/RQ4_MODEL first')

    fine_call_dir = run_dir/'paid_calls'/'fine_localization'
    try:
        fine_result, fine_response = api_call(
            fine_call_dir, localization_messages,
            protocol['localization_temperature'], protocol['localization_max_output_tokens'],
            protocol['localization_top_p'], token_field, thinking_mode, streaming,
        )
    except (PaidCallUnavailable, IncompleteStreamingResponse,
            urllib.error.URLError, http.client.IncompleteRead,
            TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
        fine = {'instance_id': args.instance_id, 'method': args.method,
            'status': 'infrastructure_failure', 'file_top_k': protocol['file_top_k'],
            'candidate_files': list(original),
            'upstream_functions': loc.get('found_functions', []),
            'locations': [], 'repair_plan': None, 'response_format': None,
            'usage': {}, 'provider_request_id': None, 'finish_reason': None,
            'error_type': type(exc).__name__, 'error_detail': str(exc)[:500],
            'retry_safety': 'no_automatic_resend_without_proven_preconnect_failure'}
        save(run_dir/'fine_localization.json', fine)
        result = {'instance_id': args.instance_id,
            'model_name_or_path': f'rq4-{args.method}-{os.environ["RQ4_MODEL"]}',
            'model_patch': '', 'status': 'infrastructure_failure',
            'protocol': protocol['name'], 'selected_candidate': None,
            'candidate_count': 0, 'valid_candidate_count': 0,
            'candidate_outcomes': {'failed:fine_localization_api': 1},
            'infrastructure_candidate_failures': 1,
            'unique_nonempty_patches': 0, 'selection': None,
            'fine_localization_status': fine['status'], 'usage': {}}
        save(run_dir/'prediction.jsonl', result, jsonl=True)
        print(f'infrastructure_failure: fine localization unavailable; '
              f'error_type={type(exc).__name__}; {run_dir / "prediction.jsonl"}', flush=True)
        return
    location_error = None
    try:
        locations, location_format, repair_plan = parse_localization(
            fine_response['choices'][0]['message']['content'], original)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # A completed provider response with empty/malformed localization is a
        # valid failed outcome, not infrastructure failure and not a reason to
        # repeat a paid call. Preserve it and let the remaining methods run.
        locations, location_format, repair_plan = [], 'invalid', None
        location_error = {'error_type': type(exc).__name__}
        if isinstance(exc, json.JSONDecodeError):
            location_error['error_location'] = {'line': exc.lineno, 'column': exc.colno}
    fine = {'instance_id': args.instance_id, 'method': args.method,
        'status': 'localized' if locations else ('failed_localization' if location_error else 'empty_localization'),
        'file_top_k': protocol['file_top_k'],
        'candidate_files': list(original), 'upstream_functions': loc.get('found_functions', []),
        'locations': locations, 'repair_plan': repair_plan,
        'response_format': location_format, 'usage': fine_result['usage'],
        'provider_request_id': fine_result.get('provider_request_id'),
        'finish_reason': ((fine_response.get('choices') or [{}])[0].get('finish_reason'))}
    if location_error:
        fine.update(location_error)
    save(run_dir/'fine_localization.json', fine)
    print(f'[fine-localization] method={args.method} status={fine["status"]} '
          f'files={len(original)} functions={len(loc.get("found_functions", []))} '
          f'locations={len(locations)}', flush=True)
    contexts, context_alignment = aligned_context(
        original, locations, repair_plan, protocol['context_window'],
        protocol['max_code_utf8_bytes'])
    context_bytes = context_alignment['context_bytes']
    if context_bytes > protocol['max_code_utf8_bytes']:
        raise RuntimeError('Localized context exceeds frozen byte budget; no repair calls made')

    candidates = []
    if contexts:
        repair_messages = [
            {'role': 'system', 'content': REPAIR_SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({'issue': sample['problem_statement'],
                'file_localization': list(original), 'function_and_line_localization': locations,
                'repair_plan': repair_plan, 'localized_code': contexts}, ensure_ascii=False)}]
        editable = {path: original[path] for path in contexts}
        save(run_dir/'repair_request.json', {'messages': repair_messages, 'context_bytes': context_bytes,
                                             'candidate_workers': args.candidate_workers,
                                             'locations': locations, 'repair_plan': repair_plan,
                                             'context_alignment': context_alignment,
                                             'files': editable})
        completed_indices = []
        def record_completion(candidate):
            completed_indices.append(candidate['candidate_index'])
            save(run_dir/'candidate_progress.json', {
                'candidate_workers': args.candidate_workers,
                'candidate_count': protocol['candidates_per_instance'],
                'completed_indices': sorted(completed_indices),
            })
        candidates = run_candidate_tasks(
            protocol['candidates_per_instance'], args.candidate_workers,
            lambda index: generate_candidate(
                index, protocol, run_dir, repair_messages, token_field,
                thinking_mode, streaming, editable, contexts, repair_plan,
                args.method,
            ),
            on_complete=record_completion,
        )
    selected, selection_review = review_candidate_selection(
        protocol, run_dir, sample['problem_statement'], repair_plan, candidates,
        token_field, thinking_mode, streaming)
    candidate_outcomes = {}
    for candidate in candidates:
        outcome = candidate['status']
        if outcome == 'failed':
            outcome += ':' + candidate.get('failure_stage', 'unknown')
        candidate_outcomes[outcome] = candidate_outcomes.get(outcome, 0) + 1
    usage = {}
    for item in [fine, *candidates, selection_review or {}]:
        for key, value in (item.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    infrastructure_failures = sum(
        x.get('failure_stage') == 'api_request' for x in candidates
    )
    result_status = (
        'generated' if selected else
        'infrastructure_failure' if infrastructure_failures else
        'empty_patch'
    )
    result = {'instance_id': args.instance_id,
        'model_name_or_path': f'rq4-{args.method}-{os.environ["RQ4_MODEL"]}',
        'model_patch': selected['model_patch'] if selected else '',
        'status': result_status,
        'protocol': protocol['name'],
        'candidate_workers': args.candidate_workers,
        'selected_candidate': selected['candidate_index'] if selected else None,
        'candidate_count': len(candidates),
        'valid_candidate_count': sum(x.get('validation', {}).get('status') == 'passed' for x in candidates),
        'candidate_outcomes': candidate_outcomes,
        'infrastructure_candidate_failures': infrastructure_failures,
        'unique_nonempty_patches': len({x.get('normalized_patch_sha256')
            for x in candidates if x.get('normalized_patch_sha256')}),
        'selection': ({'normalized_patch_sha256': selected.get('normalized_patch_sha256'),
                       'vote_count': sum(x.get('normalized_patch_sha256') == selected.get('normalized_patch_sha256')
                                         for x in candidates),
                       'changed_lines': selected.get('changed_lines'),
                       'temperature': selected.get('temperature'),
                       'top_p': selected.get('top_p'),
                       'response_format': selected.get('response_format'),
                       'review_status': (selection_review or {}).get('status'),
                       'review_reason': (selection_review or {}).get('reason')}
                      if selected else None),
        'fine_localization_status': fine['status'], 'usage': usage}
    save(run_dir/'prediction.jsonl', result, jsonl=True)
    print(f'{result["status"]}: selected={result["selected_candidate"]}; candidates={len(candidates)}; {run_dir / "prediction.jsonl"}')


if __name__ == '__main__':
    main()
