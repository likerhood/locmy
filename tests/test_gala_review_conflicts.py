from mycode.dynamic_retrieval.search_agent import _merge_closure_candidate_reviews


def merge(*rows):
    return _merge_closure_candidate_reviews([
        {'llm_candidate_review': {'status': 'ok', 'candidates': [row]}} for row in rows])


def positive(**extra):
    return dict(path='owner.js', role='patch_target', grounded=True,
                mechanism_verified=True, confidence=.99, evidence_quote='actual source', **extra)


def negative(**extra):
    return dict(path='owner.js', role='navigation_only', grounded=True,
                mechanism_verified=False, confidence=.7,
                counterevidence=['Source implements navigation, not the operation.'], **extra)


def test_default_keeps_original_strongest_review(monkeypatch):
    monkeypatch.delenv('MYCODE_GALA_REVIEW_CONFLICT_GUARD', raising=False)
    row = merge(positive(), negative())['candidates'][0]
    assert row['mechanism_verified'] is True
    assert 'responsibility_conflict' not in row


def test_conflicting_certificates_cannot_win_by_confidence_or_order(monkeypatch):
    monkeypatch.setenv('MYCODE_GALA_REVIEW_CONFLICT_GUARD', '1')
    for rows in [(positive(), negative()), (negative(), positive())]:
        row = merge(*rows)['candidates'][0]
        assert row['responsibility_conflict']
        assert not row['mechanism_verified']
        assert row['role'] == 'supporting_target'
        assert not row['counterevidence']  # unresolved is not a proven rejection
        assert len(row['review_observations']) == 2


def test_nested_round_merge_preserves_conflict(monkeypatch):
    monkeypatch.setenv('MYCODE_GALA_REVIEW_CONFLICT_GUARD', '1')
    first = merge(positive(), negative())
    result = _merge_closure_candidate_reviews([
        {'llm_candidate_review': first},
        {'llm_candidate_review': {'status': 'ok', 'candidates': [positive()]}}])
    row = result['candidates'][0]
    assert row['responsibility_conflict'] and not row['mechanism_verified']
    assert len(row['review_observations']) == 3


def test_missing_or_ungrounded_evidence_does_not_cancel_certificate(monkeypatch):
    monkeypatch.setenv('MYCODE_GALA_REVIEW_CONFLICT_GUARD', '1')
    missing = negative()
    missing.update(role='supporting_target')
    ungrounded = negative()
    ungrounded.update(grounded=False)
    row = merge(positive(), missing, ungrounded)['candidates'][0]
    assert row['mechanism_verified']
    assert not row.get('responsibility_conflict')


def test_unresolved_conflict_cannot_force_incumbent_above_seed(monkeypatch):
    from mycode.dynamic_retrieval.search_agent import RankedLocation
    from mycode.dynamic_retrieval.seed_frontier import apply_verified_seed_guard
    head, seed = RankedLocation('owner.js', 10), RankedLocation('challenger.js', 9)
    monkeypatch.setenv('MYCODE_GALA_REVIEW_CONFLICT_GUARD', '1')
    review = merge(positive(), negative())
    ordered, report = apply_verified_seed_guard([seed, head], [head, seed], ['challenger.js'], review)
    assert [row.path for row in ordered] == ['challenger.js', 'owner.js']
    assert report['changes'] == []
    assert {row.path for row in ordered} == {'owner.js', 'challenger.js'}
