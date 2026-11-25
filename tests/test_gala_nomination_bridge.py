from types import SimpleNamespace
import pytest

from mycode.dynamic_retrieval.nomination_bridge import investigation_order, align_read_entities
from mycode.dynamic_retrieval.search_agent import RankedLocation, _read_code_context
from mycode.dynamic_retrieval.seed_frontier import restore_seed_frontier


def test_low_ranked_nomination_gets_read_and_review_slot_without_score_bonus():
    ranked = [RankedLocation(f'src/{i}.js', 100 - i) for i in range(15)]
    seed = ranked[12]
    files = {item.path: 'function draw() { return borderWidth; }' for item in ranked}
    ordered = investigation_order(ranked, [seed], files=files)
    contexts = _read_code_context(SimpleNamespace(files=files), ordered, ['borderWidth'], limit=6)
    assert ordered[0].path == contexts[0]['path'] == seed.path
    assert contexts[0]['snippets']
    assert ordered[0].score == seed.score
    assert [item.path for item in ranked] == [f'src/{i}.js' for i in range(15)]
    assert len({item.path for item in ordered[:6]}) == 6


def test_missing_candidate_can_be_investigated_but_outside_index_cannot():
    head, nominee = RankedLocation('a', 100), RankedLocation('b', 1)
    ordered = investigation_order([head], [nominee, nominee, RankedLocation('../outside', 0)],
                                  files={'a': 'source', 'b': 'source'})
    assert [item.path for item in ordered] == ['b', 'a']
    assert nominee.belief == {}  # investigation is not source verification


def test_source_windows_use_overlapping_entity_not_unrelated_query_hit():
    context = {'path': 'a', 'snippets': [{'start_line': 20, 'end_line': 25}],
               'entities': [{'name': 'mapDispatchToProps'}]}
    entities = [SimpleNamespace(path='a', name='mapDispatchToProps', kind='function', start_line=80, end_line=90),
                SimpleNamespace(path='a', name='renderField', kind='function', start_line=15, end_line=30)]
    aligned = align_read_entities([context], entities)
    assert [e['name'] for e in aligned[0]['entities']] == ['renderField']


def test_no_function_is_fabricated_for_style_or_declaration_window():
    context = {'path': 'a.scss', 'snippets': [{'start_line': 1, 'end_line': 5}], 'entities': []}
    assert align_read_entities([context], [])[0]['entities'] == []


def test_missing_evidence_on_plausible_seed_is_not_counterevidence_in_guard_mode():
    owner, helper = RankedLocation('owner', 20), RankedLocation('helper', 10)
    review = {'candidates': [dict(path='owner', grounded=True, role='supporting_target',
                                counterevidence=['The specific assignment has not been supplied.'])]}
    kw = dict(index=SimpleNamespace(files={'owner': 'source', 'helper': 'source'}),
              review=review, top_k=2, limit=1)
    old, _ = restore_seed_frontier([owner, helper], [owner, helper], **kw)
    new, report = restore_seed_frontier([owner, helper], [owner, helper], protect_verified_head=True, **kw)
    assert old[0].path == 'helper'
    assert new[0].path == 'owner'
    assert report['rejected'] == []


def test_explicit_negative_verdict_can_still_exclude_seed():
    owner, helper = RankedLocation('owner', 20), RankedLocation('helper', 10)
    ranked, report = restore_seed_frontier([owner, helper], [owner, helper],
        index=SimpleNamespace(files={'owner': 'source', 'helper': 'source'}),
        review={'candidates': [dict(path='owner', grounded=True, role='unlikely',
                                  counterevidence=['The observed code only configures transport.'])]},
        top_k=2, limit=1, protect_verified_head=True)
    assert ranked[0].path == 'helper'
    assert report['rejected'][0]['reason'] == 'grounded_counterevidence'


@pytest.mark.parametrize('reservoir', [False, True])
def test_additive_batch_preserves_primary_review_for_function_ranking(tmp_path, monkeypatch, reservoir):
    from mycode.dynamic_retrieval import search_agent
    from mycode.repo_index.structure_index import RepositoryIndex
    from mycode.schemas.evidence import NormalizedSample

    for n in range(8):
        (tmp_path / f'main{n}.py').write_text(f'def broken_{n}(value):\n    return value\n')
    (tmp_path / 'nominee.py').write_text('def consumer(value):\n    return broken_behavior(value)\n')
    index = RepositoryIndex(repo='unit', instance_id='unit', dataset='unit', repo_root=tmp_path)
    assert any(row.name == 'consumer' for row in index.entities)
    monkeypatch.setenv('MYCODE_GALA_VERIFY_NOMINATIONS', '1')
    monkeypatch.setenv('MYCODE_GALA_ADDITIVE_REVIEW', '1')
    monkeypatch.setenv('MYCODE_GALA_FUNCTION_RESERVOIR', '1' if reservoir else '0')
    monkeypatch.setenv('MYCODE_JOINT_EVIDENCE_V2', '1')
    monkeypatch.setenv('MYCODE_AGENT_FOLLOWUP_MODE', 'off')
    monkeypatch.setattr(search_agent, 'plan_fast_seeds', lambda **kw: dict(
        seed_files=['nominee.py'], persistent_seed_files=['nominee.py'],
        global_file_selector={'candidate_files': ['nominee.py']}, evidence={}))
    monkeypatch.setattr(search_agent, 'run_react_tool_agent', lambda **kw: {})
    batches = []

    def review(**kw):
        result = dict(status='ok', candidates=[], unit_batch=len(batches),
                      continue_search=True, next_queries=['inspect concrete operation'],
                      missing_evidence=['operation window missing'])
        batches.append(kw)
        return result

    monkeypatch.setattr(search_agent, 'review_candidates', review)
    original = search_agent._rank_entities
    function_batches = []

    def rank_entities(**kw):
        function_batches.append(kw['candidate_review']['unit_batch'])
        return original(**kw)

    monkeypatch.setattr(search_agent, '_rank_entities', rank_entities)
    original_read = search_agent._read_code_context
    def read_with_concrete_request(index, ranked, queries, **kw):
        return original_read(index, ranked, list(queries) + ['broken_behavior'], **kw)
    monkeypatch.setattr(search_agent, '_read_code_context', read_with_concrete_request)
    result = search_agent.dynamic_localize(
        NormalizedSample(instance_id='unit', repo='unit', dataset='unit',
                         issue_text='broken consumer behavior', raw={}), {}, index=index,
        top_k=8, max_rounds=1, max_react_steps=1, controller_llm=lambda prompt: '{}')
    assert len(batches) == 2
    assert function_batches == ([0, 1] if reservoir else [0])
    if reservoir:
        extra = result['dynamic_rounds'][0]['verifier']['supplemental_function_candidates']
        assert extra and all(isinstance(row, dict) and row.get('id') for row in extra)
        ledger = result['best_round_selection']['joint_evidence_v2']['functions']['source_ledger']
        assert {row['id'] for row in extra} <= {row['id'] for row in ledger}
    bridge = result['dynamic_rounds'][0]['verifier']['nomination_bridge']
    assert bridge['primary_review']['unit_batch'] == 0
    assert bridge['supplemental_review']['unit_batch'] == 1
    assert 'nominee.py' in {row.path for row in batches[1]['ranked']}
    merged = result['dynamic_rounds'][0]['verifier']['llm_candidate_review']
    assert merged['continue_search']
    assert 'inspect concrete operation' in merged['next_queries']
    assert 'operation window missing' in merged['missing_evidence']
