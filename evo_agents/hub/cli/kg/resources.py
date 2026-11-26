"""The gateways of the ``hub kg`` commands to a project's graph API on the hub (``/v1/kg/<project>/...``).

:class:`KgResource` is ``evo_agents.hub.api.kg.KgCollection``, the graph routes' shared base, as these commands
call it (a call without a body still passes ``None``): the one place that quotes the project and the parts of a path
under ``/v1/kg``. Each command module declares the collection it asks
(``KgBranches``, ``KgSettings``, ...) on top of it; :class:`KgTools` asks the kg tools (``kg_status``) as the hub
answers them over HTTP. Standard library only.
"""

from __future__ import annotations

from typing import Any

from evo_agents.clikit import Arg
from evo_agents.hub.api.kg import KgCollection
from evo_agents.hub.cli.base import call_object

KG_PROJECT = Arg("--project", required=True, help="project name on the hub")
SINK = Arg("--sink", help="sink whose clearance applies (default: the agent session's)")


class KgResource(KgCollection):
    """One collection of a project's graph on the hub: ``/v1/kg/<project>/<collection>/<part>...?<query>``. An empty
    ``collection`` puts the parts right after the project."""

    body_always = True

    def object(self, method: str, *parts: Any, body: dict | None = None, **query: Any) -> dict:
        """The hub's answer, for a route that answers a JSON object; HubError when it did not."""
        return call_object(self.hub, method, self.path(*parts, **query), body)


class KgTools(KgResource):
    """The kg tools of a project's graph over HTTP: POST /v1/kg/{p}/tools/{tool}."""

    collection = "tools"

    def status(self, sink: str | None = None) -> tuple[dict, str | None]:
        """(the structured answer of kg_status as ``sink`` reads the graph, why the hub refused it or None)."""
        body = {"arguments": {}, **({"sink": sink} if sink else {})}
        found = self.call("POST", "kg_status", body=body)
        if found.get("isError"):
            text = " ".join(block.get("text", "") for block in found.get("content", []))
            return {}, text.removeprefix("error: ") or "the hub refused kg_status"
        return found.get("structuredContent") or {}, None


class GraphUnread(Exception):
    """The hub gave no graph to read (no ready graph, none of what was asked, a refused kg_status): the command says
    ``error: <why>`` on stdout and exits 1, as the hub kg commands that read kg_status always have."""


def unread(exc: Exception) -> int | None:
    """The ``failed`` hook's answer for :class:`GraphUnread`; None for any other failure."""
    if not isinstance(exc, GraphUnread):
        return None
    print(f"error: {exc}")
    return 1
