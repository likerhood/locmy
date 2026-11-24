import json
import re
from types import SimpleNamespace as NS
from mycode.dynamic_retrieval.fileloc_revision import (
    targeted_contexts, calibrate_functions, recover_source_candidates, pairwise_rank, backed,
)


def ent(path, name, start, end, body):
    return NS(path=path, name=name, kind='function', start_line=start, end_line=end, text=body)


def item(path, name='run', score=1, start=1, end=3):
    return NS(path=path, name=name, kind='function', id=path+'::function:'+name,
              start_line=start, end_line=end, score=score, reasons=[])


def proof(path):
    return dict(path=path, grounded=True, quote_supported=True, evidence_quote='return output;',
                role='supporting_target', atomic_entity_quote_aligned=True)


def test_exact_definition_beats_file_header_and_preserves_independent_window():
    lines=['# header']*30+['def attach_rows(result):','    return result.index']+['# end']*3
    p='source.py'; idx=NS(files={p:'\n'.join(lines)},entities=[ent(p,'attach_rows',31,32,'\n'.join(lines[30:32]))])
    old=dict(path=p,snippets=[dict(start_line=1,end_line=1,text='1: # header')])
    contexts,log=targeted_contexts(idx,[NS(path=p)],[old],['attach_rows changes result index'])
    assert contexts[0]['snippets'][0]['start_line']==31
    assert any(s['start_line']==1 for s in contexts[0]['snippets'])
    _,next_log=targeted_contexts(idx,[NS(path=p)],contexts,['attach_rows changes result index'],attempted=log['attempted_signatures'])
    assert not next_log['supplemented']


def test_changed_operation_window_is_not_file_level_duplicate():
    p='source.py';lines=['noise']*150;lines[10]='firstOperation = alpha';lines[110]='secondOperation = beta'
    idx=NS(files={p:'\n'.join(lines)},entities=[])
    cs,log=targeted_contexts(idx,[NS(path=p)],[],['firstOperation'])
    cs,second=targeted_contexts(idx,[NS(path=p)],cs,['secondOperation'],attempted=log['attempted_signatures'])
    assert second['supplemented'] and any('secondOperation' in s['text'] for s in cs[0]['snippets'])


def test_function_calibration_bounds_raw_score_and_long_wrapper():
    small='def attach_rows(result):\n    return result.index'
    wrapper='function wrapper() {\n'+' result index irrelevant noise '*3000+'\n}'
    a=item('source.py','attach_rows',20,1,2); b=item('other.js','wrapper',100000,1,3)
    idx=NS(entities=[ent(a.path,a.name,1,2,small),ent(b.path,b.name,1,3,wrapper)])
    result=calibrate_functions(idx,[b,a],[NS(path='source.py')],'`attach_rows` should preserve result index')
    assert result[0].path=='source.py'
    assert all(x.score<100 for x in result) and b.score==100000


def test_continuity_only_source_backed_history_not_permanent_union():
    ranked=[NS(path=f'{n}.py') for n in range(15)]; lost=NS(path='lost.py')
    contexts=[dict(path='lost.py',snippets=[dict(text='return output;')])]
    unchanged,log=recover_source_candidates(ranked,[[lost]],[],contexts)
    assert unchanged==ranked and not log['recovered']
    recovered,log=recover_source_candidates(ranked,[[lost]],[proof('lost.py')],contexts)
    assert recovered[0].path==ranked[0].path and len(recovered)==15
    assert recovered[-1].path=='lost.py' and log['recovered'][0]['replaced']=='14.py'


def pair_fixture():
    ranked=[NS(path=f'{n}.py') for n in range(3)]
    contexts=[dict(path=r.path,snippets=[dict(text='return output;')]) for r in ranked]
    return ranked,contexts


def test_pairwise_swapped_order_consistency_and_exact_set():
    ranked,contexts=pair_fixture()
    def llm(prompt,**kwargs):
        data=json.loads(prompt.split('INPUT:\n')[1]);answers=[]
        for pair in data['pairs']:
            winner=max(c['id'] for c in pair['choices'])
            answers.append(dict(pair_id=pair['pair_id'],winner=winner,reason='direct operation',evidence_ids=[f'{winner}S0']))
        return json.dumps(dict(comparisons=answers))
    result,log=pairwise_rank(llm,'wrong operation',ranked,contexts,[])
    assert [r.path for r in result]==['2.py','1.py','0.py'] and log['status']=='ok'
    assert set(log['before'])==set(log['after'])


def test_pairwise_order_bias_ties_and_invalid_citations_are_not_success():
    ranked,contexts=pair_fixture()
    def biased(prompt,**kwargs):
        data=json.loads(prompt.split('INPUT:\n')[1]);answers=[]
        for p in data['pairs']:
            w=p['choices'][0]['id'];answers.append(dict(pair_id=p['pair_id'],winner=w,reason='position',evidence_ids=[f'{w}S0']))
        return json.dumps(dict(comparisons=answers))
    result,log=pairwise_rank(biased,'issue',ranked,contexts,[])
    assert result==ranked and all(c['winner'] is None for c in log['comparisons'])
    result,log=pairwise_rank(lambda *a,**k:'{"comparisons":[]}','issue',ranked,contexts,[])
    assert result==ranked and log['status']=='partial' and log['omissions']


def test_incomplete_group_has_one_targeted_recovery(monkeypatch):
    from mycode.dynamic_retrieval.candidate_reviewer import review_candidates
    from mycode.utils.model_profile import ModelProtocolProfile
    monkeypatch.setenv('MYCODE_FILELOC_EXECUTION_V3','1')
    monkeypatch.setenv('MYCODE_FILELOC_OWNER_CHALLENGE','0')
    ranked=[NS(path=f'src/revision{i}.py') for i in range(5)]
    contexts=[dict(path=r.path,snippets=[dict(text='def run(): return 1',start_line=1,end_line=1)],
        entities=[dict(name='run',kind='function',start_line=1,end_line=1)]) for r in ranked]
    calls=[]
    def llm(prompt,**kwargs):
        paths=list(dict.fromkeys(re.findall(r'"path": "(src/revision\d+\.py)"',prompt)))
        calls.append(paths)
        if len(paths)==5:paths=paths[:-1]
        return json.dumps(dict(reviews=[dict(path=p,verdict='plausible',confidence=.4,snippet_id=None,
            entity_id=None,flow_id=None,causal_chain='need operation') for p in paths],continue_search=True,next_queries=[]))
    result=review_candidates(llm=llm,issue_text='revision omission fixture',issue_sketch={},ranked=ranked,
        code_contexts=contexts,flow_traces=[],round_no=1,candidate_limit=6,repair_attempts=0,
        model_profile=ModelProtocolProfile(name='mimo'))
    assert result['reviewed_count']==5 and not result['unreviewed_paths']
    assert len(calls)==2 and calls[-1]==['src/revision4.py']


def test_missing_alignment_cannot_certify_source_responsibility():
    p=proof('source.py');p.pop('atomic_entity_quote_aligned')
    assert not backed(p)


def test_revision_enabled_pipeline_is_file_permutation_and_source_scoped(monkeypatch):
    from test_react_interprocedural_agent import _chartjs_sample
    from mycode.dynamic_retrieval.search_agent import dynamic_localize
    for flag in ['MYCODE_FILELOC_REVIEW_COVERAGE','MYCODE_FILELOC_LISTWISE_RANK',
                 'MYCODE_FILELOC_READ_RECONCILE','MYCODE_FILELOC_OWNER_CHALLENGE',
                 'MYCODE_FILELOC_EXECUTION_V3','MYCODE_CANDIDATE_CONTINUITY_V3',
                 'MYCODE_FUNCTION_CALIBRATION_V3','MYCODE_FILELOC_PAIRWISE_V3']:
        monkeypatch.setenv(flag,'1')
    tmp,sample,evidence,index=_chartjs_sample()
    try:
        result=dynamic_localize(sample,evidence,index=index,max_rounds=2,max_react_steps=5,top_k=15)
        snapshots=result['rank_stage_snapshots']
        before=[r['path'] for r in snapshots['fileloc_pairwise_before']]
        after=[r['path'] for r in snapshots['fileloc_pairwise_after']]
        assert before==after and set(after)<=set(index.files)
        assert result['best_round_selection']['fileloc_pairwise_v3']['status']=='skipped'
        assert 'fileloc_listwise_after' not in snapshots
        assert all(len(r['agent_observation'].get('fileloc_review_coverage',{}).get('supplemented',[]))<=5
                   for r in result['dynamic_rounds'])
    finally:tmp.cleanup()
