"""The parts of kg_impact that need no session: the files and lines a unified diff touches, the files a bare
path can name, the coverage note of the seeds' languages, and what one call found. ``serve.Session`` walks the
graph and renders the result."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from evo_agents.kg.extract.languages import coverage_of, language_of

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
COVERAGE_NAMES = {"calls_across_files": "calls across files", "inheritance": "inheritance", "overrides": "overrides"}


@dataclass
class Impact:
    """What one kg_impact call found, as its text and its structured result render it."""

    build: int
    depth: int
    seeds: dict[str, dict] = field(default_factory=dict)
    reached: dict[str, dict] = field(default_factory=dict)  # {"hop", "node", "edge", "rels", "hub"} by node id
    mentions: dict[str, list[dict]] = field(default_factory=dict)  # mentions edges by the node that mentions
    docs: dict[str, dict] = field(default_factory=dict)
    coverage: list[dict] = field(default_factory=list)
    cut: list[dict] = field(default_factory=list)  # {"seed", "read", "total"}: seeds with more edges than read
    ambiguous: list[dict] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    missing: int = 0


def seed_coverage(seeds: Iterable[dict]) -> list[dict]:
    """The coverage rows (``extract.languages``) of the languages of the code among the seeds: a symbol or file
    seed has its file's path, from which its language follows."""
    languages = {language_of(str(n["props"].get("path") or "")) for n in seeds}
    return [{"language": lang, **coverage_of(lang)} for lang in sorted(languages - {None})]


def coverage_line(row: dict) -> str:
    """One line per language: each table's yes or no, the reason of a no, and what a yes still misses."""
    parts = []
    for key, name in COVERAGE_NAMES.items():
        cell = row[key]
        if not cell["supported"]:
            parts.append(f"{name} no ({cell['note']})")
        else:
            parts.append(f"{name} yes, not: {cell['missing']}" if cell.get("missing") else f"{name} yes")
    return f"coverage of {row['language']} seeds: " + "; ".join(parts)


def _diff_path(header: str, prefix: str) -> str | None:
    path = header.split("\t", 1)[0].strip()
    if len(path) > 1 and path[0] == path[-1] == '"':
        path = path[1:-1]
    if path == "/dev/null":
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def diff_lines(diff: str) -> dict[str, set[int]]:
    """The files a unified diff touches and their changed lines. A removed line counts in the old numbering
    and an added one in the new, each with its neighbour in the other numbering, so the lines name the
    right symbols whether the graph was built before the change or after it."""
    files: dict[str, set[int]] = {}
    old_path = None
    current: set[int] | None = None
    old = new = old_left = new_left = 0
    for raw in diff.splitlines():
        if current is not None and (old_left > 0 or new_left > 0):
            tag = raw[:1]
            if tag == "-":
                current.update((old, max(new - 1, 1)))
                old, old_left = old + 1, old_left - 1
            elif tag == "+":
                current.update((new, max(old - 1, 1)))
                new, new_left = new + 1, new_left - 1
            elif tag != "\\":  # a context line, maybe with its leading space stripped
                old, new, old_left, new_left = old + 1, new + 1, old_left - 1, new_left - 1
            continue
        if raw.startswith("diff --git "):
            # Seen even when no hunk follows (a binary or mode-only change).
            rest = raw[len("diff --git ") :]
            current, old_path = None, None
            if " b/" in rest:
                files.setdefault(rest[rest.rfind(" b/") + 3 :], set())
        elif raw.startswith("--- "):
            old_path = _diff_path(raw[4:], "a/")
        elif raw.startswith("+++ "):
            path = _diff_path(raw[4:], "b/") or old_path
            current = files.setdefault(path, set()) if path else None
        elif current is not None and (m := _HUNK.match(raw)):
            old, new = int(m[1]), int(m[3])
            old_left = int(m[2]) if m[2] is not None else 1
            new_left = int(m[4]) if m[4] is not None else 1
    return files


def path_candidates(path: str, files: list[dict]) -> list[dict]:
    """The files a bare path can name: those with exactly that path, else those sharing its longest
    suffix at a directory boundary (a diff may be taken from a parent or a child directory)."""
    exact = [f for f in files if f["props"]["path"] == path]
    if exact:
        return exact
    best, out = 0, []
    for f in files:
        p = f["props"]["path"]
        if p.endswith("/" + path) or path.endswith("/" + p):
            size = min(len(p), len(path))
            if size > best:
                best, out = size, [f]
            elif size == best:
                out.append(f)
    return out
