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


def split_passages(text: str, start: int, max_chars: int) -> list[dict]:
    """``text``, whose first line is line ``start`` of its file, cut into passages of about ``max_chars`` at most:
    ``{"start", "end", "text"}``, line numbers of the file, in order, every line in one passage. A cut falls between
    blocks, a block being a paragraph (lines up to a blank line) or a fenced code block whole; a block longer than
    ``max_chars`` is cut between its lines, and a line longer than that is a passage of its own."""
    lines = text.splitlines(keepends=True)
    blocks: list[list[int]] = []  # line indexes of each block
    current: list[int] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        opens = fence is None and fence_match is not None
        if opens and current:
            blocks.append(current)
            current = []
        current.append(i)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0] * 3
            elif marker.startswith(fence):
                fence = None
                blocks.append(current)
                current = []
        elif fence is None and not line.strip():
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    pieces: list[list[int]] = []  # blocks no longer than max_chars
    for block in blocks:
        size = sum(len(lines[i]) for i in block)
        if size <= max_chars:
            pieces.append(block)
            continue
        part: list[int] = []
        held = 0
        for i in block:
            if part and held + len(lines[i]) > max_chars:
                pieces.append(part)
                part, held = [], 0
            part.append(i)
            held += len(lines[i])
        if part:
            pieces.append(part)

    out: list[dict] = []
    group: list[int] = []
    held = 0
    for piece in pieces:
        size = sum(len(lines[i]) for i in piece)
        if group and held + size > max_chars:
            out.append(_passage(lines, group, start))
            group, held = [], 0
        group += piece
        held += size
    if group:
        out.append(_passage(lines, group, start))
    return [p for p in out if p["text"].strip()]


def _passage(lines: list[str], indexes: list[int], start: int) -> dict:
    return {
        "start": start + indexes[0],
        "end": start + indexes[-1],
        "text": "".join(lines[i] for i in indexes),
    }
