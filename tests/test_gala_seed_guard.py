from types import SimpleNamespace

from mycode.dynamic_retrieval.seed_frontier import restore_seed_frontier


def row(path):
    return SimpleNamespace(path=path, reasons=[], belief={})


def run(review, enabled=True):
    paths = ['src/owner.js', 'src/helper.js', 'src/entry.js']
    return restore_seed_frontier([row(p) for p in paths], [row('src/entry.js')],
        index=SimpleNamespace(files={p: 'actual source' for p in paths}),
        review={'candidates': review}, top_k=3, limit=1, protect_verified_head=enabled)


def verified(path, **kwargs):
    return dict(path=path, role='patch_target', grounded=True, mechanism_verified=True,
                evidence_quote='return updateState(value);', **kwargs)


def test_verified_incumbent_precedes_unverified_seed_without_changing_membership():
    original, _ = run([verified('src/owner.js')], enabled=False)
    guarded, report = run([verified('src/owner.js')])
    assert [r.path for r in original] == ['src/entry.js', 'src/owner.js', 'src/helper.js']
    assert [r.path for r in guarded] == ['src/owner.js', 'src/entry.js', 'src/helper.js']
    assert set(r.path for r in original) == set(r.path for r in guarded)
    assert report['verified_head_guard']['changes'][0]['path'] == 'src/owner.js'


def test_confidence_or_missing_source_does_not_activate_guard():
    for decision in [dict(path='src/owner.js', confidence=1.0),
                     dict(path='src/owner.js', role='patch_target', grounded=True,
                          mechanism_verified=True, evidence_quote='')]:
        ranked, report = run([decision])
        assert ranked[0].path == 'src/entry.js'
        assert not report['verified_head_guard']['changes']


def test_actual_counterevidence_disables_protection():
    ranked, report = run([verified('src/owner.js', counterevidence_grounded=True)])
    assert ranked[0].path == 'src/entry.js'
    assert not report['verified_head_guard']['changes']


def test_verified_seed_remains_a_competing_responsibility():
    ranked, report = run([verified('src/entry.js'), verified('src/owner.js')])
    assert ranked[0].path == 'src/entry.js'
    assert not report['verified_head_guard']['changes']


def test_late_verified_file_is_not_automatically_promoted_to_head():
    paths = ['src/a.js', 'src/b.js', 'src/c.js', 'src/late.js', 'src/seed.js']
    ranked, report = restore_seed_frontier([row(p) for p in paths], [row('src/seed.js')],
        index=SimpleNamespace(files={p: 'source' for p in paths}),
        review={'candidates': [verified('src/late.js')]}, top_k=5, limit=1,
        protect_verified_head=True)
    assert ranked[0].path == 'src/seed.js'
    assert not report['verified_head_guard']['changes']


def test_verified_second_candidate_cannot_jump_over_original_incumbent():
    ranked, report = run([verified('src/helper.js')])
    assert [r.path for r in ranked] == ['src/entry.js', 'src/owner.js', 'src/helper.js']
    assert not report['verified_head_guard']['changes']
