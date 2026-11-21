"""Deterministic, query-focused implementation windows; no evaluation labels."""
import math
import re

from mycode.repo_index.structure_index import tokenize


def focused_windows(text, queries, *, limit=3):
    lines = text.splitlines()
    stop = {"the", "and", "for", "from", "this", "that", "with", "file", "code", "read", "src", "function", "import", "return", "const", "export"}
    terms = {t.lower() for t in tokenize(" ".join(queries)) if len(t) >= 3} - stop
    if not terms:
        return []
    tokens = [set(re.findall(r"[a-zA-Z_$][\w$]*", line.lower())) for line in lines]
    frequencies = {t: sum(t in row for row in tokens) for t in terms}
    scores = []
    for i, row in enumerate(tokens):
        matched = row & terms
        if not matched:
            continue
        weight = sum(1 + math.log((len(lines) + 1) / (1 + frequencies[t])) for t in matched)
        if re.match(r"\s*(import\b|export\s+\{|//|\*)", lines[i]):
            weight *= .25
        scores.append((weight, i))
    selected = []
    for _, i in sorted(scores, key=lambda pair: (-pair[0], pair[1])):
        start, end = max(0, i - 3), min(len(lines), i + 7)
        if any(start < old_end and end > old_start for old_start, old_end in selected):
            continue
        selected.append((start, end))
        if len(selected) >= limit:
            break
    return [{"start_line": start + 1, "end_line": end,
             "text": "\n".join(f"{j + 1}: {lines[j]}" for j in range(start, end))}
            for start, end in selected]
