"""The difference between two revisions of a plan, line by line, for people reading the plan's history.

Both revisions are written as their git copy writes them (``evo_agents.hub.mirror.plan_text``: keys in the copy's
fixed order, long text folded), so the lines that change are the lines a person would see change in the copy. The
result is a unified diff split into hunks, each line numbered in the text it comes from: context lines carry both
numbers, an added line only the new one, a removed line only the old one. Standard library only, besides the
mirror's YAML writer.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Literal

from evo_agents.hub.mirror import plan_text

DEFAULT_CONTEXT = 3
MAX_CONTEXT = 50

LineKind = Literal["context", "added", "removed"]


@dataclass(frozen=True)
class DiffLine:
    kind: LineKind
    old: int | None  # 1-based number in the old text; None for an added line
    new: int | None  # 1-based number in the new text; None for a removed line
    text: str  # without its line break


@dataclass
class Hunk:
    old_start: int  # 1-based, as in ``@@ -old_start,old_lines +new_start,new_lines @@``
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass(frozen=True)
class TextDiff:
    hunks: list[Hunk]
    added: int
    removed: int


def _start(first: int, count: int) -> int:
    """Where a hunk starts in one text, as unified diff writes it: the line before it when it holds none there."""
    return first + 1 if count else first


def diff_lines(old: list[str], new: list[str], context: int = DEFAULT_CONTEXT) -> TextDiff:
    """The hunks that turn ``old`` into ``new`` (lists of lines without line breaks), with ``context`` unchanged
    lines around each change; hunks closer than twice that are merged, as ``diff -u`` does."""
    if not 0 <= context <= MAX_CONTEXT:
        raise ValueError(f"context must be between 0 and {MAX_CONTEXT}")
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    hunks: list[Hunk] = []
    added = removed = 0
    for group in matcher.get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        old_count, new_count = last[2] - first[1], last[4] - first[3]
        hunk = Hunk(_start(first[1], old_count), old_count, _start(first[3], new_count), new_count)
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                hunk.lines += [DiffLine("context", i1 + k + 1, j1 + k + 1, old[i1 + k]) for k in range(i2 - i1)]
                continue
            if tag in ("replace", "delete"):
                hunk.lines += [DiffLine("removed", i + 1, None, old[i]) for i in range(i1, i2)]
                removed += i2 - i1
            if tag in ("replace", "insert"):
                hunk.lines += [DiffLine("added", None, j + 1, new[j]) for j in range(j1, j2)]
                added += j2 - j1
        hunks.append(hunk)
    return TextDiff(hunks, added, removed)


def plan_diff(old: dict, new: dict, context: int = DEFAULT_CONTEXT) -> TextDiff:
    """The line diff between two plan bodies, each written as its git copy."""
    return diff_lines(plan_text(old).splitlines(), plan_text(new).splitlines(), context)
