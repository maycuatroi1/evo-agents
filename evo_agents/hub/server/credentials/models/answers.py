"""The shapes of an answer to a credential request (``service.answers``): what ``checked`` read and the secret
that keeps a value."""

from __future__ import annotations

from dataclasses import dataclass

KIND = "env"


@dataclass(frozen=True)
class Asked:
    """An answer to a decision as ``checked`` read it: the option (the one a value stands for when it names none) and
    the value pasted."""

    option: str | None
    value: str | None

    def __repr__(self) -> str:  # the value stays out of tracebacks and logs
        return f"Asked(option={self.option!r}, value={'***' if self.value is not None else None})"


@dataclass(frozen=True)
class Kept:
    name: str
    created: bool
