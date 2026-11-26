"""The base of the knowledge graph's class commands (``evo_agents.clikit``): their context, their failures and how
they print.

- :class:`KgContext` gives a command its project, resolved on first use from ``--project`` (a name or a harness path)
  or from the current directory; a project that does not resolve is ``error: <message>`` and exit status 2.
- :class:`KgCommand` runs in a :class:`KgContext` and turns a :class:`Failed` into its line on stderr and its exit
  status; with ``hub_errors``, a hub that cannot be reached, refuses the caller or is not signed in to is
  ``error: the hub: <message>`` and exit status 1.
- :class:`Shown` is the result of a command with ``--json``: it writes its view either way, so the command says how
  its JSON reads (:class:`Json`, as the kg commands have always printed it).

The arguments many commands share are constants here (:data:`PROJECT`, :data:`JSON`); so are the helpers several
commands use: the argument type :func:`bounded`, the ``--repo NAME=PATH`` reader :func:`repo_overrides`, the hub
project of ``kg prune`` and ``kg scrub`` (:func:`hub_project_args`) and :func:`on_hub`, which runs an ``evo-agents
hub kg`` command for a project.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, TextIO

from evo_agents.clikit import EXIT_USAGE, NOTHING, Arg, Command, Context, Result, UsageError, View
from evo_agents.kg.project import Project, ProjectError, resolve_project

PROJECT = Arg("--project", help="project name or harness path (default: from the cwd)")
JSON = Arg("--json", action="store_true", help="machine-readable output")
WITH_PROJECT = (PROJECT, JSON)  # what every command on one project takes first


class Failed(Exception):
    """A command that cannot go on: ``<prefix><message>`` on stderr and exit status ``status``."""

    def __init__(self, message: str, status: int = 1, prefix: str = "error: "):
        super().__init__(message)
        self.status = status
        self.prefix = prefix


def project_failed(exc: ProjectError) -> Failed:
    """A project that does not resolve, as every kg command says it."""
    return Failed(str(exc), EXIT_USAGE)


@dataclass(frozen=True)
class Json(View):
    """``data`` as JSON: ``indent`` 2 and the text as it is unless told otherwise, a value JSON has no form for as
    its str."""

    data: Any
    indent: int = 2
    ascii: bool = False

    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=self.ascii, indent=self.indent, default=str)

    def lines(self) -> list[str]:
        return self.text().splitlines()

    def write(self, out: TextIO) -> None:
        print(self.text(), file=out)


@dataclass(frozen=True)
class Shown(Result):
    """A result whose view is written with ``--json`` too: the command picked it for ``--json`` (a :class:`Json`)."""

    def show(self, out: TextIO, as_json: bool) -> None:
        self.view.write(out)


def done(status: int = 0) -> Shown:
    """The result of a command that wrote what it shows as it went, or had another command write it."""
    return Shown(None, NOTHING, status)


def shown(ctx: KgContext, data: Any, view: View, status: int = 0) -> Shown:
    """``data`` as :class:`Json` with ``--json``, else ``view``."""
    return Shown(data, Json(data) if ctx.args.json else view, status)


class KgContext(Context):
    """What a kg command works with: its arguments, and the project they name, resolved on first use."""

    @cached_property
    def project(self) -> Project:
        try:
            return resolve_project(getattr(self.args, "project", None))
        except ProjectError as exc:
            raise project_failed(exc) from exc


class KgCommand(Command[KgContext]):
    """A command of ``evo-agents kg``. ``hub_errors``: a ``HubError`` is ``error: the hub: <message>`` and exit 1."""

    hub_errors: ClassVar[bool] = False

    def context(self, args: argparse.Namespace) -> KgContext:
        return KgContext(args)

    def failed(self, exc: Exception) -> int | None:
        if isinstance(exc, Failed):
            print(f"{exc.prefix}{exc}", file=sys.stderr)
            return exc.status
        if self.hub_errors:
            from evo_agents.hub.client import HubError

            if isinstance(exc, HubError):
                print(f"error: the hub: {exc}", file=sys.stderr)
                return 1
        return None


def bounded(low: int, high: int):
    """The argument type of a whole number from ``low`` to ``high``."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{value} is not within {low} to {high}")
        return value

    return parse


def repo_overrides(pairs: list[str] | None) -> dict[str, Path]:
    """The checkouts ``--repo NAME=PATH`` names, by repo; ValueError for a pair of another form."""
    found: dict[str, Path] = {}
    for pair in pairs or []:
        name, sep, path = pair.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"--repo takes NAME=PATH, not {pair!r}")
        found[name.strip()] = Path(path.strip()).expanduser().resolve()
    return found


def hub_project_args(args: argparse.Namespace) -> argparse.Namespace:
    """``args`` of ``kg prune`` and ``kg scrub``, ``project`` set to the hub project they act on: None with --all (every
    project), else --project or the project of the cwd."""
    if args.all and args.project:
        raise UsageError("--all goes without --project")
    name = None if args.all else args.project
    if not args.all and not (name and "/" not in name and name not in (".", "..")):
        try:
            name = resolve_project(name).name
        except ProjectError as exc:
            raise project_failed(exc) from exc
    args.project = name
    return args


def on_hub(args: argparse.Namespace, command, **extra: Any) -> int:
    """Run the ``evo-agents hub kg`` command ``command`` for the hub's name of ``args.project``, with ``--json`` as
    given and ``extra``."""
    from evo_agents.hub.kg_remote import hub_name

    try:
        name = hub_name(args.project)
    except ProjectError as exc:
        raise project_failed(exc) from exc
    return command(argparse.Namespace(project=name, json=args.json, **extra))
