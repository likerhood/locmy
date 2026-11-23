"""Bounded source supplementation and file-only permutation. No benchmark labels."""
from copy import deepcopy
import json
import re


def supplement(index, ranked, contexts, queries, attempted=()):
    """Make the current Top15 readable; supplement at most five explicit gaps once."""
    from mycode.dynamic_retrieval.source_windows import focused_windows
    result = {c['path']: deepcopy(c) for c in contexts}
    attempted = set(attempted)
    events, used = [], []
    requested = ' '.join(queries)
    for item in ranked[:15]:
        path = item.path
        text = index.files.get(path, '')
        context = result.setdefault(path, dict(path=path, snippets=[], entities=[], reasons=[]))
        definitions = [e for e in index.entities if e.path == path]
        if not context.get('entities'):
            context['entities'] = [dict(name=e.name,kind=e.kind,start_line=e.start_line,end_line=e.end_line)
                                   for e in definitions[:8]]
        if not context.get('snippets') and text:
            context['snippets'] = focused_windows(text, queries, limit=3)
        names = [e for e in definitions if re.search(r'(?<![\w$])'+re.escape(e.name)+r'(?![\w$])', requested)]
        signature = path + '::' + ','.join(e.name for e in names[:2])
        needs = not context.get('snippets') or path in requested or bool(names)
        if needs and text and signature not in attempted and len(used) < 5:
            lines = text.splitlines()
            windows = []
            for e in names[:2]:
                start = max(0,e.start_line-1)
                end = min(len(lines),e.end_line,start+80)
                windows.append(dict(start_line=start+1,end_line=end,
                    text='\n'.join(f'{j+1}: {lines[j]}' for j in range(start,end))))
            if not windows:
                windows = focused_windows(text,queries,limit=2)
            if not windows:
                windows = [dict(start_line=1,end_line=min(80,len(lines)),
                    text='\n'.join(f'{j+1}: {line}' for j,line in enumerate(lines[:80])))]
            context['snippets'] = windows[:2]
            if names:
                context['entities'] = [dict(name=e.name,kind=e.kind,start_line=e.start_line,end_line=e.end_line) for e in names[:8]]
            used.append(signature)
            events.append(dict(path=path,reason='definition_or_operation_gap',windows=len(windows)))
        context['fileloc_source_status'] = 'source_available' if context.get('snippets') else 'unread_or_empty_source'
    # All fifteen candidates are visible, with three optional original contexts.
    paths = list(dict.fromkeys([r.path for r in ranked[:15]] + list(result)))[:18]
    return [result[p] for p in paths], dict(supplemented=events,attempted_signatures=sorted(attempted|set(used)),
        max_files=5,max_windows=2,max_lines=80,consumed_paths=[p for p in paths if result[p].get('snippets')])


def merge_evidence(reviews):
    """Keep source-backed responsibility until a source-backed refutation, not an omission."""
    memory, events = {}, []
    for round_no, review in enumerate(reviews,1):
        for item in review.get('candidates',[]):
            path=item['path']
            grounded=bool(item.get('grounded') and item.get('quote_supported') and item.get('evidence_quote'))
            aligned = item.get('atomic_entity_quote_aligned') is not False or item.get('file_anchor_verified') or item.get('cross_function_supported')
            refuted=grounded and aligned and item.get('role')=='unlikely' and bool(item.get('counterevidence'))
            supported=grounded and aligned and item.get('role') in {'patch_target','supporting_target'}
            if refuted:
                memory[path]=dict(item,source_round=round_no,evidence_state='source_refuted')
                events.append(dict(path=path,round=round_no,action='refuted'))
            elif supported:
                old=memory.get(path)
                if old and old.get('mechanism_verified') and not item.get('mechanism_verified'):
                    events.append(dict(path=path,round=round_no,action='kept_prior_source_responsibility'))
                else:
                    memory[path]=dict(item,source_round=round_no,evidence_state='source_supported')
            elif path not in memory:
                memory[path]=dict(item,source_round=round_no,evidence_state='unconfirmed')
    return list(memory.values()),events


def listwise_rank(llm, issue, ranked, contexts, reviews):
    """Only permute existing candidates. Stable tiers prevent unsupported promotions."""
    from mycode.dynamic_retrieval.candidate_reviewer import _invoke_llm,_response_text
    from mycode.evidence.tools.json_response import parse_json_response
    before=[r.path for r in ranked]
    if not llm or len(before)<2:
        return ranked,dict(status='skipped',before=before,after=before)
    context_map={c['path']:c for c in contexts}
    review_map={c['path']:c for c in reviews}
    packet=[]
    tiers={}
    for n,path in enumerate(before):
        review=review_map.get(path,{})
        source=context_map.get(path,{})
        aligned=review.get('atomic_entity_quote_aligned') is not False or review.get('file_anchor_verified') or review.get('cross_function_supported')
        backed=bool(review.get('grounded') and review.get('quote_supported') and review.get('evidence_quote') and aligned)
        tiers[path]=0 if backed and review.get('mechanism_verified') else 1 if backed and review.get('role') in {'patch_target','supporting_target'} else 3 if backed and review.get('role')=='unlikely' and review.get('counterevidence') else 2
        packet.append(dict(id=n,path=path,evidence_tier=tiers[path],
            responsibility={k:review.get(k) for k in ['role','patch_mechanism','evidence_quote','counterevidence','evidence_state']},
            source=[dict(id=f'{n}S{i}',start_line=s.get('start_line'),end_line=s.get('end_line'),text=str(s.get('text',''))[:1800])
                for i,s in enumerate(source.get('snippets',[])[:2])]))
    prompt=('Rank repository files by required change responsibility, not similarity or graph degree. '
        'Compare faulty operation/missing check, callers and consumers; several files may jointly require edits. '
        'Use only the supplied source. Return JSON {"order":[all integer ids exactly once],'
        '"top5":[{"id":integer,"reason":"specific comparison","evidence_ids":["0S0"]}]}. '
        'Explain every first-five id using its own source IDs. Equal evidence retains original order; '
        'unread guesses cannot outrank source-supported responsibility.\n'+json.dumps(dict(issue=issue,candidates=packet),ensure_ascii=False))
    attempts=[]
    for attempt in range(2):
        try:
            raw=_response_text(_invoke_llm(llm,prompt,max_tokens=4000))
            data,_=parse_json_response(raw,required_keys=('order','top5'))
            order=data.get('order',[])
            assert all(type(i) is int for i in order) and len(order)==len(before) and set(order)==set(range(len(before)))
            explanations=data.get('top5',[])
            assert len(explanations)==min(5,len(before))
            assert {x.get('id') for x in explanations}==set(order[:5])
            for x in explanations:
                valid_ids={s['id'] for s in packet[x['id']]['source']}
                assert str(x.get('reason','')).strip()
                assert x.get('evidence_ids') and set(x['evidence_ids']) <= valid_ids
            # Hard evidence tiers, then the evidence-grounded comparative order.
            # Within a tier, identical reviewed responsibility keeps the prior order.
            positions={i:n for n,i in enumerate(order)}
            ordered=sorted(range(len(before)),key=lambda i:(tiers[before[i]],positions[i]))
            equal_groups={}
            for i in range(len(before)):
                signature=json.dumps(packet[i]['responsibility'],sort_keys=True,default=str)
                equal_groups.setdefault((tiers[before[i]],signature),[]).append(i)
            for ids in equal_groups.values():
                slots=[n for n,i in enumerate(ordered) if i in ids]
                for slot,ident in zip(slots,ids):
                    ordered[slot]=ident
            after=[before[i] for i in ordered]
            by_path={r.path:r for r in ranked}
            return [by_path[p] for p in after],dict(status='ok',before=before,after=after,
                raw_order=[before[i] for i in order],evidence_tiers=tiers,explanations=explanations,attempts=attempts)
        except Exception as exc:
            attempts.append(dict(attempt=attempt+1,error=f'{type(exc).__name__}: {exc}'))
            prompt+='\nPrevious answer failed validation. Return the exact permutation and five source-cited explanations. Do not invent ids.'
    return ranked,dict(status='fallback',before=before,after=before,attempts=attempts)
