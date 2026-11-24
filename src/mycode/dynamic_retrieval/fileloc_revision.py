"""Source-scoped revision policies. No dataset labels, evaluation state or task IDs."""
from copy import deepcopy
from collections import Counter
import itertools
import json
import math
import re


def backed(review):
    return bool(review.get('grounded') and review.get('quote_supported') and review.get('evidence_quote')
                and (review.get('atomic_entity_quote_aligned') is True or review.get('cross_function_supported')
                     or review.get('file_anchor_verified'))
                and review.get('role') in {'patch_target', 'supporting_target'})


def targeted_contexts(index, ranked, contexts, requests, *, issue_text='', attempted=()):
    """Keep independent windows; fulfill exact definition requests before lexical snippets."""
    from mycode.dynamic_retrieval.source_windows import focused_windows
    result = {c['path']: deepcopy(c) for c in contexts}
    done = set(attempted)
    events = []
    query = '\n'.join(map(str, requests)) + '\n' + issue_text
    definitions = {}
    for ent in index.entities:
        definitions.setdefault(ent.path, []).append(ent)
    candidates = []
    for position, item in enumerate(ranked[:15]):
        entities = definitions.get(item.path, [])
        exact = [e for e in entities if len(e.name) > 2 and re.search(
            r'(?<![\w$])' + re.escape(e.name) + r'(?![\w$])', query)]
        explicit = any(item.path in str(q) for q in requests)
        candidates.append((not bool(exact), not explicit, position, item.path, exact))
    for _, _, _, path, exact in sorted(candidates):
        text = index.files.get(path, '')
        if not text:
            continue
        context = result.setdefault(path, dict(path=path, snippets=[], entities=[], reasons=[]))
        lines = text.splitlines()
        windows = []
        for e in exact[:2]:
            start = max(0, e.start_line - 1)
            end = min(len(lines), e.end_line, start + 80)
            windows.append(dict(start_line=start+1, end_line=end,
                text='\n'.join(f'{j+1}: {lines[j]}' for j in range(start, end))))
            # Long implementations also get an operation-focused interior window.
            if e.end_line - e.start_line >= 80 and len(windows) < 2:
                body = '\n'.join(lines[start:min(e.end_line, len(lines))])
                for w in focused_windows(body, requests or [issue_text], limit=2):
                    a = start + w['start_line']; b = start + w['end_line']
                    if a >= end:
                        windows.append(dict(start_line=a, end_line=b,
                            text='\n'.join(f'{j+1}: {lines[j]}' for j in range(a-1, b))))
                        break
        if not windows:
            windows = focused_windows(text, requests or [issue_text], limit=2)
        previous = context.get('snippets', [])
        added = []
        for window in windows[:2]:
            key = f"{path}:{window['start_line']}:{window['end_line']}"
            if key not in done:
                added.append(window); done.add(key)
        if added and len(events) < 5:
            # Preserve a distinct earlier source window, not just a file-level read marker.
            unique = {(w['start_line'], w['end_line']): w for w in added + previous}
            context['snippets'] = list(unique.values())[:3]
            if exact:
                context['entities'] = [dict(name=e.name, kind=e.kind, start_line=e.start_line,
                    end_line=e.end_line) for e in exact[:8]]
            events.append(dict(path=path, entities=[e.name for e in exact[:2]],
                windows=[(w['start_line'], w['end_line']) for w in added], reason='exact_definition_or_operation'))
        elif added:
            for window in added:
                done.discard(f"{path}:{window['start_line']}:{window['end_line']}")
    order = list(dict.fromkeys([r.path for r in ranked[:15]] + list(result)))[:18]
    return [result[p] for p in order], dict(supplemented=events, attempted_signatures=sorted(done),
        max_files=5, max_windows=2, max_lines=80, consumed_paths=[p for p in order if result[p].get('snippets')])


def calibrate_functions(index, items, ranked_files, issue_text, reviews=()):
    """Saturate arbitrary accumulated score; compare actual implementation term density."""
    from mycode.dynamic_retrieval.search_agent import _entity_semantic_atoms
    source = {(e.path, e.kind, e.name, e.start_line, e.end_line): e for e in index.entities}
    terms = _entity_semantic_atoms(issue_text)
    candidates = []
    for item in items:
        ent = source.get((item.path, item.kind, item.name, item.start_line, item.end_line))
        body = ent.text if ent else ''
        tokens = _entity_semantic_atoms(item.name + '\n' + body)
        candidates.append((item, body, tokens & terms))
    frequency = Counter(t for _, _, matches in candidates for t in matches)
    positive = {r['path']: r for r in reviews if backed(r)}
    file_positions = {r.path: n for n, r in enumerate(ranked_files, 1)}
    result = []
    for item, body, matches in candidates:
        current = deepcopy(item)
        raw = max(0., float(item.score))
        lexical_prior = min(6., math.log1p(raw))
        unique_support = sum(math.log1p(len(candidates) / frequency[t]) for t in matches)
        length_penalty = math.sqrt(1. + math.log1p(max(1, len(body.split())) / 40.))
        semantic = min(28., 7. * unique_support / length_penalty)
        review = positive.get(item.path, {})
        exact_review = any(h.get('name') == item.name and h.get('kind') in {item.kind, 'function', 'method'}
                           for h in review.get('entities', []))
        explicit = bool(re.search(r'[`\'"]' + re.escape(item.name) + r'(?:\(\))?[`\'"]', issue_text))
        concrete_flow = any(r.startswith('llm_review_exact_entity:') for r in item.reasons) and exact_review
        responsibility = 24. if exact_review else 4. if review else 0.
        parent_prior = 4. / (2 + file_positions.get(item.path, 50))
        current.score = lexical_prior + semantic + responsibility + parent_prior + (12. if explicit else 0.) + (3. if concrete_flow else 0.)
        current.reasons.append('function_calibration_v3:' + json.dumps(dict(raw=round(raw,3),
            prior=round(lexical_prior,3), semantic=round(semantic,3), responsibility=responsibility,
            exact_quoted_symbol=explicit, source_available=bool(body)), sort_keys=True))
        result.append(current)
    return sorted(result, key=lambda x: (-x.score, x.path, x.name))


def recover_source_candidates(ranked, rankings, reviews, contexts, *, top_k=15):
    """Restore only independently source-backed history into unbacked tail slots."""
    positive = {r['path']: r for r in reviews if backed(r)}
    source = {c['path'] for c in contexts if c.get('snippets')}
    result = list(ranked)
    present = {r.path for r in result}
    missing = {}
    for round_no, ranking in enumerate(rankings, 1):
        for position, item in enumerate(ranking[:top_k], 1):
            if item.path not in present and item.path in source and item.path in positive:
                missing[item.path] = (round_no, position, item)
    events = []
    for _, _, item in sorted(missing.values(), key=lambda x: (-x[0], x[1], x[2].path))[:2]:
        victims = [n for n in range(len(result)-1, 7, -1) if result[n].path not in positive]
        if len(result) < top_k:
            result.append(item); events.append(dict(path=item.path, replaced=None))
        elif victims:
            victim = result[victims[0]]; result[victims[0]] = item
            events.append(dict(path=item.path, replaced=victim.path))
    return result, dict(recovered=events, criterion='grounded_aligned_source_positive_only')


def pairwise_rank(llm, issue, ranked, contexts, reviews):
    """Bidirectional local comparisons; ties preserve order; output is a pure permutation."""
    from mycode.dynamic_retrieval.candidate_reviewer import _invoke_llm, _response_text
    from mycode.evidence.tools.json_response import parse_json_response
    before = [r.path for r in ranked]
    if not llm or len(ranked) < 2:
        return ranked, dict(status='skipped', before=before, after=before)
    cm = {c['path']: c for c in contexts}
    rm = {r['path']: r for r in reviews}
    # Every eligible tail candidate competes on the same source criterion, not its ID.
    pool = list(range(min(8, len(ranked))))
    backed_tail = [n for n in range(8, len(ranked)) if backed(rm.get(before[n], {}))]
    if backed_tail:
        pool = pool[:max(3, 8-len(backed_tail[:3]))] + backed_tail[:3]
    packets = {}
    for n in pool:
        packets[n] = dict(id=n, path=before[n], responsibility={k:rm.get(before[n],{}).get(k) for k in
            ['role','patch_mechanism','counterevidence','evidence_quote']},source=[dict(id=f'{n}S{i}',text=str(s.get('text',''))[:2400])
                for i,s in enumerate(cm.get(before[n],{}).get('snippets',[])[:2])])
    pairs = list(itertools.combinations(pool, 2))
    votes = Counter(); comparisons = []; errors = []; omissions = []
    for offset in range(0, len(pairs), 7):
        group = pairs[offset:offset+7]; answers = []
        for reverse in [False, True]:
            payload = [dict(pair_id=offset+i, choices=[packets[b],packets[a]] if reverse else [packets[a],packets[b]])
                       for i,(a,b) in enumerate(group)]
            prompt = ('Compare each pair by responsibility for the original issue. Identify the actual faulty operation or missing check, '
                'not merely the symptom consumer or related filename. Use supplied code only; uncertain is a tie. '
                'Return JSON {"comparisons":[{"pair_id":integer,"winner":integer_or_null,"reason":"comparison",'
                '"evidence_ids":["0S0"]}]}. A winner must cite its own source.\nINPUT:\n' +
                json.dumps(dict(issue=issue,pairs=payload),ensure_ascii=False))
            decision = {}
            try:
                raw = _response_text(_invoke_llm(llm,prompt,max_tokens=2400))
                data,_ = parse_json_response(raw,required_keys=('comparisons',))
                entries=data.get('comparisons',[])
                counts=Counter(x.get('pair_id') for x in entries if isinstance(x,dict))
                for x in entries:
                    if not isinstance(x,dict): continue
                    ident=x.get('pair_id'); winner=x.get('winner')
                    if type(ident) is not int or not offset<=ident<offset+len(group) or counts[ident]!=1:continue
                    if winner is None:decision[ident]=None;continue
                    a,b=group[ident-offset]
                    if type(winner) is not int or winner not in {a,b}:continue
                    ids={s['id'] for s in packets[winner]['source']}
                    citations=x.get('evidence_ids',[])
                    if isinstance(citations,list) and citations and set(citations)<=ids and str(x.get('reason','')).strip():
                        decision[ident]=winner
            except Exception as exc:
                errors.append(dict(batch=offset,reverse=reverse,error=f'{type(exc).__name__}: {exc}'))
            for i in range(len(group)):
                if offset+i not in decision:
                    omissions.append(dict(pair_id=offset+i, reverse=reverse))
            answers.append(decision)
        for i,(a,b) in enumerate(group):
            ident=offset+i; first=answers[0].get(ident);second=answers[1].get(ident)
            winner=first if first is not None and first==second else None
            # Unsupported explanations cannot defeat source-backed responsibility.
            if winner is not None and backed(rm.get(before[b if winner==a else a],{})) and not backed(rm.get(before[winner],{})):
                winner=None
            if winner is not None:votes[winner]+=1
            else:votes[a]+=.5;votes[b]+=.5
            comparisons.append(dict(a=before[a],b=before[b],first=first,second=second,winner=winner))
    order=sorted(pool,key=lambda n:(-votes[n],n))
    after=list(ranked)
    for slot,ident in zip(sorted(pool),order):after[slot]=ranked[ident]
    assert len(after)==len(ranked) and {r.path for r in after}==set(before)
    return after,dict(status='ok' if not errors and not omissions else 'partial',before=before,after=[r.path for r in after],
        comparisons=comparisons,errors=errors,omissions=omissions,pool=[before[n] for n in pool],strategy='bidirectional_pairs_tie_preserving_v3')
