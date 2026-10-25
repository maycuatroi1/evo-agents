"""Markdown in and out: a document read from the headings of a template (``parse``) and written back (``render``).

Reading: every ATX heading (``#`` to ``######``, never inside a fenced code block) starts an item whose body is the
text up to the next heading; the text before the first heading is the preamble. ``template.heading`` says what a
heading makes. A use case's body gives its fields: the rows of its table of items (ID, Status, Objective, Actor,
Trigger, Pre-condition, Post-condition), the ID's other codes as aliases, and the steps of its Activity Flow table. A
screen's code is the comment ``<!-- srs: SCR-3 -->`` right after its heading, when there is one. In a section, the
first run of table rows whose first cell is a code (``NFR-SEC-01`` anywhere, ``CBR 2`` under Common Business Rules)
becomes one item per row; the table's head stays the section's body and what follows it the section's ``tail``.

Writing gives the same structure back: the preamble, then each item's heading at its level (the screen's code comment
after it with ``anchors``), its body, its rows and its tail, a blank line between blocks. ``normalize`` is the form two
texts are compared in: trailing whitespace dropped, runs of blank lines as one, and the screens' comments left out
when asked; a document read and written again gives its source back in that form.
"""

from __future__ import annotations

import re

from evo_agents.hub.server.srs.doc import WARNING, Document, Item, Problem
from evo_agents.hub.server.srs.template import (
    ANCHOR,
    CBR_SECTION,
    Template,
    code_key,
    heading,
    heading_text,
    tidy,
)

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SEPARATOR_CELL = re.compile(r":?-+:?")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
ALIAS_SPLIT = re.compile(r"[(),;/]")
ITEM_FIELDS = {  # the first cell of a row of a use case's table of items, lowercase -> its field
    "id": "id",
    "status": "status",
    "objective": "objective",
    "goal": "objective",
    "intent": "intent",
    "actor": "actor",
    "actors": "actor",
    "context": "context",
    "background": "context",
    "trigger": "trigger",
    "pre-condition": "pre_condition",
    "pre-conditions": "pre_condition",
    "precondition": "pre_condition",
    "preconditions": "pre_condition",
    "post-condition": "post_condition",
    "post-conditions": "post_condition",
    "postcondition": "post_condition",
    "postconditions": "post_condition",
    "expectation": "expectation",
    "expectations": "expectation",
    "expected result": "expectation",
    "expected results": "expectation",
    "expected outcome": "expectation",
    "acceptance criteria": "expectation",
}
HELD_FIELDS = frozenset({"steps"})  # what a row of the table of items never takes, its Activity Flow's


# Lines and tables


def lines_of(text: str) -> list[str]:
    """The lines of ``text``, line ends of any system, trailing whitespace dropped."""
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def block(lines: list[str]) -> str:
    """``lines`` as one text, the blank lines at either end left out."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def cells(line: str) -> list[str]:
    """The cells of a table row, stripped."""
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip() for cell in CELL_SPLIT.split(text)]


def is_separator(line: str) -> bool:
    found = cells(line)
    return bool(found) and all(SEPARATOR_CELL.fullmatch(cell) for cell in found)


def tables(body: str) -> list[list[list[str]]]:
    """Each table of ``body``: its rows as cells, the separator row left out, the head first."""
    found: list[list[list[str]]] = []
    current: list[list[str]] | None = None
    for line in body.split("\n"):
        if not is_row(line):
            current = None
            continue
        if current is None:
            current = []
            found.append(current)
        if not is_separator(line):
            current.append(cells(line))
    return found


def _plain(cell: str) -> str:
    return tidy(cell.replace("*", "").replace("`", ""))


def _slug(text: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", _plain(text).lower()).strip("_")
    return slug or f"col{index + 1}"


# Fields of items


def _field_key(label: str) -> str:
    """The key of a row of a use case's table of items the template does not name: its label, lowercase, each run of
    other characters than letters and digits one underscore (``Claim`` -> ``claim``)."""
    return re.sub(r"[\W_]+", "_", _plain(label).lower()).strip("_")


def use_case_fields(body: str) -> dict:
    """The fields a use case's body holds: those of its table of items, and its steps from its Activity Flow table.

    The table of items is a table one of whose rows the template names (``ITEM_FIELDS``); its other rows are kept
    under their own key (``_field_key``), its head only when the template names it."""
    fields: dict = {}
    for table in tables(body):
        if not table:
            continue
        head = [_plain(cell).lower() for cell in table[0]]
        if head and "step" in head[0]:
            keys = [_slug(cell, index) for index, cell in enumerate(table[0])]
            fields["steps"] = [dict(zip(keys, row, strict=False)) for row in table[1:]]
            continue
        rows = [row for row in table if len(row) >= 2]
        if not any(_plain(row[0]).lower() in ITEM_FIELDS for row in rows):
            continue
        for row in rows:
            name = ITEM_FIELDS.get(_plain(row[0]).lower())
            if name is None and row is not table[0]:
                name = _field_key(row[0])
            if name and name not in HELD_FIELDS and name not in fields:
                fields[name] = row[1]
    return fields


def aliases_of(code: str, item_id: str | None) -> list[str]:
    """The other codes of a use case its ID row names, such as ``UC-ORD-001 (ORD-01)``: each part that holds a digit
    and is not ``code``."""
    found: list[str] = []
    for part in ALIAS_SPLIT.split(item_id or ""):
        alias = tidy(part.replace("*", "").replace("`", ""))
        if alias and any(ch.isdigit() for ch in alias) and code_key(alias) != code_key(code):
            if code_key(alias) not in {code_key(a) for a in found}:
                found.append(alias)
    return found


def screen_fields(body: str) -> dict:
    found = tables(body)
    if not found or len(found[0]) < 2:
        return {}
    keys = [_slug(cell, index) for index, cell in enumerate(found[0][0])]
    return {"components": [dict(zip(keys, row, strict=False)) for row in found[0][1:]]}


def fields_of(kind: str, body: str, held: dict | None = None) -> dict:
    """The fields of an item of ``kind`` whose body is ``body``, over ``held`` (what its heading holds)."""
    fields = dict(held or {})
    if kind == "use_case":
        fields.pop("steps", None)
        fields.update(use_case_fields(body))
    elif kind == "screen":
        fields.pop("components", None)
        fields.update(screen_fields(body))
    return fields


# Reading


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every heading outside fenced code blocks."""
    found, fence = [], None
    for index, line in enumerate(lines):
        opened = FENCE.match(line)
        if opened:
            mark = opened[1][0]
            if fence is None:
                fence = mark
            elif fence == mark:
                fence = None
            continue
        match = HEADING.match(line) if fence is None else None
        if match:
            found.append((index, len(match[1]), match[2].strip()))
    return found


def _coded_row(line: str, template: Template, in_cbr: bool) -> str | None:
    if not is_row(line) or is_separator(line):
        return None
    return template.row_kind(cells(line)[0], in_cbr)


def split_rows(body: str, template: Template, in_cbr: bool) -> tuple[str, list[tuple[str, str]], str, bool]:
    """A section's body as (its head, its coded rows as (kind, line), its tail, whether more coded rows stay text)."""
    lines = body.split("\n")
    start = next((i for i, line in enumerate(lines) if _coded_row(line, template, in_cbr)), None)
    if start is None:
        return body, [], "", False
    end, rows = start, []
    while end < len(lines):
        kind = _coded_row(lines[end], template, in_cbr)
        if kind is None:
            break
        rows.append((kind, lines[end]))
        end += 1
    rest = lines[end:]
    more = any(_coded_row(line, template, in_cbr) for line in rest)
    return block(lines[:start]), rows, block(rest), more


def row_parts(kind: str, line: str) -> tuple[str, str, dict]:
    """The code, title and fields of a table row that is an item of ``kind``: the code is its first cell, the title the
    last cell that says something (the rule, the requirement), an NFR's group the middle part of its code."""
    found = cells(line)
    code = tidy(found[0].replace("*", ""))
    rest = [_plain(cell) for cell in found[1:] if _plain(cell)]
    fields: dict = {"cells": found}
    if kind == "nfr":
        fields["group"] = code.split("-")[1] if code.count("-") >= 2 else None
    return code, (rest[-1] if rest else code)[:500], fields


def _row_item(kind: str, line: str, parent: int, number: int) -> Item:
    """The item of a coded table row; ``number`` is the line of the heading whose body holds the table."""
    code, title, fields = row_parts(kind, line)
    return Item(kind, title, line, code, level=None, fields=fields, parent=parent, line=number)


def _screen_code(body: str) -> tuple[str | None, str]:
    lines = body.split("\n")
    found = ANCHOR.match(lines[0].strip()) if lines and lines[0].strip() else None
    if found is None:
        return None, body
    return tidy(found["code"]), block(lines[1:])


def _parent(stack: list[tuple[int, int]], level: int) -> int | None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    return stack[-1][1] if stack else None


def _note(doc: Document, check: str, message: str, line: int, code: str | None = None) -> None:
    doc.notes.append(Problem(WARNING, check, message, code=code, line=line))


def _in_cbr(doc: Document, parent: int | None, title: str) -> bool:
    if CBR_SECTION.search(title):
        return True
    while parent is not None:
        if CBR_SECTION.search(doc.items[parent].title):
            return True
        parent = doc.items[parent].parent
    return False


def _add_heading_item(doc: Document, template: Template, level: int, text: str, body: str, parent, line: int) -> int:
    """Add the item of a heading, and the items of its coded rows after it; the heading item's index."""
    found = heading(text)
    item = Item(found.kind, found.title[:500], body, found.code, level=level, parent=parent, line=line)
    item.fields = fields_of(found.kind, body, dict(found.fields))
    if found.kind == "use_case":
        item.aliases = aliases_of(found.code or "", item.fields.get("id"))
    elif found.kind == "screen":
        item.code, item.body = _screen_code(body)
        item.fields = fields_of("screen", item.body)
    if not found.known:
        _note(doc, "heading_unknown", f"heading {text!r} is not one the template names: kept as a section", line)
    rows: list[tuple[str, str]] = []
    if found.kind in ("section", "glossary"):
        head, rows, tail, more = split_rows(body, template, _in_cbr(doc, parent, found.title))
        item.body = head
        if tail:
            item.fields["tail"] = tail
        if more:
            _note(doc, "rows_as_text", f"coded rows after the first table of {text!r} stay text", line)
    doc.items.append(item)
    index = len(doc.items) - 1
    for kind, row in rows:
        doc.items.append(_row_item(kind, row, index, line))
    return index


def parse(text: str, template: Template) -> Document:
    """The document ``text`` holds, read with ``template``."""
    lines = lines_of(text)
    marks = _headings(lines)
    doc = Document(preamble=block(lines[: marks[0][0]] if marks else lines))
    stack: list[tuple[int, int]] = []
    for number, (at, level, title) in enumerate(marks):
        end = marks[number + 1][0] if number + 1 < len(marks) else len(lines)
        parent = _parent(stack, level)
        if parent is not None and level > (doc.items[parent].level or 0) + 1:
            _note(doc, "heading_jump", f"heading {title!r} skips a level under its parent", at + 1)
        index = _add_heading_item(doc, template, level, title, block(lines[at + 1 : end]), parent, at + 1)
        stack.append((level, index))
    return doc


def title_of(doc: Document) -> str | None:
    """The document's title: the first bold line of its preamble, else its first heading's title."""
    for line in doc.preamble.split("\n"):
        found = re.fullmatch(r"\*\*(.+?)\*\*:?", line.strip())
        if found:
            return found[1].strip()[:300]
    return doc.items[0].title[:300] if doc.items else None


# Writing


def block_text(item: Item, rows: list[Item], anchors: bool) -> str:
    """The block of a heading item as a document exports it: its heading, its screen code comment when ``anchors``, its
    body, its coded table ``rows`` and the text after them; a row's own line for a row."""
    if item.level is None:
        return item.body
    text = "#" * item.level + " " + heading_text(item.kind, item.title, item.code, item.fields)
    if item.kind == "screen" and anchors and item.code:
        text += f"\n<!-- srs: {item.code} -->"
    if item.body:
        text += "\n\n" + item.body
    if rows:
        text += ("\n" if item.body else "\n\n") + "\n".join(row.body for row in rows)
    tail = item.fields.get("tail")
    if tail:
        text += "\n\n" + tail
    return text


def render(doc: Document, *, anchors: bool = True) -> str:
    """``doc`` as Markdown, with each screen's code comment when ``anchors``."""
    blocks = [doc.preamble] if doc.preamble else []
    items, index = doc.items, 0
    while index < len(items):
        after = index + 1
        while after < len(items) and items[after].level is None and items[after].parent == index:
            after += 1
        blocks.append(block_text(items[index], items[index + 1 : after], anchors))
        index = after
    return "\n\n".join(text for text in blocks if text) + "\n"


def normalize(text: str, *, anchors: bool = True) -> str:
    """``text`` in the form two documents are compared in: trailing whitespace dropped, runs of blank lines as one,
    no blank line at either end, and without the screens' code comments unless ``anchors``."""
    kept: list[str] = []
    for line in lines_of(text):
        if not anchors and ANCHOR.match(line.strip()):
            continue
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    return block(kept) + "\n"


__all__ = [
    "aliases_of",
    "block_text",
    "cells",
    "fields_of",
    "normalize",
    "parse",
    "render",
    "row_parts",
    "split_rows",
    "tables",
    "title_of",
    "use_case_fields",
]
