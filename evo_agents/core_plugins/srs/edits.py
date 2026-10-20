"""Saving edits: the changes a member made to the items of a version they opened (``base``), applied to the latest
version of the document, which the next version is built on.

A change edits an item (its title, body, the steps of a business rule, its links), drops it (with its subtree), or adds
one under a parent and after a sibling. When an item a change touches changed between the version the member opened
and the latest one, the save is a conflict (409) whose detail holds both texts, and the member merges by hand; edits of
different items merge. A use case's and a screen's fields are read again from the body; a table row's code, title and
fields from its line. ``retiring`` turns codes into the changes that drop their items, for an admin who retires items
by their codes rather than by their keys.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace

from evo_agents.core_plugins.srs import CODED_KINDS, markdown
from evo_agents.core_plugins.srs.diff import text_of
from evo_agents.core_plugins.srs.doc import Document, Item, SrsError
from evo_agents.core_plugins.srs.store import Snapshot
from evo_agents.core_plugins.srs.template import ROW_KINDS, code_key, tidy
from evo_agents.hub.tables_srs import ITEM_KINDS, REF_RELS

KEPT_FIELDS = {  # the fields an item's heading or place holds, which editing its body leaves
    "business_rule": ("steps", "step_label", "dash"),
    "section": ("tail",),
    "glossary": ("tail",),
}


@dataclass(frozen=True)
class NewItem:
    """An item to add: its kind, title, body and code, its parent and the sibling it follows (keys), its level."""

    kind: str
    title: str
    body: str = ""
    code: str | None = None
    parent: str | None = None
    after: str | None = None
    level: int | None = None
    steps: list[str] | None = None


@dataclass(frozen=True)
class Change:
    """One change: of the item ``key`` (its title, body, steps or links, or ``retire`` it), or ``new`` to add."""

    key: str | None = None
    title: str | None = None
    body: str | None = None
    steps: list[str] | None = None
    refs: list[tuple[str, str]] | None = None  # (rel, the code or key of the item it links to)
    retire: bool = False
    new: NewItem | None = None


def step_label(steps: list[str]) -> str | None:
    if not steps:
        return None
    return f"Step ({steps[0]})" if len(steps) == 1 else f"Steps ({', '.join(steps)})"


def _row_check(kind: str, body: str, code: str | None) -> tuple[str, str, dict]:
    if not markdown.is_row(body) or "\n" in body.strip():
        raise SrsError(422, f"a {kind} is one table row, such as | {code or 'CODE'} | ... |")
    found_code, title, fields = markdown.row_parts(kind, body.strip())
    if code is not None and code_key(found_code) != code_key(code):
        raise SrsError(422, f"the first cell of the row is {found_code!r}, not its code {code!r}")
    return found_code, title, fields


def refresh(item: Item) -> None:
    """Read again the fields (and for a row its code and title) that an item's body gives."""
    if item.kind in ROW_KINDS:
        item.code, item.title, item.fields = _row_check(item.kind, item.body, item.code)
        return
    held = {name: value for name, value in item.fields.items() if name in KEPT_FIELDS.get(item.kind, ())}
    item.fields = markdown.fields_of(item.kind, item.body, held)
    if item.kind == "use_case":
        named = markdown.aliases_of(item.code or "", item.fields.get("id"))
        item.aliases = list(dict.fromkeys([*item.aliases, *named]))


def _conflict(key: str, base: Item, latest: Item) -> dict:
    return {"item": key, "code": latest.code, "base": text_of(base), "latest": text_of(latest)}


class Draft:
    """The latest version's items as a list naming parents by key, while changes apply to it."""

    def __init__(self, latest: Document):
        self.items = copy.deepcopy(latest.items)
        self.parents = [latest.items[item.parent].key if item.parent is not None else None for item in latest.items]

    def index(self, key: str | None) -> int:
        found = next((at for at, item in enumerate(self.items) if item.key == key), None)
        if found is None:
            raise SrsError(422, f"the latest version has no item {key}")
        return found

    def subtree_end(self, at: int) -> int:
        """The index just past the item at ``at`` and its descendants, which follow it."""
        inside, end = {self.items[at].key}, at + 1
        while end < len(self.items) and self.parents[end] in inside:
            inside.add(self.items[end].key)
            end += 1
        return end

    def drop(self, key: str) -> None:
        at = self.index(key)
        end = self.subtree_end(at)
        del self.items[at:end], self.parents[at:end]

    def place(self, new: NewItem) -> int:
        """Where ``new`` goes: after its sibling's subtree, a row after its parent's rows, else at its parent's end."""
        if new.kind in ROW_KINDS:
            if new.parent is None:
                raise SrsError(422, f"a {new.kind} is a row of a section's table: name the section as its parent")
            at = self.index(new.parent) + 1
            while at < len(self.items) and self.items[at].level is None and self.parents[at] == new.parent:
                at += 1
            return at
        if new.after is not None:
            return self.subtree_end(self.index(new.after))
        return self.subtree_end(self.index(new.parent)) if new.parent is not None else len(self.items)

    def _level(self, new: NewItem) -> int | None:
        if new.kind in ROW_KINDS:
            return None
        parent_level = self.items[self.index(new.parent)].level if new.parent is not None else 0
        return new.level or min((parent_level or 0) + 1, 6)

    def add(self, new: NewItem) -> None:
        if new.kind not in ITEM_KINDS:
            raise SrsError(422, f"no kind of item {new.kind!r}")
        if new.kind in CODED_KINDS and new.kind != "screen" and not new.code:
            raise SrsError(422, f"a new {new.kind.replace('_', ' ')} needs its code")
        if new.after is not None and new.parent is None:
            new = replace(new, parent=self.parents[self.index(new.after)])
        at = self.place(new)
        code = tidy(new.code) if new.code else None
        item = Item(new.kind, tidy(new.title)[:500], new.body.strip(), code, level=self._level(new))
        if new.kind == "business_rule":
            steps = [step.strip() for step in new.steps or [] if step.strip()]
            item.fields = {"steps": steps, "step_label": step_label(steps), "dash": "–"}
        refresh(item)
        self.items.insert(at, item)
        self.parents.insert(at, new.parent)

    def links(self, item: Item, refs: list[tuple[str, str]]) -> None:
        by_code = {code_key(code): other.key for other in self.items for code in other.codes()}
        keys = {other.key for other in self.items}
        found = []
        for rel, target in refs:
            key = target if target in keys else by_code.get(code_key(target))
            if rel not in REF_RELS or key is None or key == item.key:
                raise SrsError(422, f"a link ({rel} {target}) names no other item of the latest version, or no rel")
            found.append((rel, key))
        item.refs = list(dict.fromkeys(found))

    def document(self, preamble: str) -> Document:
        at = {item.key: index for index, item in enumerate(self.items) if item.key}
        for item, parent in zip(self.items, self.parents, strict=True):
            item.parent = at.get(parent) if parent is not None else None
        return Document(preamble=preamble, items=self.items)


def _edit(draft: Draft, change: Change) -> None:
    item = draft.items[draft.index(change.key)]
    if change.title is not None:
        item.title = tidy(change.title)[:500]
    if change.body is not None:
        item.body = change.body.strip()
    if change.steps is not None and item.kind == "business_rule":
        steps = [step.strip() for step in change.steps if step.strip()]
        item.fields = {**item.fields, "steps": steps, "step_label": step_label(steps)}
    if change.body is not None or change.title is not None:
        refresh(item)
    if change.refs is not None:
        draft.links(item, change.refs)


def _conflicts(latest: Snapshot, base: Snapshot, changes: list[Change], preamble: str | None) -> list[dict]:
    held = {item.key: item for item in base.doc.items}
    now = {item.key: item for item in latest.doc.items}
    found = []
    for change in changes:
        if change.key is None:
            continue
        if change.key not in now:
            raise SrsError(422, f"the latest version has no item {change.key}")
        before, after = held.get(change.key), now[change.key]
        if before is not None and (text_of(before) != text_of(after) or before.fields != after.fields):
            found.append(_conflict(change.key, before, after))
    if preamble is not None and latest.doc.preamble != base.doc.preamble:
        found.append({"item": None, "code": None, "base": base.doc.preamble, "latest": latest.doc.preamble})
    return found


def retiring(latest: Document, codes: list[str]) -> tuple[list[Change], list[str]]:
    """The changes that drop the items of ``latest`` holding ``codes`` (each a code or an alias, compared as srs_codes
    compares them, ``code_key``), each with its subtree, and the codes no item of ``latest`` holds. A code of an item
    under another one retired, or a second code of the same item, adds no change: its item goes with the first."""
    at = {code_key(code): index for index, item in enumerate(latest.items) for code in item.codes()}
    unknown = list(dict.fromkeys(tidy(code) for code in codes if code_key(code) not in at))
    chosen = {at[code_key(code)] for code in codes if code_key(code) in at}
    tops = sorted(index for index in chosen if not chosen.intersection(latest.ancestors(index)))
    return [Change(latest.items[index].key, retire=True) for index in tops], unknown


def edited(latest: Snapshot, base: Snapshot, changes: list[Change], preamble: str | None = None) -> Document:
    """The content of the next version: ``changes``, made by a member who opened ``base``, applied to ``latest``.
    SrsError 409 with both texts of each item that changed since ``base``, 422 for a change that cannot apply."""
    found = _conflicts(latest, base, changes, preamble)
    if found:
        version = latest.version.number
        raise SrsError(409, f"items changed since version {base.version.number}, in version {version}", detail=found)
    draft = Draft(latest.doc)
    for change in changes:
        if change.key is not None and not change.retire:
            _edit(draft, change)
    for change in changes:
        if change.key is not None and change.retire:
            draft.drop(change.key)
    for change in changes:
        if change.new is not None:
            draft.add(change.new)
    return draft.document(latest.doc.preamble if preamble is None else preamble.strip())


__all__ = ["Change", "NewItem", "edited", "refresh", "retiring", "step_label"]
