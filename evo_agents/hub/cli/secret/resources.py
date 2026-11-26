"""The routes of the hub ``evo-agents hub secret`` and ``hub account`` ask, one gateway each. They belong to the caller
and not to a project, so they are not ``evo_agents.hub.cli.base.ProjectResource`` but :class:`HubResource`, the same
gateway over ``/v1/<collection>``.

- :class:`Secrets`: GET /v1/secrets, PUT, PATCH and DELETE /v1/secrets/{name}, and POST /v1/secrets/{name}/shares.
- :class:`Shares`: DELETE /v1/shares/{id}.
- :class:`Accounts`: GET /v1/accounts and /v1/accounts/{name}, and PATCH /v1/accounts/{name}.
- :class:`AccountPolicies`: GET, PUT and DELETE /v1/account-policies/{project}.
- :class:`Principal`: GET /v1/auth/whoami, /v1/workers and /v1/worker-pools, what a login checks its bindings against.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import quote

from evo_agents.hub.api import Query, Resource, paths, query_string, quoted, route
from evo_agents.hub.cli.base import HubContext
from evo_agents.hub.client import Hub


class HubResource(Resource):
    """One collection of the hub's own API, ``/v1/<collection>`` (Gateway): the one place that quotes its parts."""

    def __init__(self, hub: Hub):
        super().__init__(hub)

    @classmethod
    def of(cls, ctx: HubContext) -> Self:
        return cls(ctx.hub)

    @property
    def root(self) -> str:
        """``/v1/<collection>``: the collection is the base, and :meth:`Resource.path` adds no second segment."""
        return route(self.collection)

    def path(self, *parts: Any, safe: str = "", query: Query = (), **named: Any) -> str:
        pairs = [*(query.items() if isinstance(query, Mapping) else query), *named.items()]
        return self.root + quoted(*parts, safe=safe) + query_string(pairs)

    def call(self, method: str, *parts: Any, body: Any = None, query: Any = "", **named: Any) -> Any:
        """The hub's answer to ``method`` on the path of ``parts``, ``query`` (``?...``, quoted already) after it."""
        return self.hub.call(method, self.path(*parts) + query, body)


class Secrets(HubResource):
    collection = "secrets"

    def list(self) -> list[dict]:
        return self.call("GET")

    def find(self, name: str) -> dict | None:
        """The caller's secret ``name``; None when they keep none of that name."""
        return next((secret for secret in self.list() if secret["name"] == name), None)

    def put(self, name: str, body: dict) -> dict:
        return self.call("PUT", name, body=body)

    def rebind(self, name: str, body: dict) -> dict:
        return self.call("PATCH", name, body=body)

    def delete(self, name: str) -> None:
        self.call("DELETE", name)

    def share(self, name: str, body: dict) -> dict:
        return self.call("POST", name, "shares", body=body)


class Shares(HubResource):
    collection = "shares"

    def revoke(self, share_id: int) -> dict:
        return self.call("DELETE", share_id)


class Accounts(HubResource):
    collection = "accounts"

    def list(self) -> list[dict]:
        return self.call("GET")

    def get(self, name: str, owner: str | None = None) -> dict:
        """The account ``name``; of the member ``owner`` who shares it with the caller, when given."""
        return self.call("GET", name, query=f"?owner={quote(owner, safe='')}" if owner else "")

    def change(self, name: str, body: dict) -> dict:
        return self.call("PATCH", name, body=body)


class AccountPolicies(HubResource):
    collection = "account-policies"

    def get(self, project: str) -> dict | None:
        return self.call("GET", project)

    def put(self, project: str, body: dict) -> dict:
        return self.call("PUT", project, body=body)

    def delete(self, project: str) -> None:
        self.call("DELETE", project)


class Principal:
    """Who the caller is on the hub, and the workers and pools they may bind to (Gateway)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def get(self, path: str) -> Any:
        return self.hub.call("GET", path)

    def whoami(self) -> dict:
        return self.get(paths.WHOAMI)

    def workers(self) -> list[dict]:
        return self.get(paths.WORKERS)

    def pools(self) -> list[dict]:
        return self.get(paths.WORKER_POOLS)
