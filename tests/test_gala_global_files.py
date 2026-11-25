from types import SimpleNamespace

from mycode.dynamic_retrieval.fast_seed_planner import plan_fast_seeds
from mycode.dynamic_retrieval.global_file_selector import nominate_global_files, visual_observations


def test_global_discovers_file_excluded_from_lexical_shortlist(monkeypatch):
    monkeypatch.setenv('MYCODE_FAST_SEED_PLANNER', '1')
    monkeypatch.setenv('MYCODE_GALA_GLOBAL_FILES', '1')
    paths = [f'src/noise{i}.js' for i in range(12)] + ['src/render/arc.ts']
    index = SimpleNamespace(files={p: 'export const x = 1;' for p in paths}, entities=[],
        search_files=lambda *a, **kw: [SimpleNamespace(path=p, reasons=[]) for p in paths[:10]])
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        if 'Repository files, grouped' in prompt:
            assert 'arc.ts' in prompt
            return {'files': ['invented.js', 'src/render/arc.ts']}
        return {'seed_files': [{'path': 'src/render/arc.ts', 'role': 'implementation',
                               'evidence_channels': ['source_verified', 'global_semantic_prior']}]}

    result = plan_fast_seeds(index=index, issue_text='hover draws an unexpected border',
        query_groups={'concern': ['hover border']}, evidence_result={}, controller_llm=model)
    assert result['seed_files'][0] == 'src/render/arc.ts'
    assert len(result['candidate_files']) == 11  # all 10 old candidates survive
    assert result['evidence']['src/render/arc.ts']['channels'] == ['global_semantic_prior']
    assert not result['global_file_selector']['source_verified']
    assert len(prompts) == 2
    assert 'invented.js' not in result['candidate_files']


def test_off_makes_no_global_call_and_preserves_candidates(monkeypatch):
    monkeypatch.setenv('MYCODE_FAST_SEED_PLANNER', '1')
    monkeypatch.setenv('MYCODE_GALA_GLOBAL_FILES', '0')
    index = SimpleNamespace(files={'src/a.js': 'x', 'src/b.js': 'x'}, entities=[],
        search_files=lambda *a, **kw: [SimpleNamespace(path='src/a.js', reasons=[])])
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return {'seed_files': ['src/a.js']}

    result = plan_fast_seeds(index=index, issue_text='a', query_groups={'symbol': ['a']},
                             evidence_result={}, controller_llm=model)
    assert result['candidate_files'] == ['src/a.js']
    assert result['seed_files'] == ['src/a.js']
    assert len(prompts) == 1
    assert not result['global_file_selector']['enabled']


def test_directory_expansion_reaches_nonlexical_branch_without_inventing_paths():
    paths = [f'alpha/sub{i}/longfilename_{j}.js' for i in range(5) for j in range(10)]
    paths += ['omega/target.ts']
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        if 'Repository directories:' in prompt:
            assert 'omega/' in prompt
            return {'directories': ['../outside', 'omega', 'omega']}
        return {'files': ['omega/target.ts', 'alpha/not_offered.js']}

    result = nominate_global_files(paths=paths, issue_text='x', evidence={},
                                    controller=model, tree_chars=250)
    assert result['candidate_files'] == ['omega/target.ts']
    assert len(prompts) == 2
    assert result['trace'][0]['offered_file_count'] == 51


def test_parse_and_network_failure_are_not_negative_source_evidence():
    for model in [lambda p: 'not JSON', lambda p: {'files': None}]:
        result = nominate_global_files(paths=['src/a.js'], issue_text='x', evidence={}, controller=model)
        assert result['status'] == 'invalid_or_empty'
        assert result['candidate_files'] == []
        assert not result['source_verified']

    def fail(prompt):
        raise TimeoutError('private endpoint details')

    result = nominate_global_files(paths=['src/a.js'], issue_text='x', evidence={}, controller=fail)
    assert result['status'] == 'request_failed'
    assert 'private endpoint' not in str(result)


def test_vlm_whitelist_handles_real_nested_schema_and_excludes_evaluation():
    evidence = {'gold_files': ['secret.py'], 'tool_observations': [
        {'tool': 'evaluation', 'extracted': {'visible_text': ['secret.py']}},
        {'tool': 'vlm_image_inspector', 'extracted': {'vlm_status': 'ok',
            'gold_files': ['secret.py'], 'vlm_analysis': {'visible_text': ['border artifact'],
                'visual_ir': {'nodes': [{'id': 'a', 'text': 'stray line'}]}, 'gold_files': ['secret.py']}}},
        {'tool': 'vlm_image_inspector', 'status': 'error', 'extracted': {'visible_text': ['failed']}}
    ]}
    summary = visual_observations(evidence)
    assert 'border artifact' in summary and 'stray line' in summary
    assert 'secret.py' not in summary and 'failed' not in summary


def test_directory_budget_exhaustion_is_explicit_and_does_not_truncate_alphabetically():
    result = nominate_global_files(paths=[f'd{i}/x.js' for i in range(100)], issue_text='x', evidence={},
        controller=lambda prompt: (_ for _ in ()).throw(AssertionError('must not call')),
        tree_chars=20)
    assert result['status'] == 'directory_menu_exceeds_budget'
    assert result['candidate_files'] == []
    assert result['index_file_count'] == 100


def test_numeric_directory_ids_are_exact_and_reject_out_of_range_choices():
    paths = [f'alpha/sub{i}/longfilename_{j}.js' for i in range(5) for j in range(10)] + ['omega/target.ts']

    def model(prompt):
        if 'Repository directories:' in prompt:
            target_line = next(line for line in prompt.splitlines() if ': omega/' in line)
            number = int(target_line.split(':')[0])
            return {'directory_ids': [9999, -1, True, number, number]}
        return {'files': ['omega/target.ts']}

    result = nominate_global_files(paths=paths, issue_text='x', evidence={}, controller=model, tree_chars=250)
    assert result['candidate_files'] == ['omega/target.ts']
    assert result['trace'][0]['selected_directories'] == ['omega']


def test_global_error_uses_the_original_seed_selection_prompt(monkeypatch):
    monkeypatch.setenv('MYCODE_FAST_SEED_PLANNER', '1')
    monkeypatch.setenv('MYCODE_GALA_GLOBAL_FILES', '1')
    index = SimpleNamespace(files={'src/a.js': 'x'}, entities=[],
        search_files=lambda *a, **kw: [SimpleNamespace(path='src/a.js', reasons=[])])
    prompts = []

    def model(prompt):
        if 'Repository files, grouped' in prompt:
            raise TimeoutError()
        prompts.append(prompt)
        return {'seed_files': ['src/a.js']}

    result = plan_fast_seeds(index=index, issue_text='a', query_groups={'symbol': ['a']},
                             evidence_result={}, controller_llm=model)
    monkeypatch.setenv('MYCODE_GALA_GLOBAL_FILES', '0')
    original = plan_fast_seeds(index=index, issue_text='a', query_groups={'symbol': ['a']},
                               evidence_result={}, controller_llm=model)
    assert result['seed_files'] == original['seed_files']
    assert prompts[0] == prompts[1]
    assert result['global_file_selector']['status'] == 'request_failed'
