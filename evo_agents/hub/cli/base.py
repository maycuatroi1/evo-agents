"""The base of the hub's class commands (``evo_agents.clikit``): their context, their failures and the hub's resources.

- :class:`HubContext` gives a command the signed-in hub client and the project, each built on first use: from
  ``--project``, or from ``hub.project`` in the harness.yaml around the current directory.
- :class:`HubCommand` runs in a :class:`HubContext` and turns a :class:`HubError` into ``error: <message>`` and the
  exit status ``client.exit_status`` gives it.
- :class:`ProjectResource` is the gateway to one collection of a project's API (``/v1/projects/<p>/<collection>``):
  the one place that quotes the project and the parts of a path. A command asks its resource, never builds a path.
  :class:`HubRoutes` is the base of a gateway to routes outside a project (``/v1/tokens``, ``/v1/admin``).

The helpers commands share live here too, the one copy of each: :func:`call_object` and :func:`when`;
``signed_in`` and ``EXIT_FAILED`` are the client's, taken from here.

Tests give commands a fake hub by patching :func:`signed_in` here once, for every class command.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from functools import cached_property
from typing import Any, ClassVar, Self
from urllib.parse import quote, urlencode

from evo_agents.clikit import Arg, Command, Context
from evo_agents.hub.client import EXIT_FAILED as EXIT_FAILED
from evo_agents.hub.client import Credentials, Hub, HubError, exit_status
from evo_agents.hub.client import signed_in as signed_in  # every hub command takes it from here
from evo_agents.hub.mirror import harness_project, harness_root

PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
PROJECT = Arg("--project", help=PROJECT_HELP)
NO_PROJECT = "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"


def call_object(hub: Hub, method: str, path: str, body: dict | None = None) -> dict:
    """The hub's answer to ``method path``, for a route that answers a JSON object; HubError when it did not."""
    found = hub.call(method, path, body)
    if not isinstance(found, dict):
        raise HubError(f"the hub answered {method} {path} with no JSON object")
    return found


def when(value: str | None) -> str:
    """An ISO timestamp from the hub as ``YYYY-MM-DD HH:MM`` UTC; ``-`` for none."""
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%d %H:%M")


def project_of(args: argparse.Namespace) -> str:
    """The project a command works on: ``--project``, else ``hub.project`` of the harness around the current
    directory; HubError when there is neither."""
    if getattr(args, "project", None):
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(NO_PROJECT) from None
    return harness_project(root)


class HubContext(Context):
    @cached_property
    def session(self) -> tuple[Hub, Credentials]:
        return signed_in()

    @property
    def hub(self) -> Hub:
        return self.session[0]

    @property
    def credentials(self) -> Credentials:
        return self.session[1]

    @cached_property
    def project(self) -> str:
        return project_of(self.args)

    def with_project(self) -> str:
        """The ``--project`` to repeat in a command printed for the person, when the harness did not give it."""
        return f" --project {self.project}" if getattr(self.args, "project", None) else ""


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


class HubRoutes:
    """Routes of the hub outside a project, under ``path`` (Gateway): the base of a small gateway such as the tokens'
    or the emails'. ``call`` sends a body only when there is one."""

    path: ClassVar[str]

    def __init__(self, hub: Hub):
        self.hub = hub

    def call(self, method: str, tail: str = "", body: dict | None = None) -> Any:
        """The hub's answer to ``method <path><tail>``; ``tail`` is quoted by the caller."""
        if body is None:
            return self.hub.call(method, self.path + tail)
        return self.hub.call(method, self.path + tail, body)
