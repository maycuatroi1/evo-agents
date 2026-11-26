"""The result of a hub command that syncs files and reports on them (``hub memory push|pull``, ``hub skills sync``,
``hub sessions backfill|sync``): what it found, plus lines written to stderr with ``--json`` too, after the rest.

A sync's conflicts and errors go to stderr whether or not ``--json`` prints the report, and a sync that did not run
(another one holds this machine) prints its reason to stderr and nothing at all to stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from evo_agents.clikit import NOTHING, Result, Stderr, Text, View


@dataclass(frozen=True)
class Reported(Result):
    """A :class:`Result` whose ``after`` is written after the rest, with ``--json`` too; ``printed`` False writes
    nothing but ``after``."""

    after: View = NOTHING
    printed: bool = True

    def show(self, out: TextIO, as_json: bool) -> None:
        if self.printed:
            super().show(out, as_json)
        self.after.write(out)


def to_stderr(*lines: str) -> View:
    """``lines`` written to stderr; nothing for none."""
    return Stderr(Text(*lines)) if lines else NOTHING
