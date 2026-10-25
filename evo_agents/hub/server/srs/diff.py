"""Two versions of a document compared item by item: each item matched by its key, added, removed, changed (its
heading, body or fields) or moved (another parent or heading level), with the lines of its text that changed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from evo_agents.hub.server.srs.doc import Document, Item
from evo_agents.hub.server.srs.template import heading_text

CONTEXT_LINES = 2


@dataclass(frozen=True)
class ItemChange:
    """What became of one item between two versions."""

    key: str
    kind: str
    code: str | None
    change: str  # added, removed, changed, moved
    title_from: str | None
    title_to: str | None
    lines: list[str] = field(default_factory=list)  # a unified diff of the item's text, without its file header


def text_of(item: Item) -> str:
    """An item as its Markdown reads: its heading, then its body; a table row is its line."""
    if item.level is None:
        return item.body
    head = "#" * item.level + " " + heading_text(item.kind, item.title, item.code, item.fields)
    return (
        head
        + ("\n\n" + item.body if item.body else "")
        + ("\n\n" + item.fields["tail"] if item.fields.get("tail") else "")
    )


def lines_between(old: str, new: str) -> list[str]:
    """A unified diff of two texts, without its file header: hunks, then lines marked ' ', '-' or '+'."""
    found = difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=CONTEXT_LINES)
    return [line for line in found if not line.startswith(("---", "+++"))]


def _parent_key(doc: Document, item: Item) -> str | None:
    return doc.items[item.parent].key if item.parent is not None else None


def _change(old_doc: Document, old: Item, new_doc: Document, new: Item) -> ItemChange | None:
    before, after = text_of(old), text_of(new)
    if before != after or old.fields != new.fields or old.code != new.code:
        return ItemChange(new.key, new.kind, new.code, "changed", old.title, new.title, lines_between(before, after))
    if _parent_key(old_doc, old) != _parent_key(new_doc, new) or old.level != new.level:
        return ItemChange(new.key, new.kind, new.code, "moved", old.title, new.title)
    return None


def diff(old: Document, new: Document) -> list[ItemChange]:
    """What changed from ``old`` to ``new``: the items of ``new`` in its order, then those it dropped in the order of
    ``old``; an item left as it was is not listed."""
    held = {item.key: item for item in old.items}
    kept = {item.key for item in new.items}
    found: list[ItemChange] = []
    for item in new.items:
        before = held.get(item.key)
        if before is None:
            lines = lines_between("", text_of(item))
            found.append(ItemChange(item.key, item.kind, item.code, "added", None, item.title, lines))
            continue
        changed = _change(old, before, new, item)
        if changed is not None:
            found.append(changed)
    for item in old.items:
        if item.key not in kept:
            lines = lines_between(text_of(item), "")
            found.append(ItemChange(item.key, item.kind, item.code, "removed", item.title, None, lines))
    return found


__all__ = ["ItemChange", "diff", "lines_between", "text_of"]
