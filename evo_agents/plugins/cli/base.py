"""The base of the commands of ``evo-agents plugin`` (``evo_agents.clikit``): :class:`PluginCommand` runs on this
machine alone and turns :class:`Refused` into ``error: <message>`` and exit status 1."""

from __future__ import annotations

import argparse
import sys

from evo_agents.clikit import Arg, Command, Context

EXIT_FAILED = 1
JSON = Arg("--json", action="store_true", help="machine-readable output")


class Refused(Exception):
    """What the command could not do, in one line."""


class PluginCommand(Command[Context]):
    """A command of the author of a plugin, or of a market, on their own machine."""

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def failed(self, exc: Exception) -> int | None:
        if not isinstance(exc, Refused):
            return None
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
