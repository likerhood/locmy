#!/usr/bin/env python3
"""Thin wrapper around the locked official SWE-bench run_instances API."""
import argparse
import json
from pathlib import Path


def run_with_git_mode(container, command, timeout, original, *, workdir, user):
    """Ignore image-layer permission changes before the unchanged official eval.sh."""
    if command == '/bin/bash /eval.sh':
        result = container.exec_run(
            ['git', 'config', '--local', 'core.filemode', 'false'],
            workdir=workdir, user=user,
        )
        if result.exit_code:
            detail = result.output.decode('utf-8', errors='replace')[:500].strip()
            raise RuntimeError(
                'Cannot normalize Git file-mode tracking in the test container. '
                f'Inspect the image baseline. {detail}'
            )
        print('[rq4] Git file-mode tracking disabled in test container; '
              'official eval.sh unchanged.', flush=True)
    return original(container, command, timeout)


def parse_calypso_without_stray_brace(log, test_spec, original):
    """Normalize stray Calypso suite prefixes only with complete unique coverage.

    The official parser treats any indented line before Jest's results as a suite
    name.  Shell tracing can therefore prepend ``}`` (or other wrapper output) to
    otherwise valid test IDs, and can displace the outer Jest suite name.  Match
    on the longest complete ``" - "``-delimited suffix from the right.  This
    normally retains ``#function`` plus description; a description-only match is
    accepted only when it is uniquely best.  Refuse to change anything unless
    every expected ID has exactly one distinct source key.
    """
    parsed = original(log, test_spec)
    expected = set(test_spec.FAIL_TO_PASS) | set(test_spec.PASS_TO_PASS)
    if not parsed or not expected or expected.issubset(parsed):
        return parsed

    # A deleted line printed before Jest output can make the official parser
    # treat a lone ``-`` as the outer suite name.  Prefer this exact,
    # one-to-one repair before suffix matching: repeated descriptions such as
    # ``should render`` are still unambiguous when the full expected test ID is
    # retained after removing the literal prefix.
    dash_sources = {}
    for test_id in expected:
        source = test_id if test_id in parsed else f'- {test_id}'
        if source not in parsed:
            break
        dash_sources[test_id] = source
    changed = sum(source != test_id for test_id, source in dash_sources.items())
    if (changed and len(dash_sources) == len(expected)
            and len(set(dash_sources.values())) == len(expected)):
        corrected = dict(parsed)
        for test_id, source in dash_sources.items():
            if source != test_id:
                corrected.pop(source)
            corrected[test_id] = parsed[source]
        print(f'[rq4] Removed stray dash suite prefix for {changed} Calypso '
              f'test IDs for {test_spec.instance_id}; all {len(expected)} expected IDs matched.',
              flush=True)
        return corrected

    def common_component_suffix(actual, wanted):
        actual_parts = actual.split(' - ')
        wanted_parts = wanted.split(' - ')
        common = 0
        for actual_part, wanted_part in zip(reversed(actual_parts), reversed(wanted_parts)):
            if actual_part != wanted_part:
                break
            common += 1
        return common

    sources = {}
    for test_id in expected:
        scores = {name: common_component_suffix(name, test_id) for name in parsed}
        best = max(scores.values(), default=0)
        matches = [name for name, score in scores.items() if score == best and score > 0]
        if len(matches) != 1:
            return parsed
        sources[test_id] = matches[0]
    if len(set(sources.values())) != len(expected):
        return parsed

    corrected = dict(parsed)
    for test_id, source in sources.items():
        if source != test_id:
            corrected.pop(source, None)
        corrected[test_id] = parsed[source]
    print(f'[rq4] Normalized stray suite prefixes for {len(expected)} Calypso '
          f'test IDs for {test_spec.instance_id}; all {len(expected)} expected IDs matched.',
          flush=True)
    return corrected


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--predictions',type=Path,required=True)
    p.add_argument('--workers',type=int,default=1)
    p.add_argument('--timeout',type=int,default=1800)
    p.add_argument('--run-id',required=True)
    p.add_argument('--no-op',action='store_true')
    args=p.parse_args()
    from swebench.harness import run_evaluation
    from swebench.harness.log_parsers import PARSER_REGISTRY
    if run_evaluation.CONTAINER_USER != 'root':
        raise RuntimeError(
            'The RQ4 evaluator adapter requires the locked SWE-bench root '
            'container user; regenerate and re-audit the evaluator before running.'
        )
    original_calypso = PARSER_REGISTRY['parse_log_calypso']
    def normalized_calypso(log, test_spec):
        return parse_calypso_without_stray_brace(log, test_spec, original_calypso)
    PARSER_REGISTRY['parse_log_calypso'] = normalized_calypso
    original = run_evaluation.exec_run_with_timeout
    def normalized_exec(container, command, timeout):
        return run_with_git_mode(
            container, command, timeout, original,
            workdir=run_evaluation.CONTAINER_WORKDIR,
            user=run_evaluation.CONTAINER_USER,
        )
    run_evaluation.exec_run_with_timeout = normalized_exec
    rows=[json.loads(x) for x in args.dataset.read_text().splitlines() if x]
    predictions={r['instance_id']:r for r in map(json.loads,args.predictions.read_text().splitlines())}
    rows=[r for r in rows if r['instance_id'] in predictions and
          (args.no_op or predictions[r['instance_id']].get('model_patch'))]
    if rows:
        run_evaluation.run_instances(
            predictions, rows, args.workers, args.run_id, args.timeout,
            skip_patch=args.no_op,
        )


if __name__=='__main__':
    main()
