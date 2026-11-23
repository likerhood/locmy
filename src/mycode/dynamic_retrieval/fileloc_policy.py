"""Opt-in file-localization execution and evidence checks, without evaluation inputs."""
import os
import re
from pathlib import PurePosixPath


def enabled(name):
    return os.environ.get(name, "0").lower() in {"1", "true", "yes", "on"}


def responsibility_aligned(review):
    return bool(review.get('atomic_entity_quote_aligned') or review.get('cross_function_supported')
                or review.get('file_anchor_verified'))


def resolve_reads(queries, files):
    """Only exact indexed paths or unique same-directory/stem aliases are executable."""
    paths, events = [], []
    for query in queries:
        # URLs are evidence, not relative source paths.
        clean = re.sub(r"https?://\S+", " ", str(query))
        for token in re.findall(r"[\w.@+\-/\\]+\.[A-Za-z0-9]+", clean):
            path = token.replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path.startswith("/") or ".." in path.split("/"):
                events.append(dict(request=token, reason="outside_repository"))
                continue
            matches = [path] if path in files else sorted(
                p for p in files if PurePosixPath(p).parent == PurePosixPath(path).parent
                and PurePosixPath(p).stem == PurePosixPath(path).stem)
            if len(matches) == 1:
                if matches[0] not in paths:
                    paths.append(matches[0])
                events.append(dict(request=token, path=matches[0], reason="exact" if matches[0] == path else "unique_same_directory_stem"))
            else:
                events.append(dict(request=token, reason="ambiguous" if matches else "not_in_index", matches=matches[:8]))
    return paths, events


def quote_alignment(quote, entities, snippets, entity_definitions):
    """The quoted implementation must belong to the claimed entity, not a sibling."""
    normalized = " ".join(str(quote).split()).lower()
    quoted = [s for s in snippets if normalized and normalized in " ".join(str(s.get("text", "")).split()).lower()]
    for entity in entities:
        name = str(entity.get("name", ""))
        if not name:
            continue
        # Token boundaries are essential: texture != textureMode.
        exact = re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", normalized)
        declaration = re.search(r"(?:\b(?:function|def|class)\s+" + re.escape(name)
            + r"\b|(?<![\w$])" + re.escape(name) + r"\s*(?:=\s*(?:function|async|\([^)]*\)\s*=>)|\([^)]*\)\s*\{))", normalized)
        if exact and quoted and declaration:
            return True
        definitions = [e for e in entity_definitions if e.get("name") == name]
        # Use the actual numbered quote span, not the much larger containing snippet.
        quote_lines = [int(n) for n in re.findall(r"(?:^|\s)(\d+)\s*[:|]", str(quote))]
        if quote_lines and quoted:
            for definition in definitions:
                start, end = definition.get("start_line"), definition.get("end_line")
                if isinstance(start, int) and isinstance(end, int) and start <= min(quote_lines) <= max(quote_lines) <= end:
                    return True
    return False


def operation_quote(quote):
    """Import/comment-only citations cannot establish a state-changing mechanism."""
    lines = [re.sub(r"^\s*\d+\s*[:|]\s*", "", line).strip()
             for line in re.split(r"\n|\s+(?=\d+\s*[:|])", str(quote))]
    meaningful = [line for line in lines if line and not re.match(r"^(?:import\b|from\b|//|/\*|\*|#|['\"]use strict)", line)]
    if not meaningful:
        return False
    text = " ".join(meaningful)
    # Includes operations, returns, JSX and CSS/config assignments; no language whitelist.
    return bool(re.search(r"[=(:<>]|\b(?:return|throw|raise|yield)\b", text))


def review_gaps(candidates):
    return [f"Read actual implementation {c['path']} " + " ".join(e.get("name", "") for e in c.get("entities", []))
            for c in candidates if c.get("role") in {"patch_target", "supporting_target"}
            and c.get("responsibility_unconfirmed")][:3]


def challenge_paths(index, ranked, issue_text=None):
    """A single bounded contrast of the current owner, rival and observed delegate."""
    if not ranked:
        return [], []
    head = ranked[0].path
    paths = [item.path for item in ranked[:2] if item.path in index.files]
    source = index.files.get(head, "")
    delegates = []
    # Only real source import/require strings; never issue URL fragments or imagined paths.
    for target in re.findall(r"(?:from\s*|require\s*\(\s*)['\"]([^'\"]+)['\"]", source):
        base = str(PurePosixPath(head).parent / target) if target.startswith(".") else target
        # Normalize lexical .. without allowing a repository escape.
        parts = []
        for part in base.split("/"):
            if part == "..":
                if not parts:
                    parts = []
                    break
                parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        normalized = "/".join(parts)
        if not normalized:
            continue
        matches = sorted(p for p in index.files if p == normalized or
            str(PurePosixPath(p).with_suffix("")) == normalized or
            str(PurePosixPath(p).parent) == normalized and PurePosixPath(p).stem == "index")
        if len(matches) == 1 and matches[0] not in paths:
            candidate = matches[0]
            relevant = True
            if issue_text is not None:
                clean = re.sub(r'https?://\S+', ' ', issue_text)
                stop = {'this','that','when','with','from','have','does','should','expected','actual','issue','version','return','function','const','import','export'}
                terms = set(re.findall(r'[A-Za-z_$][\w$]{3,}',clean.lower())) - stop
                body = set(re.findall(r'[A-Za-z_$][\w$]*',index.files.get(candidate,'').lower()))
                literals = re.findall(r'`([A-Za-z_$][\w$]*)`',clean)
                relevant = len(terms & body) >= 2 or any(x.lower() in body for x in literals)
            if relevant:
                delegates.append(candidate)
    # Prefer a delegate already competing in the retrieval frontier, then source order.
    positions = {item.path: n for n, item in enumerate(ranked)}
    delegates.sort(key=lambda p: positions.get(p, len(positions)))
    if delegates:
        paths.append(delegates[0])
    return paths[:3], [{"path": p, "source_path": head, "reason": "observed_import_delegate"} for p in delegates[:1]]
