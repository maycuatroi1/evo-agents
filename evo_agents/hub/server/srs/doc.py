"""A document in memory: its items in order, and the problems found in it. ``markdown`` reads and writes it, ``checks``
finds its problems, ``store`` loads a version into it and ``writes`` keeps one.

Items are in document order, each naming its parent by index in that order; a parent always comes before its
children, and a table row (a cbr or nfr item, ``level`` None) follows the heading item whose body holds its table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evo_agents.hub.tables_srs import code_key

ERROR, WARNING = "error", "warning"
# Problems that refuse a save or an import: nothing is written while one holds. Every other error blocks submitting.
SAVE_CHECKS = frozenset({"code_pattern", "code_duplicate", "code_taken", "code_changed", "coded_item_dropped"})


@dataclass
class Item:
    """One item: its kind, title, body and code, the aliases it is also known by, its heading level (None for a table
    row), the fields its kind holds, its parent (index), the line of the source it came from, the hub's key once it is
    kept, and its links to other items (rel, key)."""

    kind: str
    title: str
    body: str = ""
    code: str | None = None
    aliases: list[str] = field(default_factory=list)
    level: int | None = None
    fields: dict = field(default_factory=dict)
    parent: int | None = None
    line: int | None = None
    key: str | None = None
    refs: list[tuple[str, str]] = field(default_factory=list)

    def codes(self) -> list[str]:
        """Its code, then its aliases."""
        return ([self.code] if self.code else []) + list(self.aliases)


@dataclass(frozen=True)
class Problem:
    """Something wrong with a document: an error or a warning, the check that found it, what it says, and the item
    (code, key, line) it is about."""

    level: str
    check: str
    message: str
    code: str | None = None
    item: str | None = None
    line: int | None = None

    def refuses_save(self) -> bool:
        return self.level == ERROR and self.check in SAVE_CHECKS

    def as_json(self) -> dict:
        return {
            "level": self.level,
            "check": self.check,
            "message": self.message,
            "code": self.code,
            "item": self.item,
            "line": self.line,
        }


@dataclass
class Document:
    """A document's content: the text before its first heading and its items in order, with what reading it noted."""

    preamble: str = ""
    items: list[Item] = field(default_factory=list)
    notes: list[Problem] = field(default_factory=list)

    def children(self, index: int | None) -> list[int]:
        """The indexes of the items whose parent is ``index`` (None: the top level), in order."""
        return [i for i, item in enumerate(self.items) if item.parent == index]

    def ancestors(self, index: int) -> list[int]:
        """The indexes of the parents of item ``index``, nearest first."""
        found, at = [], self.items[index].parent
        while at is not None:
            found.append(at)
            at = self.items[at].parent
        return found

    def item_index(self, ref: str, where: str) -> int:
        """The index of the item ``ref`` names: the first whose key is ``ref`` or one of whose codes is ``ref`` as codes
        are compared; else the one item with a code that reads ``ref`` once its kind word is left out (``ABC-1`` for
        ``UC ABC-1``). A 404 when no item does, which names each item when several do; ``where`` is the version the
        message names (``version 3 of order-flow``)."""
        wanted = code_key(ref)
        for at, item in enumerate(self.items):
            if item.key == ref or wanted in {code_key(code) for code in item.codes()}:
                return at
        bare = [(at, code) for at, item in enumerate(self.items) if (code := _bare_code(item, wanted))]
        if len(bare) == 1:
            return bare[0][0]
        message = f"{where} has no item {ref}"
        if bare:
            named = "; ".join(_candidate(self.items[at], code) for at, code in bare)
            message += f"; {len(bare)} items have a code that reads {ref} after its kind word: {named}"
            message += "; read one by its code or key"
        raise SrsError(404, message)


def _without_kind(code: str) -> str | None:
    """``code`` as codes are compared, its first word (the kind, ``UC`` of ``UC ABC-1``) left out; None for a code of
    one word."""
    _, space, rest = code_key(code).partition(" ")
    return rest if space else None


def _bare_code(item: Item, wanted: str) -> str | None:
    """The first code of ``item`` that reads ``wanted`` once its kind word is left out."""
    return next((code for code in item.codes() if _without_kind(code) == wanted), None)


def _candidate(item: Item, code: str) -> str:
    alias = f"alias {code}, " if code != item.code else ""
    return f"{item.code or '-'} ({item.kind}, {alias}key {item.key})"


class SrsError(Exception):
    """Why the hub refuses a request about a document, with the HTTP status a route answers, the problems that refused
    it and any other detail (a conflict's two texts); nothing was written."""

    def __init__(self, status: int, message: str, problems: list[Problem] | None = None, detail: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.problems = list(problems or [])
        self.detail = detail


__all__ = ["ERROR", "SAVE_CHECKS", "WARNING", "Document", "Item", "Problem", "SrsError"]
