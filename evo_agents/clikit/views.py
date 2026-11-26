"""What a command shows a person, kept apart from what it found (Strategy, and Composite for views made of views).

A command's ``run`` returns a :class:`Result`: the data, which ``--json`` prints as it is, and a :class:`View`, which
writes the same data for a person. Commands never print themselves, so the JSON and the text cannot drift apart and a
test reads either without capturing streams.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def table_lines(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """``header`` and ``rows`` as aligned columns, two spaces apart, without trailing blanks."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=False)]
    return [
        "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (header, *rows)
    ]


class View(ABC):
    """How a person reads a result."""

    @abstractmethod
    def lines(self) -> list[str]: ...

    def write(self, out: TextIO) -> None:
        for line in self.lines():
            print(line, file=out)


@dataclass(frozen=True)
class Text(View):
    """Lines as they are."""

    text: tuple[str, ...]

    def __init__(self, *text: str):
        object.__setattr__(self, "text", text)

    def lines(self) -> list[str]:
        return list(self.text)


@dataclass(frozen=True)
class Table(View):
    """Rows under a header; ``empty`` instead when there are none (nothing at all when it is None)."""

    header: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    empty: str | None = None

    def __init__(self, header: Sequence[str], rows: Sequence[Sequence[Any]], empty: str | None = None):
        object.__setattr__(self, "header", tuple(header))
        object.__setattr__(self, "rows", tuple(tuple(row) for row in rows))
        object.__setattr__(self, "empty", empty)

    def lines(self) -> list[str]:
        if not self.rows:
            return [] if self.empty is None else [self.empty]
        return table_lines(self.header, self.rows)


@dataclass(frozen=True)
class Views(View):
    """Views one after the other (Composite)."""

    parts: tuple[View, ...]

    def __init__(self, *parts: View):
        object.__setattr__(self, "parts", parts)

    def lines(self) -> list[str]:
        return [line for part in self.parts for line in part.lines()]


NOTHING = Views()


@dataclass(frozen=True)
class Result:
    """What one run of a command found: ``data`` for ``--json``, ``view`` for a person, and the exit status."""

    data: Any
    view: View = NOTHING
    status: int = 0

    def show(self, out: TextIO, as_json: bool) -> None:
        if as_json:
            print(json_text(self.data), file=out)
        else:
            self.view.write(out)
