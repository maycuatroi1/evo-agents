"""Commands as objects: the one skeleton every command of evo-agents runs through.

- :class:`Command` is the Command pattern with a Template Method: ``__call__`` checks the options, builds the
  :class:`Context`, runs the command, maps a known failure to its exit status and shows the :class:`Result`. A command
  writes ``run`` alone, plus the hooks it needs (``check``, ``failed``, ``add_arguments``).
- :class:`Group` is a Composite: a command word holding commands and groups, which builds its own argparse subtree.
- :class:`Arg` is an argument as a value. Options many commands share are constants (``PROJECT``), never mixins.

The parser stays argparse, and ``configure`` sets the same defaults ``json_option`` sets, so ``evo-agents hub contract``
reads a class command exactly as it reads a function. Inheritance stops at two levels: :class:`Command`, then a kind
of command (:class:`ListCommand`) or a package's base (``evo_agents.hub.cli.base.HubCommand``).
"""

from __future__ import annotations

import argparse
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, TextIO, TypeVar

from evo_agents.clikit.json_output import JsonOutput, json_option
from evo_agents.clikit.views import Result, Table

EXIT_USAGE = 2


class UsageError(Exception):
    """The options say two things, or miss one: ``error: <text>`` and exit status 2."""


@dataclass(frozen=True, init=False)
class Arg:
    """One argument of a parser, as ``add_argument`` takes it."""

    flags: tuple[str, ...]
    options: Mapping[str, Any]

    def __init__(self, *flags: str, **options: Any):
        object.__setattr__(self, "flags", flags)
        object.__setattr__(self, "options", options)

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(*self.flags, **self.options)


class Context:
    """What one run of a command works with: its arguments and the stream it writes to. A package's context adds its
    services as cached properties, built on first use, so a command that fails its checks touches none of them."""

    def __init__(self, args: argparse.Namespace, out: TextIO | None = None):
        self.args = args
        self._out = out

    @property
    def out(self) -> TextIO:
        return self._out or sys.stdout  # looked up when written, so a captured sys.stdout is the one used


C = TypeVar("C", bound=Context)


class Node(Protocol):
    """A command or a group: something that attaches itself under a parser's subcommands."""

    def attach(self, subparsers: argparse._SubParsersAction) -> None: ...


class Command(ABC, Generic[C]):
    """One command. Subclasses set ``name``, ``help``, ``arguments`` and ``output`` and write ``run``."""

    name: ClassVar[str]
    help: ClassVar[str]
    arguments: ClassVar[tuple[Arg, ...]] = ()
    output: ClassVar[JsonOutput | None] = None  # what --json prints; None: the command has no --json
    json_help: ClassVar[str] = "machine-readable output"
    parser_options: ClassVar[Mapping[str, Any]] = {}  # more keywords of add_parser (description, aliases, ...)

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        self.configure(subparsers.add_parser(self.name, help=self.help, **self.parser_options))

    def configure(self, parser: argparse.ArgumentParser) -> None:
        for arg in self.arguments:
            arg.add_to(parser)
        self.add_arguments(parser)
        if self.output is not None:
            json_option(parser, self.output, self.json_help)
        parser.set_defaults(func=self)

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Hook: arguments that cannot be values (a mutually exclusive group, choices read at run time)."""

    def check(self, args: argparse.Namespace) -> None:
        """Hook: raise UsageError for options that cannot go together, before any service is touched."""

    @abstractmethod
    def context(self, args: argparse.Namespace) -> C: ...

    @abstractmethod
    def run(self, ctx: C) -> Result: ...

    def failed(self, exc: Exception) -> int | None:
        """Hook: the exit status of a failure the package knows (and its message, written to stderr); None raises it."""
        return None

    def __call__(self, args: argparse.Namespace) -> int:
        try:
            self.check(args)
            ctx = self.context(args)
            result = self.run(ctx)
        except UsageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except Exception as exc:
            status = self.failed(exc)
            if status is None:
                raise
            return status
        result.show(ctx.out, bool(getattr(args, "json", False)))
        return result.status


class ListCommand(Command[C]):
    """A command that lists items as rows: ``fetch`` the items, turn each into a ``row`` under ``columns``."""

    columns: ClassVar[tuple[str, ...]]

    @abstractmethod
    def fetch(self, ctx: C) -> list: ...

    @abstractmethod
    def row(self, item: Any) -> tuple: ...

    def empty(self, ctx: C) -> str | None:
        """What is printed when there is nothing to list."""
        return None

    def run(self, ctx: C) -> Result:
        items = self.fetch(ctx)
        return Result(items, Table(self.columns, [self.row(item) for item in items], self.empty(ctx)))


class Group:
    """A command word holding commands and groups (Composite); ``dest`` names the word chosen under it, ``arguments``
    are options of the word itself (``evo-agents --version``). :meth:`parser` makes it the program's own parser."""

    def __init__(
        self,
        name: str,
        help: str,
        children: Iterable[Node],
        *,
        dest: str | None = None,
        arguments: Iterable[Arg] = (),
        **options: Any,
    ):
        self.name, self.help, self.children = name, help, tuple(children)
        self.dest = dest or f"{name.replace('-', '_')}_command"
        self.arguments = tuple(arguments)
        self.options = options

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        self.fill(subparsers.add_parser(self.name, help=self.help, **self.options))

    def parser(self) -> argparse.ArgumentParser:
        """The parser of a program whose name is ``name`` and whose description is ``help``."""
        parser = argparse.ArgumentParser(prog=self.name, description=self.help, **self.options)
        self.fill(parser)
        return parser

    def fill(self, parser: argparse.ArgumentParser) -> None:
        for arg in self.arguments:
            arg.add_to(parser)
        below = parser.add_subparsers(dest=self.dest, required=True)
        for child in self.children:
            child.attach(below)
