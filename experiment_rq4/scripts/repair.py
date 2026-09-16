#!/usr/bin/env python3
"""CoSIL-RQ3-aligned fine localization and multi-candidate repair runner.

Each sample/method gets one function/line localization call, followed by one
greedy and K-1 stochastic repair calls. Every paid call has an independent
durable attempt record. Only explicit transient HTTP responses receive bounded
retries; uncertain transport failures are never retried automatically.
"""
import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import urllib.error
import urllib.request
from preflight import load_env

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUEST_TIMEOUT = 900
SEARCH_MARKER = '<' * 7 + ' SEARCH'
DIVIDER_MARKER = '=' * 7
REPLACE_MARKER = '>' * 7 + ' REPLACE'


class PaidCallUnavailable(RuntimeError):
    """A durable paid-call directory exists without a reusable response."""

LOCALIZATION_SYSTEM_PROMPT = """You are the fine-grained localization stage of a software repair system.
Identify the smallest defensible function and line intervals that must be inspected or edited to solve the issue.

Return exactly one valid JSON object with this schema:
{"locations":[{"path":"relative/path.ext","function":"symbol or scope name","start_line":1,"end_line":2}]}

Rules:
- Use only file paths supplied in file_localization.
- Line numbers are 1-based, inclusive, and must exist in the supplied evidence.
- Prefer narrow intervals that contain the likely fault and enough surrounding context to edit it.
- Use upstream function evidence when it is relevant, but do not invent symbols or paths.
- Do not return prose, analysis, comments, Markdown, or code fences.
- Use double-quoted JSON strings and escape embedded newlines and quotes correctly.
- Never return an empty response. If no defensible location exists, return {"locations":[]}.
- Before answering, verify that the output is syntactically valid JSON and contains only the locations key."""

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
- Multiple edits and multiple files are allowed when the supplied localized code supports them.
- Brief reasoning may precede the edits, but do not place commentary inside an edit block.
- Finish with the edit blocks. Do not append prose after the final block.
- If no defensible edit can be made, return exactly NO_VALID_EDIT.
- Before answering, verify every path, SEARCH block, and replacement for exact applicability."""


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
    response_format = 'json'
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        blocks = re.findall(r'```(?:json)?[ \t]*\r?\n?(.*?)```', content,
                            flags=re.DOTALL | re.IGNORECASE)
        if len(blocks) != 1:
            raise
        payload = json.loads(blocks[0].strip())
        response_format = 'single_json_fence'
    if not isinstance(payload, dict):
        raise ValueError('Model response must be a JSON object')
    return payload, response_format


def parse_model_edits(content):
    """Parse CoSIL SEARCH/REPLACE output, retaining JSON as a compatibility fallback."""
    if not isinstance(content, str):
        raise TypeError('Model content must be a string')
    if content.strip() == 'NO_VALID_EDIT':
        return [], 'no_valid_edit'
    try:
        payload, response_format = parse_json_object(content)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if payload is not None:
        if set(payload) != {'edits'} or not isinstance(payload['edits'], list):
            raise ValueError('Model JSON must contain only the edits list')
        return payload['edits'], response_format

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
        raise ValueError('No valid SEARCH/REPLACE edits found')
    marker_count = content.count(SEARCH_MARKER)
    if marker_count != len(edits):
        raise ValueError('One or more SEARCH/REPLACE blocks are malformed')
    return edits, 'cosil_search_replace'


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


def parse_locations(content, files):
    payload, response_format = parse_json_object(content)
    if set(payload) != {'locations'} or not isinstance(payload['locations'], list):
        raise ValueError('Localization JSON must contain only the locations list')
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
    return result, response_format


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


def api_call(call_dir, messages, temperature, max_tokens, top_p=1.0):
    """Execute a durable request, retrying only explicit transient HTTP failures."""
    result_path, response_path = call_dir/'result.json', call_dir/'response.json'
    if result_path.exists() and response_path.exists():
        return json.loads(result_path.read_text()), json.loads(response_path.read_text())
    if call_dir.exists():
        raise PaidCallUnavailable(
            f'Uncertain or failed paid request at {call_dir}; the original request is not resent'
        )
    call_dir.mkdir(parents=True)
    body = {'model': os.environ['RQ4_MODEL'], 'messages': messages,
            'temperature': temperature, 'top_p': top_p,
            'max_tokens': max_tokens}
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
        'max_tokens': max_tokens, 'started_at': time.time(),
        'http_retry_limit': http_retries, 'http_failures': []}
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
                data = json.load(response)
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
    """Filter validated patches, normalize them, then use deterministic voting."""
    nonempty = [x for x in candidates if x['status'] == 'generated' and x['model_patch']
                and x.get('validation', {}).get('status') == 'passed']
    if not nonempty:
        return None
    groups = {}
    for candidate in nonempty:
        digest = candidate.get('normalized_patch_sha256') or hashlib.sha256(
            candidate['model_patch'].encode()).hexdigest()
        groups.setdefault(digest, []).append(candidate)
    winning = min(groups.values(), key=lambda group: (
        -len(group), min(x.get('changed_lines', float('inf')) for x in group),
        min(x['candidate_index'] for x in group)))
    return min(winning, key=lambda x: (x.get('changed_lines', float('inf')),
                                       x['candidate_index']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=['swe', 'omni'], required=True)
    parser.add_argument('--method', choices=['locagent', 'cosil', 'gala', 'graphlocator', 'magnet'], required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--env-file', type=Path, default=ROOT/'.env.local')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    load_env(args.env_file)
    protocol = json.loads((ROOT/'configs/protocol.json').read_text())
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
        'candidate_files': list(original), 'upstream_functions': loc.get('found_functions', []),
        'rejected_paths': rejected, 'localization_messages': localization_messages,
        'localization_prompt_sha256': hashlib.sha256(json.dumps(localization_messages, ensure_ascii=False).encode()).hexdigest()})
    if not args.execute:
        print(f'OFFLINE dry-run passed; planned calls={1 + protocol["candidates_per_instance"]}; request={run_dir / "request.json"}')
        return
    if not all(os.getenv(k) for k in ['RQ4_BASE_URL', 'RQ4_API_KEY', 'RQ4_MODEL']):
        raise SystemExit('Configure RQ4_BASE_URL/RQ4_API_KEY/RQ4_MODEL first')

    fine_result, fine_response = api_call(run_dir/'paid_calls'/'fine_localization', localization_messages,
        protocol['localization_temperature'], protocol['localization_max_output_tokens'],
        protocol['localization_top_p'])
    location_error = None
    try:
        locations, location_format = parse_locations(
            fine_response['choices'][0]['message']['content'], original)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # A completed provider response with empty/malformed localization is a
        # valid failed outcome, not infrastructure failure and not a reason to
        # repeat a paid call. Preserve it and let the remaining methods run.
        locations, location_format = [], 'invalid'
        location_error = {'error_type': type(exc).__name__}
        if isinstance(exc, json.JSONDecodeError):
            location_error['error_location'] = {'line': exc.lineno, 'column': exc.colno}
    fine = {'instance_id': args.instance_id, 'method': args.method,
        'status': 'localized' if locations else ('failed_localization' if location_error else 'empty_localization'),
        'file_top_k': protocol['file_top_k'],
        'candidate_files': list(original), 'upstream_functions': loc.get('found_functions', []),
        'locations': locations, 'response_format': location_format, 'usage': fine_result['usage'],
        'provider_request_id': fine_result.get('provider_request_id'),
        'finish_reason': ((fine_response.get('choices') or [{}])[0].get('finish_reason'))}
    if location_error:
        fine.update(location_error)
    save(run_dir/'fine_localization.json', fine)
    print(f'[fine-localization] method={args.method} status={fine["status"]} '
          f'files={len(original)} functions={len(loc.get("found_functions", []))} '
          f'locations={len(locations)}', flush=True)
    contexts = localized_context(original, locations, protocol['context_window'])
    context_bytes = sum(len(x.encode()) for x in contexts.values())
    if context_bytes > protocol['max_code_utf8_bytes']:
        raise RuntimeError('Localized context exceeds frozen byte budget; no repair calls made')

    candidates = []
    if contexts:
        repair_messages = [
            {'role': 'system', 'content': REPAIR_SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({'issue': sample['problem_statement'],
                'file_localization': list(original), 'function_and_line_localization': locations,
                'localized_code': contexts}, ensure_ascii=False)}]
        editable = {path: original[path] for path in contexts}
        save(run_dir/'repair_request.json', {'messages': repair_messages, 'context_bytes': context_bytes,
                                             'locations': locations, 'files': editable})
        for index in range(protocol['candidates_per_instance']):
            temperature = protocol['greedy_temperature'] if index == 0 else protocol['sampling_temperature']
            top_p = protocol['greedy_top_p'] if index == 0 else protocol['sampling_top_p']
            max_tokens = protocol['greedy_max_output_tokens'] if index == 0 else protocol['sampling_max_output_tokens']
            call_dir = run_dir/'paid_calls'/f'repair_{index:02d}'
            try:
                call_result, response = api_call(
                    call_dir, repair_messages, temperature, max_tokens, top_p,
                )
            except (PaidCallUnavailable, urllib.error.URLError, TimeoutError,
                    ConnectionError, json.JSONDecodeError) as exc:
                candidate = failed_request_candidate(index, temperature, top_p, exc, call_dir)
                save(run_dir/'candidates'/f'{index:02d}.json', candidate)
                candidates.append(candidate)
                print(f'[repair-candidate] method={args.method} '
                      f'candidate={index+1}/{protocol["candidates_per_instance"]} '
                      f'temperature={temperature} top_p={top_p} '
                      f'status=failed failure_stage=api_request '
                      f'error_type={type(exc).__name__} patch_bytes=0', flush=True)
                continue
            candidate = {'candidate_index': index, 'temperature': temperature, 'top_p': top_p,
                'status': 'failed', 'failure_stage': 'response_parse',
                'model_patch': '', 'usage': call_result['usage'],
                'provider_request_id': call_result.get('provider_request_id'),
                'finish_reason': ((response.get('choices') or [{}])[0].get('finish_reason')),
                'response_content_chars': len(str(
                    ((response.get('choices') or [{}])[0].get('message') or {}).get('content') or ''))}
            try:
                edits, response_format = parse_model_edits(response['choices'][0]['message']['content'])
                candidate['failure_stage'] = 'edit_application'
                updated = apply_edits(editable, edits, visible=contexts)
                candidate['failure_stage'] = 'static_validation'
                candidate['validation'] = validate_updated_files(editable, updated)
                candidate['model_patch'] = make_patch(editable, updated)
                candidate['status'] = 'generated' if candidate['model_patch'] else 'empty_patch'
                candidate['response_format'] = response_format
                if candidate['model_patch']:
                    candidate['normalized_patch_sha256'] = normalized_patch_key(editable, updated)
                    candidate['changed_lines'] = changed_line_count(candidate['model_patch'])
                candidate.pop('failure_stage', None)
            except Exception as exc:
                candidate['error_type'] = type(exc).__name__
                candidate['error_detail'] = str(exc)[:500]
                if isinstance(exc, json.JSONDecodeError):
                    candidate['error_location'] = {'line': exc.lineno, 'column': exc.colno}
            save(run_dir/'candidates'/f'{index:02d}.json', candidate)
            candidates.append(candidate)
            print(f'[repair-candidate] method={args.method} candidate={index+1}/{protocol["candidates_per_instance"]} '
                  f'temperature={temperature} top_p={top_p} status={candidate["status"]} '
                  f'patch_bytes={len(candidate["model_patch"].encode())}', flush=True)
    selected = choose_candidate(candidates)
    candidate_outcomes = {}
    for candidate in candidates:
        outcome = candidate['status']
        if outcome == 'failed':
            outcome += ':' + candidate.get('failure_stage', 'unknown')
        candidate_outcomes[outcome] = candidate_outcomes.get(outcome, 0) + 1
    usage = {}
    for item in [fine, *candidates]:
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
                       'response_format': selected.get('response_format')}
                      if selected else None),
        'fine_localization_status': fine['status'], 'usage': usage}
    save(run_dir/'prediction.jsonl', result, jsonl=True)
    print(f'{result["status"]}: selected={result["selected_candidate"]}; candidates={len(candidates)}; {run_dir / "prediction.jsonl"}')


if __name__ == '__main__':
    main()
