"""Bounded package-entry hints from issue text and actual repository paths."""
import re


def package_entry_candidates(files, issue_text, *, limit=6):
    terms = set(re.findall(r"[a-z0-9]+", issue_text.lower()))
    mentioned = issue_text.lower().replace("@babel/", "babel-")
    packages = {}
    for path in files:
        parts = path.split("/")
        if len(parts) < 4 or parts[0] not in {"packages", "modules", "crates"}:
            continue
        name = parts[1].lower()
        tokens = set(re.findall(r"[a-z0-9]+", name)) - {
            "babel", "plugin", "transform", "proposal", "package", "core", "src",
        }
        if name not in mentioned and not (len(tokens) >= 2 and tokens <= terms):
            continue
        if parts[2] != "src" or any(p in {"test", "tests", "fixtures", "generated"} for p in parts):
            continue
        if not path.endswith((".js", ".ts", ".tsx", ".py", ".java", ".rs")):
            continue
        if path.endswith(".d.ts") or ".test." in path or ".spec." in path:
            continue
        if not files[path].strip():
            continue
        overlap = len(set(re.findall(r"[a-z0-9]+", parts[-1].lower())) & terms)
        entry = parts[-1] in {"index.js", "index.ts", "__init__.py", "lib.rs"}
        packages.setdefault(parts[1], []).append((-overlap, -int(entry), len(parts), path))
    # At most two files per explicitly supported package; global recall remains separate.
    chosen = []
    for name in sorted(packages):
        chosen.extend(row[-1] for row in sorted(packages[name])[:2])
    return chosen[:limit]


def navigation_only_file(path, issue_text):
    name = path.rsplit("/", 1)[-1].lower()
    text = issue_text.lower()
    if name in text:
        return False
    if name.startswith(("changelog", "changes.", "readme")):
        return not bool(re.search(r"\b(?:documentation|readme|changelog|release notes)\b", text))
    if name in {"makefile", "makefile.js", "gulpfile.js", "gruntfile.js"}:
        return not bool(re.search(r"\b(?:build|building|makefile|gulp|grunt|packaging)\b", text))
    return False
