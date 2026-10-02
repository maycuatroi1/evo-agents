"""Text helpers shared by connectors: markdown sections as fragments, heading slugs."""

from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def slugify(title: str) -> str:
    """GitHub-style heading slug: lowercase, letters and digits kept (any script), spaces to hyphens."""
    title = re.sub(r"`([^`]*)`", r"\1", title).strip().lower()
    out = []
    for ch in title:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch.isspace():
            out.append("-")
    return "".join(out) or "section"


def split_markdown(text: str) -> list[dict]:
    """Split markdown into disjoint sections, one fragment per heading.

    Each line belongs to exactly one fragment, so a fragment is a clean ownership unit: changing one
    section changes exactly one fragment hash. Text before the first heading becomes ``_preamble``.
    Headings inside fenced code blocks are ignored.
    """
    lines = text.splitlines(keepends=True)
    sections: list[dict] = []
    current = {"anchor": "_preamble", "title": "", "level": 0, "start": 1, "lines": []}
    stack: list[tuple[int, str]] = []  # (level, anchor)
    used: dict[str, int] = {}
    fence: str | None = None

    def close(section: dict, end: int) -> None:
        body = "".join(section["lines"])
        if section["anchor"] == "_preamble" and not body.strip():
            return
        frag = {
            "anchor": section["anchor"],
            "level": section["level"],
            "text": body,
            "span": f"lines {section['start']}-{end}",
        }
        if section["title"]:
            frag["title"] = section["title"]
        if section.get("parent"):
            frag["parent"] = section["parent"]
        sections.append(frag)

    for number, line in enumerate(lines, start=1):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0] * 3
            elif marker.startswith(fence):
                fence = None
        heading = None if fence is not None or fence_match else _HEADING.match(line.rstrip("\n"))
        if heading:
            close(current, number - 1)
            level = len(heading.group(1))
            title = heading.group(2).strip()
            base = slugify(title)
            count = used.get(base, 0)
            used[base] = count + 1
            anchor = base if count == 0 else f"{base}-{count}"
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            stack.append((level, anchor))
            current = {
                "anchor": anchor,
                "title": title,
                "level": level,
                "start": number,
                "lines": [line],
                "parent": parent,
            }
        else:
            current["lines"].append(line)
    close(current, len(lines))
    return sections
