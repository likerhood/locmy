import json
from types import SimpleNamespace

from mycode.dynamic_retrieval.fileloc_coverage import supplement,merge_evidence,listwise_rank
from mycode.dynamic_retrieval.fileloc_policy import challenge_paths
from mycode.dynamic_retrieval.search_agent import RankedLocation
from mycode.dynamic_retrieval.candidate_reviewer import review_candidates,_validate
from mycode.utils.model_profile import ModelProtocolProfile


def test_all_fifteen_are_batched_even_when_old_limit_is_six(monkeypatch):
    monkeypatch.setenv('MYCODE_FILELOC_REVIEW_COVERAGE','1')
    monkeypatch.setenv('MYCODE_FILELOC_OWNER_CHALLENGE','0')
    ranked=[RankedLocation(path=f'src/{i}.py',score=15-i) for i in range(15)]
    contexts=[dict(path=r.path,snippets=[dict(text='def run(): return 1',start_line=1,end_line=1)],
                   entities=[dict(name='run',kind='function',start_line=1,end_line=1)]) for r in ranked]
    calls=[]
    def llm(prompt,**kwargs):
        data=json.loads(prompt.split('INPUT:\n')[-1]) if 'INPUT:\n' in prompt else None
        # Prompt format has a fenced payload; take the concrete paths with regex.
        import re
        paths=list(dict.fromkeys(re.findall(r'"path": "(src/\d+\.py)"',prompt)))
        calls.append(paths)
        return json.dumps(dict(reviews=[dict(path=p,verdict='plausible',confidence=.5,
            snippet_id=None,entity_id=None,flow_id=None,causal_chain='need implementation') for p in paths],
            continue_search=True,next_queries=[]))
    result=review_candidates(llm=llm,issue_text='run wrong',issue_sketch={},ranked=ranked,
        code_contexts=contexts,flow_traces=[],round_no=1,candidate_limit=6,repair_attempts=0,
        model_profile=ModelProtocolProfile(name='mimo'))
    assert len(calls)==3 and all(len(c)==5 for c in calls)
    assert result['reviewed_count']==15 and not result['unreviewed_paths']


def test_group_parse_failure_is_not_refutation(monkeypatch):
    monkeypatch.setenv('MYCODE_FILELOC_REVIEW_COVERAGE','1')
    ranked=[RankedLocation(path=f'{i}.py',score=1) for i in range(7)]
    result=review_candidates(llm=lambda *a,**k:'bad json',issue_text='bug',issue_sketch={},ranked=ranked,
        code_contexts=[],flow_traces=[],round_no=1,repair_attempts=0)
    assert result['candidates']==[]
    assert len(result['unreviewed_paths'])==7 and result['continue_search']


def test_supplement_limits_and_no_repeat():
    index=SimpleNamespace(files={f'{i}.py':'\n'.join(['def run():']+['    value = 1']*100) for i in range(15)},entities=[])
    ranked=[RankedLocation(path=p,score=1) for p in index.files]
    contexts,diag=supplement(index,ranked,[],['unmatched'])
    assert len(contexts)==15 and len(diag['supplemented'])==5
    assert all(s['end_line']-s['start_line']<80 for c in contexts for s in c['snippets'])
    _,next_diag=supplement(index,ranked,contexts,['unmatched'],diag['attempted_signatures'])
    assert set(e['path'] for e in next_diag['supplemented']).isdisjoint(e['path'] for e in diag['supplemented'])


def fact(path,role='supporting_target',**kwargs):
    return dict(path=path,role=role,grounded=True,quote_supported=True,evidence_quote='return value;',**kwargs)


def test_source_memory_survives_omission_and_confidence():
    old=fact('a.py',mechanism_verified=True,confidence=.6)
    evidence,events=merge_evidence([dict(candidates=[old]),dict(candidates=[]),
        dict(candidates=[dict(path='a.py',role='unlikely',confidence=.99)])])
    assert evidence[0]['mechanism_verified']
    evidence,_=merge_evidence([dict(candidates=[old]),dict(candidates=[fact('a.py','unlikely',counterevidence=['source contradicts'])])])
    assert evidence[0]['evidence_state']=='source_refuted'


def test_unrelated_import_does_not_consume_delegate_slot():
    index=SimpleNamespace(files={'caller.js':"import errors from './errors';",'errors.js':'function formatError(message) {}'})
    paths,_=challenge_paths(index,[SimpleNamespace(path='caller.js')],issue_text='`attach_rows` changes Series index')
    assert paths==['caller.js']


def ranked_fixture():
    ranked=[RankedLocation(path=f'{i}.py',score=1) for i in range(3)]
    contexts=[dict(path=r.path,snippets=[dict(text='return value;',start_line=1,end_line=1)]) for r in ranked]
    return ranked,contexts


def test_listwise_permutation_and_proof_tiers():
    ranked,contexts=ranked_fixture()
    reviews=[fact('0.py',mechanism_verified=True),fact('1.py',evidence_state='distinct')]
    def llm(*a,**k):
        return json.dumps(dict(order=[2,1,0],top5=[dict(id=i,reason='comparison',evidence_ids=[f'{i}S0']) for i in [2,1,0]]))
    result,diag=listwise_rank(llm,'bug',ranked,contexts,reviews)
    assert [r.path for r in result]==['0.py','1.py','2.py']
    assert set(diag['after'])==set(diag['before']) and diag['status']=='ok'


def test_listwise_invalid_output_repairs_once_then_falls_back():
    ranked,contexts=ranked_fixture();calls=[]
    def llm(*a,**k):
        calls.append(1);return '{"order":[0,0,99],"top5":[]}'
    result,diag=listwise_rank(llm,'bug',ranked,contexts,[])
    assert result==ranked and diag['status']=='fallback' and len(calls)==2


def test_equal_evidence_preserves_order():
    ranked,contexts=ranked_fixture()
    def llm(*a,**k):
        return json.dumps(dict(order=[2,1,0],top5=[dict(id=i,reason='same',evidence_ids=[f'{i}S0']) for i in [2,1,0]]))
    result,_=listwise_rank(llm,'bug',ranked,contexts,[])
    assert result==ranked


def test_valid_cross_function_requires_actual_call(monkeypatch):
    monkeypatch.setenv('MYCODE_FILELOC_REVIEW_COVERAGE','1')
    monkeypatch.setenv('MYCODE_FILELOC_OWNER_CHALLENGE','1')
    snippets={'S1':dict(text='1: return owner(value);',start_line=1,end_line=1),
              'S2':dict(text='4: function owner(value) { return broken(value); }',start_line=4,end_line=4)}
    kwargs=dict(allowed_paths={'a.js'},grounded_paths={'a.js'},context_text_by_path={'a.js':' '.join(s['text'] for s in snippets.values())},
        direct_flow_paths={'a.js'},snippets_by_path={'a.js':snippets},
        entities_by_path={'a.js':{'E1':dict(name='owner',kind='function',start_line=4,end_line=4)}},
        flow_paths_by_id={'F1':{'a.js'}},profile=ModelProtocolProfile(name='mimo'))
    item=dict(path='a.js',verdict='verified',snippet_id='S1',entity_id='E1',flow_id='F1',
        operation_snippet_id='S2',relation_snippet_id='S1',operation_entity_id='E1',causal_chain='caller -> owner -> faulty effect')
    result=_validate(dict(reviews=[item]),**kwargs)
    assert result['candidates'][0]['cross_function_supported']
    snippets['S1']['text']='1: return unrelated(value);'
    result=_validate(dict(reviews=[item]),**kwargs)
    assert not result['candidates'][0]['cross_function_supported']


def test_flags_enabled_offline_pipeline_keeps_valid_snapshots(monkeypatch):
    from test_react_interprocedural_agent import _chartjs_sample
    from mycode.dynamic_retrieval.search_agent import dynamic_localize
    for flag in ['MYCODE_FILELOC_REVIEW_COVERAGE','MYCODE_FILELOC_LISTWISE_RANK',
                 'MYCODE_FILELOC_READ_RECONCILE','MYCODE_FILELOC_OWNER_CHALLENGE']:
        monkeypatch.setenv(flag,'1')
    tmp,sample,evidence,index=_chartjs_sample()
    try:
        output=dynamic_localize(sample,evidence,index=index,max_rounds=2,max_react_steps=5,top_k=15)
        before=[r['path'] for r in output['rank_stage_snapshots']['fileloc_listwise_before']]
        after=[r['path'] for r in output['rank_stage_snapshots']['fileloc_listwise_after']]
        assert set(before)==set(after) and len(before)==len(after)
        assert output['best_round_selection']['fileloc_listwise']['status']=='skipped'
        assert all(len(r['agent_observation'].get('fileloc_review_coverage',{}).get('supplemented',[]))<=5
                   for r in output['dynamic_rounds'])
    finally:
        tmp.cleanup()
