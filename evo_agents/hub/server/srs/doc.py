"""A document in memory: its items in order, and the problems found in it. ``markdown`` reads and writes it, ``checks``
finds its problems, ``store`` loads a version into it and ``writes`` keeps one.

Items are in document order, each naming its parent by index in that order; a parent always comes before its
children, and a table row (a cbr or nfr item, ``level`` None) follows the heading item whose body holds its table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
