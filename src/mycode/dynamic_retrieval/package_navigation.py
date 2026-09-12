"""Bounded package-entry hints from issue text and actual repository paths."""
import re


def package_entry_candidates(files, issue_text, *, limit=6):
    if limit <= 0:
        return []
    terms = set(re.findall(r"[a-z0-9]+", issue_text.lower()))
    mentioned = issue_text.lower().replace("@babel/", "babel-")
    packages = {}
    python_packages = {path.rsplit("/", 1)[0] for path in files if path.endswith("/__init__.py")}
    for path in files:
        parts = path.split("/")
        if len(parts) < 2:
            continue
        if len(parts) >= 4 and parts[0] in {"packages", "modules", "crates"} and parts[2] == "src":
            package = "/".join(parts[:2])
        elif len(parts) >= 5 and parts[1:4] == ["src", "main", "java"]:
            package = parts[0]
        elif path.endswith(".py") and path.rsplit("/", 1)[0] in python_packages:
            package = path.rsplit("/", 1)[0]
        elif parts[0] == "lib" and path.endswith((".js", ".ts")):
            package = "/".join(parts[:-1])
        else:
            continue
        name = package.rsplit("/", 1)[-1].lower()
        tokens = set(re.findall(r"[a-z0-9]+", name)) - {
            "babel", "plugin", "transform", "proposal", "package", "core", "src",
        }
        stem = parts[-1].rsplit(".", 1)[0].lower()
        symbol_match = stem not in {"index", "__init__", "lib", "util", "utils"} and stem in terms
        package_match = name not in {"lib", "src", "core", "util", "utils"} and bool(
            re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", mentioned)
            or (len(tokens) >= 2 and tokens <= terms)
        )
        if not symbol_match and not package_match:
            continue
        if any(p.lower() in {"test", "tests", "fixtures", "generated", "dist", "build", "examples", "demo"} for p in parts):
            continue
        if not path.endswith((".js", ".ts", ".tsx", ".py", ".java", ".rs")):
            continue
        if path.endswith(".d.ts") or ".test." in path or ".spec." in path:
            continue
        if not files[path].strip():
            continue
        overlap = len(set(re.findall(r"[a-z0-9]+", parts[-1].lower())) & terms)
        entry = parts[-1] in {"index.js", "index.ts", "__init__.py", "lib.rs"}
        packages.setdefault(package, []).append((-int(symbol_match), -overlap, -int(entry), len(parts), path))
    # At most two files per explicitly supported package; global recall remains separate.
    chosen = []
    for name in sorted(packages, key=lambda name: min(packages[name])):
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
