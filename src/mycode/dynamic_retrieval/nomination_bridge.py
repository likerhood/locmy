"""Give selected global hypotheses a bounded source-review opportunity.

This changes investigation order, not verification verdicts or file scores.
Only existing indexed files are eligible; no evaluation labels are accepted.
"""
from __future__ import annotations

import copy


def investigation_order(ranked, seeds, *, files, limit=3):
    originals = list(ranked)
    by_path = {item.path: item for item in originals}
    chosen = []
    for seed in seeds:
        if len(chosen) >= limit:
            break
        if seed.path not in files or not files[seed.path].strip():
            continue
        if seed.path not in {item.path for item in chosen}:
            chosen.append(copy.deepcopy(by_path.get(seed.path, seed)))
    selected = {item.path for item in chosen}
    return chosen + [item for item in originals if item.path not in selected]


def align_read_entities(contexts, entities):
    """Attribute actual windows to overlapping indexed entities, not query hits."""
    by_path = {}
    for entity in entities:
        by_path.setdefault(entity.path, []).append(entity)
    for context in contexts:
        windows = context.get('snippets', [])
        overlapping = [entity for entity in by_path.get(context['path'], [])
            if any(entity.start_line <= window.get('end_line', 0)
                   and window.get('start_line', 0) <= entity.end_line for window in windows)]
        # Prefer the narrow containing function over an enclosing whole-file class.
        overlapping.sort(key=lambda entity: (entity.end_line - entity.start_line, entity.start_line))
        context['entities'] = [dict(kind=e.kind, name=e.name, start_line=e.start_line,
                                    end_line=e.end_line) for e in overlapping[:8]]
    return contexts


def explicit_exclusion(decision):
    """A positive/plausible verdict with missing evidence is not a rejection."""
    return bool(decision.get('grounded') and decision.get('counterevidence')
                and decision.get('role') in {'unlikely', 'navigation_only',
                                             'reproduction_only', 'test_or_docs'})
