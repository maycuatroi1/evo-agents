"""The outcome a task states in its description: the last section headed ``Kết quả`` or ``Outcome`` (any heading
level), up to the next heading of the same level or above, or the end."""

from __future__ import annotations

import re

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
NAMES = ("kết quả", "outcome")


def outcome_of(markdown: str) -> str | None:
    lines = markdown.splitlines()
    start, level = None, 0
    for index, line in enumerate(lines):
        found = HEADING.match(line)
        if found and found[2].strip().casefold() in NAMES:
            start, level = index, len(found[1])
    if start is None:
        return None
    body = []
    for line in lines[start + 1 :]:
        found = HEADING.match(line)
        if found and len(found[1]) <= level:
            break
        body.append(line)
    return "\n".join(body).strip() or None


__all__ = ["outcome_of"]
