"""The gateways to the collections of the hub's API: the one place that writes their paths.

A :class:`Resource` is one collection, ``/v1/<base>/<scope>.../<collection>/<part>...?<query>``: ``base`` and
``collection`` are constants written as the hub's routes are, the scope (a project, a run) and the parts are values,
quoted whole. It asks through any client whose ``call(method, path, body)`` reaches the hub: the synchronous
``evo_agents.hub.client.Hub`` of the CLI answers a value, the asynchronous ``evo_agents.worker.hubapi.WorkerHub`` of the
daemon an awaitable, and ``call`` hands back whichever it got, so both clients share the same paths.

- :class:`ProjectResource`: a collection of a project, ``/v1/projects/<project>/<collection>``.
- :class:`HubRoutes`: routes outside a project under one ``base`` (``tokens``, ``admin``), with a tail the caller
  quotes.
- :class:`FeatureResource`: a hub feature's routes of a project, ``/v1/x/<feature>/<project>``.
- :class:`KgResource`: a collection of a project's graph, ``/v1/kg/<project>/<collection>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol, Self

from evo_agents.hub.api.routes import Query, query_string, quoted, route


class Client(Protocol):
    url: str

    def call(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any: ...


class Resource:
    """One collection of the hub's API (Gateway), for a synchronous or an asynchronous client."""

    base: ClassVar[str]
    collection: ClassVar[str] = ""

    def __init__(self, hub: Any, *scope: Any):
        self.hub = hub
        self.scope = scope

    @property
    def root(self) -> str:
        """``/v1/<base>/<scope>...``."""
        return route(self.base, *self.scope)

    def path(self, *parts: Any, safe: str = "", query: Query = (), **named: Any) -> str:
        """``<root>/<collection>/<part>...?<query>``: each part quoted whole (but for the characters in ``safe``),
        ``query`` (pairs a key may repeat in) then ``named``, a value of None left out."""
        head = self.root + (f"/{self.collection}" if self.collection else "")
        pairs = [*(query.items() if isinstance(query, Mapping) else query), *named.items()]
        return head + quoted(*parts, safe=safe) + query_string(pairs)

    def call(self, method: str, *parts: Any, body: Any = None, **query: Any) -> Any:
        """The client's answer to ``method`` on the path of ``parts``: a value, or an awaitable of an async client."""
        return self.hub.call(method, self.path(*parts, **query), body)


class ProjectResource(Resource):
    """One collection of a project's API on the hub: ``/v1/projects/<project>/<collection>``."""

    base = "projects"

    def __init__(self, hub: Any, project: str):
        super().__init__(hub, project)
        self.project = project

    @classmethod
    def of(cls, ctx: Any) -> Self:
        """The resource of a command's context: its hub and its project."""
        return cls(ctx.hub, ctx.project)


class FeatureResource(ProjectResource):
    """The routes of a hub feature (a core plugin) in a project: ``/v1/x/<feature>/<project>/<part>...``."""

    feature: ClassVar[str]

    @property
    def root(self) -> str:
        return route(f"x/{self.feature}", self.project)


class KgResource(ProjectResource):
    """One collection of a project's graph on the hub: ``/v1/kg/<project>/<collection>``; an empty ``collection``
    puts the parts right after the project."""

    base = "kg"


class HubRoutes:
    """Routes of the hub outside a project, under ``base`` (Gateway): the base of a small gateway such as the tokens'
    or the emails'. ``call`` sends a body only when there is one."""

    base: ClassVar[str]

    def __init__(self, hub: Any):
        self.hub = hub

    @classmethod
    def of(cls, ctx: Any) -> Self:
        return cls(ctx.hub)

    @property
    def path(self) -> str:
        """``/v1/<base>``."""
        return route(self.base)

    def call(self, method: str, tail: str = "", body: dict | None = None) -> Any:
        """The hub's answer to ``method <path><tail>``; ``tail`` is quoted by the caller (:func:`quoted`)."""
        if body is None:
            return self.hub.call(method, self.path + tail)
        return self.hub.call(method, self.path + tail, body)
