"""The gateway base of the commands of a hub feature served under ``/v1/x/<feature>/<project>/`` (``hub canvas``,
``hub srs``): the one place that quotes the project and the parts of such a path, and that lists on stderr the
problems a refusal of the hub carries before the ``error:`` line. :func:`answer` is the result of a command that
prints what the hub answered.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Any, Self

from evo_agents.clikit import Result, View
from evo_agents.hub.api import FeatureResource
from evo_agents.hub.cli.base import HubContext
from evo_agents.hub.client import HubError

Query = Mapping[str, Any] | list[tuple[str, Any]] | tuple[()]


def answer(ctx: HubContext, found: Any, view: Callable[[Any], View]) -> Result:
    """``found`` as it is with ``--json``, else as ``view`` shows it; the view is not built for ``--json``, so an
    answer it cannot read still prints."""
    return Result(found) if ctx.args.json else Result(found, view(found))


class PluginRoutes(FeatureResource):
    """The routes of one feature of a project, ``/v1/x/<feature>/<project>/<part>...`` (Gateway).

    ``problems`` is the feature's own reading of a refusal: the lines, one per problem the hub listed, written to
    stderr before the HubError goes up to the command."""

    @classmethod
    def of(cls, ctx: HubContext) -> Self:
        return cls(ctx.hub, ctx.args.project)  # --project is required: the harness is never read

    def call(self, method: str, *parts: Any, body: dict | None = None, query: Query = ()) -> Any:
        try:
            return self.hub.call(method, self.path(*parts, query=query), body)
        except HubError as exc:
            detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
            for line in self.problems(detail or []):
                print(line, file=sys.stderr)
            raise

    def object(self, method: str, *parts: Any, body: dict | None = None, query: Query = ()) -> dict:
        """``call`` of a route that answers a JSON object; HubError when it did not."""
        found = self.call(method, *parts, body=body, query=query)
        if not isinstance(found, dict):
            raise HubError(f"the hub answered {method} {self.path(*parts, query=query)} with no JSON object")
        return found

    def problems(self, detail: Any) -> list[str]:
        """The lines of the problems the ``detail`` of a refusal lists; none unless the feature reads them."""
        return []
