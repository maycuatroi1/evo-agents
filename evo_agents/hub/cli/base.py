"""The base of the hub's class commands (``evo_agents.clikit``): their context, their failures and the hub's resources.

- :class:`HubContext` gives a command the signed-in hub client and the project, each built on first use: from
  ``--project``, or from ``hub.project`` in the harness.yaml around the current directory.
- :class:`HubCommand` runs in a :class:`HubContext` and turns a :class:`HubError` into ``error: <message>`` and the
  exit status ``client.exit_status`` gives it, as ``cli_client.client_command`` does for a function.
- :class:`ProjectResource` is the gateway to one collection of a project's API (``/v1/projects/<p>/<collection>``):
  the one place that quotes the project and the parts of a path. A command asks its resource, never builds a path.

Tests give commands a fake hub by patching :func:`signed_in` here once, for every class command.
"""

from __future__ import annotations

import argparse
import sys
from functools import cached_property
from typing import Any, ClassVar, Self
from urllib.parse import quote, urlencode

from evo_agents.clikit import Arg, Command, Context
from evo_agents.hub.cli_client import signed_in
from evo_agents.hub.client import Hub, HubError, exit_status
from evo_agents.hub.mirror import harness_project, harness_root

PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
PROJECT = Arg("--project", help=PROJECT_HELP)
NO_PROJECT = "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"


class HubContext(Context):
    @cached_property
    def hub(self) -> Hub:
        hub, _ = signed_in()
        return hub

    @cached_property
    def project(self) -> str:
        if getattr(self.args, "project", None):
            return self.args.project
        try:
            root = harness_root(None)
        except HubError:
            raise HubError(NO_PROJECT) from None
        return harness_project(root)


class HubCommand(Command[HubContext]):
    """A command that asks the hub."""

    def context(self, args: argparse.Namespace) -> HubContext:
        return HubContext(args)

    def failed(self, exc: Exception) -> int | None:
        if not isinstance(exc, HubError):
            return None
        print(f"error: {exc}", file=sys.stderr)
        return exit_status(exc)


class ProjectResource:
    """One collection of a project's API on the hub (Gateway)."""

    collection: ClassVar[str]

    def __init__(self, hub: Hub, project: str):
        self.hub = hub
        self.project = project

    @classmethod
    def of(cls, ctx: HubContext) -> Self:
        return cls(ctx.hub, ctx.project)

    def path(self, *parts: Any, **query: Any) -> str:
        """``/v1/projects/<project>/<collection>/<part>...?<query>``, each part quoted whole; a query value of None is
        left out."""
        tail = "".join(f"/{quote(str(part), safe='')}" for part in parts)
        given = {name: value for name, value in query.items() if value is not None}
        return f"/v1/projects/{quote(self.project, safe='')}/{self.collection}{tail}" + (
            f"?{urlencode(given)}" if given else ""
        )

    def call(self, method: str, *parts: Any, body: dict | None = None, **query: Any) -> Any:
        return self.hub.call(method, self.path(*parts, **query), body)
