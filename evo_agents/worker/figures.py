"""The figures a review run's worker counts in its worktrees before its agent starts, without any model: what lives in
files rather than on the hub. ``evo_agents.hub.server.collect`` counts the rest.

- ``reports``: the open items of the reports, in the markdown files under a ``reports`` directory of each repo: each
  unchecked box (``- [ ]``), and each item of a list under a heading that names open work (OPEN_HEADING: "Việc còn
  mở", "Open items", "Follow-ups", "TODO", "Nợ kỹ thuật", "Tech debt"), until the next heading of that level or
  higher. Each with its repo, path and line, which ``code:REPO:PATH:LINE`` evidence cites as it is.
- ``learned_skills``: the learned skills waiting for review, in a ``skills/_pending`` directory (continuous-learning
  keeps them there), each with its repo and path.

Standard library only; a file that cannot be read is passed over. At most MAX_FILES files are read per repo and
MAX_ITEMS items kept in all.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_FILES = 500
MAX_ITEMS = 100
MAX_FILE_BYTES = 1024 * 1024
TEXT_CHARS = 200
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
OPEN_HEADING = re.compile(
    r"việc còn mở|còn mở|chưa làm|open items?|open questions?|follow[- ]?ups?|\btodo\b|nợ kỹ thuật|tech(?:nical)? debt",
    re.I,
)
UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.*)$")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".evo-run"})


def _cut(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TEXT_CHARS else line[: TEXT_CHARS - 3].rstrip() + "..."


def _files(root: Path, pattern: str):
    """The files of ``root`` matching ``pattern`` (rglob), outside SKIPPED_DIRS, at most MAX_FILES, sorted."""
    found = []
    for path in root.rglob(pattern):
        parts = path.relative_to(root).parts
        if any(part in SKIPPED_DIRS for part in parts) or not path.is_file() or path.is_symlink():
            continue
        found.append(path)
        if len(found) >= MAX_FILES:
            break
    return sorted(found)


def open_items(text: str) -> list[tuple[int, str]]:
    """(line, text) of each open item of a markdown report (see the module's docstring)."""
    found: list[tuple[int, str]] = []
    under: int | None = None  # the level of the heading of open work the lines are under
    fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if under is not None and level <= under:
                under = None
            if OPEN_HEADING.search(heading.group(2)):
                under = level
            continue
        unchecked = UNCHECKED.match(line)
        if unchecked:
            found.append((number, _cut(unchecked.group(1))))
            continue
        item = ITEM.match(line)
        if under is not None and item and not line[:1].isspace():  # an item of the list, not a note nested in one
            found.append((number, _cut(item.group(1))))
    return found


def scan(worktrees: dict[str, Path]) -> dict:
    """The figures of the worktrees ``worktrees`` maps each repo to: reports and learned_skills."""
    reports, skills = [], []
    for repo, root in sorted(worktrees.items()):
        if not root.is_dir():
            continue
        for path in _files(root, "*.md"):
            relative = path.relative_to(root)
            if "reports" not in relative.parts[:-1] or len(reports) >= MAX_ITEMS:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line, item in open_items(text):
                if len(reports) >= MAX_ITEMS:
                    break
                reports.append({"repo": repo, "path": relative.as_posix(), "line": line, "text": item})
        for path in _files(root, "SKILL.md"):
            relative = path.relative_to(root)
            parts = relative.parts
            if any(parts[index : index + 2] == ("skills", "_pending") for index in range(len(parts) - 1)):
                if len(skills) < MAX_ITEMS:
                    skills.append({"repo": repo, "path": relative.as_posix(), "name": path.parent.name})
    return {"reports": reports, "learned_skills": skills}
